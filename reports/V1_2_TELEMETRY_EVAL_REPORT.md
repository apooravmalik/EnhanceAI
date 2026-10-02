# V1.2 telemetry and evaluation verification report

**Status: passed locally on 2026-10-03.** V1.2 was verified against the five
public evaluation snapshots created for V1.1, using the default SQLite-backed,
128-dimension BM25 + hash + RRF + rerank workflow.

## Delivered

- Persisted run/span telemetry through `aisys telemetry` and `Project.telemetry`.
- Retrieval recall, hit rate, precision at 1/3/5/10, MRR, and document-level nDCG.
- Answer exact match and token F1 where the case and workflow provide answers.
- Mean and p95 per-case latency plus `metadata.slice` summaries.
- Local metric leaderboards and threshold/regression gates with a CI-friendly
  non-zero exit on failure.

During verification, nDCG initially exceeded one for multi-chunk source
documents. The cause was repeated chunks being scored as repeated document
relevance. The shared scorer now deduplicates a source document at its first
rank; a regression test covers the case. The values below are the post-fix
verified results.

## Public snapshot results

| Dataset | Cases | Recall@5 | Hit@5 | MRR | nDCG@k | Mean / p95 latency (ms) |
|---|---:|---:|---:|---:|---:|---:|
| SQuAD | 40 | 0.950000 | 0.950000 | 0.950000 | 0.950000 | 7.152 / 7.626 |
| HotpotQA | 30 | 0.516667 | 0.800000 | 0.750000 | 0.546033 | 29.503 / 31.276 |
| CoQA | 81 | 0.901235 | 0.901235 | 0.784979 | 0.814545 | 8.056 / 9.058 |
| TriviaQA | 15 | 0.752609 | 1.000000 | 0.866667 | 0.843233 | 131.140 / 133.689 |
| DuoRC | 20 | 1.000000 | 1.000000 | 0.960000 | 0.969343 | 13.781 / 14.137 |

All 186 cases completed with zero failures. Exact metric records, retrieval
cutoffs, answer baseline scores, misses, and dataset/system hashes are in:

- `reports/v1-2-verified-squad.md`
- `reports/v1-2-verified-hotpotqa.md`
- `reports/v1-2-verified-coqa.md`
- `reports/v1-2-verified-triviaqa.md`
- `reports/v1-2-verified-duorc.md`

## Telemetry and gate checks

The final DuoRC answer run recorded five component spans. Its telemetry view
contained the expected BM25, hash, RRF, rerank, and answer components; it
reported component durations, retrieval counts, index dimensions, the RRF
constant, and answer word counts.

- `aisys gate v1-2-verified-squad --metric source_recall_at_k --minimum 0.90`
  passed with exit code 0.
- `aisys gate v1-2-verified-hotpotqa --metric source_recall_at_k --minimum 0.90`
  correctly failed with exit code 1.

## Checks

- Package compilation passed.
- Four local tests passed, including the full CLI lifecycle, telemetry,
  leaderboard, passing gate, sweep, custom component, and duplicate-chunk
  document-metric regression check.
- Workflow validation, SQLite integrity check, and whitespace diff checks passed.
