import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from patchgate.core.ast_guard import AstGuard
from patchgate.core.config import Config
from patchgate.core.import_graph import ImportGraph
from patchgate.core.lsp_client import PyrightClient
from patchgate.core.models import ResolvedEdit, Status
from patchgate.core.policy import Policy
from patchgate.core.telemetry import Telemetry
from patchgate.core.verifier import VerifierContext, after_commit, verify_batch
from tests.lsp_helpers import FAKE, changes_for


def build(tmp_path: Path, scenario: dict, files: dict[str, str], cfg_over=None, with_lsp=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    log = tmp_path / "fake.log"
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"log": str(log), **scenario}))
    os.environ["FAKE_LSP_SCENARIO"] = str(sc)
    over = {"lsp": {"timeout_sec": 3, "quiescence_ms": 80}, **(cfg_over or {})}
    cfg = Config(**over)
    lsp = PyrightClient(repo, cfg.lsp, command=[sys.executable, str(FAKE)]) if with_lsp else None
    pol = Policy(repo, cfg)
    ctx = VerifierContext(repo, cfg, pol, AstGuard(pol, cfg), lsp, ImportGraph(repo, cfg.ignore_dirs),
                          Telemetry(repo))
    return ctx, log


def edit(ctx, rel, new) -> ResolvedEdit:
    p = ctx.repo_root / rel
    return ResolvedEdit(path=p, rel_path=rel, old_content=p.read_text() if p.exists() else None, new_content=new)


RULES = {"rules": [{"contains": "BAD", "message": "undefined name", "code": "reportUndefinedVariable"}]}


async def test_short_circuit_order_policy_then_ast_then_lsp(tmp_path):
    ctx, log = build(tmp_path, RULES, {"src/a.py": "x = 1\n", "tests/test_t.py": "def test_a():\n    assert f()\n"})
    try:
        # policy violation AND syntax error AND lsp-bad: policy wins, LSP never started
        r = await verify_batch([edit(ctx, "tests/test_t.py", "def (:\nBAD\n")], ctx)
        assert r.status == Status.REJECTED_POLICY and r.timings.ast_ms == 0 and r.timings.lsp_total_ms == 0
        # syntax error AND lsp-bad: AST wins
        r = await verify_batch([edit(ctx, "src/a.py", "def (:\nBAD\n")], ctx)
        assert r.status == Status.REJECTED_AST and r.timings.lsp_total_ms == 0
        assert not (tmp_path / "fake.log").exists() or changes_for(log, "/a.py") == []
        # clean for policy+AST, LSP rejects
        r = await verify_batch([edit(ctx, "src/a.py", "x = 1\nBAD\n")], ctx)
        assert r.status == Status.REJECTED_LSP and "src/a.py:2: undefined name [reportUndefinedVariable]" in r.message
        assert r.timings.lsp_total_ms > 0 and r.timings.total_ms >= r.timings.lsp_total_ms
    finally:
        await ctx.lsp.stop()


async def test_accept_and_reject_restore_overlay(tmp_path):
    ctx, log = build(tmp_path, RULES, {"a.py": "x = 1\n"})
    try:
        bad = await verify_batch([edit(ctx, "a.py", "x = 1\nBAD\n")], ctx)
        assert bad.status == Status.REJECTED_LSP
        await asyncio.sleep(0.3)
        assert changes_for(log, "/a.py")[-1] == "x = 1\n"  # restored to original
        ok = await verify_batch([edit(ctx, "a.py", "x = 2\n")], ctx)
        assert ok.status == Status.ACCEPTED
        assert ctx.lsp.doc_text("a.py") == "x = 2\n"  # accepted overlay stays open
    finally:
        await ctx.lsp.stop()


XR = {"xrules": [{"file": "user.py", "contains": "f(1)", "other_file": "lib.py",
                  "other_contains": "def f(a, b)", "message": "Expected 2 positional arguments",
                  "code": "reportCallIssue"}]}
FILES = {"lib.py": "def f(a):\n    return a\n", "user.py": "from lib import f\n\nprint(f(1))\n"}


async def test_cross_file_batch_passes_but_partial_edit_is_rejected(tmp_path):
    ctx, _ = build(tmp_path, XR, FILES)
    try:
        lib_new = "def f(a, b):\n    return a + b\n"
        user_new = "from lib import f\n\nprint(f(1, 2))\n"
        partial = await verify_batch([edit(ctx, "lib.py", lib_new)], ctx)
        assert partial.status == Status.REJECTED_LSP
        assert partial.dependents_checked == ["user.py"]
        assert any(d.rel_path == "user.py" for d in partial.net_new_diagnostics)
        full = await verify_batch([edit(ctx, "lib.py", lib_new), edit(ctx, "user.py", user_new)], ctx)
        assert full.status == Status.ACCEPTED, full.message
    finally:
        await ctx.lsp.stop()


async def test_baseline_errors_do_not_block_unrelated_edit(tmp_path):
    ctx, _ = build(tmp_path, RULES, {"a.py": "BAD\nx = 1\n"})  # pre-existing error
    try:
        r = await verify_batch([edit(ctx, "a.py", "# new header\nBAD\nx = 1\ny = 2\n")], ctx)
        assert r.status == Status.ACCEPTED, r.message
    finally:
        await ctx.lsp.stop()


@pytest.mark.parametrize("mode,status", [("allow_with_warning", Status.ACCEPTED), ("reject", Status.LSP_TIMEOUT)])
async def test_timeout_modes(tmp_path, mode, status):
    ctx, log = build(tmp_path, {**RULES, "drop": True}, {"a.py": "x = 1\n"},
                     cfg_over={"lsp": {"timeout_sec": 0.5, "quiescence_ms": 50, "on_timeout": mode}})
    try:
        r = await verify_batch([edit(ctx, "a.py", "x = 2\n")], ctx)
        assert r.status == status
        if status == Status.ACCEPTED:
            assert "incomplete" in r.message.lower()
        await asyncio.sleep(0.3)
        assert changes_for(log, "/a.py")[-1] == "x = 1\n"  # restored after the timeout
        if status == Status.ACCEPTED:  # after the (simulated) commit the adapter re-syncs
            await after_commit(ctx, [edit(ctx, "a.py", "x = 2\n")])
            await asyncio.sleep(0.3)
            assert changes_for(log, "/a.py")[-1] == "x = 2\n"
    finally:
        await ctx.lsp.stop()


async def test_non_python_files_skip_lsp_but_get_secret_scan(tmp_path):
    ctx, log = build(tmp_path, RULES, {"notes.txt": "hi\n"})
    r = await verify_batch([edit(ctx, "notes.txt", "BAD\n")], ctx)
    assert r.status == Status.ACCEPTED and r.timings.lsp_total_ms == 0
    secret = "ghp_" + "z" * 36
    r = await verify_batch([edit(ctx, "notes.txt", f"token {secret}\n")], ctx)
    assert r.status == Status.REJECTED_AST and secret not in r.model_dump_json()
    assert ctx.lsp is not None and ctx.lsp._proc is None  # never started


async def test_secret_never_in_trace_and_message_limit(tmp_path):
    ctx, _ = build(tmp_path, RULES, {"a.py": "x = 1\n"})
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    r = await verify_batch([edit(ctx, "a.py", f"x = 1\nk = '{secret}'\n")], ctx)
    assert r.status == Status.REJECTED_AST
    assert secret not in ctx.telemetry.path.read_text()
    # message cap: 25 policy-free LSP errors -> 20 listed + count
    many = "x = 1\n" + "BAD\n" * 25
    r = await verify_batch([edit(ctx, "a.py", many)], ctx)
    assert r.status == Status.REJECTED_LSP
    assert "... and 5 more" in r.message and r.message.count("undefined name") == 20
    await ctx.lsp.stop()


async def test_never_raises_internal_error_becomes_ERROR(tmp_path):
    ctx, _ = build(tmp_path, RULES, {"a.py": "x = 1\n"}, with_lsp=False)

    class Boom:
        def check_batch(self, *_):
            raise RuntimeError("boom")
    ctx.ast_guard = Boom()
    r = await verify_batch([edit(ctx, "a.py", "x = 2\n")], ctx)
    assert r.status == Status.ERROR and "boom" in r.message


async def test_telemetry_batch_verified_and_stats(tmp_path):
    ctx, _ = build(tmp_path, RULES, {"a.py": "x = 1\n"})
    try:
        await verify_batch([edit(ctx, "a.py", "x = 2\n")], ctx)
        await verify_batch([edit(ctx, "a.py", "x = 3\n")], ctx)
        ev = [json.loads(l) for l in ctx.telemetry.path.read_text().splitlines()]
        assert [e["event"] for e in ev] == ["batch_verified"] * 2
        assert "+x = 2" in ev[0]["payload"]["diff"]
        st = ctx.telemetry.latency_stats()
        assert st["total_ms"]["count"] == 2 and st["lsp_total_ms"]["p50"] is not None
    finally:
        await ctx.lsp.stop()
