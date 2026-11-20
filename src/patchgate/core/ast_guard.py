"""Structural anti-tamper checks on the syntax tree (tree-sitter) plus a secret scan.

What these rules CANNOT catch (documented limitation): hardcoded expected outputs / special-casing
test inputs in source code, weakened-but-nontrivial assertions (e.g. `assert x is not None`),
replacing real collaborators with mocks, `pytest.skip()` calls inside a test body, and skip markers
applied through `pytestmark` or imported aliases (`from pytest import mark`).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser, Tree

from .config import Config
from .models import PolicyViolation, ResolvedEdit
from .policy import Policy

_PY = Language(tspython.language())
_parser = Parser(_PY)

SKIP_DECORATOR_RE = re.compile(
    r"^@\s*(pytest\.mark\.(skip|skipif|xfail)|unittest\.(skip|skipIf|skipUnless|expectedFailure))\b")
_LITERALS = {"integer", "float", "string", "true", "false", "none"}


def parse(code: str) -> Tree:
    return _parser.parse(code.encode("utf-8"))


def walk(root: Node):
    """Iterative pre-order traversal (explicit stack, no recursion)."""
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _text(n: Node) -> str:
    return n.text.decode("utf-8") if n.text is not None else ""


def _norm(s: str) -> str:
    return " ".join(s.split())


@dataclass
class TestFn:
    qualname: str
    asserts: int


def collect_test_functions(root: Node) -> dict[str, TestFn]:
    """Functions whose own name starts with `test`, qualified through enclosing classes only."""
    found: dict[str, TestFn] = {}
    stack: list[tuple[Node, str]] = [(root, "")]
    while stack:
        node, prefix = stack.pop()
        if node.type == "class_definition":
            name = node.child_by_field_name("name")
            new_prefix = prefix + (_text(name) if name else "?") + "."
            for c in reversed(node.children):
                stack.append((c, new_prefix))
        elif node.type == "function_definition":
            name_n = node.child_by_field_name("name")
            name = _text(name_n) if name_n else ""
            if name.startswith("test"):
                n_assert = sum(1 for d in walk(node) if d.type == "assert_statement")
                found[prefix + name] = TestFn(prefix + name, n_assert)
            # nested defs inside functions are not separate test functions
        else:
            for c in reversed(node.children):
                stack.append((c, prefix))
    return found


def _condition(assert_node: Node) -> Node | None:
    named = [c for c in assert_node.children if c.is_named]
    return named[0] if named else None


def is_trivial_assert(assert_node: Node) -> bool:
    """Trivial = condition is literally `True`, a NON-ZERO int literal, or a comparison of exactly two
    identical literals using ==, <=, >= or `is` (e.g. `1 == 1`, `'a' == 'a'`). Parentheses are unwrapped.
    Nothing else is flagged (no attempt to be clever)."""
    cond = _condition(assert_node)
    while cond is not None and cond.type == "parenthesized_expression":
        inner = [c for c in cond.children if c.is_named]
        cond = inner[0] if len(inner) == 1 else None
    if cond is None:
        return False
    if cond.type == "true":
        return True
    if cond.type == "integer":
        try:
            return int(_text(cond).replace("_", ""), 0) != 0
        except ValueError:
            return False
    if cond.type == "comparison_operator":
        operands = [c for c in cond.children if c.is_named]
        ops = [_text(c) for c in cond.children if not c.is_named]
        if len(operands) == 2 and len(ops) == 1 and ops[0] in ("==", "<=", ">=", "is"):
            a, b = operands
            return a.type in _LITERALS and b.type in _LITERALS and a.type == b.type and _text(a) == _text(b)
    return False


def trivial_asserts(root: Node) -> list[tuple[int, str]]:
    return [(n.start_point[0] + 1, _norm(_text(n))) for n in walk(root)
            if n.type == "assert_statement" and is_trivial_assert(n)]


def skip_decorators(root: Node) -> list[tuple[int, str]]:
    return [(n.start_point[0] + 1, _norm(_text(n))) for n in walk(root)
            if n.type == "decorator" and SKIP_DECORATOR_RE.match(_norm(_text(n)))]


class AstGuard:
    def __init__(self, policy: Policy, config: Config):
        self.policy = policy
        self.cfg = config
        try:
            self._secret_res = [re.compile(r) for r in config.secret_regexes]
        except re.error as e:
            raise ValueError(f"invalid secret_regexes entry: {e}") from e

    # ---- individual rules ------------------------------------------------
    def _syntax(self, e: ResolvedEdit, old: Tree | None, new: Tree) -> list[PolicyViolation]:
        if not new.root_node.has_error:
            return []
        if old is not None and old.root_node.has_error:
            return []  # pre-existing syntax errors are not blamed on this edit
        err = next((n for n in walk(new.root_node) if n.is_error or n.is_missing), None)
        line = (err.start_point[0] + 1) if err else 1
        return [PolicyViolation(rule="syntax_error", rel_path=e.rel_path,
                                detail=f"syntax error introduced near line {line}")]

    def _tests(self, e: ResolvedEdit, old: Tree | None, new: Tree) -> list[PolicyViolation]:
        r = self.cfg.ast_rules
        v: list[PolicyViolation] = []
        new_fns = collect_test_functions(new.root_node)
        if old is not None:
            old_fns = collect_test_functions(old.root_node)
            if r.block_deleted_test_functions:
                for q in sorted(set(old_fns) - set(new_fns)):
                    v.append(PolicyViolation(rule="deleted_test_function", rel_path=e.rel_path,
                                             detail=f"test function {q} was removed"))
            if r.block_deleted_asserts:
                for q in sorted(set(old_fns) & set(new_fns)):
                    if new_fns[q].asserts < old_fns[q].asserts:
                        v.append(PolicyViolation(
                            rule="deleted_assert", rel_path=e.rel_path,
                            detail=f"{q}: assert count decreased {old_fns[q].asserts} -> {new_fns[q].asserts}"))
        if r.block_trivial_asserts:
            old_c = Counter(t for _, t in trivial_asserts(old.root_node)) if old else Counter()
            for line, text in trivial_asserts(new.root_node):
                if old_c[text] > 0:
                    old_c[text] -= 1
                    continue
                v.append(PolicyViolation(rule="trivial_assert", rel_path=e.rel_path,
                                         detail=f"line {line}: trivial assert added ({text})"))
        if r.block_test_skip_decorators:
            old_c = Counter(t for _, t in skip_decorators(old.root_node)) if old else Counter()
            for line, text in skip_decorators(new.root_node):
                if old_c[text] > 0:
                    old_c[text] -= 1
                    continue
                v.append(PolicyViolation(rule="test_skip_decorator", rel_path=e.rel_path,
                                         detail=f"line {line}: skip/xfail decorator added ({text})"))
        return v

    def _secrets(self, e: ResolvedEdit) -> list[PolicyViolation]:
        old_lines = set((e.old_content or "").split("\n"))
        v: list[PolicyViolation] = []
        for i, line in enumerate(e.new_content.split("\n"), 1):
            if line in old_lines:
                continue  # only ADDED lines
            for k, rx in enumerate(self._secret_res):
                if rx.search(line):
                    # never echo the match: rule, file, line number and pattern index only
                    v.append(PolicyViolation(rule="secret_added", rel_path=e.rel_path,
                                             detail=f"line {i}: text matches secret pattern #{k}"))
                    break
        return v

    # ---- entry point -----------------------------------------------------
    def check_batch(self, edits: list[ResolvedEdit]) -> list[PolicyViolation]:
        v: list[PolicyViolation] = []
        for e in edits:
            is_py = e.rel_path.endswith(".py")
            if is_py:
                new = parse(e.new_content)
                old = parse(e.old_content) if e.old_content is not None else None
                if self.cfg.ast_rules.block_syntax_errors:
                    v += self._syntax(e, old, new)
                if self.policy.is_test_file(e.rel_path):
                    v += self._tests(e, old, new)
            if self.cfg.ast_rules.block_secrets:
                v += self._secrets(e)
        return v
