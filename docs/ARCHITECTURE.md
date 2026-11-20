# ARCHITECTURE (as built)

## Layering (enforced by tests/test_layering.py)
`adapters/` -> `core/`; `hooks/` -> `core/policy` only; `scripts/` -> `core/`; `core/` depends only on
stdlib, pydantic, pyyaml, pathspec, tree-sitter. Only `adapters/mcp_server.py` imports the MCP SDK; only
`core/lsp_client.py` talks to the language server; only `core/commit.py` writes user files (telemetry
writes under `.patchgate/` only). `core/` and the MCP adapter contain no editor-specific code; Claude Code
specifics live in `hooks/ide_guard.py` and `templates/`.

## Data flow
agent -> MCP `propose_edits` -> circuit-breaker check -> resolve replace-mode edits against disk ->
`verify_batch`: Stage 1 policy -> Stage 2 AST -> Stage 3 LSP (short-circuit, cheapest first) ->
ACCEPTED: `commit_batch` then `after_commit` (import-graph invalidate + LSP re-sync) and ledger reset;
rejected: nothing written, overlay restored, failure recorded. Every decision emits a telemetry event
(including a redacted unified diff).

## Modules
- `models`: Pydantic v2 types (FileEdit/Replace/ResolvedEdit, DiagnosticItem, PolicyViolation, Status, results).
- `config`: strict YAML loader (unknown keys are errors). Missing file -> built-in defaults.
- `policy`: pathspec ("gitignore" factory) matching; resolves paths, rejects absolute/`..`/symlink escapes,
  ignore_dirs for writes; read_denied; write_protected, size/batch limits, non-UTF-8, duplicates.
- `ast_guard`: tree-sitter rules (syntax_error, deleted_test_function, deleted_assert, trivial_assert,
  test_skip_decorator, secret_added), old tree vs new tree, iterative traversal. Secrets are never echoed.
- `diagnostics`: signature = (code, severity, normalized message, context-hash) strict, with a line-hash
  loose fallback; per-file Counter multiset diff; only `block_on` severities block.
- `lsp_client`: async JSON-RPC client for Pyright; transactional overlay (below).
- `import_graph`: lazy reverse-import index (stdlib `ast`), repo root and `src/` as import roots.
- `verifier`: orchestration; never raises (internal failure -> `ERROR`, meaning not committed).
- `commit`: journal in `.patchgate/journal*`, temp file + `os.replace` per file, mode preserved, rollback on
  failure, recovery at server start. Atomic per file and per batch (journal + rollback); not power-loss safe.
- `ledger`: in-memory per-file consecutive-failure counter; trips at the configured limit (`CIRCUIT_OPEN`).
- `telemetry`: JSONL trace, nearest-rank p50/p95/p99.
- `adapters/mcp_server`, `cli`, `hooks/ide_guard`, `scripts/measure_gate.py`.

## LSP completion strategy (chosen)
Push diagnostics. A target is final when a publish with `version` >= the version sent has arrived AND no
Pyright progress is active AND `quiescence_ms` of silence has passed for that document and globally. Reason
(verified): Pyright emits a preliminary publish at the same version before the real result. Overall deadline
`timeout_sec` -> `LspTimeout` -> `on_timeout` policy. Pull diagnostics are not used (not advertised).
Overlay: baselines for original content, then all new contents sent before waiting (so cross-file changes
validate as a unit), restored in `finally` on rejection/exception; new files restore via `didClose`.

## Loose-signature fallback trade-off
Strict signature includes a 3-line context hash; the loose one only the diagnostic's own line. The fallback
avoids false blocks when a *neighbouring* line changes, at the cost that an edit which changes context while
leaving the same message on the same line text can mask a genuinely new, identical error up to the baseline
count. Measured effect on two repos: none (see README); `lsp.loose_fallback` exists to measure it.

## What the AST rules cannot catch
Hardcoded expected outputs, special-casing test inputs in source, weakened-but-non-trivial assertions,
mocks/patches that neuter behaviour. They raise the cost of tampering; they do not prevent it.

## Deviations from the spec
See `docs/DECISIONS.md` (MCP SDK v2 `MCPServer`, pathspec factory, push diagnostics, interpreter hint via
`workspace/configuration`, hook deny form, `lsp.loose_fallback` measurement switch).
