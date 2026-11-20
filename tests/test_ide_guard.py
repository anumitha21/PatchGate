import io
import json
import subprocess
import sys

import pytest

from patchgate.hooks import ide_guard

HOOK = [sys.executable, "-m", "patchgate.hooks.ide_guard"]


def run(payload, raw=None):
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run(HOOK, input=stdin, capture_output=True, text=True, timeout=30)


def pre(tool, **tin):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tin, "cwd": "/repo",
            "session_id": "s"}


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_native_edit_tools_denied_exact_contract(tool):
    r = run(pre(tool, file_path="a.py"))
    assert r.returncode == 2 and r.stdout == ""
    assert "mcp__patchgate__propose_edits" in r.stderr and tool in r.stderr
    assert r.stderr.strip() == ide_guard.edit_reason(tool, "patchgate")


def test_custom_server_name_in_reason():
    r = subprocess.run(HOOK + ["--server-name", "pg2"], input=json.dumps(pre("Write")),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "mcp__pg2__propose_edits" in r.stderr


@pytest.mark.parametrize("cmd", [
    "echo hi > src/a.py", "echo hi >> a.py", "cat x | tee a.py", "sed -i 's/a/b/' a.py",
    "sed -i.bak s/a/b/ a.py", "perl -pi -e 's/a/b/' a.py", "cp new.py src/a.py", "mv new.py src/a.py",
    "install -m 644 x src/y.py", "git apply fix.patch", "git checkout -- a.py", "git restore a.py",
    "python -c \"open('a.py','w').write('x')\"", "python3 - <<'EOF'\nopen('a.py','w').write('x')\nEOF",
    "printf x &> out.txt", "ls && echo y>a.py",
])
def test_dangerous_bash_denied(cmd):
    r = run(pre("Bash", command=cmd))
    assert r.returncode == 2, cmd
    assert "mcp__patchgate__propose_edits" in r.stderr


@pytest.mark.parametrize("cmd", [
    "ls", "ls -la src", "pytest -q", "pytest -v tests/test_x.py 2>&1", "git status", "git diff", "git log --oneline",
    "cat a.py", "grep -rn 'a > b' src", "echo 'x > y'", "python -c \"print(1)\"", "python -m pytest",
    "pytest > /dev/null", "cmd 2>/dev/null", "cp a.py /tmp/a.py", "git checkout main", "sed -n '1,5p' a.py",
    "echo hi | tee /dev/null", "diff a b >&2",
])
def test_benign_bash_allowed(cmd):
    r = run(pre("Bash", command=cmd))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", cmd


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "WebSearch", "mcp__patchgate__propose_edits", "Task"])
def test_other_tools_allowed(tool):
    r = run(pre(tool, file_path="a.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_malformed_json_fail_closed_for_edit_like_and_open_for_unknown():
    r = run(None, raw='{"tool_name": "Write", "tool_input": {broken')
    assert r.returncode == 2 and "mcp__patchgate__propose_edits" in r.stderr
    r = run(None, raw='{"tool_name": "Read", oops')
    assert r.returncode == 0
    r = run(None, raw="not json at all")
    assert r.returncode == 0 and "malformed" in r.stderr
    r = run(None, raw="")
    assert r.returncode == 0


def test_weird_shapes_never_crash():
    for payload in ([], "str", 5, None, {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": "x"},
                    {"tool_name": "Bash", "tool_input": {"command": 7}}, {"tool_name": None}):
        assert ide_guard.decide(payload)[0] == "allow"


def test_templates_are_valid_json_and_consistent():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "templates"
    s = json.loads((root / "claude_settings.json").read_text())
    m = s["hooks"]["PreToolUse"][0]["matcher"].split("|")
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} == set(m)
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"
    assert {"Edit", "Write", "NotebookEdit"} <= set(s["permissions"]["deny"])
    mcp_cfg = json.loads((root / "mcp.json").read_text())
    assert list(mcp_cfg["mcpServers"]) == ["patchgate"]
    assert mcp_cfg["mcpServers"]["patchgate"]["args"][:2] == ["mcp", "--repo"]
