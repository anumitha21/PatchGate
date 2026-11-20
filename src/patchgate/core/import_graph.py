"""Reverse-import index over *.py files (stdlib `ast`), used to find dependents of edited modules.

Assumptions (documented limitation): the repo root and, if it exists, `<root>/src` are the import
roots. Dynamic imports (`importlib`, `__import__`) and `__init__` re-exports are not tracked precisely.
"""
from __future__ import annotations

import ast
import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath

log = logging.getLogger("patchgate.import_graph")


def module_names_for(rel_path: str, has_src: bool) -> list[str]:
    """Dotted module names a repo file can be imported as (one per import root)."""
    p = PurePosixPath(rel_path)
    if p.suffix != ".py":
        return []
    names = []
    roots = [PurePosixPath(".")]
    if has_src:
        roots.append(PurePosixPath("src"))
    for r in roots:
        try:
            sub = p.relative_to(r) if str(r) != "." else p
        except ValueError:
            continue
        parts = list(sub.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return names


def imported_modules(source: str, own_names: list[str], is_package_init: bool) -> set[str]:
    """Module names (absolute dotted) that `source` imports. Raises SyntaxError on bad source."""
    tree = ast.parse(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
                bases = [base] if base else []
            else:
                bases = []
                for own in own_names:
                    pkg = own.split(".")
                    if not is_package_init:
                        pkg = pkg[:-1]
                    up = node.level - 1
                    if up > len(pkg):
                        continue
                    pkg = pkg[: len(pkg) - up] if up else pkg
                    base_parts = pkg + (node.module.split(".") if node.module else [])
                    if base_parts:
                        bases.append(".".join(base_parts))
            for b in bases:
                out.add(b)
                for a in node.names:
                    if a.name != "*":
                        out.add(f"{b}.{a.name}")
    return out


class ImportGraph:
    def __init__(self, repo_root: Path, ignore_dirs: list[str]):
        self.root = Path(repo_root).resolve()
        self.ignore = set(ignore_dirs)
        self.has_src = (self.root / "src").is_dir()
        self._file_imports: dict[str, set[str]] = {}
        self._importers: dict[str, set[str]] = defaultdict(set)  # module name -> importing files
        self._built = False

    # ---- build / maintain
    def _iter_py(self):
        for p in self.root.rglob("*.py"):
            rel = p.relative_to(self.root)
            if any(part in self.ignore for part in rel.parts[:-1]):
                continue
            yield rel.as_posix(), p

    def _index_file(self, rel: str, path: Path) -> None:
        self._drop(rel)
        try:
            src = path.read_bytes().decode("utf-8")
            mods = imported_modules(src, module_names_for(rel, self.has_src), path.name == "__init__.py")
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
            return  # unparsable files are skipped, not fatal
        self._file_imports[rel] = mods
        for m in mods:
            self._importers[m].add(rel)

    def _drop(self, rel: str) -> None:
        for m in self._file_imports.pop(rel, set()):
            s = self._importers.get(m)
            if s:
                s.discard(rel)

    def build(self) -> None:
        self.has_src = (self.root / "src").is_dir()
        self._file_imports.clear()
        self._importers.clear()
        for rel, path in self._iter_py():
            self._index_file(rel, path)
        self._built = True

    def invalidate(self, rel_paths: list[str]) -> None:
        if not self._built:
            return  # lazy: the first query builds from current disk
        for rel in rel_paths:
            path = self.root / rel
            if path.suffix == ".py" and path.is_file():
                self._index_file(rel, path)
            else:
                self._drop(rel)

    # ---- query
    def dependents_of(self, rel_paths: list[str], depth: int = 1, limit: int = 50) -> list[str]:
        """Files that import the given files' modules (closest first), excluding the files themselves."""
        if not self._built:
            self.build()
        edited = set(rel_paths)
        seen: set[str] = set(edited)
        frontier = list(rel_paths)
        out: list[str] = []
        for _ in range(max(depth, 0)):
            nxt: list[str] = []
            for rel in frontier:
                for name in module_names_for(rel, self.has_src):
                    for imp in sorted(self._importers.get(name, ())):
                        if imp not in seen:
                            seen.add(imp)
                            out.append(imp)
                            nxt.append(imp)
                            if len(out) >= limit:
                                return out
            frontier = nxt
        return out
