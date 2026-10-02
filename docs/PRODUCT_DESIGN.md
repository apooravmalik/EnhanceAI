# Product Design: AI System Engineering Platform

Status: Draft for V1

Last updated: 2026-10-02

## 1. Product direction

The product is a local engineering environment for defining, executing, tracing, evaluating, and comparing AI systems.

Its central object is an executable, reproducible system definition:

```text
Definition → Execution → Trace → Evaluation → Change → Comparison
```

The first release proves this loop with Retrieval-Augmented Generation (RAG). It is not a general visual workflow builder, hosted platform, or distributed agent runtime.

### V1 design decisions

- The user experience is CLI-first.
- The implementation is SDK-first: every CLI command calls the public Python SDK.
- YAML defines workflow wiring and built-in component configuration.
- Python defines custom component behavior.
- A compiled, immutable manifest is saved for every run.
- Workflows execute locally as directed acyclic graphs (DAGs).
- SQLite stores runs, traces, experiment results, and metadata.
- Large values are stored as content-addressed artifacts, not duplicated in SQLite.
- Evaluation datasets begin as version-controlled JSONL.
- The browser experience is read-only and generated from the same manifests and results.
- AI assists authoring and subjective grading; it does not control validation, execution, versioning, or cost accounting.

## 2. Users and jobs

### Primary user: AI application developer

The developer needs to:

- Build a real RAG system without handing credentials or data to a hosted service.
- Change models, prompts, chunking, retrieval, or reranking.
- Understand exactly what happened during a run.
- Test a change against the same examples.
- Identify quality, latency, cost, and failure regressions.
- Reproduce and share evidence without sharing secrets.

### Secondary user: technical reviewer

An engineering lead, architect, or teammate needs to:

- See the architecture without reading the entire codebase.
- Inspect the exact configuration behind a result.
- Review evaluation coverage and individual failures.
- Compare two versions on the same dataset.
- Run the system locally from the project definition.

## 3. Product mental model

The product uses seven concepts.

| Concept | Meaning |
|---|---|
| Project | A directory containing one AI system and its evaluation assets |
| System | The full set of resources, workflows, components, prompts, and policies |
| Workflow | One executable DAG with declared inputs and outputs |
| Component | A typed unit of execution |
| Run | One execution of one workflow against one input |
| Dataset | Versioned examples used for evaluation |
| Experiment | A system version run against a dataset version with a scorer set |

### RAG uses two workflows

A real RAG system has two lifecycles:

```text
ingest: documents → load → chunk → embed → index artifact

answer: question + index artifact → retrieve → generate → answer
```

The ingest workflow creates an immutable, content-addressed index artifact. The answer workflow records the exact index artifact it used. A mutable alias such as `latest` may be convenient for the developer, but every run manifest resolves it to an immutable artifact ID.

This separation is required for reproducibility: an answer cannot be reproduced if the underlying index silently changed.

## 4. End-to-end CLI experience

`aisys` is a placeholder product command.

### 4.1 Install and initialize

```bash
uvx aisys init document-qa --template rag
cd document-qa
uv sync
source .venv/bin/activate
```

The generated project pins the SDK/CLI package in `pyproject.toml`, ensuring the CLI and custom components use the same environment. The remaining examples assume that environment is active; `uv run aisys ...` is equivalent.

Generated project:

```text
document-qa/
├── pyproject.toml
├── system.yaml
├── components.py       # optional custom components
├── prompts/
│   └── answer.md
├── evals/
│   └── golden.jsonl
├── .env.example
└── .gitignore
```

Runtime data lives under `.aisys/` and is ignored by Git:

```text
.aisys/
├── state.db
├── artifacts/
└── reports/
```

### 4.2 Configure credentials

Secrets are referenced by name, never copied into YAML or run manifests:

```yaml
providers:
  openai:
    api_key: env:OPENAI_API_KEY
```

`aisys check` reports missing secret names but never prints secret values.

### 4.3 Validate before spending money

```bash
aisys check
```

Validation includes:

- YAML/schema validation
- Component resolution
- Input/output type compatibility
- Missing workflow inputs and outputs
- Cycles, which are unsupported in V1
- Missing prompt or data files
- Missing environment variables
- Unsupported provider/model combinations where known
- Invalid timeout, retry, or concurrency settings

Validation performs no provider calls unless the user explicitly requests a connection check.

### 4.4 Build the index

```bash
aisys run ingest --input ./documents
```

The result shows:

```text
Run       run_01J...
Workflow  ingest
Status    completed
Index     artifact:sha256:8f4...
Documents 42
Chunks    618
Duration  31.8s
Cost      $0.19 estimated
```

### 4.5 Run a query

```bash
aisys run answer \
  --input '{"question":"What is the refund policy?"}' \
  --resource index=latest
```

The CLI streams concise node status to stderr and writes the final machine-readable result to stdout when `--json` is used.

### 4.6 Inspect the run

```bash
aisys trace latest
aisys trace run_01J... --node retrieve
```

Example summary:

```text
answer [1.84s]
├── embed_query [0.09s, 12 tokens]
├── retrieve [0.02s, 8 candidates]
└── generate [1.71s, 1,402 input / 183 output tokens, $0.006 estimated]
```

### 4.7 Evaluate and compare

```bash
aisys eval answer evals/golden.jsonl --name baseline

# Change system.yaml, a prompt, or components.py

aisys eval answer evals/golden.jsonl \
  --name candidate \
  --compare baseline
```

The comparison reports:

- Dataset and system hashes
- Aggregate quality scores
- Retrieval scores
- Latency and cost distributions
- Error rate
- Improved, regressed, and unchanged cases
- Links or IDs for inspecting individual traces

### 4.8 View locally

```bash
aisys view
```

The local read-only report has three screens:

1. Architecture: workflows, nodes, configuration, and resolved versions
2. Run: trace tree, timing, usage, artifacts, and errors
3. Experiment: aggregate metrics and case-by-case comparison

The UI does not edit workflows in V1.

## 5. System definition

`system.yaml` owns declarative wiring and configuration. Custom business logic remains in Python, so YAML does not grow into a programming language.

Conceptual structure:

```yaml
version: 1
name: document-qa

resources:
  index:
    uses: vector.local

workflows:
  ingest:
    input:
      path: path
    nodes:
      load:
        uses: documents.load
      chunk:
        uses: documents.chunk
        with:
          size: 800
          overlap: 100
      embed:
        uses: embeddings.openai
        with:
          model: text-embedding-3-small
      store:
        uses: vector.store
        with:
          resource: index
    connections:
      - from: input.path
        to: load.path
      - from: load.documents
        to: chunk.documents
      - from: chunk.chunks
        to: embed.texts
      - from: embed.embeddings
        to: store.items
    output:
      index: store.index

  answer:
    input:
      question: string
    nodes:
      retrieve:
        uses: vector.retrieve
        with:
          resource: index
          top_k: 8
      answer:
        uses: llm.generate
        with:
          model: gpt-5-mini
          prompt: prompts/answer.md
    connections:
      - from: input.question
        to: retrieve.query
      - from: input.question
        to: answer.question
      - from: retrieve.documents
        to: answer.context
    output:
      answer: answer.text
```

The exact syntax may change during implementation. The behavioral requirements are:

- One obvious place owns each setting.
- References are resolved during compilation.
- Unknown keys fail validation.
- Prompt files are hashed into the manifest.
- Secret references remain references.
- The compiled manifest is JSON-serializable.

## 6. Component design

### 6.1 V1 built-ins

| Category | Components |
|---|---|
| Input | text, JSON, file path |
| Documents | file loader, text/PDF parser, chunker |
| Models | OpenAI-compatible embedding and generation |
| Retrieval | local vector store, retriever |
| Logic | Python function, simple router |
| Output | text, JSON/Pydantic structured output |
| Evaluation | exact match, field checks, retrieval metrics, rubric judge |

Reranking is optional for V1 and should be added only after basic retrieval works.

### 6.2 Custom component contract

A normal typed function is the extension mechanism:

```python
from aisys import RunContext, component

@component
async def filter_confidential(
    documents: list[Document],
    ctx: RunContext,
) -> list[Document]:
    return [document for document in documents if not document.metadata.get("confidential")]
```

The decorator registers the import path and derives schemas from type annotations. A custom component receives only:

- Typed inputs
- Its validated configuration
- A `RunContext` for cancellation, secrets, artifacts, and trace annotations

V1 does not require base classes, factories, dependency-injection containers, or a plugin marketplace.

### 6.3 Execution policies are not components

These settings belong to a node policy:

- Timeout
- Retry count and backoff
- Concurrency limit
- Whether outputs may be persisted
- Whether content may be exported in telemetry

Retries default to zero to prevent hidden cost and behavior changes.

### 6.4 V1 flexibility boundary

Supported:

- Built-in components configured in YAML
- Custom synchronous or asynchronous Python functions
- New provider adapters through a small Python protocol
- Parallel DAG branches
- Typed structured outputs

Deferred:

- Cyclic graphs
- Long-running checkpoints and resume
- Human-in-the-loop interrupts
- Distributed workers
- Cross-language component execution
- Arbitrary container nodes
- Dynamic runtime graph mutation

## 7. Runtime design

### 7.1 Compile phase

The compiler:

1. Loads YAML.
2. Resolves component imports and resource references.
3. Builds typed ports and edges.
4. Validates acyclicity and required inputs.
5. Resolves mutable aliases to immutable artifact versions.
6. Captures prompt and component source hashes where available.
7. Produces a canonical JSON manifest.
8. Computes the system version hash.

The manifest, not the live YAML file, is the record of what a run executed.

### 7.2 Execute phase

The runner:

1. Creates the root run and root trace span.
2. Schedules nodes when all dependencies are satisfied.
3. Runs independent nodes concurrently up to a configured limit.
4. Wraps synchronous functions with a worker thread and awaits asynchronous functions directly.
5. Enforces cancellation, timeout, and explicit retry policy.
6. Writes large outputs to the artifact store.
7. Records node status and trace data transactionally.
8. Cancels downstream nodes after an unrecoverable dependency failure.
9. Finalizes the run as completed, failed, or cancelled.

V1 reruns a failed workflow from the beginning. Step-level resume is deferred until real workloads prove that it is necessary.

### 7.3 Reproducibility record

Every run records:

- Compiled manifest and hash
- Workflow name
- Input hash and stored input, subject to privacy policy
- Resource artifact IDs
- Prompt hashes
- Component import paths and source hashes where available
- Python and platform version
- Package lock hash
- Provider and requested model
- Provider-returned model/version when available
- Start/end timestamps

Reproducibility means reconstructing the same declared system and inputs. It does not promise identical LLM output from a nondeterministic external provider.

## 8. Tracing and artifacts

### 8.1 Trace shape

```text
Run
├── Node span
│   ├── input references
│   ├── output references
│   ├── configuration hash
│   ├── latency
│   ├── tokens and estimated cost
│   └── error/retry events
└── Node span
```

Internally, spans align with OpenTelemetry concepts so a later OTLP exporter does not require a new trace model.

### 8.2 Artifact storage

Large or sensitive values are stored under content hashes:

```text
.aisys/artifacts/sha256/<digest>
```

SQLite stores metadata and references. Artifacts include:

- Loaded documents
- Chunks
- Embeddings/indexes
- Large model inputs and outputs
- Generated reports
- Dataset snapshots

### 8.3 Privacy defaults

- All storage is local by default.
- Secrets are never written to manifests, traces, reports, or exports.
- Local prompt and response capture is configurable.
- External telemetry exports exclude content by default.
- Reports redact configured fields.
- Shared reports include configuration, metrics, and selected examples only.

## 9. Evaluation design

### 9.1 Dataset format

JSONL is the V1 source format:

```json
{"id":"refund-001","input":{"question":"Can I return an opened item?"},"expected":{"answer":"Opened items can be returned within 14 days.","source_ids":["returns-policy"]},"metadata":{"slice":"policy","difficulty":"normal"}}
```

An experiment snapshots the dataset and records its content hash. Editing the original file later does not alter historical results.

### 9.2 Dataset sources

Preferred order:

1. Human-authored golden examples
2. Production or staging traces selected through feedback
3. Historical failure cases
4. Licensed/public domain benchmarks
5. AI-generated candidate cases reviewed by a domain expert

The first useful dataset should contain approximately 30–100 representative cases covering normal, edge, and adversarial inputs.

### 9.3 Scorer types

Deterministic scorers:

- Exact or normalized match
- Required/forbidden fields
- Valid JSON/schema
- Expected source present
- Retrieval precision, recall, and rank
- Citation source validity
- Latency, token, cost, and error thresholds

Model-assisted scorers:

- Answer correctness against expected facts
- Faithfulness to retrieved context
- Relevance
- Domain-specific rubric
- Blinded pairwise preference

Model-assisted scorers record their model, prompt, configuration, and rubric hash. They must be calibrated against reviewed human labels before being treated as release gates.

### 9.4 Experiment execution

For every dataset case:

1. Run the target workflow.
2. Preserve its full trace.
3. Run deterministic scorers.
4. Run enabled model-assisted scorers.
5. Store individual scores and explanations.
6. Aggregate by metric and metadata slice.

The comparison engine joins results by stable case ID and labels each case improved, regressed, unchanged, added, or removed.

## 10. Local data model

The minimum logical records are:

| Record | Purpose |
|---|---|
| run | Workflow execution status, manifest, hashes, timing |
| span | Per-component timing, usage, error, and hierarchy |
| artifact | Content hash, media type, path, size, privacy class |
| resource_version | Immutable index or other persistent resource artifact |
| experiment | Workflow/system/dataset/scorer combination |
| eval_result | One case execution and its run ID |
| score | One scorer result for one case |

The schema should remain private to the SDK in V1. The supported API is the SDK and CLI, not direct SQL against `state.db`.

## 11. AI involvement

AI is optional in the platform control plane.

Appropriate uses:

- Draft a RAG template from a short description
- Suggest component settings
- Generate candidate evaluation questions
- Propose evaluation rubrics
- Grade subjective output qualities
- Cluster and summarize failures
- Propose a change and rerun evaluations

Inappropriate uses:

- Deciding whether a graph is structurally valid
- Resolving component types
- Determining run success
- Calculating tokens, cost, or latency
- Mutating the canonical system without showing a diff
- Declaring an improvement without evaluation evidence

The product principle is:

```text
AI proposes → runner executes → evaluation provides evidence → developer decides
```

## 12. Error experience

Errors must identify:

- What failed
- Where it failed
- The relevant component or YAML path
- Whether money or external calls were already consumed
- The trace/run ID
- One actionable next step

Example:

```text
answer.retrieve: expected resource "index", but no version was resolved.

Build it with:
  aisys run ingest --input ./documents

system.yaml:47
Run: run_01J...
```

Raw provider exceptions remain available under `--debug` but are not the default UX.

## 13. Technology choice

V1 is one Python 3.11+ package:

- Pydantic v2 for schemas and validation
- Typer for CLI commands
- `asyncio` for DAG scheduling
- PyYAML for `system.yaml`
- SQLite from the standard library
- OpenTelemetry-compatible span naming and attributes
- Official provider SDKs
- NumPy brute-force similarity for the first local vector index
- `uv` for environments, locking, execution, and packaging

TypeScript is deferred until a remote API or substantial web application exists. Rust is deferred until profiling demonstrates a runtime or packaging bottleneck. FAISS or a vector database is added when the local brute-force index fails a documented corpus-size or latency requirement.

## 14. Research-informed rationale

- Anthropic recommends simple, composable workflows and warns that frameworks can obscure prompts and responses: [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents).
- Typed nodes and edges can validate behavior and generate diagrams: [Pydantic Graph](https://pydantic.dev/docs/ai/graph/graph/).
- Stateful agent runtimes introduce checkpoint, persistence, streaming, and interruption concerns that are intentionally deferred: [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview) and [persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
- OpenTelemetry provides a compatible model for nested spans and GenAI usage attributes: [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/) and [GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).
- SQLite is intended for local application storage and portable single-file data: [Appropriate uses for SQLite](https://sqlite.org/whentouse.html).
- Evaluation guidance favors task-specific tests, production examples, human calibration, and stage-level evaluation: [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices).
- RAG evaluation benefits from separating retrieval, relevance, and faithfulness metrics: [Ragas metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/).

## 15. Deferred until evidence demands it

- Hosted control plane
- Visual workflow editing
- Distributed or remote workers
- Cyclic and long-running agent graphs
- Checkpoint/resume
- Human approval interrupts
- Plugin marketplace
- Cross-language execution runtime
- Enterprise authentication and tenancy
- Production deployment orchestration
- DuckDB analytics layer
- Multiple vector database integrations
- Automatic AI optimization

These are not architectural promises. Each requires a concrete user problem or measured V1 limitation before entering scope.
