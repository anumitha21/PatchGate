# VERIFIED FACTS
Format: fact | how verified | tool version | date.  Date of all entries below: 2026-11-19.

## M0 environment
- Python 3.13.18, Linux x86_64 | `python3 --version`, `uname -a`
- Installed (pip list): pydantic 2.14.3, PyYAML 6.0.3, pathspec 1.1.1, mcp 2.3.0, tree-sitter 0.26.1, tree-sitter-python 0.25.0, pytest 9.2.0, pytest-asyncio 1.5.0, pyright (PyPI) 1.1.414
- `shutil.which` finds BOTH `pyright-langserver` and `pyright-python-langserver` in the venv bin | shutil.which
- `pyright --version` ran immediately and the Node dist is bundled under site-packages/pyright/dist; no network use observed in this sandbox (it may differ on other installs) | ran it
- `pyright-langserver` with no args errors "Connection input stream is not set ... --stdio" so `--stdio` is required | ran it
- **mcp 2.3.0 has NO `mcp.server.fastmcp`** (ImportError says FastMCP was renamed). Correct: `from mcp.server.mcpserver import MCPServer`; `MCPServer(name=..., instructions=...)`, `@server.tool(name=..., description=...)`, `server.run(transport="stdio")`. Client: `mcp.client.stdio.stdio_client(StdioServerParameters)`, `mcp.client.session.ClientSession(read, write)`, `call_tool(name, arguments)`. | import attempts + inspect.signature. See DECISIONS.md.

(Re-verified 2026-11-19 after the original M2/M3/M6 entries were lost when this file was accidentally overwritten; everything below was re-observed by re-running the spikes and experiments, not recalled.)

## M2 LSP / Pyright 1.1.414 (scripts/spike_lsp.py, raw JSON-RPC)
- Framing: `Content-Length` byte-length framing; server accepts `--stdio` only (see M0).
- `initialize` result: `textDocumentSync: 2` (incremental; we still send full-text didChange, which is allowed), `workspace.workspaceFolders.supported: true`. **No `diagnosticProvider` advertised** (push model only). A raw `textDocument/diagnostic` request did return `{"kind":"full","items":[]}`, but since it is not advertised we do NOT rely on it. Decision: push diagnostics + version/quiescence completion (DECISIONS D3).
- Server->client requests seen after `initialized`: `workspace/configuration` twice, each with ONE item: `{"scopeUri": <repo uri>, "section": "python"}` then `{"section": "pyright"}`. Answering `[{}]` per item is accepted.
- `publishDiagnostics` **includes `version`** (the didOpen/didChange version we sent).
- **Clean files are cleared with an explicit empty publish** (`n=0`), including after a didChange that fixes a file (`bad.py` v2 -> n=0).
- **Preliminary publishes happen**: a freshly opened file got `n=0` at version 1, then the real result (`n=1`) at the same version 1 after `pyright/beginProgress`. So "a publish with version >= sent" is NOT sufficient; the client also waits for quiet (no progress active, `quiescence_ms` of silence). This is why `_ready` checks version, active progress and quiescence.
- Interpreter hint: with a package installed only in a separate venv (`six`), no hint -> `reportMissingModuleSource` (stub only); `lsp.python_path` answered as `{"pythonPath": ...}` for the `python` configuration section -> 0 diagnostics. **`workspace/configuration` (section `python`) works.** No config file is written into the repo.

## M3 tree-sitter 0.26.1 / tree-sitter-python 0.25.0 (scripts/spike_treesitter.py)
- `Language(tspython.language())` + `Parser(language)` + `parser.parse(bytes)` works; `has_error` is a property on nodes.
- Node types relied on (all observed): `module`, `assert_statement`, `function_definition`, `decorated_definition`, `decorator`, `class_definition`, `block`, `identifier`, `call`, `attribute`, `comparison_operator`, `integer`, `true`, `string`, `ERROR`.
- A decorator node's text includes the `@`; `assert True` condition node type is `true`; ints are `integer`; comparisons are `comparison_operator`.
- Broken code: `root_node.has_error == True` and an `ERROR` node appears.

## pathspec 1.1.1
- `"gitwildmatch"` emits DeprecationWarning (error under `-W error`); the `"gitignore"` factory is used. `tests/**`, `**/test_*.py`, `test_*.py` match `tests/a.py`, `a/b/test_x.py`, `test_y.py` and not `src/x.py`.

## M6 Claude Code hooks (fetched https://code.claude.com/docs/en/hooks)
- PreToolUse stdin JSON: `session_id, transcript_path, cwd, permission_mode, hook_event_name, tool_name, tool_input, tool_use_id`.
- Deny, two documented mechanisms: (a) **exit 0 + stdout** `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`; (b) exit 2 with reason on stderr. Docs say not to mix them. **Chosen: (a).** Exit 0 with no output = no decision (normal permission flow); silence is not approval.
- Settings schema: `{"hooks":{"PreToolUse":[{"matcher":"...","hooks":[{"type":"command","command":"..."}]}]}}`. Matcher made only of letters/digits/`_`/`-`/space/`,`/`|` is an exact-string (alternation by `|`) match; anything else is an unanchored JS regex.
- MCP tools are named `mcp__<server>__<tool>`; with server `patchgate`: `mcp__patchgate__propose_edits`. A bare `mcp__patchgate` matches nothing.
- Timeouts/failures of a PreToolUse hook do NOT block by default; handler field `onFailure: "block"` exists (see OPEN_QUESTIONS Q2).
- `permissions.deny` is a list of rule strings, `Tool` or `Tool(specifier)` (doc example: `"Read(./.env)"`). **UNVERIFIED:** that a bare `"Edit"` rule denies all edits (not confirmed from the fetched text); and `MultiEdit` may not exist as a tool name in current Claude Code. The hook is the primary mechanism; the deny list is belt-and-braces.
