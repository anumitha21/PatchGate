from pathlib import Path

from patchgate.core.import_graph import ImportGraph, imported_modules, module_names_for


def w(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


IGN = [".git", ".venv", "node_modules", "__pycache__", ".patchgate"]


def test_module_names():
    assert module_names_for("pkg/mod.py", False) == ["pkg.mod"]
    assert module_names_for("pkg/__init__.py", False) == ["pkg"]
    assert module_names_for("src/pkg/mod.py", True) == ["src.pkg.mod", "pkg.mod"]
    assert module_names_for("README.md", False) == []


def test_imported_modules_relative_and_absolute():
    own = ["pkg.sub.a"]
    got = imported_modules("import os\nfrom . import b\nfrom .c import d\nfrom ..top import x\nimport pkg.z\n", own, False)
    assert {"os", "pkg.sub.b", "pkg.sub.c", "pkg.sub.c.d", "pkg.top", "pkg.top.x", "pkg.z"} <= got


def test_dependents_basic_and_exclusions(tmp_path):
    w(tmp_path, "lib.py", "def f(a): ...\n")
    w(tmp_path, "user.py", "from lib import f\n")
    w(tmp_path, "other.py", "import lib\n")
    w(tmp_path, "unrelated.py", "import os\n")
    w(tmp_path, ".venv/x.py", "import lib\n")
    g = ImportGraph(tmp_path, IGN)
    assert g.dependents_of(["lib.py"], 1, 50) == ["other.py", "user.py"]
    assert g.dependents_of(["lib.py", "user.py"], 1, 50) == ["other.py"]  # edited files excluded
    assert g.dependents_of(["lib.py"], 1, 1) == ["other.py"]  # limit


def test_src_layout_and_relative_imports(tmp_path):
    w(tmp_path, "src/pkg/__init__.py", "")
    w(tmp_path, "src/pkg/core.py", "X = 1\n")
    w(tmp_path, "src/pkg/use.py", "from .core import X\n")
    w(tmp_path, "src/pkg/use2.py", "from pkg.core import X\n")
    g = ImportGraph(tmp_path, IGN)
    assert g.dependents_of(["src/pkg/core.py"], 1, 50) == ["src/pkg/use.py", "src/pkg/use2.py"]


def test_depth_two_closest_first(tmp_path):
    w(tmp_path, "a.py", "")
    w(tmp_path, "b.py", "import a\n")
    w(tmp_path, "c.py", "import b\n")
    g = ImportGraph(tmp_path, IGN)
    assert g.dependents_of(["a.py"], 1, 50) == ["b.py"]
    assert g.dependents_of(["a.py"], 2, 50) == ["b.py", "c.py"]


def test_unparsable_skipped_and_invalidate(tmp_path):
    w(tmp_path, "lib.py", "")
    w(tmp_path, "bad.py", "def (:\n")
    w(tmp_path, "user.py", "import os\n")
    g = ImportGraph(tmp_path, IGN)
    assert g.dependents_of(["lib.py"], 1, 50) == []
    w(tmp_path, "user.py", "import lib\n")
    g.invalidate(["user.py"])
    assert g.dependents_of(["lib.py"], 1, 50) == ["user.py"]
    (tmp_path / "user.py").unlink()
    g.invalidate(["user.py"])
    assert g.dependents_of(["lib.py"], 1, 50) == []


def test_new_file_edit_still_finds_importers(tmp_path):
    w(tmp_path, "user.py", "import newmod\n")
    g = ImportGraph(tmp_path, IGN)
    assert g.dependents_of(["newmod.py"], 1, 50) == ["user.py"]
