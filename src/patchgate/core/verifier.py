"""The Gate: policy -> AST -> LSP. Short-circuits on the first rejection (cheapest first). Never raises."""
from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ast_guard import AstGuard
from .config import Config
from .diagnostics import diff_file
from .import_graph import ImportGraph
from .lsp_client import LspBatchResult, LspTimeout, PyrightClient
from .models import (BatchVerificationResult, DiagnosticItem, PolicyViolation, ResolvedEdit,
                     StageTimings, Status)
from .policy import Policy
from .telemetry import Telemetry

log = logging.getLogger("patchgate.verifier")
MAX_ITEMS = 20


@dataclass
class VerifierContext:
    repo_root: Path
    config: Config
    policy: Policy
    ast_guard: AstGuard
    lsp: PyrightClient | None
    import_graph: ImportGraph
    telemetry: Telemetry


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _format(violations: list[PolicyViolation], diags: list[DiagnosticItem]) -> str:
    lines = [f"{v.rel_path}: [{v.rule}] {v.detail}" for v in violations]
    lines += [f"{d.rel_path}:{d.line + 1}: {d.message} [{d.code or 'no-code'}]" for d in diags]
    shown, omitted = lines[:MAX_ITEMS], len(lines) - MAX_ITEMS
    if omitted > 0:
        shown.append(f"... and {omitted} more")
    return "\n".join(shown)


def _redacted_diff(batch: list[ResolvedEdit], ctx: VerifierContext) -> str:
    """Unified diff for the trace with any line that matches a secret pattern redacted."""
    parts = []
    for e in batch:
        d = difflib.unified_diff((e.old_content or "").splitlines(), e.new_content.splitlines(),
                                 f"a/{e.rel_path}", f"b/{e.rel_path}", lineterm="")
        parts.extend(d)
    rxs = ctx.ast_guard._secret_res
    return "\n".join("[REDACTED: secret pattern]" if any(r.search(l) for r in rxs) else l for l in parts)


def _finish(ctx: VerifierContext, batch: list[ResolvedEdit], t_all: float, timings: StageTimings,
            status: Status, message: str, violations: list[PolicyViolation] | None = None,
            net_new: list[DiagnosticItem] | None = None, warnings: list[DiagnosticItem] | None = None,
            dependents: list[str] | None = None) -> BatchVerificationResult:
    timings.total_ms = _ms(t_all)
    res = BatchVerificationResult(
        status=status, violations=violations or [], net_new_diagnostics=net_new or [],
        warnings=warnings or [], dependents_checked=dependents or [], timings=timings, message=message)
    try:
        ctx.telemetry.emit("batch_verified", {
            "status": str(status), "files": [e.rel_path for e in batch],
            "violations": [v.model_dump() for v in res.violations],
            "net_new_count": len(res.net_new_diagnostics), "warning_count": len(res.warnings),
            "dependents": res.dependents_checked, "timings": timings.model_dump(),
            "diff": _redacted_diff(batch, ctx)})
    except Exception:
        log.exception("telemetry failed")
    return res


async def verify_batch(batch: list[ResolvedEdit], ctx: VerifierContext) -> BatchVerificationResult:
    t_all = time.perf_counter()
    timings = StageTimings()
    try:
        cfg = ctx.config
        # ---- Stage 1: policy
        t0 = time.perf_counter()
        pv = ctx.policy.check_batch(batch)
        timings.policy_ms = _ms(t0)
        if pv:
            return _finish(ctx, batch, t_all, timings, Status.REJECTED_POLICY, _format(pv, []), violations=pv)

        # ---- Stage 2: AST
        t0 = time.perf_counter()
        av = ctx.ast_guard.check_batch(batch)
        timings.ast_ms = _ms(t0)
        if av:
            return _finish(ctx, batch, t_all, timings, Status.REJECTED_AST, _format(av, []), violations=av)

        # ---- Stage 3: LSP (python files only)
        py_edits = [e for e in batch if e.rel_path.endswith(".py")]
        if not py_edits or ctx.lsp is None:
            return _finish(ctx, batch, t_all, timings, Status.ACCEPTED,
                           "All checks passed." if py_edits == [] else
                           "Policy and AST checks passed; static analysis is not available (no language server).")
        edited = {e.rel_path for e in batch}
        deps = ctx.import_graph.dependents_of([e.rel_path for e in py_edits], cfg.lsp.dependents_depth,
                                              cfg.lsp.max_dependents)
        extra: dict[str, str] = {}
        for rel in deps:
            if rel in edited:
                continue
            try:
                extra[rel] = (ctx.repo_root / rel).read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        lsp_res: LspBatchResult | None = None
        accepted = False
        try:
            try:
                lsp_res = await ctx.lsp.check_batch(py_edits, extra)
            except LspTimeout as to:
                timings.lsp_baseline_ms = to.partial.baseline_ms
                timings.lsp_apply_ms = to.partial.apply_ms
                timings.lsp_wait_ms = to.partial.wait_ms
                timings.lsp_total_ms = to.partial.total_ms
                if cfg.lsp.on_timeout == "reject":
                    return _finish(ctx, batch, t_all, timings, Status.LSP_TIMEOUT,
                                   f"Static analysis did not finish within {cfg.lsp.timeout_sec}s; "
                                   "the batch was rejected (lsp.on_timeout=reject). Nothing was written.",
                                   dependents=list(extra))
                accepted = True
                return _finish(ctx, batch, t_all, timings, Status.ACCEPTED,
                               f"WARNING: static analysis timed out after {cfg.lsp.timeout_sec}s, so LSP "
                               "verification was incomplete; policy and AST checks passed.",
                               dependents=list(extra))
            timings.lsp_baseline_ms = lsp_res.baseline_ms
            timings.lsp_apply_ms = lsp_res.apply_ms
            timings.lsp_wait_ms = lsp_res.wait_ms
            timings.lsp_total_ms = lsp_res.total_ms

            blocking: list[DiagnosticItem] = []
            warns: list[DiagnosticItem] = []
            for rel, post_raw in lsp_res.post.items():
                b, w = diff_file(rel, lsp_res.baseline.get(rel, []), lsp_res.baseline_texts.get(rel, ""),
                                 post_raw, lsp_res.post_texts.get(rel, ""), cfg.lsp.block_on,
                                 loose_fallback=cfg.lsp.loose_fallback)
                blocking += b
                warns += w
            if blocking:
                return _finish(ctx, batch, t_all, timings, Status.REJECTED_LSP,
                               "New static-analysis errors introduced by this batch:\n" + _format([], blocking),
                               net_new=blocking, warnings=warns, dependents=list(extra))
            accepted = True
            return _finish(ctx, batch, t_all, timings, Status.ACCEPTED, "All checks passed.",
                           warnings=warns, dependents=list(extra))
        finally:
            if lsp_res is not None:
                await ctx.lsp.settle(lsp_res, accepted, py_edits)
    except Exception as exc:  # never raise out of verify_batch
        log.exception("verify_batch internal error")
        try:
            ctx.telemetry.emit("error", {"where": "verify_batch", "error": repr(exc)[:300]})
        except Exception:
            pass
        timings.total_ms = _ms(t_all)
        return BatchVerificationResult(status=Status.ERROR, timings=timings,
                                       message=f"Internal error; nothing was written: {type(exc).__name__}: {exc}")


async def after_commit(ctx: VerifierContext, batch: list[ResolvedEdit]) -> None:
    """Call after commit_batch succeeds: refresh the import graph and re-sync the language server."""
    try:
        ctx.import_graph.invalidate([e.rel_path for e in batch])
        if ctx.lsp is not None:
            py = {e.rel_path: e.new_content for e in batch if e.rel_path.endswith(".py")}
            if py:
                await ctx.lsp.sync_documents(py)
    except Exception:
        log.exception("after_commit failed")


async def after_failed_commit(ctx: VerifierContext, batch: list[ResolvedEdit]) -> None:
    """Call if commit_batch raised after an ACCEPTED verification: the overlay must go back to disk state."""
    try:
        if ctx.lsp is not None:
            py = {e.rel_path: e.old_content for e in batch if e.rel_path.endswith(".py")}
            if py:
                await ctx.lsp.sync_documents(py)
    except Exception:
        log.exception("after_failed_commit failed")


def build_context(repo_root: Path, config_path: Path | None = None, session_id: str | None = None,
                  config: Config | None = None) -> VerifierContext:
    """Assemble a VerifierContext for a repo (shared by the CLI, the MCP adapter and the measurement script)."""
    from .config import load_config
    root = Path(repo_root).resolve()
    cfg = config or load_config(config_path if config_path is not None else root / "harness.yaml")
    telemetry = Telemetry(root, session_id)
    policy = Policy(root, cfg)
    lsp = PyrightClient(root, cfg.lsp, on_event=telemetry.emit)
    return VerifierContext(root, cfg, policy, AstGuard(policy, cfg), lsp,
                           ImportGraph(root, cfg.ignore_dirs), telemetry)
