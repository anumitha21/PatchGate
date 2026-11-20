"""Measure the Gate on a real repository: latency, false-block rate, miss rate.

Runs synthetic proposals through `verify_batch` WITHOUT committing (nothing in the target repo is
modified; telemetry goes under <repo>/.patchgate/ only). After every ACCEPTED case the language-server
overlay is re-synced to disk so cases do not contaminate each other.

Usage:  patchgate measure --repo PATH [--samples N] [--out DIR] [--python-path PY]
Results are reported as measured, including bad ones. No claim is made beyond the data.
"""
from __future__ import annotations

import ast
import asyncio
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from patchgate.core.config import load_config
from patchgate.core.models import ResolvedEdit
from patchgate.core.telemetry import percentile
from patchgate.core.verifier import build_context, verify_batch

CASES = ["benign_comment", "benign_whitespace", "broken_undefined_name", "broken_syntax",
         "broken_signature", "fixed_signature_multifile"]
EXPECTED = {"benign_comment": "ACCEPTED", "benign_whitespace": "ACCEPTED",
            "broken_undefined_name": "REJECTED_LSP", "broken_syntax": "REJECTED_AST",
            "broken_signature": "REJECTED_LSP", "fixed_signature_multifile": "ACCEPTED"}
BENIGN = ["benign_comment", "benign_whitespace"]
BROKEN = ["broken_undefined_name", "broken_syntax", "broken_signature"]
NEW_PARAM = "patchgate_new_param"
MIN_LINES = 15
SKIP_DIRS = {"tests", "test", "docs", "doc", "examples", "example", "scripts", "benchmarks"}


# ----------------------------------------------------------------------------- file selection
def _py_files(root: Path, ignore: set[str]) -> list[str]:
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in ignore or part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        out.append(rel.as_posix())
    return out


def select_files(root: Path, ctx, n: int) -> list[str]:
    cands = []
    for rel in _py_files(root, set(ctx.config.ignore_dirs)):
        if ctx.policy.is_test_file(rel):
            continue
        try:
            text = (root / rel).read_bytes().decode("utf-8")
            ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if text.count("\n") + 1 < MIN_LINES:
            continue
        cands.append(rel)
    if len(cands) <= n:
        return cands
    step = len(cands) / n  # evenly spaced, deterministic
    return [cands[int(i * step)] for i in range(n)]


# ----------------------------------------------------------------------------- text edits
def _line_offsets(text: str) -> list[int]:
    offs, pos = [0], 0
    for line in text.splitlines(keepends=True):
        pos += len(line)
        offs.append(pos)
    return offs


def _pos(text: str, offs: list[int], lineno: int, col_utf8: int) -> int:
    """ast gives (1-based line, UTF-8 byte column) -> character index into `text`."""
    start, end = offs[lineno - 1], offs[lineno]
    line = text[start:end]
    return start + len(line.encode("utf-8")[:col_utf8].decode("utf-8", errors="ignore"))


def case_benign_comment(text: str) -> str:
    return text + ("" if text.endswith("\n") else "\n") + "# patchgate measurement comment\n"


def case_benign_whitespace(text: str) -> str:
    return "\n" + text  # shifts every line number by one


def case_undefined_name(text: str) -> str:
    return text + ("" if text.endswith("\n") else "\n") + "\n_patchgate_probe = patchgate_undefined_name_xyz\n"


def case_syntax(text: str) -> str:
    return text + ("" if text.endswith("\n") else "\n") + "\npatchgate_broken = (\n"


def find_signature_candidates(root: Path, rel: str, deps: list[str]):
    """Top-level functions in `rel` with >=1 positional param and no defaults, plus the dependents that
    really call them (heuristic: `from <mod> import name` then name(...), or <anything>.name(...))."""
    text = (root / rel).read_bytes().decode("utf-8")
    tree = ast.parse(text)
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        if not a.args or a.defaults or node.name.startswith("__"):
            continue
        callers: dict[str, list[ast.Call]] = {}
        for d in deps:
            try:
                dt = ast.parse((root / d).read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            imported = any(isinstance(n, ast.ImportFrom) and any(al.name == node.name and al.asname is None
                                                                  for al in n.names) for n in ast.walk(dt))
            calls = [c for c in ast.walk(dt) if isinstance(c, ast.Call) and (
                (imported and isinstance(c.func, ast.Name) and c.func.id == node.name)
                or (isinstance(c.func, ast.Attribute) and c.func.attr == node.name))]
            if calls:
                callers[d] = calls
        own = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and (
            (isinstance(c.func, ast.Name) and c.func.id == node.name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == node.name))]
        if callers:
            found.append((node, callers, own))
    return text, found


def add_param(text: str, fn: ast.FunctionDef) -> str:
    last = fn.args.args[-1]
    offs = _line_offsets(text)
    p = _pos(text, offs, last.end_lineno, last.end_col_offset)
    return text[:p] + f", {NEW_PARAM}: object" + text[p:]


def fixed_definition_text(text: str, fn: ast.FunctionDef, own_calls: list[ast.Call]) -> str:
    """Add the new parameter AND update calls to the function inside the defining file itself."""
    t = fix_calls(text, own_calls) if own_calls else text
    for node in ast.parse(t).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn.name:
            return add_param(t, node)
    raise ValueError("function not found after call fix")


def fix_calls(text: str, calls: list[ast.Call]) -> str:
    offs = _line_offsets(text)
    spots = []
    for c in calls:
        close = _pos(text, offs, c.end_lineno, c.end_col_offset) - 1  # index of ')'
        if text[close] != ")":
            raise ValueError("unexpected call end")
        k = close - 1
        while k >= 0 and text[k].isspace():
            k -= 1
        sep = "" if text[k] in ",(" else ", "
        spots.append((close, f"{sep}{NEW_PARAM}=None"))
    for close, ins in sorted(spots, reverse=True):
        text = text[:close] + ins + text[close:]
    return text


# ----------------------------------------------------------------------------- running
def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


def _edit(root: Path, rel: str, new: str) -> ResolvedEdit:
    return ResolvedEdit(path=root / rel, rel_path=rel, old_content=_read(root, rel), new_content=new)


async def run_case(ctx, root: Path, name: str, batch: list[ResolvedEdit], rec_list: list[dict], file: str,
                   record: bool, n_deps: int):
    res = await verify_batch(batch, ctx)
    status = str(res.status)
    timed_out = res.message.startswith("WARNING: static analysis timed out")
    if status == "ACCEPTED" and ctx.lsp is not None:  # never commit: put the overlay back on disk state
        await ctx.lsp.sync_documents({e.rel_path: e.old_content for e in batch}, wait=True)
    if record:
        rec_list.append({"case": name, "file": file, "status": status, "timed_out": timed_out,
                         "dependents": n_deps, "n_net_new": len(res.net_new_diagnostics),
                         "timings": res.timings.model_dump(), "message": res.message[:300]})
    return res


async def run_pass(root: Path, cfg, files: list[str], samples: int, record_latency: bool):
    skipped = {"fixed_signature_protected_callers": 0, "fixed_signature_generation_error": 0}
    ctx = build_context(root, config=cfg)
    records: list[dict] = []
    sig_cases = []
    try:
        for rel in files:
            text = _read(root, rel)
            deps = ctx.import_graph.dependents_of([rel], 1, cfg.lsp.max_dependents)
            nd = len(deps)
            for name, fn in (("benign_comment", case_benign_comment), ("benign_whitespace", case_benign_whitespace),
                             ("broken_undefined_name", case_undefined_name), ("broken_syntax", case_syntax)):
                await run_case(ctx, root, name, [_edit(root, rel, fn(text))], records, rel, True, nd)
        # signature cases: scan ALL candidate files (not only the sampled ones) until `samples` found
        for rel in _py_files(root, set(cfg.ignore_dirs)):
            if len(sig_cases) >= samples:
                break
            if ctx.policy.is_test_file(rel):
                continue
            try:
                deps = ctx.import_graph.dependents_of([rel], 1, cfg.lsp.max_dependents)
                if not deps:
                    continue
                text, cands = find_signature_candidates(root, rel, deps)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for fn, callers, own in cands[:1]:
                if len(callers) > 8:
                    continue
                sig_cases.append((rel, text, fn, callers, own, len(deps)))
        for rel, text, fn, callers, own, nd in sig_cases:
            broken_src = add_param(text, fn)
            await run_case(ctx, root, "broken_signature", [_edit(root, rel, broken_src)], records, rel, True, nd)
            if any(ctx.policy.is_write_protected(d) for d in callers):
                skipped["fixed_signature_protected_callers"] += 1  # a human must edit those callers
                continue
            try:
                batch = [_edit(root, rel, fixed_definition_text(text, fn, own))]
                for d, calls in callers.items():
                    batch.append(_edit(root, d, fix_calls(_read(root, d), calls)))
            except Exception:
                skipped["fixed_signature_generation_error"] += 1
                continue
            await run_case(ctx, root, "fixed_signature_multifile", batch, records, rel, True, nd)
    finally:
        if ctx.lsp is not None:
            await ctx.lsp.stop()
    return records, skipped


# ----------------------------------------------------------------------------- reporting
def _stats(vals: list[float]) -> dict:
    return {"count": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95),
            "p99": percentile(vals, 99)}


def summarize(records: list[dict]) -> dict:
    matrix: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        matrix[r["case"]][r["status"]] += 1
    benign = [r for r in records if r["case"] in BENIGN]
    broken = [r for r in records if r["case"] in BROKEN]
    fixed = [r for r in records if r["case"] == "fixed_signature_multifile"]
    rej = lambda r: r["status"] != "ACCEPTED"  # noqa: E731
    rate = lambda n, d: (n / d if d else None)  # noqa: E731
    return {
        "matrix": {c: dict(matrix[c]) for c in CASES if c in matrix},
        "counts": {c: sum(matrix[c].values()) for c in CASES},
        "false_block_rate_benign": rate(sum(map(rej, benign)), len(benign)),
        "false_blocks_benign": f"{sum(map(rej, benign))}/{len(benign)}",
        "false_block_rate_fixed_signature": rate(sum(map(rej, fixed)), len(fixed)),
        "false_blocks_fixed_signature": f"{sum(map(rej, fixed))}/{len(fixed)}",
        "miss_rate_broken": rate(sum(not rej(r) for r in broken), len(broken)),
        "misses_broken": f"{sum(not rej(r) for r in broken)}/{len(broken)}",
        "timeouts": sum(1 for r in records if r["timed_out"]),
        "unexpected": [{"case": r["case"], "file": r["file"], "expected": EXPECTED[r["case"]],
                        "actual": r["status"], "message": r["message"][:200]}
                       for r in records if r["status"] != EXPECTED[r["case"]]][:40],
    }


def latency(records: list[dict]) -> dict:
    """Cold = the first verify_batch call of the pass (includes language-server start and first analysis);
    warm = every later call. lsp_total_ms is only defined for calls that reached the LSP stage."""
    cold, warm = records[:1], records[1:]
    def pick(rs, key, lsp_only):
        out = []
        for r in rs:
            v = r["timings"][key]
            if lsp_only and not r["timings"]["lsp_total_ms"]:
                continue
            out.append(v)
        return out
    return {"cold": {"total_ms": _stats(pick(cold, "total_ms", False)),
                     "lsp_total_ms": _stats(pick(cold, "lsp_total_ms", True))},
            "warm": {"total_ms": _stats(pick(warm, "total_ms", False)),
                     "lsp_total_ms": _stats(pick(warm, "lsp_total_ms", True))},
            "note": "nearest-rank percentiles; cold has n=1 per run by definition"}


def _git_head(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def _pyright_version() -> str | None:
    exe = shutil.which("pyright") or str(Path(sys.executable).parent / "pyright")
    try:
        return subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return None


def fmt(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def render_md(rep: dict) -> str:
    e, a, b = rep["environment"], rep["with_loose_fallback"], rep["without_loose_fallback"]
    L = [f"# PatchGate measurement: {e['repo']}", "",
         "Results as measured. No claims beyond this data. Cases are synthetic; nothing was committed.", "",
         "## Environment", ""]
    for k, v in e.items():
        L.append(f"- **{k}**: {v}")
    L += ["", "## Outcomes (loose fallback ON; expected vs actual)", "",
          "| case | n | expected | actual |", "|---|---|---|---|"]
    for c in CASES:
        if c in a["matrix"]:
            L.append(f"| {c} | {a['counts'][c]} | {EXPECTED[c]} | "
                     + ", ".join(f"{k}: {v}" for k, v in sorted(a['matrix'][c].items())) + " |")
        else:
            L.append(f"| {c} | 0 | {EXPECTED[c]} | (no eligible functions/callers found) |")
    L += ["", "## Rates", "", "| metric | fallback ON | fallback OFF |", "|---|---|---|",
          f"| false-block rate, benign cases | {a['false_blocks_benign']} ({fmt(a['false_block_rate_benign'])}) | "
          f"{b['false_blocks_benign']} ({fmt(b['false_block_rate_benign'])}) |",
          f"| false-block rate, fixed multi-file refactor | {a['false_blocks_fixed_signature']} | "
          f"{b['false_blocks_fixed_signature']} |",
          f"| miss rate, broken cases | {a['misses_broken']} ({fmt(a['miss_rate_broken'])}) | "
          f"{b['misses_broken']} ({fmt(b['miss_rate_broken'])}) |",
          f"| LSP timeouts | {a['timeouts']} | {b['timeouts']} |", "",
          "## Latency (ms, loose fallback ON pass)", "", "| phase | metric | n | p50 | p95 | p99 |",
          "|---|---|---|---|---|---|"]
    for ph in ("cold", "warm"):
        for m in ("total_ms", "lsp_total_ms"):
            s = rep["latency"][ph][m]
            L.append(f"| {ph} | {m} | {s['count']} | {fmt(s['p50'])} | {fmt(s['p95'])} | {fmt(s['p99'])} |")
    L += ["", rep["latency"]["note"], "", "## Unexpected outcomes (fallback ON)", ""]
    if a["unexpected"]:
        for u in a["unexpected"]:
            L.append(f"- `{u['case']}` `{u['file']}`: expected {u['expected']}, got {u['actual']}: "
                     f"{u['message'].splitlines()[0] if u['message'] else ''}")
    else:
        L.append("None.")
    return "\n".join(L) + "\n"


def run_cli(repo: Path, samples: int, out: Path | None, config: Path | None, python_path: str | None) -> int:
    root = Path(repo).resolve()
    out = (out or Path.cwd() / "measure-out" / root.name).resolve()
    out.mkdir(parents=True, exist_ok=True)
    base_cfg = load_config(config if config else root / "harness.yaml")
    if python_path:
        base_cfg.lsp.python_path = python_path
    probe = build_context(root, config=base_cfg)
    files = select_files(root, probe, samples)
    n_py = len(_py_files(root, set(base_cfg.ignore_dirs)))
    deps_counts = [len(probe.import_graph.dependents_of([f], 1, 10_000)) for f in files]
    print(f"[measure] {root.name}: {len(files)} sampled files of {n_py}", file=sys.stderr)

    t0 = time.time()
    recs_on, skipped = asyncio.run(run_pass(root, base_cfg, files, samples, True))
    base_off = base_cfg.model_copy(deep=True)
    base_off.lsp.loose_fallback = False
    recs_off, _ = asyncio.run(run_pass(root, base_off, files, samples, False))
    rep = {
        "environment": {
            "repo": root.name, "repo_commit": _git_head(root), "python_files_in_repo": n_py,
            "files_sampled": len(files), "samples_requested": samples,
            "dependents_per_sampled_file (mean/max)": (
                f"{sum(deps_counts) / len(deps_counts):.1f}/{max(deps_counts)}" if deps_counts else "n/a"),
            "pyright": _pyright_version(), "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}", "lsp.python_path": base_cfg.lsp.python_path,
            "lsp.timeout_sec": base_cfg.lsp.timeout_sec, "lsp.quiescence_ms": base_cfg.lsp.quiescence_ms,
            "wall_time_sec (both passes)": round(time.time() - t0, 1),
            "fixed_signature cases skipped": json.dumps(skipped)},
        "with_loose_fallback": summarize(recs_on), "without_loose_fallback": summarize(recs_off),
        "latency": latency(recs_on), "records_with_loose_fallback": recs_on,
        "records_without_loose_fallback": recs_off}
    (out / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(render_md(rep), encoding="utf-8")
    print(f"[measure] wrote {out}/report.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--out", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--python-path", default=None)
    a = ap.parse_args()
    raise SystemExit(run_cli(Path(a.repo), a.samples, Path(a.out) if a.out else None,
                             Path(a.config) if a.config else None, a.python_path))
