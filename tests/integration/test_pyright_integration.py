"""Real Pyright. Auto-skipped if no language server is installed."""
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
from patchgate.core.verifier import VerifierContext, verify_batch

pytestmark = pytest.mark.integration

LIB = "def f(a: int) -> int:\n    return a\n"
USER = "from lib import f\n\nprint(f(1))\n"


def make_ctx(tmp_path: Path, files: dict[str, str], **lsp):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    cfg = Config(lsp={"timeout_sec": 30, "quiescence_ms": 150, **lsp})
    pol = Policy(repo, cfg)
    ctx = VerifierContext(repo, cfg, pol, AstGuard(pol, cfg), PyrightClient(repo, cfg.lsp),
                          ImportGraph(repo, cfg.ignore_dirs), Telemetry(repo))
    return ctx


def ed(ctx, rel, new) -> ResolvedEdit:
    p = ctx.repo_root / rel
    return ResolvedEdit(path=p, rel_path=rel, old_content=p.read_text() if p.exists() else None, new_content=new)


@pytest.fixture
async def ctx(tmp_path):
    c = make_ctx(tmp_path, {"lib.py": LIB, "user.py": USER, "solo.py": "x: int = 1\n"})
    yield c
    await c.lsp.stop()


async def test_benign_edit_accepted(ctx):
    r = await verify_batch([ed(ctx, "solo.py", "x: int = 1\n# a comment\n")], ctx)
    assert r.status == Status.ACCEPTED, r.message


async def test_undefined_name_rejected(ctx):
    r = await verify_batch([ed(ctx, "solo.py", "x: int = 1\nprint(totally_undefined)\n")], ctx)
    assert r.status == Status.REJECTED_LSP, r.message
    assert "solo.py:2" in r.message and "totally_undefined" in r.message


async def test_rejected_then_benign_still_accepted_overlay_in_sync(ctx):
    r1 = await verify_batch([ed(ctx, "solo.py", "x: int = 1\nprint(nope)\n")], ctx)
    assert r1.status == Status.REJECTED_LSP
    r2 = await verify_batch([ed(ctx, "solo.py", "x: int = 2\n")], ctx)  # baseline must be the ORIGINAL again
    assert r2.status == Status.ACCEPTED, r2.message


async def test_multifile_refactor_batch_accepted_partial_rejected(ctx):
    lib_new = "def f(a: int, b: int) -> int:\n    return a + b\n"
    user_new = "from lib import f\n\nprint(f(1, 2))\n"
    partial = await verify_batch([ed(ctx, "lib.py", lib_new)], ctx)
    assert partial.status == Status.REJECTED_LSP, partial.message
    assert "user.py" in partial.dependents_checked
    assert any(d.rel_path == "user.py" for d in partial.net_new_diagnostics), partial.message
    full = await verify_batch([ed(ctx, "lib.py", lib_new), ed(ctx, "user.py", user_new)], ctx)
    assert full.status == Status.ACCEPTED, full.message


async def test_line_shift_invariance_with_real_diagnostics(tmp_path):
    ctx = make_ctx(tmp_path, {"pre.py": "import os\n\nprint(preexisting_undefined)\n\ndef g():\n    return 1\n"})
    try:
        base = (ctx.repo_root / "pre.py").read_text()
        r = await verify_batch([ed(ctx, "pre.py", "# one\n# two\n# three\n" + base)], ctx)
        assert r.status == Status.ACCEPTED, r.message  # pre-existing error shifted 3 lines, not net-new
        r = await verify_batch([ed(ctx, "pre.py", base + "print(brand_new_undefined)\n")], ctx)
        assert r.status == Status.REJECTED_LSP and "brand_new_undefined" in r.message
    finally:
        await ctx.lsp.stop()


async def test_new_file_with_error_rejected_and_clean_new_file_accepted(ctx):
    r = await verify_batch([ed(ctx, "fresh.py", "print(zzz)\n")], ctx)
    assert r.status == Status.REJECTED_LSP
    r = await verify_batch([ed(ctx, "fresh.py", "print(1)\n")], ctx)
    assert r.status == Status.ACCEPTED, r.message


async def test_python_path_hint_resolves_venv_only_package(tmp_path):
    import subprocess
    venv = tmp_path / "otherenv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    sp_candidates = list((venv / "lib").glob("python3*/site-packages")) + [venv / "Lib" / "site-packages"]
    sp = next(p for p in sp_candidates if p.is_dir())
    (sp / "venvonlypkg.py").write_text("VALUE = 1\n")
    code = "import venvonlypkg\nprint(venvonlypkg.VALUE)\n"
    without = make_ctx(tmp_path / "a", {"m.py": "x = 1\n"})
    py_bin = venv / "Scripts" / "python.exe" if (venv / "Scripts" / "python.exe").exists() else venv / "bin" / "python"
    withhint = make_ctx(tmp_path / "b", {"m.py": "x = 1\n"}, python_path=str(py_bin))
    try:
        r0 = await verify_batch([ed(without, "m.py", code)], without)
        r1 = await verify_batch([ed(withhint, "m.py", code)], withhint)
        assert r0.status == Status.REJECTED_LSP, r0.message  # unresolved import without the hint
        assert r1.status == Status.ACCEPTED, r1.message
    finally:
        await without.lsp.stop()
        await withhint.lsp.stop()
