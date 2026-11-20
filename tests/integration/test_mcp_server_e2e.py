"""Spawn the real server over stdio and talk to it with the MCP SDK client (mcp 2.x: mcp.Client)."""
import sys
from pathlib import Path

import mcp
import pytest
from mcp.client.stdio import StdioServerParameters

pytestmark = pytest.mark.integration


def params(repo: Path) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "patchgate.adapters.mcp_server", "--repo", str(repo)])


def text(r) -> str:
    assert not r.is_error, r
    return r.content[0].text


async def test_e2e_read_propose_audit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x: int = 1\n")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "lib.py").write_text("def f(a: int) -> int:\n    return a\n")
    (repo / "user.py").write_text("from lib import f\n\nprint(f(1))\n")
    async with mcp.Client(params(repo)) as c:
        names = sorted(t.name for t in (await c.list_tools()).tools)
        assert names == ["get_audit_trace", "propose_edits", "read_file"]

        assert text(await c.call_tool("read_file", {"path": "a.py"})) == "   1 | x: int = 1"
        assert text(await c.call_tool("read_file", {"path": ".env"})).startswith("DENIED")
        assert text(await c.call_tool("read_file", {"path": "../x"})).startswith("DENIED")
        assert "SECRET" not in text(await c.call_tool("read_file", {"path": ".env"}))

        # accept
        out = text(await c.call_tool("propose_edits", {"edits": [
            {"path": "a.py", "replace": {"old_text": "x: int = 1", "new_text": "x: int = 2"}}]}))
        assert out.startswith("ACCEPTED") and (repo / "a.py").read_text() == "x: int = 2\n"

        # reject: nothing written
        out = text(await c.call_tool("propose_edits", {"edits": [
            {"path": "a.py", "new_content": "x: int = 2\nprint(undefined_here)\n"}]}))
        assert out.startswith("REJECTED_LSP") and "undefined_here" in out
        assert (repo / "a.py").read_text() == "x: int = 2\n"

        # replace-mode errors do not touch anything
        assert "0 matches" in text(await c.call_tool("propose_edits", {"edits": [
            {"path": "a.py", "replace": {"old_text": "nope", "new_text": "y"}}]}))
        (repo / "dup.py").write_text("a = 1\na = 1\n")
        assert "2 places" in text(await c.call_tool("propose_edits", {"edits": [
            {"path": "dup.py", "replace": {"old_text": "a = 1", "new_text": "a = 2"}}]}))

        # policy + secret
        assert text(await c.call_tool("propose_edits", {"edits": [
            {"path": "tests/test_x.py", "new_content": "def test_a():\n    assert f()\n"}]})).startswith("REJECTED_POLICY")
        secret = "ghp_" + "q" * 36
        out = text(await c.call_tool("propose_edits", {"edits": [{"path": "a.py", "new_content": f"t = '{secret}'\n"}]}))
        assert out.startswith("REJECTED_AST") and secret not in out

        # multi-file refactor: partial rejected, batch accepted
        partial = text(await c.call_tool("propose_edits", {"edits": [
            {"path": "lib.py", "new_content": "def f(a: int, b: int) -> int:\n    return a + b\n"}]}))
        assert partial.startswith("REJECTED_LSP") and "user.py" in partial
        both = text(await c.call_tool("propose_edits", {"edits": [
            {"path": "lib.py", "new_content": "def f(a: int, b: int) -> int:\n    return a + b\n"},
            {"path": "user.py", "new_content": "from lib import f\n\nprint(f(1, 2))\n"}]}))
        assert both.startswith("ACCEPTED"), both
        assert "f(1, 2)" in (repo / "user.py").read_text()

        trace = text(await c.call_tool("get_audit_trace", {"limit": 50}))
        assert "batch_committed" in trace and "p95" in trace and secret not in trace
    # trace file exists, contains no secret
    traces = list((repo / ".patchgate" / "traces").glob("*.jsonl"))
    assert traces and secret not in traces[0].read_text()
    assert secret not in (repo / ".patchgate" / "mcp.log").read_text()


async def test_e2e_circuit_breaker_trips_and_cannot_be_reset_by_tools(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x: int = 1\n")
    bad = {"edits": [{"path": "a.py", "new_content": "def (:\n"}]}
    async with mcp.Client(params(repo)) as c:
        for i in range(4):
            assert text(await c.call_tool("propose_edits", bad)).startswith("REJECTED_AST")
        out = text(await c.call_tool("propose_edits", bad))
        assert out.startswith("CIRCUIT_OPEN") and "Stop retrying" in out
        # even a GOOD edit to the tripped file is refused; no tool can reset it
        good = {"edits": [{"path": "a.py", "new_content": "x: int = 3\n"}]}
        assert text(await c.call_tool("propose_edits", good)).startswith("CIRCUIT_OPEN")
        assert (repo / "a.py").read_text() == "x: int = 1\n"
        # other files are unaffected
        assert text(await c.call_tool("propose_edits", {"edits": [{"path": "b.py", "new_content": "y = 1\n"}]})).startswith("ACCEPTED")


async def test_e2e_startup_journal_recovery(tmp_path):
    import hashlib, json
    repo = tmp_path / "repo"
    (repo / ".patchgate" / "journal" / "abc").mkdir(parents=True)
    (repo / "a.py").write_text("HALF-WRITTEN\n")
    (repo / ".patchgate" / "journal" / "abc" / "0.orig").write_bytes(b"ORIGINAL\n")
    (repo / ".patchgate" / "journal.json").write_text(json.dumps({"id": "abc", "entries": [
        {"rel_path": "a.py", "had_original": True,
         "original_sha256": hashlib.sha256(b"ORIGINAL\n").hexdigest(), "backup": "0.orig"}]}))
    async with mcp.Client(params(repo)) as c:
        assert text(await c.call_tool("read_file", {"path": "a.py"})) == "   1 | ORIGINAL"
