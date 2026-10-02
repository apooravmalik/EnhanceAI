"""Small local runtime, SDK, public-dataset loader, and retrieval evaluator."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import yaml


class ValidationError(ValueError):
    """Raised when a system definition cannot be run safely."""


def component(function: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a normal Python function as an Aisys component."""

    setattr(function, "__aisys_component__", True)
    return function


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    if not safe:
        raise ValidationError("A name must contain at least one letter or number.")
    return safe


@dataclass(frozen=True)
class ComponentDefinition:
    inputs: dict[str, str]
    outputs: dict[str, str]
    runner: Callable[..., Any]


@dataclass
class Node:
    name: str
    uses: str
    config: dict[str, Any]
    policy: dict[str, Any]


@dataclass
class Workflow:
    name: str
    inputs: dict[str, str]
    nodes: dict[str, Node]
    connections: list[dict[str, str]]
    outputs: dict[str, str]


@dataclass
class RunContext:
    project: Project
    run_id: str
    node_name: str
    span_id: str
    usage: dict[str, Any] = field(default_factory=dict)


class StateStore:
    """SQLite metadata plus content-addressed JSON artifacts."""

    def __init__(self, root: Path) -> None:
        state_dir = root / ".aisys"
        self.artifacts = state_dir / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(state_dir / "state.db")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              size INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resources (
              name TEXT PRIMARY KEY,
              artifact_id TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              workflow TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              manifest_json TEXT NOT NULL,
              output_json TEXT,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS spans (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              node_name TEXT NOT NULL,
              parent_id TEXT,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              input_artifact_id TEXT,
              output_artifact_id TEXT,
              usage_json TEXT,
              error TEXT,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS experiments (
              id TEXT PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              workflow TEXT NOT NULL,
              dataset_path TEXT NOT NULL,
              dataset_hash TEXT NOT NULL,
              system_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              summary_json TEXT
            );
            CREATE TABLE IF NOT EXISTS eval_results (
              experiment_id TEXT NOT NULL,
              case_id TEXT NOT NULL,
              run_id TEXT,
              status TEXT NOT NULL,
              duration_ms REAL,
              scores_json TEXT NOT NULL,
              error TEXT,
              PRIMARY KEY(experiment_id, case_id),
              FOREIGN KEY(experiment_id) REFERENCES experiments(id)
            );
            """
        )
        self.connection.commit()

    def put_artifact(self, value: Any, kind: str) -> str:
        encoded = _json(value).encode()
        artifact_id = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        target = self.artifacts / f"{artifact_id.split(':', 1)[1]}.json"
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?)",
                (artifact_id, kind, str(target.relative_to(self.artifacts.parent)), len(encoded), _now()),
            )
        return artifact_id

    def get_artifact(self, artifact_id: str) -> Any:
        row = self.connection.execute(
            "SELECT path FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"Unknown artifact {artifact_id}.")
        return json.loads((self.artifacts.parent / row["path"]).read_text())

    def set_resource(self, name: str, artifact_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO resources(name, artifact_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET artifact_id=excluded.artifact_id,
                  updated_at=excluded.updated_at
                """,
                (name, artifact_id, _now()),
            )

    def get_resource(self, name: str) -> str | None:
        row = self.connection.execute(
            "SELECT artifact_id FROM resources WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else str(row["artifact_id"])

    def create_run(self, run_id: str, workflow: str, manifest: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, 'running', ?, NULL, ?, NULL, NULL)",
                (run_id, workflow, _now(), _json(manifest)),
            )

    def finish_run(
        self, run_id: str, status: str, output: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status=?, ended_at=?, output_json=?, error=? WHERE id=?",
                (status, _now(), _json(output) if output is not None else None, error, run_id),
            )

    def create_span(self, span_id: str, run_id: str, node_name: str, parent_id: str | None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO spans VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL, NULL, NULL, NULL)",
                (span_id, run_id, node_name, parent_id, _now()),
            )

    def finish_span(
        self,
        span_id: str,
        status: str,
        input_artifact_id: str | None = None,
        output_artifact_id: str | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE spans SET status=?, ended_at=?, input_artifact_id=?, output_artifact_id=?,
                  usage_json=?, error=? WHERE id=?
                """,
                (
                    status,
                    _now(),
                    input_artifact_id,
                    output_artifact_id,
                    _json(usage or {}),
                    error,
                    span_id,
                ),
            )

    def trace(self, run_id: str) -> dict[str, Any]:
        run = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValidationError(f"Unknown run {run_id}.")
        spans = self.connection.execute(
            "SELECT * FROM spans WHERE run_id=? ORDER BY started_at, rowid", (run_id,)
        ).fetchall()
        return {"run": dict(run), "spans": [dict(span) for span in spans]}

    def latest_run_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["id"])

    def create_experiment(
        self, experiment_id: str, name: str, workflow: str, dataset_path: str, dataset_hash: str, system_hash: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, 'running', ?, NULL, NULL)",
                (experiment_id, name, workflow, dataset_path, dataset_hash, system_hash, _now()),
            )

    def add_eval_result(
        self,
        experiment_id: str,
        case_id: str,
        run_id: str | None,
        status: str,
        duration_ms: float | None,
        scores: dict[str, Any],
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, case_id, run_id, status, duration_ms, _json(scores), error),
            )

    def finish_experiment(self, experiment_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE experiments SET status=?, ended_at=?, summary_json=? WHERE id=?",
                (status, _now(), _json(summary), experiment_id),
            )

    def experiment(self, name: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM experiments WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValidationError(f"Unknown experiment {name}.")
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        result["results"] = []
        for case in self.connection.execute(
            "SELECT * FROM eval_results WHERE experiment_id=? ORDER BY case_id", (result["id"],)
        ):
            parsed = dict(case)
            parsed["scores"] = json.loads(parsed.pop("scores_json"))
            result["results"].append(parsed)
        return result


async def _load_documents(ctx: RunContext, path: str, **_: Any) -> dict[str, Any]:
    document_path = Path(path).expanduser().resolve()
    if not document_path.exists():
        raise ValidationError(f"Document file {document_path} does not exist.")
    documents = _read_jsonl(document_path)
    for document in documents:
        if not isinstance(document.get("id"), str) or not isinstance(document.get("text"), str):
            raise ValidationError("Each document row needs string id and text.")
    return {"documents": documents}


async def _chunk_documents(
    ctx: RunContext, documents: list[dict[str, Any]], size: int = 240, overlap: int = 40, **_: Any
) -> dict[str, Any]:
    if not isinstance(size, int) or not isinstance(overlap, int) or size < 20 or overlap < 0 or overlap >= size:
        raise ValidationError("Chunk size must be >= 20 and overlap must be >= 0 and smaller than size.")
    chunks: list[dict[str, Any]] = []
    for document in documents:
        words = document["text"].split()
        step = size - overlap
        for offset in range(0, max(len(words), 1), step):
            text = " ".join(words[offset : offset + size])
            if not text:
                continue
            chunks.append(
                {
                    "id": f"{document['id']}:{offset // step}",
                    "document_id": document["id"],
                    "title": document.get("title", document["id"]),
                    "text": text,
                }
            )
            if offset + size >= len(words):
                break
    return {"chunks": chunks}


async def _index_chunks(ctx: RunContext, chunks: list[dict[str, Any]], name: str = "default", **_: Any) -> dict[str, Any]:
    if not chunks:
        raise ValidationError("Cannot index zero chunks.")
    resource_name = _safe_name(name)
    index_id = ctx.project.store.put_artifact(
        {"name": resource_name, "chunks": chunks, "created_at": _now()}, "retrieval-index"
    )
    ctx.project.store.set_resource(resource_name, index_id)
    return {"index_id": index_id}


async def _retrieve_chunks(
    ctx: RunContext,
    question: str,
    name: str = "default",
    top_k: int = 5,
    resolved_index_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise ValidationError("Question must be a non-empty string.")
    if not isinstance(top_k, int) or top_k < 1:
        raise ValidationError("top_k must be a positive integer.")
    index_id = resolved_index_id or ctx.project.store.get_resource(_safe_name(name))
    if index_id is None:
        raise ValidationError(f"Resource {name!r} is not built.")
    payload = ctx.project.store.get_artifact(index_id)
    ranked = _rank_chunks(question, payload["chunks"], top_k)
    ctx.usage = {"retrieved_chunks": len(ranked), "index_id": index_id}
    return {"chunks": ranked}


async def _extractive_answer(
    ctx: RunContext, question: str, chunks: list[dict[str, Any]], **_: Any
) -> dict[str, Any]:
    if not chunks:
        return {"answer": "No relevant context was retrieved."}
    context = chunks[0]["text"]
    ctx.usage = {"input_words": len(_tokenize(question)), "output_words": len(_tokenize(context))}
    return {"answer": context}


async def _openai_compatible_answer(
    ctx: RunContext,
    question: str,
    chunks: list[dict[str, Any]],
    model: str,
    api_key_env: str,
    base_url: str = "https://api.openai.com/v1",
    prompt: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValidationError(f"Missing provider credential in environment variable {api_key_env}.")
    prompt_text = (
        (ctx.project.root / prompt).read_text() if prompt else "Answer using only the supplied context."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_text},
            {
                "role": "user",
                "content": f"Question: {question}\n\nContext:\n"
                + "\n\n".join(f"[{chunk['document_id']}] {chunk['text']}" for chunk in chunks),
            },
        ],
        "temperature": 0,
    }
    request = urllib.request.Request(
        urllib.parse.urljoin(base_url.rstrip("/") + "/", "chat/completions"),
        data=_json(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    def send() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    response = await asyncio.to_thread(send)
    usage = response.get("usage", {})
    ctx.usage = {
        "provider": "openai-compatible",
        "model": model,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    return {"answer": response["choices"][0]["message"]["content"]}


def _builtin_components() -> dict[str, ComponentDefinition]:
    return {
        "documents.load": ComponentDefinition({"path": "path"}, {"documents": "documents"}, _load_documents),
        "documents.chunk": ComponentDefinition(
            {"documents": "documents"}, {"chunks": "chunks"}, _chunk_documents
        ),
        "retrieval.index": ComponentDefinition(
            {"chunks": "chunks"}, {"index_id": "index"}, _index_chunks
        ),
        "retrieval.vector": ComponentDefinition(
            {"question": "string"}, {"chunks": "chunks"}, _retrieve_chunks
        ),
        "answer.extractive": ComponentDefinition(
            {"question": "string", "chunks": "chunks"}, {"answer": "string"}, _extractive_answer
        ),
        "llm.openai_compatible": ComponentDefinition(
            {"question": "string", "chunks": "chunks"}, {"answer": "string"}, _openai_compatible_answer
        ),
    }


class Project:
    """A local Aisys project loaded from a system.yaml definition."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.system_path = self.root / "system.yaml"
        if not self.system_path.exists():
            raise ValidationError(f"No system.yaml found in {self.root}.")
        self.store = StateStore(self.root)
        self._builtins = _builtin_components()

    @classmethod
    def load(cls, root: str | Path = ".") -> Project:
        return cls(root)

    @classmethod
    def init(cls, root: str | Path, name: str = "document-qa") -> Path:
        destination = Path(root).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ValidationError(f"{destination} is not empty.")
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "evals").mkdir()
        (destination / "prompts").mkdir()
        (destination / "data").mkdir()
        (destination / ".gitignore").write_text(".aisys/\ndata/\n__pycache__/\n")
        (destination / "pyproject.toml").write_text(
            """
[project]
name = "document-qa"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["aisys"]
""".lstrip()
        )
        (destination / "prompts" / "answer.md").write_text(
            "Answer only from the retrieved context. Cite the retrieved source IDs.\n"
        )
        (destination / "system.yaml").write_text(
            f"""
version: 1
name: {_safe_name(name)}
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
          size: 240
          overlap: 40
      index:
        uses: retrieval.index
        with:
          name: default
    connections:
      - from: $input.path
        to: load.path
      - from: load.documents
        to: chunk.documents
      - from: chunk.chunks
        to: index.chunks
    output:
      index_id: index.index_id

  answer:
    input:
      question: string
    nodes:
      retrieve:
        uses: retrieval.vector
        with:
          name: default
          top_k: 5
      answer:
        uses: answer.extractive
    connections:
      - from: $input.question
        to: retrieve.question
      - from: $input.question
        to: answer.question
      - from: retrieve.chunks
        to: answer.chunks
    output:
      answer: answer.answer
      sources: retrieve.chunks
""".lstrip()
        )
        (destination / "data" / "sample_documents.jsonl").write_text(
            "\n".join(
                [
                    _json(
                        {
                            "id": "returns",
                            "title": "Returns",
                            "text": "Opened items can be returned within fourteen days with proof of purchase.",
                        }
                    ),
                    _json(
                        {
                            "id": "shipping",
                            "title": "Shipping",
                            "text": "Standard shipping takes three to five business days.",
                        }
                    ),
                ]
            )
            + "\n"
        )
        (destination / "evals" / "golden.jsonl").write_text(
            _json(
                {
                    "id": "returns-001",
                    "input": {"question": "Can I return an opened item?"},
                    "expected": {"source_ids": ["returns"]},
                    "metadata": {"slice": "policy"},
                }
            )
            + "\n"
        )
        return destination

    def _definition(self) -> dict[str, Any]:
        try:
            value = yaml.safe_load(self.system_path.read_text())
        except yaml.YAMLError as error:
            raise ValidationError(f"Invalid YAML: {error}") from error
        if not isinstance(value, dict):
            raise ValidationError("system.yaml must contain an object.")
        return value

    def _custom_component(self, uses: str) -> ComponentDefinition:
        _, module_name, function_name = uses.split(":", 2)
        original_path = list(sys.path)
        sys.path.insert(0, str(self.root))
        try:
            module = importlib.import_module(module_name)
            function = getattr(module, function_name)
        except (ImportError, AttributeError) as error:
            raise ValidationError(f"Cannot load custom component {uses}: {error}") from error
        finally:
            sys.path[:] = original_path
        if not getattr(function, "__aisys_component__", False):
            raise ValidationError(f"{uses} must be marked with @component.")
        signature = inspect.signature(function)
        inputs = {
            name: "any"
            for name, parameter in signature.parameters.items()
            if name != "ctx" and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        }
        return ComponentDefinition(inputs, {"result": "any"}, function)

    def _component(self, uses: str) -> ComponentDefinition:
        if uses in self._builtins:
            return self._builtins[uses]
        if uses.startswith("python:") and uses.count(":") == 2:
            return self._custom_component(uses)
        raise ValidationError(f"Unknown component {uses}. Use the components command for built-ins.")

    def workflows(self) -> dict[str, Workflow]:
        definition = self._definition()
        if definition.get("version") != 1:
            raise ValidationError("system.yaml requires version: 1.")
        raw_workflows = definition.get("workflows")
        if not isinstance(raw_workflows, dict) or not raw_workflows:
            raise ValidationError("system.yaml requires at least one workflow.")
        workflows: dict[str, Workflow] = {}
        for workflow_name, raw_workflow in raw_workflows.items():
            if not isinstance(raw_workflow, dict):
                raise ValidationError(f"workflows.{workflow_name} must be an object.")
            raw_nodes = raw_workflow.get("nodes")
            if not isinstance(raw_nodes, dict) or not raw_nodes:
                raise ValidationError(f"workflows.{workflow_name}.nodes must be a non-empty object.")
            nodes: dict[str, Node] = {}
            for node_name, raw_node in raw_nodes.items():
                if not isinstance(raw_node, dict) or not isinstance(raw_node.get("uses"), str):
                    raise ValidationError(f"workflows.{workflow_name}.nodes.{node_name} needs a uses value.")
                config = raw_node.get("with", {})
                policy = raw_node.get("policy", {})
                if not isinstance(config, dict) or not isinstance(policy, dict):
                    raise ValidationError(f"Node {workflow_name}.{node_name} has invalid with or policy.")
                nodes[node_name] = Node(node_name, raw_node["uses"], config, policy)
            inputs = raw_workflow.get("input", {})
            outputs = raw_workflow.get("output", {})
            connections = raw_workflow.get("connections", [])
            if not isinstance(inputs, dict) or not isinstance(outputs, dict) or not isinstance(connections, list):
                raise ValidationError(f"Workflow {workflow_name} has invalid input, output, or connections.")
            workflows[workflow_name] = Workflow(workflow_name, inputs, nodes, connections, outputs)
        return workflows

    def validate(self) -> dict[str, Any]:
        workflows = self.workflows()
        for workflow in workflows.values():
            component_definitions = {name: self._component(node.uses) for name, node in workflow.nodes.items()}
            incoming: dict[str, set[str]] = {name: set() for name in workflow.nodes}
            dependencies: dict[str, set[str]] = {name: set() for name in workflow.nodes}
            for connection in workflow.connections:
                if set(connection) != {"from", "to"}:
                    raise ValidationError(f"Workflow {workflow.name} connections require from and to.")
                source, destination = connection["from"], connection["to"]
                if not isinstance(source, str) or not isinstance(destination, str) or "." not in destination:
                    raise ValidationError(f"Workflow {workflow.name} has malformed connection {connection}.")
                destination_node, destination_port = destination.split(".", 1)
                if destination_node not in workflow.nodes:
                    raise ValidationError(f"Connection destination {destination_node} does not exist.")
                if destination_port not in component_definitions[destination_node].inputs:
                    raise ValidationError(f"{destination} is not an input port.")
                incoming[destination_node].add(destination_port)
                if source.startswith("$input."):
                    input_name = source.removeprefix("$input.")
                    if input_name not in workflow.inputs:
                        raise ValidationError(f"{source} is not a declared workflow input.")
                else:
                    if "." not in source:
                        raise ValidationError(f"Connection source {source} is malformed.")
                    source_node, source_port = source.split(".", 1)
                    if source_node not in workflow.nodes:
                        raise ValidationError(f"Connection source {source_node} does not exist.")
                    if source_port not in component_definitions[source_node].outputs:
                        raise ValidationError(f"{source} is not an output port.")
                    dependencies[destination_node].add(source_node)
                    source_type = component_definitions[source_node].outputs[source_port]
                    destination_type = component_definitions[destination_node].inputs[destination_port]
                    if source_type != "any" and destination_type != "any" and source_type != destination_type:
                        raise ValidationError(
                            f"Type mismatch: {source} ({source_type}) cannot connect to {destination} ({destination_type})."
                        )
            for node_name, definition in component_definitions.items():
                missing = set(definition.inputs) - incoming[node_name]
                if missing:
                    raise ValidationError(
                        f"Node {workflow.name}.{node_name} is missing inputs: {', '.join(sorted(missing))}."
                    )
                retries = workflow.nodes[node_name].policy.get("retries", 0)
                timeout = workflow.nodes[node_name].policy.get("timeout_seconds")
                if not isinstance(retries, int) or retries < 0:
                    raise ValidationError(f"Node {workflow.name}.{node_name} retries must be a non-negative integer.")
                if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
                    raise ValidationError(f"Node {workflow.name}.{node_name} timeout_seconds must be positive.")
            remaining = {name: set(parents) for name, parents in dependencies.items()}
            resolved: set[str] = set()
            while len(resolved) < len(remaining):
                ready = {name for name, parents in remaining.items() if name not in resolved and parents <= resolved}
                if not ready:
                    raise ValidationError(f"Workflow {workflow.name} contains a cycle; V1 supports DAGs only.")
                resolved.update(ready)
            for name, source in workflow.outputs.items():
                if not isinstance(source, str) or "." not in source:
                    raise ValidationError(f"Workflow output {name} must reference node.output.")
                node_name, output_name = source.split(".", 1)
                if node_name not in workflow.nodes or output_name not in component_definitions[node_name].outputs:
                    raise ValidationError(f"Workflow output {name} references unknown {source}.")
        return {"status": "valid", "workflows": sorted(workflows)}

    def _manifest(self, workflow: Workflow, inputs: dict[str, Any]) -> dict[str, Any]:
        nodes: dict[str, Any] = {}
        for node_name, node in workflow.nodes.items():
            config = dict(node.config)
            if node.uses == "retrieval.vector":
                resource_name = str(config.get("name", "default"))
                index_id = self.store.get_resource(resource_name)
                if index_id is None:
                    raise ValidationError(
                        f"Resource {resource_name!r} is not built. Run ingest before querying."
                    )
                config["resolved_index_id"] = index_id
            nodes[node_name] = {"uses": node.uses, "with": config, "policy": node.policy}
        manifest = {
            "system_hash": _file_hash(self.system_path),
            "workflow": workflow.name,
            "inputs": inputs,
            "nodes": nodes,
            "created_at": _now(),
            "python": sys.version,
        }
        manifest["manifest_hash"] = _hash(manifest)
        return manifest

    def run(self, workflow_name: str, input: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._run(workflow_name, input))

    async def _run(self, workflow_name: str, input: dict[str, Any]) -> dict[str, Any]:
        self.validate()
        workflows = self.workflows()
        if workflow_name not in workflows:
            raise ValidationError(f"Unknown workflow {workflow_name}.")
        workflow = workflows[workflow_name]
        missing_inputs = set(workflow.inputs) - set(input)
        if missing_inputs:
            raise ValidationError(f"Missing workflow inputs: {', '.join(sorted(missing_inputs))}.")
        manifest = self._manifest(workflow, input)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        root_span = f"span_{uuid.uuid4().hex[:12]}"
        self.store.create_run(run_id, workflow_name, manifest)
        self.store.create_span(root_span, run_id, workflow_name, None)
        started = time.perf_counter()
        try:
            node_outputs = await self._execute_workflow(workflow, run_id, root_span, manifest, input)
            outputs = {
                output_name: self._resolve_source(source, input, node_outputs)
                for output_name, source in workflow.outputs.items()
            }
            self.store.finish_span(root_span, "completed")
            result = {
                "id": run_id,
                "workflow": workflow_name,
                "status": "completed",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "manifest_hash": manifest["manifest_hash"],
                "outputs": outputs,
            }
            self.store.finish_run(run_id, "completed", result)
            return result
        except Exception as error:
            message = str(error)
            self.store.finish_span(root_span, "failed", error=message)
            self.store.finish_run(run_id, "failed", error=message)
            raise

    async def _execute_workflow(
        self,
        workflow: Workflow,
        run_id: str,
        root_span: str,
        manifest: dict[str, Any],
        workflow_input: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        definitions = {name: self._component(node.uses) for name, node in workflow.nodes.items()}
        dependencies: dict[str, set[str]] = {name: set() for name in workflow.nodes}
        inbound: dict[str, dict[str, str]] = {name: {} for name in workflow.nodes}
        for connection in workflow.connections:
            source, destination = connection["from"], connection["to"]
            destination_node, destination_port = destination.split(".", 1)
            inbound[destination_node][destination_port] = source
            if not source.startswith("$input."):
                dependencies[destination_node].add(source.split(".", 1)[0])
        pending = set(workflow.nodes)
        complete: dict[str, dict[str, Any]] = {}
        while pending:
            ready = sorted(name for name in pending if dependencies[name] <= complete.keys())
            if not ready:
                raise ValidationError(f"Workflow {workflow.name} cannot schedule remaining nodes.")
            results = await asyncio.gather(
                *[
                    self._execute_node(
                        workflow.nodes[name],
                        definitions[name],
                        inbound[name],
                        complete,
                        workflow_input,
                        run_id,
                        root_span,
                        manifest["nodes"][name],
                    )
                    for name in ready
                ]
            )
            complete.update(dict(zip(ready, results, strict=True)))
            pending.difference_update(ready)
        return complete

    async def _execute_node(
        self,
        node: Node,
        definition: ComponentDefinition,
        inbound: dict[str, str],
        complete: dict[str, dict[str, Any]],
        workflow_input: dict[str, Any],
        run_id: str,
        root_span: str,
        node_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        arguments = {
            port: self._resolve_source(source, workflow_input, complete) for port, source in inbound.items()
        }
        arguments.update(node_manifest["with"])
        span_id = f"span_{uuid.uuid4().hex[:12]}"
        self.store.create_span(span_id, run_id, node.name, root_span)
        input_artifact = self.store.put_artifact(arguments, "node-input")
        context = RunContext(self, run_id, node.name, span_id)
        attempts = int(node.policy.get("retries", 0)) + 1
        timeout = node.policy.get("timeout_seconds")
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                result = definition.runner(context, **arguments)
                if inspect.isawaitable(result):
                    if timeout is not None:
                        result = await asyncio.wait_for(result, timeout=float(timeout))
                    else:
                        result = await result
                if not isinstance(result, dict):
                    result = {"result": result}
                unexpected = set(result) - set(definition.outputs)
                if unexpected:
                    raise ValidationError(
                        f"{node.name} returned undeclared outputs: {', '.join(sorted(unexpected))}."
                    )
                missing = set(definition.outputs) - set(result)
                if missing:
                    raise ValidationError(
                        f"{node.name} did not return outputs: {', '.join(sorted(missing))}."
                    )
                output_artifact = self.store.put_artifact(result, "node-output")
                usage = {**context.usage, "attempts": attempt + 1}
                self.store.finish_span(span_id, "completed", input_artifact, output_artifact, usage)
                return result
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        assert last_error is not None
        self.store.finish_span(span_id, "failed", input_artifact, error=str(last_error))
        raise last_error

    @staticmethod
    def _resolve_source(
        source: str, workflow_input: dict[str, Any], node_outputs: dict[str, dict[str, Any]]
    ) -> Any:
        if source.startswith("$input."):
            return workflow_input[source.removeprefix("$input.")]
        node_name, output_name = source.split(".", 1)
        return node_outputs[node_name][output_name]

    def components(self) -> dict[str, dict[str, dict[str, str]]]:
        return {
            name: {"inputs": definition.inputs, "outputs": definition.outputs}
            for name, definition in sorted(self._builtins.items())
        }

    def graph(self, workflow_name: str | None = None) -> str:
        workflows = self.workflows()
        selected = [workflow_name] if workflow_name else sorted(workflows)
        lines = ["flowchart LR"]
        for name in selected:
            if name not in workflows:
                raise ValidationError(f"Unknown workflow {name}.")
            workflow = workflows[name]
            lines.append(f"  subgraph {workflow.name}")
            for node in workflow.nodes.values():
                lines.append(f'    {workflow.name}_{node.name}["{node.name}: {node.uses}"]')
            for connection in workflow.connections:
                source, destination = connection["from"], connection["to"]
                if source.startswith("$input."):
                    lines.append(
                        f'    {workflow.name}_input_{source.removeprefix("$input.")}["{source}"] --> '
                        f"{workflow.name}_{destination.split('.', 1)[0]}"
                    )
                else:
                    lines.append(
                        f"    {workflow.name}_{source.split('.', 1)[0]} --> "
                        f"{workflow.name}_{destination.split('.', 1)[0]}"
                    )
            lines.append("  end")
        return "\n".join(lines)

    def fetch_dataset(self, dataset: str, limit: int = 60) -> dict[str, Any]:
        if dataset not in {"squad", "hotpotqa"}:
            raise ValidationError("Public datasets supported in V1 are squad and hotpotqa.")
        if not 1 <= limit <= 100:
            raise ValidationError("Dataset limit must be between 1 and 100.")
        details = {
            "squad": ("rajpurkar/squad", "plain_text", "validation"),
            "hotpotqa": ("hotpotqa/hotpot_qa", "distractor", "validation"),
        }[dataset]
        rows, total = _sample_viewer_rows(*details, limit)
        data_dir = self.root / "data" / dataset
        eval_dir = self.root / "evals"
        data_dir.mkdir(parents=True, exist_ok=True)
        eval_dir.mkdir(parents=True, exist_ok=True)
        if dataset == "squad":
            documents, cases = _squad_records(rows)
        else:
            documents, cases = _hotpot_records(rows)
        documents_path = data_dir / "documents.jsonl"
        evaluation_path = eval_dir / f"{dataset}.jsonl"
        _write_jsonl(documents_path, documents)
        _write_jsonl(evaluation_path, cases)
        metadata = {
            "dataset": details[0],
            "config": details[1],
            "split": details[2],
            "requested_limit": limit,
            "sampled_rows": len(rows),
            "viewer_total_rows": total,
            "retrieved_at": _now(),
            "documents_hash": _file_hash(documents_path),
            "evaluation_hash": _file_hash(evaluation_path),
            "api": "https://datasets-server.huggingface.co/rows",
        }
        metadata_path = data_dir / "source.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        return {
            "dataset": dataset,
            "documents": str(documents_path),
            "evaluation": str(evaluation_path),
            "metadata": str(metadata_path),
            **metadata,
        }

    def evaluate(self, workflow_name: str, dataset_path: str | Path, name: str) -> dict[str, Any]:
        self.validate()
        safe_name = _safe_name(name)
        path = Path(dataset_path)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            raise ValidationError(f"Dataset {path} does not exist.")
        cases = _read_jsonl(path)
        if not cases:
            raise ValidationError("Evaluation dataset is empty.")
        for case in cases:
            if not isinstance(case.get("id"), str) or not isinstance(case.get("input"), dict):
                raise ValidationError("Each evaluation row needs string id and object input.")
        experiment_id = f"exp_{uuid.uuid4().hex[:12]}"
        dataset_hash = _file_hash(path)
        system_hash = _file_hash(self.system_path)
        self.store.create_experiment(experiment_id, safe_name, workflow_name, str(path), dataset_hash, system_hash)
        metric_values: dict[str, list[float]] = {
            "source_recall_at_k": [],
            "hit_rate_at_k": [],
            "first_relevant_rank": [],
        }
        failed = 0
        for case in cases:
            started = time.perf_counter()
            try:
                run = self.run(workflow_name, case["input"])
                chunks = run["outputs"].get("sources", [])
                retrieved = [chunk.get("document_id") for chunk in chunks]
                expected = set(case.get("expected", {}).get("source_ids", []))
                rank = next((index + 1 for index, source_id in enumerate(retrieved) if source_id in expected), None)
                covered = expected.intersection(retrieved)
                scores = {
                    "source_recall_at_k": len(covered) / len(expected) if expected else 0.0,
                    "hit_rate_at_k": 1.0 if rank is not None else 0.0,
                    "first_relevant_rank": float(rank or 0),
                    "retrieved_count": float(len(retrieved)),
                }
                metric_values["source_recall_at_k"].append(scores["source_recall_at_k"])
                metric_values["hit_rate_at_k"].append(scores["hit_rate_at_k"])
                if rank is not None:
                    metric_values["first_relevant_rank"].append(float(rank))
                self.store.add_eval_result(
                    experiment_id,
                    case["id"],
                    run["id"],
                    "completed",
                    (time.perf_counter() - started) * 1000,
                    scores,
                )
            except Exception as error:
                failed += 1
                self.store.add_eval_result(
                    experiment_id,
                    case["id"],
                    None,
                    "failed",
                    (time.perf_counter() - started) * 1000,
                    {
                        "source_recall_at_k": 0.0,
                        "hit_rate_at_k": 0.0,
                        "first_relevant_rank": 0.0,
                        "retrieved_count": 0.0,
                    },
                    str(error),
                )
                metric_values["source_recall_at_k"].append(0.0)
        summary = {
            "cases": len(cases),
            "failed_cases": failed,
            "source_recall_at_k": _mean(metric_values["source_recall_at_k"]),
            "hit_rate_at_k": _mean(metric_values["hit_rate_at_k"]),
            "mean_first_relevant_rank": _mean(metric_values["first_relevant_rank"]),
            "dataset_hash": dataset_hash,
            "system_hash": system_hash,
        }
        self.store.finish_experiment(experiment_id, "completed" if not failed else "completed_with_failures", summary)
        report_path = self._write_experiment_report(safe_name)
        return {"id": experiment_id, "name": safe_name, "report": str(report_path), **summary}

    def compare(self, baseline_name: str, candidate_name: str) -> dict[str, Any]:
        baseline = self.store.experiment(baseline_name)
        candidate = self.store.experiment(candidate_name)
        baseline_cases = {result["case_id"]: result for result in baseline["results"]}
        candidate_cases = {result["case_id"]: result for result in candidate["results"]}
        labels = {"improved": 0, "regressed": 0, "unchanged": 0, "added": 0, "removed": 0}
        for case_id in sorted(set(baseline_cases) | set(candidate_cases)):
            before, after = baseline_cases.get(case_id), candidate_cases.get(case_id)
            if before is None:
                labels["added"] += 1
                continue
            if after is None:
                labels["removed"] += 1
                continue
            before_score = float(before["scores"].get("source_recall_at_k", 0))
            after_score = float(after["scores"].get("source_recall_at_k", 0))
            if after_score > before_score:
                labels["improved"] += 1
            elif after_score < before_score:
                labels["regressed"] += 1
            else:
                labels["unchanged"] += 1
        result = {
            "baseline": baseline_name,
            "candidate": candidate_name,
            "same_dataset": baseline["dataset_hash"] == candidate["dataset_hash"],
            "source_recall_at_k_delta": round(
                candidate["summary"].get("source_recall_at_k", 0)
                - baseline["summary"].get("source_recall_at_k", 0),
                6,
            ),
            "mean_first_relevant_rank_delta": round(
                candidate["summary"].get("mean_first_relevant_rank", 0)
                - baseline["summary"].get("mean_first_relevant_rank", 0),
                6,
            ),
            "cases": labels,
        }
        report_dir = self.root / "reports"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / f"compare-{_safe_name(baseline_name)}-to-{_safe_name(candidate_name)}.md"
        report_path.write_text(
            "\n".join(
                [
                    f"# Comparison: {baseline_name} → {candidate_name}",
                    "",
                    f"- Same dataset snapshot: {result['same_dataset']}",
                    f"- Source recall@k delta: {result['source_recall_at_k_delta']:+.4f}",
                    f"- Mean first relevant rank delta: {result['mean_first_relevant_rank_delta']:+.4f}",
                    f"- Improved: {labels['improved']}",
                    f"- Regressed: {labels['regressed']}",
                    f"- Unchanged: {labels['unchanged']}",
                    f"- Added/removed: {labels['added']}/{labels['removed']}",
                    "",
                ]
            )
        )
        result["report"] = str(report_path)
        return result

    def _write_experiment_report(self, name: str) -> Path:
        experiment = self.store.experiment(name)
        report_dir = self.root / "reports"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / f"{_safe_name(name)}.md"
        summary = experiment["summary"]
        failed_cases = [result for result in experiment["results"] if result["status"] != "completed"]
        misses = [
            result
            for result in experiment["results"]
            if result["status"] == "completed" and result["scores"].get("source_recall_at_k") == 0
        ]
        report_path.write_text(
            "\n".join(
                [
                    f"# Retrieval Evaluation: {name}",
                    "",
                    f"- Workflow: {experiment['workflow']}",
                    f"- Dataset: {experiment['dataset_path']}",
                    f"- Dataset hash: {experiment['dataset_hash']}",
                    f"- System hash: {experiment['system_hash']}",
                    f"- Cases: {summary.get('cases', 0)}",
                    f"- Failed cases: {summary.get('failed_cases', 0)}",
                    f"- Mean gold-source recall@k: {summary.get('source_recall_at_k', 0):.4f}",
                    f"- Case hit rate@k: {summary.get('hit_rate_at_k', 0):.4f}",
                    f"- Mean first relevant rank: {summary.get('mean_first_relevant_rank', 0):.4f}",
                    "",
                    "## Retrieval misses",
                    "",
                ]
                + [f"- {result['case_id']} (run {result['run_id']})" for result in misses[:20]]
                + (["- None"] if not misses else [])
                + ["", "## Failed cases", ""]
                + [f"- {result['case_id']}: {result['error']}" for result in failed_cases[:20]]
                + (["- None"] if not failed_cases else [])
                + [""]
            )
        )
        return report_path


def _rank_chunks(question: str, chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    # ponytail: in-memory O(chunks × query_terms) scorer; add a persisted ANN index only after a corpus benchmark fails.
    document_frequencies: dict[str, int] = {}
    tokenized = [_tokenize(chunk["text"]) for chunk in chunks]
    for tokens in tokenized:
        for token in set(tokens):
            document_frequencies[token] = document_frequencies.get(token, 0) + 1
    count = len(chunks)
    query = _tokenize(question)
    query_weights = {
        token: (1 + math.log(query.count(token))) * (math.log((1 + count) / (1 + document_frequencies.get(token, 0))) + 1)
        for token in set(query)
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values())) or 1.0
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, tokens in zip(chunks, tokenized, strict=True):
        term_counts = {token: tokens.count(token) for token in set(tokens)}
        weights = {
            token: (1 + math.log(frequency))
            * (math.log((1 + count) / (1 + document_frequencies.get(token, 0))) + 1)
            for token, frequency in term_counts.items()
        }
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        score = sum(query_weights.get(token, 0.0) * weight for token, weight in weights.items()) / (query_norm * norm)
        scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**chunk, "score": round(score, 8)} for score, chunk in scored[:top_k]]


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "aisys/0.1"})
    try:
        context = ssl.create_default_context()
        host_bundle = Path("/etc/ssl/cert.pem")
        if host_bundle.exists():
            context.load_verify_locations(cafile=str(host_bundle))
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            return json.loads(response.read())
    except Exception as error:
        raise ValidationError(f"Public dataset request failed: {error}") from error


def _sample_viewer_rows(dataset: str, config: str, split: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    endpoint = "https://datasets-server.huggingface.co/rows"
    initial = _request_json(
        f"{endpoint}?{urllib.parse.urlencode({'dataset': dataset, 'config': config, 'split': split, 'offset': 0, 'length': 1})}"
    )
    total = int(initial.get("num_rows_total", 0))
    if total < 1:
        raise ValidationError(f"Dataset {dataset} has no available rows in {config}/{split}.")
    pages = min(10, limit)
    page_size = max(1, math.ceil(limit / pages))
    offsets = {
        0 if pages == 1 else round(index * max(total - page_size, 0) / (pages - 1))
        for index in range(pages)
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offset in sorted(offsets):
        response = _request_json(
            f"{endpoint}?{urllib.parse.urlencode({'dataset': dataset, 'config': config, 'split': split, 'offset': offset, 'length': page_size})}"
        )
        for item in response.get("rows", []):
            row = item.get("row", {})
            row_id = str(row.get("id", item.get("row_idx")))
            if row_id not in seen:
                seen.add(row_id)
                selected.append(row)
            if len(selected) >= limit:
                return selected, total
    return selected, total


def _squad_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for row in rows:
        context = str(row["context"])
        document_id = f"squad:{hashlib.sha256(context.encode()).hexdigest()[:16]}"
        documents.setdefault(
            document_id, {"id": document_id, "title": row.get("title", ""), "text": context}
        )
        cases.append(
            {
                "id": f"squad:{row['id']}",
                "input": {"question": row["question"]},
                "expected": {
                    "source_ids": [document_id],
                    "answer": (row.get("answers", {}).get("text") or [""])[0],
                },
                "metadata": {"dataset": "rajpurkar/squad", "title": row.get("title", "")},
            }
        )
    return list(documents.values()), cases


def _hotpot_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for row in rows:
        context = row["context"]
        titles = context["title"]
        sentences = context["sentences"]
        for title, paragraph in zip(titles, sentences, strict=True):
            document_id = f"hotpot:{hashlib.sha256(str(title).encode()).hexdigest()[:16]}"
            documents.setdefault(
                document_id, {"id": document_id, "title": title, "text": " ".join(paragraph)}
            )
        support_titles = set(row.get("supporting_facts", {}).get("title", []))
        expected_ids = [
            f"hotpot:{hashlib.sha256(str(title).encode()).hexdigest()[:16]}" for title in sorted(support_titles)
        ]
        cases.append(
            {
                "id": f"hotpot:{row['id']}",
                "input": {"question": row["question"]},
                "expected": {"source_ids": expected_ids, "answer": row.get("answer", "")},
                "metadata": {
                    "dataset": "hotpotqa/hotpot_qa",
                    "type": row.get("type", ""),
                    "level": row.get("level", ""),
                },
            }
        )
    return list(documents.values()), cases


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{path}:{line_number} is not valid JSONL: {error}") from error
        if not isinstance(value, dict):
            raise ValidationError(f"{path}:{line_number} must be a JSON object.")
        rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(_json(row) for row in rows) + "\n")


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0
