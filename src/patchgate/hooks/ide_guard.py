"""Claude Code PreToolUse hook (the ONLY editor-specific code in the package, with templates/).

Reads one JSON object on stdin. To BLOCK a call it writes the reason to stderr and exits with code 2
(documented blocking mechanism: https://code.claude.com/docs/en/hooks - exit 2 blocks even if the hook's
stdout is not valid JSON; exit 0 with invalid JSON would fail OPEN). To allow, it prints nothing, exit 0.

SECURITY NOTE: the Bash guard below is a BEST-EFFORT PATTERN BLOCKLIST. It is BYPASSABLE (encodings,
variable indirection, scripts written elsewhere then executed, `dd`, `truncate`, editors, etc.) and is NOT a
security boundary. Real enforcement needs an OS-level sandbox, which is out of scope for v0.1.
It also does not cover deletions (`rm`).
"""
from __future__ import annotations

import json
import re
import sys

NATIVE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DEFAULT_SERVER_NAME = "patchgate"


def mcp_tool_name(server: str = DEFAULT_SERVER_NAME) -> str:
    # Naming convention mcp__<server>__<tool> (VERIFIED against the hooks docs, see docs/VERIFIED_FACTS.md)
    return f"mcp__{server}__propose_edits"


def edit_reason(tool: str, server: str) -> str:
    return (f"PatchGate: the native {tool} tool is disabled in this repository. Every file change must go "
            f"through the {mcp_tool_name(server)} tool (use `propose_edits`), which verifies the change "
            "before anything is written.")


def bash_reason(server: str) -> str:
    return ("PatchGate: this shell command looks like it modifies repository files, which is blocked. "
            f"Use the {mcp_tool_name(server)} tool (`propose_edits`) for all file changes. "
            "Read-only commands (ls, cat, grep, pytest, git status/diff/log ...) are allowed.")


# ---- Bash heuristics (best effort, bypassable) ------------------------------------------------------
_QUOTED = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*\"""")
# `>` / `>>` / `&>` redirect whose target is not an fd (&1, &2) or a harmless device
_REDIRECT = re.compile(r"(?:^|[^<>&\d\-=])(?:\d*|&)>>?\s*(?!&\d|&-|/dev/(?:null|stdout|stderr)\b)[^\s>&|;]")
_TEE = re.compile(r"(?:^|[\s;&|(])tee\b(?!\s+(?:-\w+\s+)*/dev/(?:null|stderr|stdout)\b)")
_SED_I = re.compile(r"\bsed\b[^;&|\n]*\s(?:-[a-zA-Z]*i[a-zA-Z]*\b|--in-place\b)")
_PERL_I = re.compile(r"\bperl\b[^;&|\n]*\s-[a-zA-Z]*i[a-zA-Z]*\b")
_CP_MV = re.compile(r"(?:^|[\s;&|(])(?:cp|mv|install)\b")
_GIT_WRITE = re.compile(r"\bgit\s+(?:apply\b|am\b|restore\b|checkout\s+(?:[^;&|\n]*\s)?--(?:\s|$))")
_PY_INLINE = re.compile(r"\bpython[0-9.]*\b[^;&|\n]*\s(?:-c\b|-\s*(?:$|<|\n)|-\s)|\bpython[0-9.]*\s*<<")
_PY_WRITES = re.compile(r"open\s*\(|\.write|write_text|write_bytes|shutil\.|os\.(?:replace|rename|remove)")
_SAFE_DEST = ("/tmp/", "/var/tmp/", "/dev/null")


def _cp_mv_targets_repo(cmd: str) -> bool:
    """cp/mv/install is allowed only when its last argument is clearly a temp location."""
    for m in _CP_MV.finditer(cmd):
        seg = re.split(r"[;&|\n]", cmd[m.start():], maxsplit=1)[0]
        toks = seg.split()
        if not toks:
            continue
        last = toks[-1].strip("'\"")
        if not last.startswith(_SAFE_DEST):
            return True
    return False


def bash_is_dangerous(command: str) -> bool:
    stripped = _QUOTED.sub("''", command)  # ignore `>` etc. inside quoted strings (heuristic)
    if _REDIRECT.search(stripped) or _TEE.search(stripped):
        return True
    if _SED_I.search(stripped) or _PERL_I.search(stripped) or _GIT_WRITE.search(stripped):
        return True
    if _cp_mv_targets_repo(stripped):
        return True
    if _PY_INLINE.search(command) and _PY_WRITES.search(command):
        return True
    return False


# ---- decision ---------------------------------------------------------------------------------------
def decide(data: object, server: str = DEFAULT_SERVER_NAME) -> tuple[str, str]:
    """Return ("allow", "") or ("deny", reason). Pure function (unit-tested)."""
    if not isinstance(data, dict):
        return "allow", ""
    tool = data.get("tool_name")
    if tool in NATIVE_EDIT_TOOLS:
        return "deny", edit_reason(str(tool), server)
    if tool == "Bash":
        tin = data.get("tool_input")
        cmd = tin.get("command") if isinstance(tin, dict) else None
        if not isinstance(cmd, str):
            return "allow", ""  # nothing to inspect
        if bash_is_dangerous(cmd):
            return "deny", bash_reason(server)
    return "allow", ""


def _looks_like_edit_tool(raw: str) -> str | None:
    """Fail-closed helper for unparsable input: recover a tool name from the raw text if possible."""
    m = re.search(r'"tool_name"\s*:\s*"([^"]+)"', raw)
    if not m:
        return None
    name = m.group(1)
    if name in NATIVE_EDIT_TOOLS or re.search(r"edit|write", name, re.IGNORECASE):
        return name
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    server = DEFAULT_SERVER_NAME
    if "--server-name" in argv:
        i = argv.index("--server-name")
        if i + 1 < len(argv):
            server = argv[i + 1]
    try:
        raw = sys.stdin.read()
    except Exception as e:  # unreadable stdin: unknown tool -> fail open
        print(f"patchgate hook: could not read stdin: {e}", file=sys.stderr)
        return 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        print(f"patchgate hook: malformed JSON input ({e})", file=sys.stderr)
        name = _looks_like_edit_tool(raw)
        if name:  # fail CLOSED for edit-like tools
            print(edit_reason(name, server), file=sys.stderr)
            return 2
        return 0  # unknown tool: fail open
    try:
        action, reason = decide(data, server)
    except Exception as e:  # never crash: edit-like tools fail closed
        print(f"patchgate hook: internal error: {e!r}", file=sys.stderr)
        tool = data.get("tool_name") if isinstance(data, dict) else None
        if tool in NATIVE_EDIT_TOOLS:
            print(edit_reason(str(tool), server), file=sys.stderr)
            return 2
        return 0
    if action == "deny":
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
