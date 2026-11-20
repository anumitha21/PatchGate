import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from patchgate.core import commit as commit_mod
from patchgate.core.commit import CommitError, commit_batch, recover
from patchgate.core.models import ResolvedEdit


def mk(root: Path, rel: str, new: str) -> ResolvedEdit:
    p = root / rel
    return ResolvedEdit(path=p, rel_path=rel, old_content=p.read_text() if p.exists() else None, new_content=new)


def test_success_multi_file_and_new_file_and_no_journal_left(tmp_path):
    (tmp_path / "a.py").write_text("old a\n")
    edits = [mk(tmp_path, "a.py", "new a\n"), mk(tmp_path, "deep/er/b.py", "new b é\n")]
    assert commit_batch(tmp_path, edits) == ["a.py", "deep/er/b.py"]
    assert (tmp_path / "a.py").read_text() == "new a\n"
    assert (tmp_path / "deep/er/b.py").read_text(encoding="utf-8") == "new b é\n"
    assert not (tmp_path / ".patchgate" / "journal.json").exists()
    assert not list(tmp_path.rglob("*.pgtmp"))


def test_mode_preserved(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX permissions (0o755) not supported on Windows")
    p = tmp_path / "run.sh"
    p.write_text("echo hi\n")
    p.chmod(0o755)
    commit_batch(tmp_path, [mk(tmp_path, "run.sh", "echo bye\n")])
    assert stat.S_IMODE(p.stat().st_mode) == 0o755


def test_new_file_gets_normal_mode(tmp_path):
    commit_batch(tmp_path, [mk(tmp_path, "n.py", "x\n")])
    assert stat.S_IMODE((tmp_path / "n.py").stat().st_mode) & 0o600 == 0o600


def test_mid_batch_failure_rolls_everything_back(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("A0\n")
    (tmp_path / "b.py").write_text("B0\n")
    edits = [mk(tmp_path, "a.py", "A1\n"), mk(tmp_path, "b.py", "B1\n"), mk(tmp_path, "newdir/c.py", "C1\n")]
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the 2nd file
            raise OSError("injected")
        return real(src, dst)
    monkeypatch.setattr(commit_mod.os, "replace", flaky)
    with pytest.raises(CommitError):
        commit_batch(tmp_path, edits)
    monkeypatch.undo()
    assert (tmp_path / "a.py").read_text() == "A0\n"
    assert (tmp_path / "b.py").read_text() == "B0\n"
    assert not (tmp_path / "newdir").exists()
    assert not (tmp_path / ".patchgate" / "journal.json").exists()
    assert not list(tmp_path.rglob("*.pgtmp"))


def test_failure_on_last_file_deletes_created_new_files(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("A0\n")
    edits = [mk(tmp_path, "new1.py", "N\n"), mk(tmp_path, "a.py", "A1\n")]
    real, n = os.replace, {"n": 0}

    def flaky(src, dst):
        n["n"] += 1
        if n["n"] == 2:
            raise OSError("injected")
        return real(src, dst)
    monkeypatch.setattr(commit_mod.os, "replace", flaky)
    with pytest.raises(CommitError):
        commit_batch(tmp_path, edits)
    monkeypatch.undo()
    assert not (tmp_path / "new1.py").exists() and (tmp_path / "a.py").read_text() == "A0\n"


def test_journal_recovery_after_simulated_crash(tmp_path):
    (tmp_path / "a.py").write_text("A0\n")
    # Simulate: journal written, a.py already replaced, new file created, then the process died.
    jdir = tmp_path / ".patchgate" / "journal" / "deadbeef"
    jdir.mkdir(parents=True)
    (jdir / "0.orig").write_bytes(b"A0\n")
    (tmp_path / ".patchgate" / "journal.json").write_text(json.dumps({"id": "deadbeef", "entries": [
        {"rel_path": "a.py", "had_original": True, "original_sha256": hashlib.sha256(b"A0\n").hexdigest(),
         "backup": "0.orig"},
        {"rel_path": "fresh.py", "had_original": False, "original_sha256": None, "backup": None}]}))
    (tmp_path / "a.py").write_text("A1 (half-committed)\n")
    (tmp_path / "fresh.py").write_text("partial\n")
    assert sorted(recover(tmp_path)) == ["a.py", "fresh.py"]
    assert (tmp_path / "a.py").read_text() == "A0\n"
    assert not (tmp_path / "fresh.py").exists()
    assert not (tmp_path / ".patchgate" / "journal.json").exists()
    assert recover(tmp_path) == []  # idempotent


def test_recover_with_no_journal_is_noop(tmp_path):
    assert recover(tmp_path) == []
