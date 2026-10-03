# V1.3 semantic RAG contract

V1.3 adds an optional, OpenAI-compatible semantic retrieval path while
preserving the local YAML, artifact, trace, evaluation, and gate workflow.
The default V1.2 system remains credential-free and deterministic.

## Components

| Component | Inputs | Outputs | Purpose |
|---|---|---|---|
| embeddings.openai_compatible | chunks | entries | Batches chunk text to `/embeddings`, then caches vectors locally. |
| retrieval.semantic_index | entries | index_id, index_info | Persists semantic entries in artifact or SQLite storage. |
| retrieval.semantic | question | chunks | Embeds the query and performs local cosine retrieval. |

The embedding component records model, input tokens, embedding dimensions,
cache hits, and optional estimated cost. The semantic retriever records the
same query-side fields. Provider requests use the configured `api_key_env`;
keys are never stored in YAML, traces, artifacts, or reports.

The OpenAI embeddings API accepts text input and a model name, returns a float
embedding vector, and supports cosine similarity for retrieval. See the
[official embeddings guide](https://developers.openai.com/api/docs/guides/embeddings).

## Run the hybrid template

Copy `examples/semantic-system.yaml` into a project as `system.yaml`, then:

    export OPENAI_API_KEY=...
    uv run aisys run ingest --input data/squad/documents.jsonl
    uv run aisys eval answer evals/squad.jsonl --name semantic-squad
    uv run aisys compare baseline-squad semantic-squad
    uv run aisys gate semantic-squad --baseline baseline-squad --max-regression 0.02

The template uses BM25 plus semantic retrieval, RRF, reranking, and an
OpenAI-compatible answer component. Its `sources` output remains separate
from answer text, so applications can render citations from retrieved document
IDs without trusting generated citation strings.

To estimate cost, set the relevant provider price explicitly in the component
configuration, for example `input_cost_per_million_tokens`. The value is
recorded with each span; price policy remains a project decision rather than a
hidden SDK default.

## Evaluation aliases

Golden cases may now specify one answer or multiple accepted answers:

    {"id":"returns-001","input":{"question":"Can I return an opened item?"},"expected":{"source_ids":["returns"],"answers":["Yes", "Opened items can be returned within fourteen days."]}}

Exact match and token F1 take the best score across accepted answers. Retrieval
metrics still use document-level gold source IDs.

## Boundary

V1.3 deliberately uses persisted, brute-force cosine retrieval. It has one
provider contract and no vector-database adapter. Add an ANN or database
adapter only after a measured local latency or corpus-size limit is exceeded.
