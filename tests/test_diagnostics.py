from patchgate.core.diagnostics import diff_file


def d(line, msg="x is not defined", code="reportUndefinedVariable", sev=1):
    return {"range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 1}},
            "severity": sev, "code": code, "message": msg}


BASE = "import os\n\ndef f():\n    return x\n\nprint(1)\n"


def run(base_raw, base_txt, post_raw, post_txt, **kw):
    return diff_file("a.py", base_raw, base_txt, post_raw, post_txt, ["error"], **kw)


def test_line_shift_invariance():
    post = "# header\n# header2\n" + BASE
    blocking, warns = run([d(3)], BASE, [d(5)], post)
    assert blocking == [] and warns == []


def test_net_new_error_blocks():
    post = BASE + "y\n"
    blocking, _ = run([d(3)], BASE, [d(3), d(6, "y is not defined")], post)
    assert len(blocking) == 1 and blocking[0].message == "y is not defined"


def test_multiset_two_baseline_three_after_one_net_new():
    # identical context windows around each diagnostic -> identical strict signatures
    base = "\nx\n\n\nx\n\n"
    post = "\nx\n\n\nx\n\n\nx\n\n"
    blocking, _ = run([d(1), d(4)], base, [d(1), d(4), d(7)], post)
    assert len(blocking) == 1
    blocking, _ = run([d(1), d(4)], base, [d(1), d(4), d(7)], post, loose_fallback=False)
    assert len(blocking) == 1


def test_loose_fallback_absorbs_neighbor_change():
    base = "a = 1\nx\nb = 2\n"
    post = "a = 99\nx\nb = 2\n"  # neighbor above changed -> strict context hash differs
    blocking, _ = run([d(1)], base, [d(1)], post)
    assert blocking == []
    blocking, _ = run([d(1)], base, [d(1)], post, loose_fallback=False)
    assert len(blocking) == 1


def test_loose_does_not_double_match_one_baseline_diag():
    base = "x\n"
    post = "x\nx\n"
    blocking, _ = run([d(0)], base, [d(0), d(1)], post)
    assert len(blocking) == 1


def test_warnings_do_not_block():
    blocking, warns = run([], "a\n", [d(0, "unused", "reportUnusedVariable", sev=2)], "a\n")
    assert blocking == [] and len(warns) == 1


def test_out_of_range_line_is_guarded():
    blocking, _ = run([], "", [d(50)], "")
    assert len(blocking) == 1
