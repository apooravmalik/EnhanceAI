# V1.1 RAG component contract

V1.1 keeps the workflow definition as the source of truth. A user changes a
system by editing node settings, rewiring compatible ports, adding a typed
Python component, or running a parameter grid. The runtime validates every
connection before execution.

## Built-in components

| Component | Inputs | Outputs | Main settings |
|---|---|---|---|
| documents.load | path | documents | none |
| documents.chunk | documents | chunks | size, overlap |
| retrieval.index | chunks | index_id, index_info | name, storage, dimensions |
| retrieval.bm25 | question | chunks | name, top_k, k1, b |
| retrieval.hash | question | chunks | name, top_k |
| retrieval.hybrid | question | chunks | name, top_k, candidate_k, rrf_k, lexical_weight, vector_weight |
| retrieval.rrf | primary chunks, secondary chunks | chunks | top_k, rrf_k, primary_weight, secondary_weight |
| retrieval.rerank | question, chunks | chunks | top_k |
| answer.extractive | question, chunks | answer | none |
| llm.openai_compatible | question, chunks | answer | model, api_key_env, base_url, prompt |

retrieval.vector remains as a backward-compatible alias for retrieval.bm25.

## Storage and dimensions

retrieval.index has two local storage modes:

- artifact stores chunks and sparse hashed vectors in the content-addressed
  JSON index artifact.
- sqlite stores index entries in the local SQLite state database while keeping
  a content-addressed index manifest for reproducibility.

The dimensions setting controls the number of buckets in the deterministic
hashed-vector representation. More dimensions reduce hash collisions but can
increase the serialized vector payload. The ingest output now exposes
index_info with the selected storage mode, dimensions, chunk count, and exact
logical storage bytes for that index.

This is a local feature-hashing vector baseline, not a semantic embedding
model. Provider embeddings and ANN stores belong in a later provider package;
the V1.1 goal is to make the engineering trade-offs explicit and testable
without credentials or hidden infrastructure.

## Hybrid and RRF strategies

The default system runs BM25 and hashed-vector retrieval in parallel, fuses
their candidate lists with reciprocal rank fusion, then reranks the fused
chunks with BM25.

Users can either use retrieval.hybrid for this compact pattern or wire the
two retrievers into retrieval.rrf themselves. RRF is configurable through:

- rrf_k: the rank-decay constant
- primary_weight and secondary_weight: contribution of each ranking
- top_k: number of chunks retained after fusion

BM25 also exposes k1 and b. This gives users a controlled surface for
basic-to-intermediate retrieval experiments without an unbounded settings
schema.

## Typed custom components

Custom functions must be marked with component. Their input parameters become
ports automatically; named output ports can be declared explicitly.

    from aisys import component

    @component(outputs={"question": "string", "language": "string"})
    def normalize(ctx, question: str):
        return {"question": question.strip().lower(), "language": "en"}

Reference it in a workflow as python:components:normalize. The runtime then
validates its ports like built-ins and records its inputs and outputs in the
trace.

## Parameter sweeps

A sweep YAML runs every combination of a configuration grid. It restores the
original system definition after execution and gives every combination a new
index artifact and experiment record.

    name: retrieval-grid
    workflow: answer
    dataset: evals/squad.jsonl
    ingest:
      workflow: ingest
      input:
        path: data/squad/documents.jsonl
    parameters:
      workflows.ingest.nodes.index.with.storage: [artifact, sqlite]
      workflows.ingest.nodes.index.with.dimensions: [32, 128]
      workflows.answer.nodes.fuse.with.rrf_k: [10, 60]

Run it with:

    uv run aisys sweep sweeps/squad.yaml

The sweep is capped at 64 combinations. Split larger searches into focused
grids so results remain interpretable.
