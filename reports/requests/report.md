# PatchGate measurement: requests

Results as measured. No claims beyond this data. Cases are synthetic; nothing was committed.

## Environment

- **repo**: requests
- **repo_commit**: 3c7baaa
- **python_files_in_repo**: 20
- **files_sampled**: 19
- **samples_requested**: 20
- **dependents_per_sampled_file (mean/max)**: 5.6/14
- **pyright**: pyright 1.1.414
- **python**: 3.13.18
- **os**: Linux 6.18.44-fc-v653
- **lsp.python_path**: /home/claude/patchgate/.venv/bin/python
- **lsp.timeout_sec**: 10
- **lsp.quiescence_ms**: 150
- **wall_time_sec (both passes)**: 376.2
- **fixed_signature cases skipped**: {"fixed_signature_protected_callers": 4, "fixed_signature_generation_error": 0}

## Outcomes (loose fallback ON; expected vs actual)

| case | n | expected | actual |
|---|---|---|---|
| benign_comment | 19 | ACCEPTED | ACCEPTED: 19 |
| benign_whitespace | 19 | ACCEPTED | ACCEPTED: 19 |
| broken_undefined_name | 19 | REJECTED_LSP | REJECTED_LSP: 19 |
| broken_syntax | 19 | REJECTED_AST | REJECTED_AST: 19 |
| broken_signature | 6 | REJECTED_LSP | REJECTED_LSP: 6 |
| fixed_signature_multifile | 2 | ACCEPTED | ACCEPTED: 2 |

## Rates

| metric | fallback ON | fallback OFF |
|---|---|---|
| false-block rate, benign cases | 0/38 (0.0) | 0/38 (0.0) |
| false-block rate, fixed multi-file refactor | 0/2 | 0/2 |
| miss rate, broken cases | 0/44 (0.0) | 0/44 (0.0) |
| LSP timeouts | 0 | 0 |

## Latency (ms, loose fallback ON pass)

| phase | metric | n | p50 | p95 | p99 |
|---|---|---|---|---|---|
| cold | total_ms | 1 | 6605.9 | 6605.9 | 6605.9 |
| cold | lsp_total_ms | 1 | 6395.7 | 6395.7 | 6395.7 |
| warm | total_ms | 83 | 1634.8 | 3180.2 | 3692.5 |
| warm | lsp_total_ms | 64 | 1664.8 | 3301.8 | 3673.2 |

nearest-rank percentiles; cold has n=1 per run by definition

## Unexpected outcomes (fallback ON)

None.
