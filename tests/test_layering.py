"""Mechanical checks for the spec's hard constraints (section 4.1, 6)."""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "patchgate"
CORE = sorted((SRC / "core").glob("*.py"))
ALLOWED_CORE_THIRD_PARTY = {"pydantic", "yaml", "pathspec", "tree_sitter", "tree_sitter_python"}
STDLIB = set(__import__("sys").stdlib_module_names)


def imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module.split(".")[0])
    return out


def test_core_depends_only_on_stdlib_and_allowed_packages():
    for f in CORE:
        bad = {m for m in imports(f) if m not in STDLIB and m not in ALLOWED_CORE_THIRD_PARTY and m != "patchgate"}
        assert not bad, (f.name, bad)
        txt = f.read_text()
        assert "from ..adapters" not in txt and "from ..hooks" not in txt and "import mcp" not in txt


def test_only_adapter_imports_mcp_sdk():
    for f in SRC.rglob("*.py"):
        uses = "mcp" in imports(f)
        assert uses == (f.name == "mcp_server.py"), f


def test_only_commit_module_writes_user_files():
    # crude but effective: other core modules must not call write primitives on repo paths
    pat = re.compile(r"\.write_text\(|\.write_bytes\(|os\.replace\(|shutil\.copy|\.unlink\(|open\([^)]*['\"][wa]b?['\"]")
    for f in CORE:
        if f.name in ("commit.py", "telemetry.py"):  # telemetry writes only under .patchgate/
            continue
        assert not pat.search(f.read_text()), f.name


def test_core_and_adapter_are_editor_agnostic():
    pat = re.compile(r"claude|cursor|vs ?code|antigravity|MultiEdit|NotebookEdit|PreToolUse", re.IGNORECASE)
    for f in CORE + [SRC / "adapters" / "mcp_server.py"]:
        hits = pat.findall(f.read_text())
        assert not hits, (f.name, hits)


def test_no_print_in_mcp_process_modules():
    for f in CORE + [SRC / "adapters" / "mcp_server.py"]:
        assert not re.search(r"(?<![\w.])print\(", f.read_text()), f.name


def test_no_hardcoded_latency_promises_anywhere():
    root = SRC.parents[1]
    for f in list(SRC.rglob("*.py")) + list((root / "docs").glob("*.md")) + [root / "README.md"]:
        if f.exists():
            assert not re.search(r"\b\d+\s?ms\b.*\b(guarantee|promise|always|under)\b|\b(<|under)\s*\d+\s?ms\b",
                                 f.read_text(), re.IGNORECASE), f.name
