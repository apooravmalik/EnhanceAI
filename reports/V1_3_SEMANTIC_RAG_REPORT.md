# V1.3 semantic RAG verification report

**Status: provider contract passed locally on 2026-10-03.**

V1.3 adds OpenAI-compatible embedding, semantic-index, and semantic-retrieval
components; local embedding caching; optional explicit cost telemetry; and
multiple accepted answer aliases.

## End-to-end verification

A local HTTP server implemented the OpenAI-compatible `/v1/embeddings` and
`/v1/chat/completions` contracts. The test verified that:

- chunk embeddings are batched and persisted to SQLite;
- a second identical ingest reuses the local embedding cache without another
  embedding request;
- semantic cosine retrieval selected the correct source;
- query and generation token usage plus explicit estimated cost appeared in
  the persisted telemetry; and
- an accepted answer alias earned exact-match credit.

The full local suite passed: five tests, including V1/V1.1/V1.2 flows and this
provider-contract E2E test. Compilation, system validation, SQLite integrity,
and diff checks also passed.

## Live benchmark status

No `OPENAI_API_KEY` was available in the execution environment. Therefore no
external provider request was made and no semantic-quality comparison is
claimed. The public V1.2 snapshots and the semantic workflow template are
ready for an authorized live baseline-versus-candidate run; that run should
record its own dataset hash, model, latency, token usage, cost configuration,
and quality gate result.
