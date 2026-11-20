"""M3 spike (throwaway, kept for reference): print real tree-sitter-python node types."""
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY = Language(tspython.language())
parser = Parser(PY)

SAMPLE = '''import pytest
import unittest

@pytest.mark.skip(reason="x")
def test_a():
    assert 1 == 1
    assert True
    assert x == 2, "msg"

class TestK(unittest.TestCase):
    @unittest.skip("nope")
    def test_m(self):
        assert self.a

    @pytest.mark.xfail
    async def test_async(self):
        assert 3

    class Inner:
        def test_inner(self):
            assert 5
'''
BROKEN = "def f(:\n    x = (1,\n"


def dump(node, depth=0, maxd=6):
    if depth > maxd:
        return
    txt = node.text.decode()[:40].replace("\n", "\\n") if node.child_count == 0 or node.type in (
        "decorator", "identifier", "assert_statement") else ""
    print("  " * depth + f"{node.type}  [{node.start_point[0]}]" + (f"  {txt!r}" if txt else ""))
    for c in node.children:
        if c.is_named or node.type in ("decorator",):
            dump(c, depth + 1, maxd)


tree = parser.parse(SAMPLE.encode())
print("has_error:", tree.root_node.has_error)
dump(tree.root_node)
print("\n--- broken ---")
t2 = parser.parse(BROKEN.encode())
print("has_error:", t2.root_node.has_error)
dump(t2.root_node, maxd=3)
print("\n--- assert condition shapes ---")
for src in ["assert True", "assert 1", "assert 1 == 1", "assert 'a' == 'a'", "assert x == 1", "assert (True)", "assert 0", "assert not False"]:
    t = parser.parse(src.encode()).root_node
    a = t.children[0]
    print(src, "->", a.type, [(c.type, c.text.decode()) for c in a.children])
print("\nnode attrs:", [n for n in dir(tree.root_node) if not n.startswith("_")][:40])
print("decorated child_by_field_name('definition'):",
      parser.parse(b"@a\ndef f(): pass\n").root_node.children[0].child_by_field_name("definition").type)
