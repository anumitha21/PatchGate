"""Async JSON-RPC client for a long-lived Pyright language server (the only module that talks to it)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LspConfig
from .models import ResolvedEdit

log = logging.getLogger("patchgate.lsp")

SERVER_CANDIDATES = ["pyright-langserver", "pyright-python-langserver"]


# --------------------------------------------------------------------------- framing
def encode_message(obj: dict[str, Any]) -> bytes:
    """Content-Length is the BYTE length of the UTF-8 body."""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one framed message; None on clean EOF. Tolerates extra headers and any header case."""
    length: int | None = None
    saw_any = False
    while True:
        line = await reader.readline()
        if not line:
            if saw_any:
                raise EOFError("EOF inside LSP header block")
            return None
        saw_any = True
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length is None:
        raise ValueError("LSP message without Content-Length")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


# --------------------------------------------------------------------------- types
class LspTimeout(Exception):
    def __init__(self, partial: "LspBatchResult"):
        super().__init__("language server did not finish within timeout")
        self.partial = partial


class LspDead(Exception):
    pass


@dataclass
class DocState:
    version: int
    text: str
    open: bool = True


@dataclass
class _Published:
    version: int | None
    diagnostics: list[dict[str, Any]]
    received_at: float
    seq: int


@dataclass
class LspBatchResult:
    baseline: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # rel_path -> raw
    baseline_texts: dict[str, str] = field(default_factory=dict)
    post: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    post_texts: dict[str, str] = field(default_factory=dict)
    edited: list[str] = field(default_factory=list)  # rel_paths changed by the batch
    new_files: list[str] = field(default_factory=list)
    baseline_ms: float = 0.0
    apply_ms: float = 0.0
    wait_ms: float = 0.0
    total_ms: float = 0.0
    settled: bool = False


def find_server_command(configured: str | None) -> list[str]:
    if configured:
        return shlex.split(configured)
    here = Path(sys.executable).parent
    for name in SERVER_CANDIDATES:
        found = shutil.which(name) or (str(here / name) if (here / name).exists() else None)
        if found:
            return [found]
    raise FileNotFoundError("no Pyright language server found (looked for " + ", ".join(SERVER_CANDIDATES) + ")")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- client


def canon_uri(uri: str) -> str:
    """Canonical dictionary key for a file URI. The server may spell the same file differently from
    Path.as_uri() (Windows: 'file:///d%3A/dev/x.py' vs 'file:///D:/dev/x.py'); the diagnostics store was
    keyed by the raw string, so on Windows no lookup ever matched and every check timed out. Both the
    store writes and all lookups go through this function. Drive-letter paths are lower-cased (Windows
    paths are case-insensitive); other paths keep their case."""
    try:
        import re
        from urllib.parse import urlparse
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return uri
        m = re.match(r"^/([A-Za-z])(?::|%3[Aa])(.*)$", parsed.path)
        if m:
            drive = m.group(1).lower()
            rest = m.group(2)
            return f"file:///{drive}:{rest}"
        return uri
    except Exception:
        return uri


def rel_from_uri(uri: str, root: Path) -> str | None:
    """file:// URI -> POSIX path relative to `root`, or None if outside/unparseable.

    Windows URIs have a URL path like '/D:/dev/x.py' or '/d%3A/dev/x.py' (encoded colon, lowercase drive).
    Path(urlparse(uri).path) never matched those, so every publishDiagnostics was ignored and checks waited
    out the full timeout. urllib's url2pathname also leaves a stray leading backslash for the encoded-colon
    form, so the drive-letter form is handled explicitly (decoding exactly once)."""
    try:
        import re
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        m = re.match(r"^/([A-Za-z])(?::|%3[Aa])(/.*)?$", parsed.path)
        if m:
            local = f"{m.group(1)}:{unquote(m.group(2) or '/')}"
        else:
            local = url2pathname(parsed.path)
        return type(root)(local).relative_to(root).as_posix()
    except Exception:
        return None


class PyrightClient:
    def __init__(self, repo_root: Path, cfg: LspConfig,
                 command: list[str] | None = None, on_event: Any = None):
        self.root = Path(repo_root).resolve()
        self.cfg = cfg
        self._command = command
        self._on_event = on_event  # callable(event, payload) for telemetry (lsp_restart, lsp_timeout)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._docs: OrderedDict[str, DocState] = OrderedDict()  # LRU order (oldest first)
        self._store: dict[str, _Published] = {}
        self._seq = 0
        self._active_progress = 0
        self._last_event_at = 0.0
        self._cond = asyncio.Condition()
        self._baseline_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._started = False
        self._dead = False
        self._restarts = 0
        self._lock = asyncio.Lock()  # one batch check at a time
        self.server_capabilities: dict[str, Any] = {}

    # ---- helpers
    def uri_of(self, rel: str) -> str:
        return (self.root / rel).as_uri()

    def _rel_of(self, uri: str) -> str | None:
        return rel_from_uri(uri, self.root)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(event, payload)
            except Exception:  # telemetry must never break the client
                log.exception("telemetry callback failed")

    # ---- lifecycle
    async def start(self) -> None:
        if self._started and not self._dead:
            return
        cmd = (self._command or find_server_command(self.cfg.server_command)) + ["--stdio"]
        log.info("starting language server: %s", cmd)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(self.root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._dead = False
        self._pending.clear()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        root_uri = self.root.as_uri()
        resp = await asyncio.wait_for(self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"versionSupport": True},
                    "synchronization": {"dynamicRegistration": False, "didSave": False},
                },
                "workspace": {"configuration": True, "workspaceFolders": True},
            },
        }), timeout=max(30.0, self.cfg.timeout_sec * 3))
        self.server_capabilities = (resp or {}).get("capabilities", {})
        await self._notify("initialized", {})
        self._started = True

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if not self._dead and proc.returncode is None:
                try:
                    await asyncio.wait_for(self._request("shutdown", None), timeout=5)
                    await self._notify("exit", None)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
        finally:
            for t in (self._reader_task, self._stderr_task):
                if t:
                    t.cancel()
            self._proc = None
            self._started = False
            self._dead = True

    async def _ensure_alive(self) -> None:
        if not self._started:
            await self.start()
            return
        if self._dead or (self._proc and self._proc.returncode is not None):
            if self._restarts >= 1 and self._restarts_exhausted():
                raise LspDead("language server died repeatedly")
            self._restarts += 1
            log.warning("language server died; restarting (restart #%d)", self._restarts)
            self._emit("lsp_restart", {"restart": self._restarts})
            tracked = [(u, d.text) for u, d in self._docs.items() if d.open]
            self._started = False
            self._store.clear()
            await self.start()
            for uri, text in tracked:  # re-open tracked documents
                st = self._docs[uri]
                st.version += 1
                await self._notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": "python", "version": st.version, "text": text}})

    def _restarts_exhausted(self) -> bool:
        return self._restarts > 3

    # ---- transport
    async def _send(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            self._dead = True
            raise LspDead("language server is not running")
        try:
            self._proc.stdin.write(encode_message(obj))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._dead = True
            raise LspDead(str(e)) from e

    async def _request(self, method: str, params: Any) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await fut

    async def _notify(self, method: str, params: Any) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while (line := await self._proc.stderr.readline()):
                log.debug("pyright stderr: %s", line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            pass

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                msg = await read_message(self._proc.stdout)
                if msg is None:
                    break
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("language server reader failed")
        finally:
            self._dead = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LspDead("language server connection closed"))
            self._pending.clear()
            async with self._cond:
                self._cond.notify_all()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:  # server -> client REQUEST: always answer
            await self._answer_server_request(msg)
        elif "method" in msg:
            method = msg["method"]
            if method in ("textDocument/publishDiagnostics", "pyright/beginProgress", "pyright/endProgress"):
                self._last_event_at = time.monotonic()
            if method == "textDocument/publishDiagnostics":
                p = msg.get("params", {})
                self._seq += 1
                self._store[canon_uri(p["uri"])] = _Published(p.get("version"), p.get("diagnostics", []),
                                                   time.monotonic(), self._seq)
            elif method == "pyright/beginProgress":
                self._active_progress += 1
            elif method == "pyright/endProgress":
                self._active_progress = max(0, self._active_progress - 1)
            async with self._cond:
                self._cond.notify_all()
        elif "id" in msg:  # response
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"LSP error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))

    async def _answer_server_request(self, msg: dict[str, Any]) -> None:
        method, rid, params = msg["method"], msg["id"], msg.get("params") or {}
        try:
            if method == "workspace/configuration":
                items = params.get("items", [])
                result: Any = [self._config_for(it) for it in items]  # exactly one entry per item
            elif method in ("window/workDoneProgress/create", "client/registerCapability",
                            "client/unregisterCapability", "window/showMessageRequest"):
                result = None
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            else:
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": f"Method not found: {method}"}})
                return
            await self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LspDead:
            raise
        except Exception as e:  # still reply, or the server may stall
            log.exception("failed answering server request %s", method)
            try:
                await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
            except LspDead:
                pass

    def _config_for(self, item: dict[str, Any]) -> Any:
        # Verified (docs/VERIFIED_FACTS.md): section "python" + {"pythonPath": ...} changes import resolution.
        if item.get("section") == "python" and self.cfg.python_path:
            return {"pythonPath": self.cfg.python_path}
        return {}

    # ---- documents
    async def _open_or_set(self, rel: str, text: str) -> int:
        """Ensure the doc is open with `text`; return the version of the last notification sent
        (or the current version if nothing needed sending)."""
        uri = self.uri_of(rel)
        st = self._docs.get(uri)
        if st is None or not st.open:
            version = (st.version + 1) if st else 1
            self._docs[uri] = DocState(version, text, True)
            self._docs.move_to_end(uri)
            await self._notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "python", "version": version, "text": text}})
            return version
        self._docs.move_to_end(uri)
        if st.text != text:
            st.version += 1
            st.text = text
            await self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": st.version},
                "contentChanges": [{"text": text}]})
        return st.version

    async def _close(self, rel: str) -> None:
        uri = self.uri_of(rel)
        st = self._docs.get(uri)
        if st and st.open:
            st.open = False
            await self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            self._store.pop(canon_uri(uri), None)

    async def _evict(self, protected: set[str]) -> None:
        open_uris = [u for u, d in self._docs.items() if d.open]
        excess = len(open_uris) - self.cfg.max_open_documents
        for u in open_uris:
            if excess <= 0:
                break
            if u in protected:
                continue
            rel = self._rel_of(u)
            if rel is not None:
                await self._close(rel)
                excess -= 1

    # ---- completion
    def _ready(self, uri: str, sent_version: int, seq_at_send: int, now: float) -> bool:
        pub = self._store.get(canon_uri(uri))
        if pub is None:
            return False
        if pub.version is not None:
            if pub.version < sent_version:
                return False
        elif pub.seq <= seq_at_send:  # server supplies no versions: need a publish since the change
            return False
        if self._active_progress > 0:
            return False
        # quiescence: preliminary publishes happen (docs/VERIFIED_FACTS.md), so require silence both
        # for this document and globally (dependents may be re-analysed without a changed version).
        q = self.cfg.quiescence_ms
        return (now - pub.received_at) * 1000 >= q and (now - self._last_event_at) * 1000 >= q

    async def _wait_for(self, targets: dict[str, int], seq_at_send: int, deadline: float) -> bool:
        """Wait until all targets (uri -> version sent) are final. False on deadline."""
        quiet = self.cfg.quiescence_ms / 1000
        while True:
            now = time.monotonic()
            if all(self._ready(u, v, seq_at_send, now) for u, v in targets.items()):
                return True
            if self._dead:
                raise LspDead("language server died while waiting for diagnostics")
            remaining = deadline - now
            if remaining <= 0:
                return False
            try:
                async with self._cond:
                    await asyncio.wait_for(self._cond.wait(), timeout=min(remaining, quiet / 2 or 0.01))
            except asyncio.TimeoutError:
                pass

    def _diags(self, rel: str) -> list[dict[str, Any]]:
        pub = self._store.get(canon_uri(self.uri_of(rel)))
        return list(pub.diagnostics) if pub else []

    # ---- public API
    async def check_batch(self, edits: list[ResolvedEdit], extra_documents: dict[str, str]) -> LspBatchResult:
        """Transactional overlay check. On normal return the overlay is left APPLIED; the caller must
        call settle(result, accepted). On exception/timeout originals are restored before raising."""
        async with self._lock:
            return await self._check_locked(edits, extra_documents)

    async def _check_locked(self, edits: list[ResolvedEdit], extra: dict[str, str]) -> LspBatchResult:
        await self._ensure_alive()
        t_start = time.monotonic()
        res = LspBatchResult()
        deadline = t_start + self.cfg.timeout_sec
        originals: dict[str, str | None] = {e.rel_path: e.old_content for e in edits}
        res.edited = [e.rel_path for e in edits]
        res.new_files = [e.rel_path for e in edits if e.old_content is None]
        protected = {self.uri_of(r) for r in list(originals) + list(extra)}
        applied = False
        try:
            # 1-2: originals + baseline
            t0 = time.monotonic()
            base_targets: dict[str, int] = {}
            seq_base = self._seq
            for rel, text in list(extra.items()) + [(r, o) for r, o in originals.items() if o is not None]:
                res.baseline_texts[rel] = text
                key = (rel, _sha(text))
                if key in self._baseline_cache and self._docs.get(self.uri_of(rel), DocState(0, "", False)).text == text \
                        and self._docs[self.uri_of(rel)].open:
                    res.baseline[rel] = self._baseline_cache[key]
                    continue
                base_targets[rel] = await self._open_or_set(rel, text)
            for r in res.new_files:
                res.baseline_texts[r] = ""
                res.baseline[r] = []
            if base_targets:
                ok = await self._wait_for({self.uri_of(r): v for r, v in base_targets.items()}, seq_base, deadline)
                for rel in base_targets:
                    res.baseline[rel] = self._diags(rel)
                    self._baseline_cache[(rel, _sha(res.baseline_texts[rel]))] = res.baseline[rel]
                if not ok:
                    res.baseline_ms = (time.monotonic() - t0) * 1000
                    raise LspTimeout(res)
            res.baseline_ms = (time.monotonic() - t0) * 1000

            # 3: apply NEW content for all edited files before waiting for anything
            t1 = time.monotonic()
            targets: dict[str, int] = {}
            applied = True
            seq_at_send = self._seq
            for e in edits:
                targets[self.uri_of(e.rel_path)] = await self._open_or_set(e.rel_path, e.new_content)
                res.post_texts[e.rel_path] = e.new_content
            for rel, text in extra.items():
                # dependents must be re-analysed against the new overlay; their text is unchanged,
                # so no notification is sent and we wait for the server to republish them.
                res.post_texts[rel] = text
                st = self._docs[self.uri_of(rel)]
                targets[self.uri_of(rel)] = st.version
            res.apply_ms = (time.monotonic() - t1) * 1000

            # 4: wait
            t2 = time.monotonic()
            await self._evict(protected)
            ok = await self._wait_for(targets, seq_at_send, deadline)
            res.wait_ms = (time.monotonic() - t2) * 1000
            for rel in res.post_texts:
                res.post[rel] = self._diags(rel)
            if not ok:
                raise LspTimeout(res)
            res.total_ms = (time.monotonic() - t_start) * 1000
            return res
        except BaseException as exc:
            res.total_ms = (time.monotonic() - t_start) * 1000
            if isinstance(exc, LspTimeout):
                self._emit("lsp_timeout", {"timeout_sec": self.cfg.timeout_sec})
            if applied or res.baseline_texts:
                await self._restore(res, originals)
            raise

    async def _restore(self, res: LspBatchResult, originals: dict[str, str | None]) -> None:
        """Put the server back in sync with disk (original contents). Never raises."""
        res.settled = True
        for rel, orig in originals.items():
            try:
                if orig is None:
                    await self._close(rel)
                else:
                    await self._open_or_set(rel, orig)
            except Exception:
                log.exception("restore failed for %s", rel)

    async def settle(self, res: LspBatchResult, accepted: bool, edits: list[ResolvedEdit]) -> None:
        """Call from the verifier's `finally`. Rejected -> restore originals. Accepted -> keep overlay
        (matches disk after commit) and drop cached baselines that may now be stale."""
        if res.settled:
            return
        res.settled = True
        if accepted:
            self._baseline_cache.clear()
            return
        await self._restore(res, {e.rel_path: e.old_content for e in edits})

    async def sync_documents(self, texts: dict[str, str | None], wait: bool = False) -> None:
        """Re-sync the server with disk (text, or None = file does not exist -> close).
        wait=True blocks (bounded by timeout_sec) until the server has re-analysed the synced documents;
        used by the measurement script, which never commits, to leave the server idle between cases."""
        async with self._lock:
            self._baseline_cache.clear()
            targets: dict[str, int] = {}
            seq0 = self._seq
            for rel, text in texts.items():
                try:
                    if text is None:
                        await self._close(rel)
                    else:
                        targets[self.uri_of(rel)] = await self._open_or_set(rel, text)
                except Exception:
                    log.exception("sync failed for %s", rel)
            if wait and targets:
                try:
                    await self._wait_for(targets, seq0, time.monotonic() + self.cfg.timeout_sec)
                except LspDead:
                    pass

    # introspection for tests
    def doc_text(self, rel: str) -> str | None:
        st = self._docs.get(self.uri_of(rel))
        return st.text if st and st.open else None

    @property
    def restarts(self) -> int:
        return self._restarts