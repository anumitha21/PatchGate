import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from patchgate.core.config import Config, load_config
from patchgate.core.models import FileEdit, ResolvedEdit
from patchgate.core.policy import PathError, Policy

REPO_YAML = Path(__file__).resolve().parents[1] / "harness.yaml"


def mk(root: Path, **over) -> Policy:
    return Policy(root, Config(**over))


def re_(rel, new="x = 1\n", old=None):
    return ResolvedEdit(path=Path("/x") / rel, rel_path=rel, old_content=old, new_content=new)


def test_example_harness_yaml_loads():
    cfg = load_config(REPO_YAML)
    assert cfg.circuit_breaker.max_consecutive_failures_per_file == 4
    assert cfg.lsp.on_timeout == "allow_with_warning"


def test_unknown_key_is_error(tmp_path):
    f = tmp_path / "h.yaml"
    f.write_text("version: 1\nwrite_protectd: ['x']\n")
    with pytest.raises(ValidationError):
        load_config(f)


def test_unknown_nested_key_is_error(tmp_path):
    f = tmp_path / "h.yaml"
    f.write_text("lsp:\n  timeout_secs: 3\n")
    with pytest.raises(ValidationError):
        load_config(f)


def test_missing_config_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == Config()


def test_fileedit_requires_exactly_one_mode():
    with pytest.raises(ValidationError):
        FileEdit(path="a.py")
    with pytest.raises(ValidationError):
        FileEdit(path="a.py", new_content="x", replace={"old_text": "a", "new_text": "b"})
    FileEdit(path="a.py", new_content="x")
    FileEdit(path="a.py", replace={"old_text": "a", "new_text": "b"})


@pytest.mark.parametrize("rel,expected", [
    ("tests/a/b.py", True), ("test_x.py", True), ("pkg/test_x.py", True), ("pkg/x_test.py", True),
    ("pkg/conftest.py", True), (".github/w/ci.yml", True), ("harness.yaml", True),
    ("pyproject.toml", True), ("pkg/x.py", False), ("src/pkg/tests/y.py", False),
])
def test_write_protected_globs(tmp_path, rel, expected):
    assert mk(tmp_path).is_write_protected(rel) is expected


@pytest.mark.parametrize("rel,denied", [
    (".env", True), (".env.local", True), ("a/b/key.pem", True), ("deep/x/secrets.json", True),
    ("pkg/x.py", False),
])
def test_read_denied(tmp_path, rel, denied):
    p = mk(tmp_path)
    if denied:
        with pytest.raises(PathError):
            p.check_read(rel)
    else:
        assert p.check_read(rel)[1] == rel


def test_traversal_absolute_and_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    has_symlink = True
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        has_symlink = False
    p = mk(root)
    bad_list = ["../x", "a/../../x", str(tmp_path / "abs.py"), "/etc/passwd", ""]
    if has_symlink:
        bad_list.append("link/evil.py")
    for bad in bad_list:
        with pytest.raises(PathError):
            p.resolve(bad)
    assert p.resolve("ok/new.py")[1] == "ok/new.py"


def test_batch_violations(tmp_path):
    p = mk(tmp_path, limits={"max_file_bytes": 10, "max_batch_files": 2})
    rules = lambda es: {v.rule for v in p.check_batch(es)}
    assert rules([re_("tests/test_a.py")]) == {"write_protected"}
    assert rules([re_("a.py"), re_("a.py")]) == {"duplicate_path"}
    assert "file_too_large" in rules([re_("a.py", new="y" * 11)])
    assert "batch_too_large" in rules([re_("a.py"), re_("b.py"), re_("c.py")])
    assert rules([re_(".venv/lib/x.py")]) == {"ignored_dir"}
    assert rules([re_("new_ok.py")]) == set()
