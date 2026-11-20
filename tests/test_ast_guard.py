from pathlib import Path

import pytest

from patchgate.core.ast_guard import AstGuard
from patchgate.core.config import Config
from patchgate.core.models import ResolvedEdit
from patchgate.core.policy import Policy


def guard(tmp_path, **ast_over):
    cfg = Config(ast_rules={**Config().ast_rules.model_dump(), **ast_over})
    return AstGuard(Policy(tmp_path, cfg), cfg)


def check(tmp_path, old, new, rel="tests/test_x.py", **over):
    e = ResolvedEdit(path=Path("/x") / rel, rel_path=rel, old_content=old, new_content=new)
    return guard(tmp_path, **over).check_batch([e])


def rules(vs):
    return sorted(v.rule for v in vs)


BASE = "def test_a():\n    assert f() == 2\n    assert g()\n"


# ----- syntax_error
def test_syntax_error_new_vs_preexisting(tmp_path):
    assert rules(check(tmp_path, "x = 1\n", "x = (\n", rel="a.py")) == ["syntax_error"]
    assert check(tmp_path, "x = (\n", "x = (\ny = 2\n", rel="a.py") == []  # already broken before
    assert check(tmp_path, "x = 1\n", "x = 2\n", rel="a.py") == []
    assert rules(check(tmp_path, None, "def f(:\n", rel="new.py")) == ["syntax_error"]  # any error in new file


def test_syntax_rule_toggle(tmp_path):
    assert check(tmp_path, "x = 1\n", "x = (\n", rel="a.py", block_syntax_errors=False) == []


def test_non_py_files_skip_syntax(tmp_path):
    assert check(tmp_path, "a\n", "def f(:\n", rel="notes.txt") == []


# ----- deleted_test_function
def test_deleted_test_function_plain_and_in_class(tmp_path):
    assert rules(check(tmp_path, BASE, "x = 1\n")) == ["deleted_test_function"]
    old = "class TestK:\n    def test_m(self):\n        assert 1 == f()\n    def helper(self):\n        pass\n"
    new = "class TestK:\n    def helper(self):\n        pass\n"
    vs = check(tmp_path, old, new)
    assert rules(vs) == ["deleted_test_function"] and "TestK.test_m" in vs[0].detail


def test_deleted_test_function_negative(tmp_path):
    assert check(tmp_path, BASE, BASE + "def test_b():\n    assert h() == 1\n") == []
    assert check(tmp_path, "def helper():\n    pass\n", "x = 1\n") == []  # not a test function


def test_rename_counts_as_deletion(tmp_path):
    assert "deleted_test_function" in rules(check(tmp_path, BASE, BASE.replace("test_a", "test_renamed")))


# ----- deleted_assert
def test_deleted_assert(tmp_path):
    new = "def test_a():\n    assert f() == 2\n"
    vs = check(tmp_path, BASE, new)
    assert rules(vs) == ["deleted_assert"] and "2 -> 1" in vs[0].detail


def test_deleted_assert_negative_same_or_more(tmp_path):
    assert check(tmp_path, BASE, BASE.replace("g()", "g() == 3")) == []
    assert check(tmp_path, BASE, BASE + "    assert k()\n") == []


def test_deleted_assert_in_method_of_class(tmp_path):
    old = "class T:\n    def test_m(self):\n        assert a()\n        assert b()\n"
    new = "class T:\n    def test_m(self):\n        assert a()\n"
    assert rules(check(tmp_path, old, new)) == ["deleted_assert"]


# ----- trivial_assert
@pytest.mark.parametrize("cond", ["True", "1", "42", "(True)", "1 == 1", "'a' == 'a'", "2 >= 2"])
def test_trivial_assert_positive(tmp_path, cond):
    new = BASE + f"def test_b():\n    assert {cond}\n"
    assert rules(check(tmp_path, BASE, new)) == ["trivial_assert"]


@pytest.mark.parametrize("cond", ["0", "False", "x == 1", "1 == 2", "f() == f()", "1 != 1", "not False", "a is not None"])
def test_trivial_assert_negative(tmp_path, cond):
    new = BASE + f"def test_b():\n    assert {cond}\n"
    assert "trivial_assert" not in rules(check(tmp_path, BASE, new))


def test_preexisting_trivial_assert_not_blamed(tmp_path):
    old = BASE + "def test_b():\n    assert True\n"
    assert check(tmp_path, old, old + "# comment\n") == []


# ----- test_skip_decorator
@pytest.mark.parametrize("deco", [
    "@pytest.mark.skip", "@pytest.mark.skip(reason='x')", "@pytest.mark.skipif(True, reason='x')",
    "@pytest.mark.xfail", "@unittest.skip('x')", "@unittest.skipIf(True, 'x')",
    "@unittest.skipUnless(False, 'x')", "@unittest.expectedFailure"])
def test_skip_decorators_positive(tmp_path, deco):
    new = BASE.replace("def test_a", f"{deco}\ndef test_a")
    assert rules(check(tmp_path, BASE, new)) == ["test_skip_decorator"]


def test_skip_decorator_negative(tmp_path):
    new = BASE.replace("def test_a", "@pytest.mark.parametrize('x', [1])\ndef test_a")
    assert check(tmp_path, BASE, new) == []
    old = "@pytest.mark.skip\n" + BASE
    assert check(tmp_path, old, old + "# touched\n") == []  # pre-existing skip not blamed


def test_test_rules_only_apply_to_test_globs(tmp_path):
    assert check(tmp_path, BASE, "x = 1\n", rel="src/mod.py") == []
    assert check(tmp_path, "x\n", "@pytest.mark.skip\ndef f(): pass\n", rel="src/mod.py") == []


def test_toggle_each_test_rule(tmp_path):
    assert check(tmp_path, BASE, "x = 1\n", block_deleted_test_functions=False) == []
    new = "def test_a():\n    assert f() == 2\n"
    assert check(tmp_path, BASE, new, block_deleted_asserts=False) == []
    assert check(tmp_path, BASE, BASE + "def test_b():\n    assert True\n", block_trivial_asserts=False) == []
    assert check(tmp_path, BASE, "@pytest.mark.skip\n" + BASE, block_test_skip_decorators=False) == []


# ----- secrets
SECRETS = {
    "private key": "-----BEGIN RSA PRIVATE KEY-----",
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github": "ghp_" + "a" * 36,
    "openai-ish": "sk-" + "b" * 24,
}


@pytest.mark.parametrize("name", SECRETS)
def test_secret_added_all_file_types(tmp_path, name):
    secret = SECRETS[name]
    for rel in ("src/a.py", "config.txt"):
        vs = check(tmp_path, "x = 1\n", f"x = 1\nkey = '{secret}'\n", rel=rel)
        assert rules(vs) == ["secret_added"], rel
        assert vs[0].detail.startswith("line 2")


def test_secret_not_echoed(tmp_path):
    secret = SECRETS["github"]
    vs = check(tmp_path, None, f"token = '{secret}'\n", rel="src/a.py")
    blob = " ".join(v.model_dump_json() for v in vs)
    assert secret not in blob and secret[:10] not in blob


def test_preexisting_secret_line_not_blamed_but_changed_line_is(tmp_path):
    s = SECRETS["aws"]
    old = f"k = '{s}'\n"
    assert check(tmp_path, old, old + "y = 2\n", rel="a.py") == []
    assert rules(check(tmp_path, old, f"k = '{s}'  # edited\n", rel="a.py")) == ["secret_added"]


def test_secret_toggle_and_negative(tmp_path):
    assert check(tmp_path, "a\n", f"k = '{SECRETS['aws']}'\n", rel="a.py", block_secrets=False) == []
    assert check(tmp_path, "a\n", "k = 'AKIA-short'\n", rel="a.py") == []


def test_invalid_secret_regex_is_error(tmp_path):
    cfg = Config(secret_regexes=["("])
    with pytest.raises(ValueError):
        AstGuard(Policy(tmp_path, cfg), cfg)
