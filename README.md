# PatchGate v1.1

PatchGate is an **automated, deterministic verification layer between an AI coding agent and the files on disk**. The agent proposes edits (single file or multi-file batches). PatchGate checks the proposal *in memory before anything is written*:

1. **Policy Guard**: Path safety (no directory traversal, no symlink escape), write-protected files (`tests/**`, `.github/**`, configs), read-denied paths (`.env`, secrets), size and batch limits.
2. **AST Guard (tree-sitter)**: Concrete syntax tree validation, anti-test-tampering rules (no deleted tests, no deleted asserts, no trivial asserts, no skip decorators), and secret scanning on added lines.
3. **Static Analysis (Pyright LSP)**: In-memory transactional overlay through a persistent Pyright language server daemon. Computes multiset diffs of position-independent diagnostic signatures so only *net-new* errors block. Includes reverse import graph dependents (depth 1) to prevent caller breakage.

Only if all three stages pass are the files written to disk (atomic per file via temporary files + `os.replace`, atomic per batch via journal and rollback). If any check fails, zero files are touched, and the agent receives structured diagnostic explanations. PatchGate treats the LLM as an **untrusted code generator**; PatchGate itself is strictly deterministic and never calls an LLM.

# Archietecture
<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/f2e621a9-fab6-4f14-83ab-5185ce07b0c6" />

---

## What's New in v1.1

- **Cross-Platform & Windows Compatibility**:
  - **URI Canonicalization (`canon_uri`)**: Normalizes VS Code and Pyright Windows URIs (e.g. `file:///d%3A/...` vs `file:///D:/...`) so diagnostic notifications match immediately without timeouts.
  - **Single-Decode Path Parsing (`rel_from_uri`)**: Explicit drive-letter parsing with single-pass decoding to prevent corruption on filenames containing `%`.
  - **Windows Process Resolution**: Automatically finds `pyright-langserver.exe` in Windows virtual environment `Scripts/`.
  - **CRLF Normalization**: Strips trailing `\r` on Windows line endings for clean `read_file` line outputs.
  - **Cross-Platform Test Suite**: Full test suite runs green on both Windows and Linux x86_64.

---

## Quickstart

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/anumitha21/PatchGate.git
cd PatchGate/patchgate-v0.1/patchgate

# Create and activate virtual environment
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install PatchGate in editable mode with development dependencies
pip install -e ".[dev]"
```

### 2. Configure for Claude Code

In your target repository:
```bash
cp /path/to/patchgate/harness.yaml .                         # Policy configuration
cp /path/to/patchgate/templates/mcp.json .mcp.json          # MCP server registration
mkdir -p .claude && cp /path/to/patchgate/templates/claude_settings.json .claude/settings.json
```

- `.mcp.json` registers the MCP server `patchgate` with tools: `read_file`, `propose_edits`, `get_audit_trace`.
- `.claude/settings.json` wires a `PreToolUse` hook (`python -m patchgate.hooks.ide_guard`) that denies native `Edit`/`Write`/`MultiEdit`/`NotebookEdit` tools and best-effort blocks destructive file-writing `Bash` commands, directing the agent to `mcp__patchgate__propose_edits`.

### 3. Configure for VS Code / Cursor / Windsurf

Add PatchGate to your IDE's MCP configuration (`.vscode/mcp.json` or `.cursor/mcp.json`):

```json
{
  "servers": {
    "patchgate": {
      "type": "stdio",
      "command": "patchgate",
      "args": ["mcp", "--repo", "${workspaceFolder}"]
    }
  }
}
```

---

## CLI Usage

PatchGate provides a unified command-line interface:

```bash
# 1. Start the MCP server daemon for AI agents
patchgate mcp --repo .

# 2. One-shot verification of a proposed file replacement without an agent
patchgate check --repo . --file src/module.py --content-file proposed_module.py

# 3. Benchmark verification latency and false-rejection rate across a repository
patchgate measure --repo . --samples 20
```

---

## Verification Pipeline Details

```
Propose Edits (JSON)
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. Policy Guard (policy.py)                                 │
│    • Path escape checks (no `..`, no symlink escape)        │
│    • Write-protected rules (`tests/**`, configs)            │
│    • Read-denied checks (`.env`, secrets)                   │
│    • Size / batch limits                                    │
└──────────────────────────────┬──────────────────────────────┘
                               │ Pass
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. AST Guard (ast_guard.py)                                 │
│    • Tree-sitter concrete syntax validation                 │
│    • Anti-test-tampering (no deleted asserts / test defs)  │
│    • No skip decorators (@pytest.mark.skip, unittest.skip)  │
│    • Secret scanning on added lines                         │
└──────────────────────────────┬──────────────────────────────┘
                               │ Pass
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Static Analysis (lsp_client.py, diagnostics.py)          │
│    • In-memory Pyright language server overlay              │
│    • Dependent import checking (depth 1 reverse graph)      │
│    • Net-new diagnostic differencing                        │
└──────────────────────────────┬──────────────────────────────┘
                               │ Pass
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Atomic Commit (commit.py)                                │
│    • Journaled multi-file transaction (.patchgate/journal)  │
│    • Atomic write via temporary files + os.replace          │
│    • Automatic rollback on any failure                      │
└─────────────────────────────────────────────────────────────┘
```

---

## Measured Performance & Accuracy

Tested against real open-source repositories (`itsdangerous`, `requests`):

| Metric | itsdangerous (8 py files) | requests (20 py files) |
|---|---|---|
| Files sampled | 8 | 19 |
| Benign changes wrongly rejected | **0 / 16** | **0 / 38** |
| Broken changes accepted (errors/syntax/signatures) | **0 / 18** | **0 / 44** |
| Multi-file signature refactors accepted as batch | **1 / 1** | **2 / 2** |
| LSP timeouts | 0 | 0 |
| Warm verification latency (p50 / p95) | 545 ms / 1570 ms | 1635 ms / 3180 ms |

---

## Limitations

1. **Native Shell Bypass**: The PreToolUse hook's Bash guard is a heuristic pattern matcher. Absolute enforcement requires an OS-level sandbox or container boundary.
2. **Semantic Tampering**: The AST guard detects structural tampering (deleting assertions, skip marks), but cannot detect semantic cheats (e.g. hardcoded lookup tables in source code matching test cases).
3. **Power-Loss Safety**: File writes are atomic per-file and transactional per-batch via journal, but not resilient against physical sudden power loss during disk flush.
4. **Language Scope**: Type checking and static analysis targets Python (Pyright). Non-Python files receive full Policy, Size, and Secret scanning checks.

---

## Running Tests

```bash
pytest -v
```
All unit tests and real Pyright integration tests pass cross-platform.
