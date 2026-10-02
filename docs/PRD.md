# Product Requirements Document: AI System Engineering Platform

Status: Draft

Version: 0.1

Target: Local RAG V1

Last updated: 2026-10-02

## 1. Executive summary

AI applications are assembled from models, prompts, retrieval, tools, data, and runtime policies, but their architecture, execution details, traces, evaluations, and results are commonly stored in different systems.

V1 will provide a local, developer-controlled environment where a developer can:

```text
Define → Execute → Trace → Evaluate → Change → Compare
```

The first supported system is a basic RAG application with separate ingest and answer workflows. The product consists of a Python SDK, a thin CLI, a declarative YAML definition, local persistence, an evaluation runner, and a read-only local report.

## 2. Problem statement

Developers cannot reliably answer all of these questions from one reproducible artifact:

- What architecture ran?
- Which model, prompt, parameters, and retrieval settings were used?
- Which indexed documents and embedding configuration were used?
- What happened inside each component?
- What were the latency, token usage, cost, and errors?
- Which dataset and scorers evaluated the system?
- Did the new version improve or regress?
- Can another developer reproduce the same declared run?

Diagrams show intended architecture but not necessarily executed behavior. Source code executes behavior but often hides the architecture. Evaluation scripts and traces frequently live elsewhere. The absence of a connected system definition makes changes hard to prove.

## 3. Product thesis

If an AI architecture cannot be executed, traced, and evaluated, it is not a complete system definition.

The product will win by connecting the system definition to the evidence produced by execution, rather than by offering the largest component catalog or the most elaborate visual editor.

## 4. Target users

### Primary

AI application developers building RAG and LLM workflows locally.

### Secondary

- ML/AI engineers comparing architecture and model changes
- Platform engineers reviewing runtime behavior and integration boundaries
- Engineering leads reviewing evidence before a change is adopted

### Not targeted in V1

- Nontechnical users seeking a no-code builder
- Enterprises seeking a hosted multi-tenant control plane
- Teams seeking a production deployment orchestrator
- Researchers seeking a general distributed training platform

## 5. Jobs to be done

1. When starting a RAG project, I want a runnable structure so that I can reach a real answer quickly.
2. When changing a model, prompt, chunker, or retrieval setting, I want to run the same evaluation so that I can see whether the change helped.
3. When a result is wrong, I want to inspect the component trace so that I can identify where it failed.
4. When sharing a result, I want the architecture, configuration, and evidence to travel together without credentials.
5. When adding proprietary behavior, I want a small Python extension point rather than modifying the runtime.

## 6. Goals

### G1. Reproducible system definition

Every run must reference an immutable compiled manifest containing the declared workflow, configuration, prompt hashes, component identities, and resource versions.

### G2. Real local execution

The YAML definition must execute actual components locally; it must not be a diagram-only representation.

### G3. Component-level traceability

Every node execution must produce a trace span containing timing, status, usage, artifact references, and errors.

### G4. Evaluation-driven iteration

A developer must be able to run the same versioned dataset against two system versions and inspect aggregate and case-level differences.

### G5. Developer ownership

Credentials, source data, indexed content, traces, and results remain local unless the developer explicitly exports them.

### G6. Minimal extensibility

A developer must be able to add a typed Python function as a component without building a plugin package or modifying the execution engine.

## 7. Non-goals

V1 will not provide:

- A hosted SaaS
- A drag-and-drop workflow editor
- Distributed execution
- Production deployment
- Cyclic agent graphs
- Durable long-running checkpoints
- Human-in-the-loop interrupts
- A plugin marketplace
- Enterprise authentication or multi-tenancy
- Every LLM or vector database provider
- Kubernetes orchestration
- Automatic architecture optimization
- Cross-language component execution

## 8. V1 scope

### Included

- Python SDK and CLI
- Project initialization from a RAG template
- YAML system definition
- Typed built-in and custom Python components
- Separate ingest and answer workflows
- OpenAI-compatible generation and embedding integration
- Local immutable vector index artifact
- Local DAG execution
- SQLite run, trace, and evaluation metadata
- Content-addressed artifact storage
- JSONL evaluation datasets
- Deterministic and optional model-assisted scorers
- Baseline/candidate experiment comparison
- Read-only local architecture, trace, and experiment report
- Secret references and redacted exports

### Conditional P1

- Anthropic generation provider
- Reranker component
- AI-assisted candidate dataset generation
- OTLP trace export
- Windows support

## 9. Primary user journey

```bash
uvx aisys init document-qa --template rag
cd document-qa
uv sync
source .venv/bin/activate

aisys check
aisys run ingest --input ./documents
aisys run answer --input '{"question":"What is the refund policy?"}'
aisys trace latest

aisys eval answer evals/golden.jsonl --name baseline

# Developer changes model, prompt, chunking, or retrieval configuration.

aisys check
aisys eval answer evals/golden.jsonl \
  --name candidate \
  --compare baseline

aisys view
```

## 10. Functional requirements

Priority definitions:

- P0: required for V1 release
- P1: valuable after the P0 loop works
- P2: explicitly deferred

| ID | Priority | Requirement |
|---|---:|---|
| PRJ-01 | P0 | `aisys init --template rag` creates a minimal runnable project |
| PRJ-02 | P0 | Runtime state is isolated under a Git-ignored `.aisys/` directory |
| SPEC-01 | P0 | `system.yaml` declares resources, named workflows, nodes, connections, inputs, and outputs |
| SPEC-02 | P0 | Unknown configuration keys and invalid references fail validation |
| SPEC-03 | P0 | Prompts may live in separate files and are hashed into run manifests |
| COMP-01 | P0 | V1 includes loader, parser, chunker, embedding, local vector store, retriever, LLM, and output components |
| COMP-02 | P0 | Component inputs and outputs have machine-validatable types |
| COMP-03 | P0 | A decorated Python function can be loaded as a custom component |
| COMP-04 | P1 | A reranker can be inserted between retrieval and generation |
| VAL-01 | P0 | `aisys check` validates schema, graph, component types, files, secret references, and policies without paid model calls |
| VAL-02 | P0 | V1 rejects cyclic workflows with an actionable error |
| RUN-01 | P0 | `aisys run <workflow>` compiles and executes a local DAG |
| RUN-02 | P0 | Independent ready nodes may execute concurrently within a configured limit |
| RUN-03 | P0 | Nodes support explicit timeout and retry policies; retries default to zero |
| RUN-04 | P0 | A failed dependency prevents downstream execution and marks the run failed |
| RUN-05 | P0 | Ingest creates an immutable index artifact; answer records the resolved artifact ID |
| RUN-06 | P0 | `--json` provides stable machine-readable output suitable for scripts and CI |
| TRACE-01 | P0 | Every workflow run creates a root span and every executed node creates a child span |
| TRACE-02 | P0 | Spans record timing, status, configuration hash, usage, artifacts, retries, and errors |
| TRACE-03 | P0 | `aisys trace` renders a concise tree and supports selecting a node |
| TRACE-04 | P1 | Traces may be exported through OTLP without exporting captured content by default |
| DATA-01 | P0 | SQLite stores run, span, experiment, case, and score metadata |
| DATA-02 | P0 | Large values are stored in a content-addressed local artifact directory |
| DATA-03 | P0 | An experiment snapshots and hashes its dataset |
| EVAL-01 | P0 | JSONL rows support `id`, `input`, optional `expected`, and optional `metadata` |
| EVAL-02 | P0 | The runner applies the same workflow version to every dataset case |
| EVAL-03 | P0 | Deterministic scorers support structure, exact/normalized checks, expected sources, retrieval metrics, latency, tokens, cost, and errors |
| EVAL-04 | P0 | A model-assisted rubric scorer records its model, prompt, configuration, and explanation |
| EVAL-05 | P0 | Metrics aggregate across the dataset and metadata slices |
| EVAL-06 | P0 | Failures remain visible and count toward experiment summaries |
| CMP-01 | P0 | A candidate experiment can be compared against a baseline using stable case IDs |
| CMP-02 | P0 | Comparison labels cases improved, regressed, unchanged, added, or removed |
| CMP-03 | P0 | Comparison reports quality, retrieval, latency, token, cost, and error differences |
| VIEW-01 | P0 | A local read-only report shows architecture, run trace, and experiment comparison |
| SDK-01 | P0 | CLI commands call public Python SDK operations rather than separate implementations |
| SDK-02 | P0 | The SDK can load, validate, run, trace, evaluate, and compare without invoking the CLI |
| AI-01 | P1 | AI can generate candidate evaluation cases that are marked unreviewed until a user approves them |
| AI-02 | P2 | AI may propose system changes only after the evaluation loop is reliable |

## 11. CLI requirements

### Required commands

| Command | Required behavior |
|---|---|
| `init` | Generate the minimal project and sample dataset |
| `check` | Validate locally and explain actionable failures |
| `components` | List built-ins and describe their typed ports/configuration |
| `graph` | Render the compiled workflow as text and Mermaid |
| `run` | Execute a named workflow |
| `trace` | Inspect a run or individual node |
| `eval` | Execute a dataset and optionally compare to a baseline |
| `view` | Open the local read-only report |

### Output behavior

- Human-readable progress goes to stderr.
- Machine-readable results go to stdout with `--json`.
- Every command supports `--help`.
- Validation errors include a YAML path or Python import when applicable.
- Run failures include a run ID and identify whether provider calls already occurred.
- Commands use nonzero exit codes for invalid configuration, failed runs, and failed evaluation gates.
- Debug output is opt-in.

## 12. SDK requirements

The SDK must expose equivalent operations:

```python
from aisys import Project

project = Project.load(".")
project.check()

ingest = project.run("ingest", input={"path": "./documents"})
answer = project.run(
    "answer",
    input={"question": "What is the refund policy?"},
)

experiment = project.evaluate(
    workflow="answer",
    dataset="evals/golden.jsonl",
    name="baseline",
)
```

The exact API can change during implementation, but it must preserve these rules:

- CLI and SDK share one execution path.
- Public result objects have stable IDs and serializable summaries.
- Custom components use normal Python types.
- Provider-specific data remains available under namespaced metadata without leaking into the core interfaces.

## 13. Evaluation requirements

### Required V1 metrics

Retrieval:

- Expected-source hit rate
- Recall@k
- Precision@k where relevance labels exist
- Rank of first expected source

Generation:

- Valid output/schema
- Citation validity
- Answer correctness rubric
- Faithfulness rubric

Operational:

- End-to-end latency
- Per-component latency
- Input/output tokens when supplied by the provider
- Estimated cost with price-table version
- Error and timeout rate

### Evaluation rules

- Deterministic checks take precedence over model graders.
- Model graders cannot be a release gate until compared with human-reviewed labels.
- An error is not dropped from the denominator.
- An experiment records all workflow, resource, dataset, and scorer versions.
- Comparisons use the same dataset snapshot unless the report clearly identifies dataset changes.
- Aggregate scores never replace per-case inspection.

## 14. Nonfunctional requirements

### Local-first

- No hosted account or external database is required.
- The application runs without a background service.
- macOS and Linux are P0; Windows is P1.

### Reproducibility

- Every run has a canonical manifest hash.
- Every experiment has dataset and scorer hashes.
- Mutable resource aliases resolve to immutable artifact IDs before execution.
- The package-lock hash and Python/platform versions are recorded.

### Privacy and security

- Secret values never appear in YAML, manifests, logs, traces, reports, or exports.
- External telemetry export is disabled by default.
- Content export is opt-in and independently configurable from metadata export.
- Path traversal outside declared project/data inputs is rejected where the runtime controls the path.
- Unsafe YAML object construction is prohibited.

### Reliability

- Interrupted runs become `cancelled` or `failed` rather than remaining `running` indefinitely.
- SQLite writes use transactions.
- Artifact writes are atomic.
- Partial experiments retain completed cases and accurately report incomplete status.

### Usability

- A developer with credentials and sample documents should reach the first answer in under 15 minutes.
- The generated RAG project should work without editing Python.
- Validation must occur before paid execution where possible.
- Errors must provide one direct remediation.

### Performance

- `aisys check` should complete within two seconds for the generated project on the release benchmark machine.
- The reference local vector index must support at least 10,000 chunks.
- Similarity search over the reference index should complete within 250 ms at p95, excluding query embedding time, on the release benchmark machine.
- Evaluation concurrency is configurable and bounded.

These are local-runtime targets; external provider latency is measured but not controlled.

## 15. Release acceptance criteria

V1 is releasable when all of the following scenarios pass on macOS and Linux.

### A. First run

1. Initialize the RAG template.
2. Configure one provider secret.
3. Validate successfully.
4. Ingest sample documents.
5. Ask a question.
6. Receive an answer with retrieved source references.

### B. Trace inspection

1. Open the latest trace.
2. See every executed node in parent/child order.
3. Inspect latency, token usage, estimated cost, inputs/outputs or artifact references, and any errors.

### C. Evaluation comparison

1. Run the bundled dataset as `baseline`.
2. Change a visible system setting.
3. Run `candidate` against the same dataset.
4. View aggregate deltas and individual regressions.
5. Open the run trace for a regressed case.

### D. Custom component

1. Add a decorated Python function.
2. Reference it from YAML.
3. Validate its typed connections.
4. Execute it and see its trace span.

### E. Reproducibility

1. Inspect a historical run.
2. Retrieve its manifest, dataset/resource IDs, prompt hashes, and environment metadata.
3. Re-execute the declared workflow when required provider models remain available.
4. Clearly report any dependency or provider version that cannot be restored.

### F. Failure handling

1. Trigger a missing secret, invalid edge, provider failure, timeout, and scorer failure.
2. Receive an actionable error and nonzero exit code.
3. Preserve a truthful run/experiment status.
4. Avoid executing downstream dependent nodes.

### G. Secret safety

Automated checks confirm that fixture secrets do not appear in:

- SQLite text fields
- Artifacts
- Console output
- Trace exports
- Generated reports

## 16. Product success measures

### V1 release measures

- 100% of runs have a manifest hash and trace.
- 100% of experiments have dataset and scorer hashes.
- 100% of comparison results link to case-level runs.
- Zero known secret leakage in release tests.
- A new user can complete init → ingest → answer → eval → compare from the documentation.

### Early product validation

- At least five target developers attempt the workflow without live assistance.
- At least four complete first answer and first evaluation.
- Median time to first successful answer is under 15 minutes after credentials are available.
- Users can correctly identify why a deliberately regressed candidate performed worse.
- At least three users prefer the connected run/eval workflow to their existing collection of scripts.

## 17. Milestones

### M0. Contracts

- System schema
- Component function contract
- Compiled manifest
- Run/span/artifact records
- Dataset/scorer/result records

### M1. Define, run, and trace

- CLI/SDK skeleton
- YAML validation
- DAG executor
- SQLite/artifact store
- Trace inspection

### M2. End-to-end RAG

- Ingest and answer workflows
- Provider integration
- Local vector index
- Source-aware answer output

### M3. Evaluate and compare

- JSONL datasets
- Deterministic scorers
- Optional rubric scorer
- Experiment comparison

### M4. Review and release

- Read-only local report
- Secret/redaction verification
- Cross-platform test
- Documentation and sample project
- Target-user validation

No dates should be assigned until M0 contracts are reviewed.

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| YAML becomes a programming language | Keep flow to DAG wiring; move custom behavior to Python |
| Built-in catalog expands without validation | Add a component only for a demonstrated workflow need |
| Mutable indexes break reproducibility | Version indexes as immutable artifacts and resolve aliases before running |
| LLM variability makes comparisons noisy | Support repetitions, show distributions, and preserve case-level outputs |
| Model judges give misleading scores | Prefer deterministic metrics and calibrate judges against human labels |
| Trace capture leaks sensitive data | Local storage, configurable capture, redaction, content-off external exports |
| Provider APIs drift | Thin adapters, preserved raw metadata, integration tests |
| Custom Python code cannot be recreated | Record import/source and lock hashes; report unresolved dependencies honestly |
| SQLite becomes an analytics bottleneck | Measure first; add DuckDB only when experiment queries require it |
| Brute-force vector search stops scaling | Publish the V1 ceiling; add FAISS/vector adapters after a measured breach |

## 19. Open product decisions

These decisions should be resolved before implementation begins:

1. Product and CLI name; `aisys` is a placeholder.
2. Open-source license and package namespace.
3. First supported OpenAI-compatible provider/model pair for the sample.
4. PDF parser choice and its licensing/installation impact.
5. Whether local prompt/response bodies are captured by default or require opt-in.
6. The exact release benchmark machine used for local performance targets.

## 20. Decision rule for new scope

A proposed feature enters V1 only if it is required to complete:

```text
Define → Execute → Trace → Evaluate → Change → Compare
```

for the reference RAG project.

Everything else waits for a demonstrated user problem or measured system limit.
