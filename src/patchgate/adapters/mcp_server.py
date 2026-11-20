"""MCP adapter: the only module that touches the MCP SDK. stdout belongs to the SDK's JSON-RPC framing:
NEVER print here; log to stderr and .patchgate/mcp.log only."""
from __future__ import annotations

# --- logging is configured at import time, before anything else (stderr only; file added in setup_file_log)
import logging
import sys

_LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_root = logging.getLogger("patchgate")
_root.setLevel(logging.INFO)
if not any(getattr(h, "_patchgate_stderr", False) for h in _root.handlers):
    _h = logging.StreamHandler(sys.stderr)
    _h._patchgate_stderr = True  # type: ignore[attr-defined]
    _h.setFormatter(logging.Formatter(_LOG_FMT))
    _root.addHandler(_h)
_root.propagate = False

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402  (mcp 2.x; FastMCP no longer exists, see DECISIONS D1)

from ..core import commit as commit_mod  # noqa: E402
from ..core.ledger import STOP_MESSAGE, Ledger  # noqa: E402
from ..core.models import (BatchVerificationResult, FileEdit, PolicyViolation, ResolvedEdit,  # noqa: E402
                           StageTimings, Status)
from ..core.policy import PathError  # noqa: E402
from ..core.verifier import (after_commit, after_failed_commit, build_context,  # noqa: E402
                             verify_batch)

log = logging.getLogger("patchgate.mcp")
MAX_READ_LINES = 2000

INSTRUCTIONS = (
    "PatchGate verifies every file change before it is written. All file modifications must go through "
    "the propose_edits tool; edits are verified (policy, syntax-tree checks, static analysis) before "
    "being written, and a rejected batch writes nothing. Use read_file to read files."
)


def setup_file_log(repo: Path) -> None:
    d = repo / ".patchgate"
    d.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(d / "mcp.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FMT))
    _root.addHandler(fh)


class Runtime:
    def __init__(self, repo: Path, config_path: Path | None):
        self.repo = repo.resolve()
        if config_path is None or not Path(config_path).is_file():
            log.info("no harness.yaml at %s; using built-in defaults", config_path)
        self.ctx = build_context(self.repo, config_path)
        self.ledger = Ledger(self.ctx.config.circuit_breaker.max_consecutive_failures_per_file)
        self.lock = asyncio.Lock()
        recovered = commit_mod.recover(self.repo)  # crash recovery at startup
        self.ctx.telemetry.emit("session_start", {"repo": str(self.repo), "recovered": recovered})

    # ---------------------------------------------------------------- read
    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        try:
            abs_path, rel = self.ctx.policy.check_read(path)
        except PathError as e:
            self.ctx.telemetry.emit("read_denied", {"path": str(path)[:200]})  # never any contents
            return f"DENIED: {e}"
        if not abs_path.is_file():
            return f"ERROR: {rel!r} is not an existing file."
        try:
            text = abs_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return f"ERROR: {rel!r} is not valid UTF-8 text."
        lines = [l.rstrip("\r") for l in text.split("\n")]
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        start = max(1, start_line)
        end = total if end_line is None else min(end_line, total)
        if start > max(total, 1) or end < start:
            return f"ERROR: line range {start_line}-{end_line} is outside {rel!r} ({total} lines)."
        end_capped = min(end, start + MAX_READ_LINES - 1)
        width = max(4, len(str(end_capped)))
        body = "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end_capped + 1))
        self.ctx.telemetry.emit("read_file", {"path": rel, "start": start, "end": end_capped})
        note = ""
        if end_capped < end:
            note = f"\n[truncated: showing lines {start}-{end_capped} of {total}; call read_file with start_line={end_capped + 1} to continue]"
        return body + note

    # ---------------------------------------------------------------- propose
    def _resolve(self, edits: list[FileEdit]) -> tuple[list[ResolvedEdit], str | None, list[PolicyViolation]]:
        """Resolve against disk. Returns (resolved, error_text, pre_violations)."""
        resolved: list[ResolvedEdit] = []
        pre: list[PolicyViolation] = []
        for fe in edits:
            try:
                abs_path, rel = self.ctx.policy.resolve(fe.path)
            except PathError as e:
                return [], f"ERROR: invalid path {fe.path!r}: {e}. Nothing was written.", []
            old: str | None = None
            if abs_path.exists():
                if not abs_path.is_file():
                    return [], f"ERROR: {rel!r} is not a regular file. Nothing was written.", []
                try:
                    old = abs_path.read_bytes().decode("utf-8")  # bytes->str: no newline translation
                except UnicodeDecodeError:
                    pre.append(PolicyViolation(rule="non_utf8_existing_file", rel_path=rel,
                                               detail="existing file is not valid UTF-8; edits are not supported"))
                    continue
            if fe.replace is not None:
                if old is None:
                    return [], f"ERROR: replace-mode edit targets {rel!r}, which does not exist. Nothing was written.", []
                n = old.count(fe.replace.old_text)
                if n == 0:
                    return [], (f"ERROR: replace old_text was not found in {rel!r} (0 matches). "
                                "Re-read the file and copy the text exactly. Nothing was written."), []
                if n > 1:
                    return [], (f"ERROR: replace old_text matches {n} places in {rel!r}; it must occur exactly once. "
                                "Include more surrounding context. Nothing was written."), []
                new = old.replace(fe.replace.old_text, fe.replace.new_text, 1)
            else:
                new = fe.new_content or ""
            resolved.append(ResolvedEdit(path=abs_path, rel_path=rel, old_content=old, new_content=new))
        return resolved, None, pre

    def _recent_text(self, files: list[str]) -> str:
        rec = self.ledger.recent_for(files, 3)
        if not rec:
            return ""
        return "\nRecent rejections on these files:\n" + "\n".join(
            f"- {f.status}: {f.summary}" for f in rec)

    async def propose_edits(self, edits: list[FileEdit]) -> str:
        if not edits:
            return "ERROR: no edits given. Nothing was written."
        async with self.lock:
            resolved, err, pre = self._resolve(edits)
            if err:
                return err
            files = [e.rel_path for e in resolved] + [v.rel_path for v in pre]
            if self.ledger.is_tripped(files):
                tripped = self.ledger.tripped_files(files)
                self.ctx.telemetry.emit("circuit_open", {"files": tripped})
                return (f"CIRCUIT_OPEN: too many consecutive rejections on {', '.join(tripped)}. "
                        f"{STOP_MESSAGE}{self._recent_text(files)}")
            if pre:
                res = BatchVerificationResult(
                    status=Status.REJECTED_POLICY, violations=pre, timings=StageTimings(),
                    message="\n".join(f"{v.rel_path}: [{v.rule}] {v.detail}" for v in pre))
                self.ctx.telemetry.emit("batch_verified", {"status": str(res.status), "files": files,
                                                           "violations": [v.model_dump() for v in pre],
                                                           "timings": res.timings.model_dump()})
            else:
                res = await verify_batch(resolved, self.ctx)

            if res.status == Status.ACCEPTED:
                try:
                    written = await asyncio.to_thread(commit_mod.commit_batch, self.repo, resolved)
                except commit_mod.CommitError as e:
                    await after_failed_commit(self.ctx, resolved)
                    self.ctx.telemetry.emit("error", {"where": "commit", "error": str(e)[:300]})
                    return f"ERROR: {e}"
                await after_commit(self.ctx, resolved)
                self.ledger.reset_files(written)
                self.ctx.telemetry.emit("batch_committed", {"files": written})
                out = f"ACCEPTED: wrote {len(written)} file(s): {', '.join(written)}\n{res.message}"
                if res.warnings:
                    out += "\nNon-blocking warnings introduced:\n" + "\n".join(
                        f"{w.rel_path}:{w.line + 1}: {w.message} [{w.code or 'no-code'}]" for w in res.warnings[:20])
                return out

            if res.status == Status.ERROR:  # infrastructure failure: not the agent's fault, not counted
                return f"ERROR: {res.message}"
            mlines = res.message.split("\n")
            summary = mlines[1] if len(mlines) > 1 and mlines[0].endswith(":") else mlines[0]
            self.ledger.record_failure(files, res.status, summary[:200])
            out = f"{res.status}: nothing was written.\n{res.message}"
            if self.ledger.is_tripped(files):
                self.ctx.telemetry.emit("circuit_open", {"files": self.ledger.tripped_files(files)})
                out += f"\n{STOP_MESSAGE}"
            return out + self._recent_text(files)

    def audit(self, limit: int = 10) -> str:
        events = []
        for ev in self.ctx.telemetry.recent(limit):
            payload = {k: v for k, v in ev["payload"].items() if k != "diff"}
            events.append({"ts": round(ev["ts"], 3), "event": ev["event"], "payload": payload})
        return json.dumps({"session_id": self.ctx.telemetry.session_id, "events": events,
                           "latency": self.ctx.telemetry.latency_stats()}, indent=2, default=str)


def create_server(repo: Path, config_path: Path | None = None) -> tuple[MCPServer, Runtime]:
    rt = Runtime(repo, config_path)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {}
        finally:
            await rt.ctx.lsp.stop()

    server = MCPServer("patchgate", instructions=INSTRUCTIONS, lifespan=lifespan)

    @server.tool()
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a text file from the repository, returned as numbered lines ("  12 | code").
        Paths are relative to the repo root. Files covered by the read-deny policy cannot be read.
        Output is capped at 2000 lines; use start_line/end_line (1-based, inclusive) to page."""
        return rt.read_file(path, start_line, end_line)

    @server.tool()
    async def propose_edits(edits: list[FileEdit]) -> str:
        """The ONLY way to modify files. All file modifications must go through this tool.
        Edits are verified before anything is written (path/access policy, syntax-tree checks, static
        analysis of the changed files and the files that import them). If every check passes, all files
        are written together; if any check fails, NOTHING is written and the reasons are returned.
        Each edit is either {"path": ..., "new_content": <entire new file text>} or
        {"path": ..., "replace": {"old_text": ..., "new_text": ...}} where old_text must occur exactly
        once in the current file. Send related changes (e.g. a function signature and all its callers)
        in ONE call so they are validated together."""
        return await rt.propose_edits(edits)

    @server.tool()
    def get_audit_trace(limit: int = 10) -> str:
        """Return the most recent PatchGate events for this session (verifications, commits, rejections)
        and measured p50/p95/p99 latency of the verification gate."""
        return rt.audit(limit)

    return server, rt


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="patchgate mcp")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    setup_file_log(repo)
    cfg = Path(a.config) if a.config else repo / "harness.yaml"
    server, _ = create_server(repo, cfg)
    log.info("patchgate MCP server starting for %s", repo)
    server.run("stdio")


if __name__ == "__main__":
    main()
