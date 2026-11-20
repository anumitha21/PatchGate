# DECISIONS
- **D1 MCP SDK v2 API.** Spec says `mcp.server.fastmcp.FastMCP`. mcp 2.3.0 removed it; we use `mcp.server.mcpserver.MCPServer` (`@server.tool`, `server.run(transport="stdio")`) and `mcp>=2` in pyproject. Observation wins (spec 2.4).
- **D2 pathspec factory.** Spec says gitwildmatch; pathspec 1.1.1 deprecates it, so the `"gitignore"` factory is used. Behaviour for the spec's globs is identical (tested).
- **D3 Push diagnostics, not pull.** Pyright does not advertise `diagnosticProvider`, so spec 5.5 item 4 is not taken. Completion = version >= sent AND no active progress AND `quiescence_ms` of silence (per doc and global), because preliminary publishes at the same version exist.
- **D4 Interpreter hint.** `lsp.python_path` is delivered via `workspace/configuration` (section `python`, `{"pythonPath": ...}`); verified to change import resolution. `didChangeConfiguration` not needed.
- **D5 Hook deny form.** exit 0 + `permissionDecision: deny` JSON (not exit 2).
- **D6 Loose-fallback switch.** `lsp.loose_fallback` (default true) exists only so `measure_gate.py` can report false-block rate with/without the fallback, as spec 5.4 requires.
- **D7 pyproject.** `mcp>=2` pinned as a lower bound because of D1.
- (process note) docs/VERIFIED_FACTS.md, DECISIONS.md and OPEN_QUESTIONS.md were regenerated on 2026-11-19 after an accidental overwrite.
