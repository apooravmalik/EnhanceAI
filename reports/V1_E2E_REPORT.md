# V1 End-to-End Verification Report

**Status: passed locally on 2026-10-02.** Verification was completed before
the repository's first push.

## What was built

V1 is a Python SDK with a thin local CLI. A single system.yaml defines two
workflows:

1. ingest: JSONL documents → word chunking → a content-addressed local index.
2. answer: question → deterministic local retrieval → extractive answer and
   returned source chunks.

The runtime validates ports, types, and DAG structure before execution. It
persists manifests, content-addressed node artifacts, runs, spans, source
snapshots, and evaluation results in .aisys/ (SQLite plus JSON artifacts).
Each query manifest pins the exact index artifact it used, so later ingests do
not change an already-recorded run.

The CLI covers init, check, components, graph, run, trace, dataset fetch, eval,
and compare. The SDK entry point is aisys.Project.

## Public datasets and snapshots

The Dataset Viewer API was used to take evenly distributed validation-split
samples, convert them into local document JSONL and golden evaluation JSONL,
and record immutable hashes.

| Dataset | Source / configuration | Rows sampled | Documents | Evaluation hash |
|---|---|---:|---:|---|
| SQuAD | rajpurkar/squad / plain_text, validation | 60 | 18 unique contexts | 8d22f4b0758e6e15671a8456cfaf993423a0a6f1b18d5f9c1fc3052bec5c0e7e |
| HotpotQA | hotpotqa/hotpot_qa / distractor, validation | 40 | 398 context documents | e0f6563bfc1ea07890a28abcf69535f1d9f6cce15d5d611e9c5819fe296d5802 |

The local data and source metadata are under data/; evaluation snapshots are
under evals/. The source metadata captures both document hashes and the API
retrieval time.

## Retrieval experiment

The only candidate change was retrieval.vector.top_k: 2 → 5. Chunking stayed
at 180 words with 30-word overlap. Each comparison uses the same data hash and
the index generated from that dataset.

**Mean gold-source recall@k** is the fraction of expected source documents
retrieved per case. **Case hit rate@k** is the fraction of cases with at least
one expected source retrieved. The latter is useful for SQuAD; the former is
essential for multi-hop HotpotQA.

| Dataset | Configuration | Gold-source recall@k | Case hit rate@k | Mean first relevant rank | Failed cases |
|---|---|---:|---:|---:|---:|
| SQuAD | top-2 | 0.9500 | 0.9500 | 1.0000 | 0 / 60 |
| SQuAD | top-5 | 1.0000 | 1.0000 | 1.1167 | 0 / 60 |
| HotpotQA | top-2 | 0.5000 | 0.8500 | 1.3529 | 0 / 40 |
| HotpotQA | top-5 | 0.6625 | 0.9000 | 1.5000 | 0 / 40 |

Observed deltas:

- SQuAD: +0.0500 gold-source recall; 3 improved, 0 regressed.
- HotpotQA: +0.1625 gold-source recall; 12 improved, 0 regressed.

The top-5 configuration is retained in system.yaml. The higher mean first
relevant rank is not a regression: it reflects newly recovered cases whose
first gold chunk occurs below rank 2. The comparison reports are linked below.

- [SQuAD top-2](squad-v1-top2.md), [top-5](squad-v1-top5.md), and [comparison](compare-squad-v1-top2-to-squad-v1-top5.md)
- [HotpotQA top-2](hotpotqa-v1-top2.md), [top-5](hotpotqa-v1-top5.md), and [comparison](compare-hotpotqa-v1-top2-to-hotpotqa-v1-top5.md)

## Checks performed

| Check | Result |
|---|---|
| Dependency install and package build: uv sync | Passed |
| Static compilation: uv run python -m compileall -q src | Passed |
| Local CLI E2E suite: uv run python -m unittest discover -s tests -v | Passed: 2 tests |
| Workflow validation and Mermaid graph generation | Passed |
| Public dataset fetch/conversion for both datasets | Passed |
| Two ingest workflows and four evaluation experiments | Passed; 0 failed evaluation cases |
| Trace inspection | Passed; manifests, span inputs/outputs, index artifact IDs, and usage recorded |
| SQLite PRAGMA integrity_check | ok |
| git diff --check | Passed |

The test suite creates a fresh project through the CLI, validates it, ingests
documents, runs retrieval, evaluates it, checks a persisted trace, confirms
partial multi-source coverage scores as 0.5, and rejects a cyclic workflow.

## Issue found and resolved during verification

The first live dataset fetch failed because this host's Python installation
did not discover a CA bundle. The client now loads the host bundle at
/etc/ssl/cert.pem when present; certificate verification remains enabled.

The first retrieval metric also used a binary "any source found" score under
the name recall. This was corrected before final evaluation to score actual
gold-source coverage and to report the binary hit rate separately. All results
in the table above use the corrected implementation.

## Known V1 limits

- Retrieval is an intentionally local, deterministic TF-IDF-style word scorer,
  not embeddings or ANN. It is suitable for this proof loop, not large corpora.
- The answer component returns the top retrieved chunk; answer correctness is
  not claimed by this retrieval-only evaluation.
- The optional OpenAI-compatible component validates credentials and supports
  provider calls, but was not executed because no provider credential was
  supplied and no model inference was required for the requested chunk
  retrieval test.
- Public samples are reproducible by saved hashes, but the Dataset Viewer API
  itself is a moving external source; use a committed/exported snapshot for a
  release benchmark.
