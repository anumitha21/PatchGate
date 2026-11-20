"""The ONLY module allowed to write user files. Journaled multi-file commit with rollback.

Guarantees: atomic per file (temp file + os.replace in the same directory); atomic per batch via
journal + rollback; NOT safe against power loss (fsync is best-effort, directory entries are not synced).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from .models import ResolvedEdit

log = logging.getLogger("patchgate.commit")


class CommitError(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _journal_paths(root: Path) -> tuple[Path, Path]:
    return root / ".patchgate" / "journal.json", root / ".patchgate" / "journal"


def _fsync_write(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _replace_with(target: Path, data: bytes, mode_from: Path | None) -> None:
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.pgtmp")
    try:
        _fsync_write(tmp, data)
        if mode_from is not None and mode_from.exists():
            shutil.copymode(mode_from, tmp)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def commit_batch(root: Path, edits: list[ResolvedEdit]) -> list[str]:
    """Write all edits or none. Returns the rel_paths written."""
    root = Path(root).resolve()
    jfile, jdir_base = _journal_paths(root)
    jid = uuid.uuid4().hex[:12]
    jdir = jdir_base / jid
    jdir.mkdir(parents=True, exist_ok=True)

    entries = []
    originals: dict[str, bytes | None] = {}
    for i, e in enumerate(edits):
        had = e.path.exists()
        data = e.path.read_bytes() if had else None
        originals[e.rel_path] = data
        entry = {"rel_path": e.rel_path, "had_original": had,
                 "original_sha256": _sha(data) if data is not None else None,
                 "backup": f"{i}.orig" if had else None}
        if had:
            _fsync_write(jdir / entry["backup"], data)
        entries.append(entry)
    _fsync_write(jfile, json.dumps({"id": jid, "entries": entries}).encode("utf-8"))

    replaced: list[ResolvedEdit] = []
    created_dirs: list[Path] = []
    try:
        for e in edits:
            parent = e.path.parent
            missing = []
            p = parent
            while not p.exists():
                missing.append(p)
                p = p.parent
            parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(missing)
            _replace_with(e.path, e.new_content.encode("utf-8"), e.path if originals[e.rel_path] is not None else None)
            replaced.append(e)
    except BaseException as exc:
        log.error("commit failed on %s; rolling back %d file(s): %r",
                  edits[len(replaced)].rel_path if len(replaced) < len(edits) else "?", len(replaced), exc)
        _rollback(replaced, originals, created_dirs)
        _cleanup_journal(root, jid)
        raise CommitError(f"commit failed, nothing was changed: {exc!r}") from exc
    _cleanup_journal(root, jid)
    return [e.rel_path for e in edits]


def _rollback(replaced: list[ResolvedEdit], originals: dict[str, bytes | None], created_dirs: list[Path]) -> None:
    for e in reversed(replaced):
        orig = originals[e.rel_path]
        try:
            if orig is None:
                e.path.unlink(missing_ok=True)
            else:
                _replace_with(e.path, orig, e.path)
        except Exception:
            log.exception("rollback failed for %s", e.rel_path)
    for d in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def _cleanup_journal(root: Path, jid: str) -> None:
    jfile, jdir_base = _journal_paths(root)
    try:
        jfile.unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(jdir_base / jid, ignore_errors=True)


def recover(root: Path) -> list[str]:
    """Server-startup crash recovery: restore originals from a leftover journal. Returns recovered paths."""
    root = Path(root).resolve()
    jfile, jdir_base = _journal_paths(root)
    if not jfile.exists():
        return []
    recovered: list[str] = []
    try:
        j = json.loads(jfile.read_text(encoding="utf-8"))
        jdir = jdir_base / j["id"]
        for ent in j["entries"]:
            target = root / ent["rel_path"]
            if ent["had_original"]:
                data = (jdir / ent["backup"]).read_bytes()
                if not target.exists() or _sha(target.read_bytes()) != ent["original_sha256"]:
                    _replace_with(target, data, target if target.exists() else None)
                    recovered.append(ent["rel_path"])
            elif target.exists():
                target.unlink()
                recovered.append(ent["rel_path"])
        shutil.rmtree(jdir, ignore_errors=True)
        jfile.unlink(missing_ok=True)
        log.warning("recovered interrupted commit; restored: %s", recovered)
    except Exception:
        log.exception("journal recovery failed; leaving journal in place for manual inspection")
        raise
    return recovered
