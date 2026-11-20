# PatchGate measurement: itsdangerous

Results as measured. No claims beyond this data. Cases are synthetic; nothing was committed.

## Environment

- **repo**: itsdangerous
- **repo_commit**: 672971d
- **python_files_in_repo**: 8
- **files_sampled**: 8
- **samples_requested**: 10
- **dependents_per_sampled_file (mean/max)**: 4.0/10
- **pyright**: pyright 1.1.414
- **python**: 3.13.18
- **os**: Linux 6.18.44-fc-v653
- **lsp.python_path**: /home/claude/patchgate/.venv/bin/python
- **lsp.timeout_sec**: 10
- **lsp.quiescence_ms**: 150
- **wall_time_sec (both passes)**: 54.0
- **fixed_signature cases skipped**: {"fixed_signature_protected_callers": 1, "fixed_signature_generation_error": 0}

## Outcomes (loose fallback ON; expected vs actual)

| case | n | expected | actual |
|---|---|---|---|
| benign_comment | 8 | ACCEPTED | ACCEPTED: 8 |
| benign_whitespace | 8 | ACCEPTED | ACCEPTED: 8 |
| broken_undefined_name | 8 | REJECTED_LSP | REJECTED_LSP: 8 |
| broken_syntax | 8 | REJECTED_AST | REJECTED_AST: 8 |
| broken_signature | 2 | REJECTED_LSP | REJECTED_LSP: 2 |
| fixed_signature_multifile | 1 | ACCEPTED | ACCEPTED: 1 |

## Rates

| metric | fallback ON | fallback OFF |
|---|---|---|
| false-block rate, benign cases | 0/16 (0.0) | 0/16 (0.0) |
| false-block rate, fixed multi-file refactor | 0/1 | 0/1 |
| miss rate, broken cases | 0/18 (0.0) | 0/18 (0.0) |
| LSP timeouts | 0 | 0 |

## Latency (ms, loose fallback ON pass)

| phase | metric | n | p50 | p95 | p99 |
|---|---|---|---|---|---|
| cold | total_ms | 1 | 1101.2 | 1101.2 | 1101.2 |
| cold | lsp_total_ms | 1 | 921.5 | 921.5 | 921.5 |
| warm | total_ms | 34 | 545.1 | 1569.6 | 1589.6 |
| warm | lsp_total_ms | 26 | 579.4 | 1568.5 | 1588.8 |

nearest-rank percentiles; cold has n=1 per run by definition

## Unexpected outcomes (fallback ON)

None.
