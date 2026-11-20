"""`patchgate mcp|check|measure`."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path


def _cmd_check(a: argparse.Namespace) -> int:
    from .core.models import ResolvedEdit
    from .core.verifier import build_context, verify_batch

    repo = Path(a.repo).resolve()
    ctx = build_context(repo, Path(a.config) if a.config else None)
    try:
        abs_path, rel = ctx.policy.resolve(a.file)
    except Exception as e:  # PathError
        print(json.dumps({"status": "ERROR", "message": str(e)}))
        return 2
    old = abs_path.read_bytes().decode("utf-8") if abs_path.exists() else None
    new = Path(a.content_file).read_bytes().decode("utf-8")
    edit = ResolvedEdit(path=abs_path, rel_path=rel, old_content=old, new_content=new)

    async def run():
        try:
            return await verify_batch([edit], ctx)
        finally:
            await ctx.lsp.stop()

    res = asyncio.run(run())
    print(res.model_dump_json(indent=2))
    return 0 if str(res.status) == "ACCEPTED" else 1


def _cmd_measure(a: argparse.Namespace) -> int:
    script = Path(__file__).resolve().parents[2] / "scripts" / "measure_gate.py"
    if not script.is_file():
        print("measure needs a source checkout (scripts/measure_gate.py not found next to the package)",
              file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("measure_gate", script)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.run_cli(Path(a.repo), a.samples, Path(a.out) if a.out else None,
                       Path(a.config) if a.config else None, a.python_path)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "mcp":  # keep argparse out of the way of the server's own arg handling
        from .adapters.mcp_server import main as mcp_main
        mcp_main(argv[1:])
        return
    ap = argparse.ArgumentParser(prog="patchgate", description="Verification layer for AI coding agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="run the MCP server (--repo PATH [--config PATH])")
    c = sub.add_parser("check", help="one-shot verification of a proposed full-file replacement")
    c.add_argument("--repo", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--content-file", required=True)
    c.add_argument("--config", default=None)
    m = sub.add_parser("measure", help="measure gate latency and false-block rate on a repo")
    m.add_argument("--repo", required=True)
    m.add_argument("--samples", type=int, default=20)
    m.add_argument("--out", default=None)
    m.add_argument("--config", default=None)
    m.add_argument("--python-path", default=None,
                   help="interpreter whose site-packages Pyright should use (best-effort hint)")
    a = ap.parse_args(argv)
    code = _cmd_check(a) if a.cmd == "check" else _cmd_measure(a)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
