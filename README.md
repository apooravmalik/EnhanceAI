# AI System Engineering Platform

An engineering environment for defining, executing, tracing, evaluating, comparing, and proving AI systems.

## Main idea

Modern AI applications combine models, retrieval, tools, data stores, prompts, retries, routing, and infrastructure. Today, their architecture, runtime behavior, evaluations, traces, costs, and results usually live in disconnected places.

This project creates one reproducible AI system definition that connects them all:

```text
Define → Execute → Trace → Evaluate → Change → Compare
```

An architecture is only complete when it can be executed and evaluated. The platform must let a developer identify the exact configuration and system version behind every result, inspect what happened during a run, and compare evidence across changes.

## Product principles

- CLI and SDK are the source of truth; YAML is the declarative system definition.
- The UI is generated from that definition for visualization, results, traces, and sharing—not a separate workflow editor.
- Execution is local and developer-controlled: developers retain their credentials, data, models, and infrastructure.
- Evaluation and tracing are first-class parts of the system, not dashboards added later.
- The system exposes real engineering complexity—model choices, prompts, retrieval settings, latency, token usage, cost, errors, and fallbacks—so results remain reproducible and explainable.
- Sharing and portable exports must exclude secrets.

## Product documents

- [Product design](docs/PRODUCT_DESIGN.md)
- [V1 product requirements](docs/PRD.md)
- [V1.1 RAG components and sweep contract](docs/V1_1_RAG_COMPONENTS.md)

## V1.1: configurable local RAG engineering

V1 stays intentionally narrow. It should allow a developer to:

1. Initialize a project and define a basic RAG workflow with YAML and/or the SDK.
2. Run it locally with developer-provided credentials and data.
3. Store runs, system versions, metrics, outputs, and component-level traces locally.
4. Create an evaluation dataset and run evaluations for retrieval and answer quality, latency, cost, and errors.
5. Change the architecture or configuration, repeat the evaluation, compare results, inspect failures, and reproduce prior runs.
6. Generate a lightweight visual architecture from the same system definition.

The implemented component set includes document loading and chunking, BM25,
hashed-vector retrieval, RRF fusion, reranking, artifact or SQLite-backed
indexes, typed custom Python components, and parameter sweeps. SQLite stores
local metadata and traces. External infrastructure is optional.

## Run it

    uv sync
    uv run aisys check
    uv run aisys dataset fetch squad --limit 40
    uv run aisys run ingest --input data/squad/documents.jsonl
    uv run aisys eval answer evals/squad.jsonl --name squad-hybrid
    uv run aisys sweep sweeps/squad.yaml
    uv run aisys trace latest

See the [V1 E2E verification report](reports/V1_E2E_REPORT.md) and the
[V1.1 RAG verification report](reports/V1_1_RAG_REPORT.md) for the public
datasets, observed retrieval results, and all checks.

## What this is not yet

V1 does not need a hosted SaaS, enterprise observability, every provider or vector database, a large visual workflow editor, Kubernetes, multi-tenant billing, or complex collaboration.

Those are later possibilities. The immediate product question is simpler:

> Can a developer define a real AI system locally, execute it, evaluate it, inspect its trace, and reproduce the result?

If the answer is yes, the foundation is in place. RAG is the first proving ground; richer retrieval variants, agents, production replay, coding-agent integration, sharing, and deployment follow only once this loop is solid.
