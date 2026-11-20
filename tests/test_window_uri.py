"""Regression: on Windows, urlparse(uri).path is '/D:/dev/...'; the old Path(...) approach never matched,
so every publishDiagnostics was ignored and checks waited out the full timeout."""
from pathlib import PureWindowsPath

from patchgate.core.lsp_client import rel_from_uri


def test_posix_uri_roundtrip(tmp_path):
    root = tmp_path.resolve()
    assert rel_from_uri((root / "a" / "b c.py").as_uri(), root) == "a/b c.py"
    assert rel_from_uri("file:///elsewhere/x.py", root) is None
    assert rel_from_uri("untitled:Untitled-1", root) is None


def test_percent_in_filename_not_double_decoded(tmp_path):
    root = tmp_path.resolve()
    assert rel_from_uri((root / "100%25.py").as_uri(), root) == "100%25.py"


def test_windows_uri_forms():
    root = PureWindowsPath("D:/dev/PatchGate/repo")
    for uri in ("file:///D:/dev/PatchGate/repo/src/m.py",
                "file:///d%3A/dev/PatchGate/repo/src/m.py",   # lowercase drive, encoded colon (VS Code style)
                "file:///D:/dev/PatchGate/repo/src/sp%20ace.py"):
        got = rel_from_uri(uri, root)
        assert got is not None and got.startswith("src/") and "\\" not in got, (uri, got)
    assert rel_from_uri("file:///D:/other/m.py", root) is None


def test_windows_drive_case_insensitive_and_exact_values():
    root = PureWindowsPath("D:/dev/PatchGate/repo")
    assert rel_from_uri("file:///d%3A/dev/PatchGate/repo/src/m.py", root) == "src/m.py"
    assert rel_from_uri("file:///D:/dev/PatchGate/repo/src/sp%20ace.py", root) == "src/sp ace.py"
    assert rel_from_uri("file:///D:/dev/PatchGate/repo", root) == "."


def test_canon_uri_makes_server_and_client_spellings_equal():
    from patchgate.core.lsp_client import canon_uri
    ours = "file:///D:/dev/PatchGate/repo/src/m.py"            # Path.as_uri() on Windows
    theirs = "file:///d%3A/dev/PatchGate/repo/src/m.py"        # what Pyright/VS Code-style servers send
    assert canon_uri(ours) == canon_uri(theirs)
    assert canon_uri("file:///D:/a/sp%20ace.py") == canon_uri("file:///d%3A/a/sp%20ace.py")
    assert canon_uri("file:///home/u/A.py") != canon_uri("file:///home/u/a.py")   # POSIX stays case-sensitive
    assert canon_uri("untitled:x") == "untitled:x"