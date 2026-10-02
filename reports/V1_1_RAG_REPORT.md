# V1.1 RAG verification report

**Status: passed locally on 2026-10-03.** This report covers the V1.1
component expansion and the five-dataset retrieval sweep.

## Delivered capability

V1.1 adds configurable BM25 retrieval, deterministic hashed-vector retrieval,
artifact or SQLite index storage, hybrid retrieval, explicit reciprocal rank
fusion, reranking, typed multi-output custom Python components, and YAML grid
sweeps. The default workflow is now:

    documents → chunks → SQLite hash index
    question → BM25 and hash retrieval → RRF → BM25 rerank → answer

Every ingest reports its chosen storage, dimensions, chunk count, and logical
index bytes through index_info. Every query still records its immutable index
artifact ID in the trace.

## Public datasets

Five Hugging Face Dataset Viewer validation snapshots were fetched and
converted into local documents plus golden source-retrieval cases.

| Dataset | Source configuration | Sampled rows | Documents | Evaluated cases |
|---|---|---:|---:|---:|
| SQuAD | rajpurkar/squad, plain_text | 40 | 15 | 40 |
| HotpotQA | hotpotqa/hotpot_qa, distractor | 30 | 300 | 30 |
| CoQA | stanfordnlp/coqa, default | 5 | 5 | 81 |
| TriviaQA | mandarjoshi/trivia_qa, rc | 15 | 70 | 15 |
| DuoRC | ibm-research/duorc, SelfRC | 20 | 10 | 20 |

The exact evaluation hashes are saved in each data source metadata file and
the local evaluation JSONL snapshots.

## Permutation grid

Each dataset ran this complete 2 × 2 × 2 grid:

- storage: artifact, sqlite
- hash dimensions: 32, 128
- RRF rank constant: 10, 60

That is 40 configurations and 1,488 retrieval evaluation executions in total.
All configurations completed with zero failed cases.

| Dataset | Best gold-source recall@5 | Best hit rate@5 | Selected dimensions | Selected RRF k |
|---|---:|---:|---:|---:|
| SQuAD | 0.950000 | 0.950000 | 128 | 10 |
| HotpotQA | 0.566667 | 0.933333 | 32 | 10 |
| CoQA | 0.901235 | 0.901235 | 128 | 10 |
| TriviaQA | 0.752609 | 1.000000 | 128 | 10 |
| DuoRC | 1.000000 | 1.000000 | 128 | 10 |

Artifact and SQLite storage produced identical rankings for the same data and
retrieval settings. That is the intended correctness result: the storage
choice changes persistence layout, not retrieval semantics. Dimension choice
did alter rankings. The 128-dimension setting was best on four datasets;
HotpotQA was better at 32 dimensions on this snapshot. RRF k=10 was best or
tied best on every snapshot, so it is retained as the default.

The raw result records are retained in:

- reports/v1-1-squad-sweep.json
- reports/v1-1-hotpotqa-sweep.json
- reports/v1-1-coqa-sweep.json
- reports/v1-1-triviaqa-sweep.json
- reports/v1-1-duorc-sweep.json

## Verification

- Package compilation passed.
- Three local end-to-end tests passed.
- The test suite covers CLI lifecycle, retrieval evaluation, persisted traces,
  partial multi-source coverage, cycle rejection, SQLite-backed hybrid RAG,
  typed custom ports, and a four-combination sweep.
- Workflow validation, SQLite integrity check, and git diff check passed.
- A Hugging Face HTTP 429 was encountered during source fetches. The client
  now retries with bounded backoff and falls back to a single preview response
  if paging remains rate-limited. TLS verification stays enabled.

## Deferred to V1.2

V1.2 should add durable telemetry views, latency and cost aggregation,
provider-level trace fields, richer retrieval metrics and slice analysis,
answer-quality evaluation, and regression gates. V1.1 provides the component
and experiment foundation those features need.
