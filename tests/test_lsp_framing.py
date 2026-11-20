import asyncio
import json
from pathlib import Path

import pytest

from patchgate.core.diagnostics import diff_file
from patchgate.core.lsp_client import (LspTimeout, encode_message, read_message)
from patchgate.core.models import ResolvedEdit
from tests.lsp_helpers import changes_for, make_client, read_log


def feed(chunks: list[bytes]) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    for c in chunks:
        r.feed_data(c)
    r.feed_eof()
    return r


# ----------------------------------------------------------------------- framing
async def test_byte_length_not_char_length():
    msg = {"jsonrpc": "2.0", "method": "x", "params": {"t": "é😀 日本"}}
    raw = encode_message(msg)
    header, _, body = raw.partition(b"\r\n\r\n")
    assert int(header.split(b":")[1]) == len(body) > len(body.decode("utf-8"))
    assert await read_message(feed([raw])) == msg


async def test_split_reads_every_byte():
    msg = {"jsonrpc": "2.0", "id": 1, "result": "é😀"}
    raw = encode_message(msg)
    r = asyncio.StreamReader()

    async def drip():
        for i in range(len(raw)):
            r.feed_data(raw[i:i + 1])
            await asyncio.sleep(0)
        r.feed_eof()
    t = asyncio.create_task(drip())
    assert await read_message(r) == msg
    await t


async def test_multiple_headers_and_case():
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "result": None}).encode()
    raw = (b"content-type: application/vscode-jsonrpc; charset=utf-8\r\n"
           b"CONTENT-LENGTH: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    assert (await read_message(feed([raw])))["id"] == 3


async def test_back_to_back_in_one_chunk_then_eof():
    a, b = {"jsonrpc": "2.0", "method": "a"}, {"jsonrpc": "2.0", "method": "b", "params": {"x": "é"}}
    r = feed([encode_message(a) + encode_message(b)])
    assert await read_message(r) == a
    assert await read_message(r) == b
    assert await read_message(r) is None


# ----------------------------------------------------------------------- server requests
async def test_configuration_reply_has_one_entry_per_item_and_unknown_gets_32601(tmp_path):
    client, log = make_client(tmp_path, {"config_items": 3, "unknown_request": True})
    client.cfg.python_path = "/some/python"
    await client.start()
    await asyncio.sleep(0.4)
    await client.stop()
    replies = {m["id"]: m for m in read_log(log) if "id" in m and "method" not in m}
    assert len(replies[9001]["result"]) == 3
    assert replies[9002]["error"]["code"] == -32601


async def test_python_path_hint_only_for_python_section(tmp_path):
    client, _ = make_client(tmp_path, {})
    client.cfg.python_path = "/venv/bin/python"
    assert client._config_for({"section": "python"}) == {"pythonPath": "/venv/bin/python"}
    assert client._config_for({"section": "pyright"}) == {}


# ----------------------------------------------------------------------- helpers
def edit(repo: Path, rel: str, old, new) -> ResolvedEdit:
    return ResolvedEdit(path=repo / rel, rel_path=rel, old_content=old, new_content=new)


RULE = {"rules": [{"contains": "BAD", "message": "bad thing", "code": "R1"}]}


# ----------------------------------------------------------------------- completion logic
async def test_version_based_wait_and_diff(tmp_path):
    client, log = make_client(tmp_path, RULE)
    try:
        e = edit(client.root, "a.py", "x = 1\n", "x = 1\nBAD\n")
        res = await client.check_batch([e], {})
        assert res.baseline["a.py"] == []
        assert len(res.post["a.py"]) == 1 and res.post["a.py"][0]["message"] == "bad thing"
        assert res.baseline_ms >= 0 and res.wait_ms >= 0 and res.total_ms >= res.wait_ms
        await client.settle(res, accepted=True, edits=[e])
        assert client.doc_text("a.py") == "x = 1\nBAD\n"
    finally:
        await client.stop()


async def test_quiescence_wait_without_versions(tmp_path):
    client, _ = make_client(tmp_path, {**RULE, "versions": False, "delay_ms": 150})
    try:
        res = await client.check_batch([edit(client.root, "a.py", "x\n", "BAD\n")], {})
        assert len(res.post["a.py"]) == 1
    finally:
        await client.stop()


async def test_preliminary_empty_publish_is_not_final(tmp_path):
    # empty publish at the same version first, real one 200ms later (observed with real Pyright)
    client, _ = make_client(tmp_path, {**RULE, "preliminary_empty_ms": 200}, quiescence_ms=400, timeout_sec=5)
    try:
        res = await client.check_batch([edit(client.root, "a.py", "x\n", "BAD\n")], {})
        assert len(res.post["a.py"]) == 1
    finally:
        await client.stop()


@pytest.mark.parametrize("on_timeout", ["allow_with_warning", "reject"])
async def test_timeout_raises_with_partial_and_restores(tmp_path, on_timeout):
    client, log = make_client(tmp_path, {**RULE, "drop": True}, timeout_sec=0.5, on_timeout=on_timeout)
    try:
        with pytest.raises(LspTimeout) as ei:
            await client.check_batch([edit(client.root, "a.py", "orig\n", "BAD\n")], {})
        assert ei.value.partial is not None
    finally:
        await client.stop()


# ----------------------------------------------------------------------- restore
async def test_restore_on_reject_two_files_and_new_file_closed(tmp_path):
    client, log = make_client(tmp_path, RULE)
    try:
        edits = [edit(client.root, "a.py", "A orig\n", "A BAD\n"),
                 edit(client.root, "b.py", "B orig\n", "B new\n"),
                 edit(client.root, "new.py", None, "BAD\n")]
        res = await client.check_batch(edits, {})
        await client.settle(res, accepted=False, edits=edits)
        await asyncio.sleep(0.3)  # let the fake server process the restore notifications
        assert changes_for(log, "/a.py")[-1] == "A orig\n"
        assert changes_for(log, "/b.py")[-1] == "B orig\n"
        assert client.doc_text("a.py") == "A orig\n"
        assert client.doc_text("new.py") is None
        closes = [m for m in read_log(log) if m.get("method") == "textDocument/didClose"]
        assert any(m["params"]["textDocument"]["uri"].endswith("/new.py") for m in closes)
    finally:
        await client.stop()


async def test_restore_after_exception_during_wait(tmp_path):
    client, log = make_client(tmp_path, {**RULE, "drop": True}, timeout_sec=0.4)
    try:
        with pytest.raises(LspTimeout):
            await client.check_batch([edit(client.root, "a.py", "orig\n", "BAD\n")], {})
        await asyncio.sleep(0.3)
        assert changes_for(log, "/a.py")[-1] == "orig\n"
        assert client.doc_text("a.py") == "orig\n"
    finally:
        await client.stop()


async def test_cross_file_batch_sends_all_changes_before_waiting(tmp_path):
    client, log = make_client(tmp_path, {})
    try:
        edits = [edit(client.root, "a.py", "1\n", "2\n"), edit(client.root, "b.py", "1\n", "2\n")]
        res = await client.check_batch(edits, {})
        await client.settle(res, accepted=True, edits=edits)
        order = [(m["method"], m["params"]["textDocument"]["uri"][-4:]) for m in read_log(log)
                 if m.get("method") in ("textDocument/didOpen", "textDocument/didChange")]
        # originals opened first (baseline), then BOTH new-content changes back to back
        assert [o[0] for o in order[-2:]] == ["textDocument/didChange"] * 2
    finally:
        await client.stop()


async def test_extra_documents_are_checked_as_dependents(tmp_path):
    rules = {"rules": [{"contains": "BAD", "message": "dep broke", "code": "R2"}]}
    client, _ = make_client(tmp_path, rules)
    try:
        res = await client.check_batch([edit(client.root, "lib.py", "x\n", "y\n")], {"user.py": "ok\n"})
        assert "user.py" in res.post and res.baseline["user.py"] == []
    finally:
        await client.stop()


async def test_restart_once_after_abrupt_exit_and_reopen(tmp_path):
    client, log = make_client(tmp_path, {"exit_after_opens": 1})
    try:
        # first check: server dies after first open -> wait raises (LspDead/timeout)
        with pytest.raises(Exception):
            await client.check_batch([edit(client.root, "a.py", "x\n", "y\n")], {})
        # next call restarts the server once
        import os, json
        sc = json.loads(Path(os.environ["FAKE_LSP_SCENARIO"]).read_text())
        sc.pop("exit_after_opens")
        Path(os.environ["FAKE_LSP_SCENARIO"]).write_text(json.dumps(sc))
        res = await client.check_batch([edit(client.root, "a.py", "x\n", "y\n")], {})
        assert client.restarts == 1
        assert "a.py" in res.post
    finally:
        await client.stop()
