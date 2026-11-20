"""Path safety, read/write ACLs and batch-level policy checks."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pathspec

from .config import Config
from .models import PolicyViolation, ResolvedEdit


class PathError(ValueError):
    pass


def _spec(patterns: list[str]) -> pathspec.PathSpec:
    # "gitignore" factory: "gitwildmatch" is deprecated in pathspec 1.x (see DECISIONS D2).
    return pathspec.PathSpec.from_lines("gitignore", patterns)


class Policy:
    def __init__(self, repo_root: Path, config: Config):
        self.root = Path(repo_root).resolve()
        self.cfg = config
        self._read_denied = _spec(config.read_denied)
        self._write_protected = _spec(config.write_protected)
        self._tests = _spec(config.test_globs)

    # ---- path safety -------------------------------------------------
    def resolve(self, rel_path: str) -> tuple[Path, str]:
        """Return (absolute path, normalized POSIX rel path) or raise PathError."""
        if not rel_path or "\x00" in rel_path:
            raise PathError("empty or invalid path")
        p = PurePosixPath(rel_path.replace("\\", "/"))
        if p.is_absolute() or Path(rel_path).is_absolute():
            raise PathError(f"absolute paths are not allowed: {rel_path!r}")
        if ".." in p.parts:
            raise PathError(f"'..' traversal is not allowed: {rel_path!r}")
        abs_path = (self.root / Path(*p.parts)).resolve()
        if not abs_path.is_relative_to(self.root):
            raise PathError(f"path escapes repo root (symlink or traversal): {rel_path!r}")
        rel = abs_path.relative_to(self.root).as_posix()
        if rel == ".":
            raise PathError("path refers to the repo root")
        return abs_path, rel

    def in_ignored_dir(self, rel: str) -> bool:
        parts = set(PurePosixPath(rel).parts[:-1]) | {PurePosixPath(rel).parts[0]}
        return any(d in parts for d in self.cfg.ignore_dirs)

    # ---- classification ----------------------------------------------
    def is_write_protected(self, rel: str) -> bool:
        return self._write_protected.match_file(rel)

    def is_test_file(self, rel: str) -> bool:
        return self._tests.match_file(rel)

    def check_read(self, rel_path: str) -> tuple[Path, str]:
        """Resolve and enforce read_denied. Raises PathError."""
        abs_path, rel = self.resolve(rel_path)
        if self._read_denied.match_file(rel):
            raise PathError(f"reading {rel!r} is denied by policy (read_denied)")
        return abs_path, rel

    # ---- batch check -------------------------------------------------
    def check_batch(self, edits: list[ResolvedEdit]) -> list[PolicyViolation]:
        v: list[PolicyViolation] = []
        lim = self.cfg.limits
        if len(edits) > lim.max_batch_files:
            v.append(PolicyViolation(
                rule="batch_too_large", rel_path="*",
                detail=f"{len(edits)} files > max_batch_files={lim.max_batch_files}"))
        seen: set[str] = set()
        for e in edits:
            if e.rel_path in seen:
                v.append(PolicyViolation(rule="duplicate_path", rel_path=e.rel_path,
                                         detail="path appears more than once in the batch"))
            seen.add(e.rel_path)
            if self.is_write_protected(e.rel_path):
                v.append(PolicyViolation(rule="write_protected", rel_path=e.rel_path,
                                         detail="path is write-protected; a human must edit it directly"))
            if self.in_ignored_dir(e.rel_path):
                v.append(PolicyViolation(rule="ignored_dir", rel_path=e.rel_path,
                                         detail="writes into ignored directories are not allowed"))
            size = len(e.new_content.encode("utf-8"))
            if size > lim.max_file_bytes:
                v.append(PolicyViolation(rule="file_too_large", rel_path=e.rel_path,
                                         detail=f"{size} bytes > max_file_bytes={lim.max_file_bytes}"))
        return v
