# V1.2 telemetry and evaluation contract

V1.2 turns the local run, span, and experiment records already produced by
the runtime into developer-facing telemetry, richer evaluation evidence, and
repeatable quality gates. No telemetry data leaves the project directory.

## Telemetry

Each run already has a root span and one persisted span per component. The
new telemetry view derives, from those durable records:

- total run duration and status;
- component span count, success/failure count, total and mean duration; and
- numeric component usage such as retry attempts, retrieved chunk counts,
  index dimensions, RRF settings, and provider token counts when supplied by
  an OpenAI-compatible endpoint.

    uv run aisys telemetry                 # latest run
    uv run aisys telemetry run_abc123

The equivalent SDK call is `Project.load('.').telemetry()`.

There is deliberately no provider cost calculation yet. A token count is not
a cost without a versioned model-price policy. Add that only with an explicit
pricing configuration instead of silently inventing costs.

## Retrieval and answer evaluation

`aisys eval` retains the V1.1 compatibility metrics and now reports:

- document-level recall, hit rate, and precision at 1, 3, 5, and 10;
- MRR and nDCG over the workflow's returned sources;
- mean and p95 end-to-end per-case latency;
- exact match and token F1 when both `expected.answer` and a string
  `answer` output are present; and
- per-slice metric summaries when a JSONL case has `metadata.slice`.

Source IDs are document IDs, but a RAG workflow returns chunks. The evaluator
therefore collapses repeated chunks from one document at their first rank
before calculating document metrics. This keeps nDCG bounded by one and
prevents a document with many chunks from receiving extra relevance credit.

The built-in extractive answer component returns its leading retrieved chunk;
its answer scores are a baseline signal, not a claim of generated answer
quality. Use an answer-producing component to make answer EM/F1 meaningful.

## Leaderboards and gates

    uv run aisys leaderboard --metric ndcg_at_k
    uv run aisys gate candidate --metric source_recall_at_k --minimum 0.90
    uv run aisys gate candidate --baseline baseline --max-regression 0.02

`leaderboard` sorts recorded evaluations by any numeric summary metric.
`gate` exits zero when the candidate meets all requested thresholds and exits
one otherwise, making it usable in a local or CI script. A baseline
regression gate refuses to compare different dataset snapshots.

SDK equivalents are `project.leaderboard(metric)` and
`project.gate(candidate, metric, minimum, baseline, max_regression)`.

## Intentional boundary

V1.2 is a local engineering loop, not a hosted observability service. It does
not add remote event ingestion, dashboards, retention policies, alerts, or a
model-pricing catalogue. Those belong after this project has a real use case
for shared, production telemetry.
