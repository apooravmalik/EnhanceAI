"""Small local runtime, SDK, public-dataset loader, and retrieval evaluator."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import inspect
import itertools
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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError

import yaml


class ValidationError(ValueError):
    """Raised when a system definition cannot be run safely."""


def component(
    function: Callable[..., Any] | None = None,
    *,
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
) -> Callable[..., Any]:
    """Mark a Python function as a component, optionally declaring typed ports."""

    def decorate(candidate: Callable[..., Any]) -> Callable[..., Any]:
        setattr(candidate, "__aisys_component__", True)
        setattr(candidate, "__aisys_inputs__", inputs)
        setattr(candidate, "__aisys_outputs__", outputs)
        return candidate

    return decorate(function) if function is not None else decorate


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
            CREATE TABLE IF NOT EXISTS index_entries (
              index_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY(index_id, ordinal)
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

    def put_index_entries(self, index_id: str, entries: list[dict[str, Any]]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM index_entries WHERE index_id=?", (index_id,))
            self.connection.executemany(
                "INSERT INTO index_entries(index_id, ordinal, payload_json) VALUES (?, ?, ?)",
                [(index_id, ordinal, _json(entry)) for ordinal, entry in enumerate(entries)],
            )

    def get_index_entries(self, index_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM index_entries WHERE index_id=? ORDER BY ordinal", (index_id,)
            )
        ]

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

    def telemetry(self, run_id: str) -> dict[str, Any]:
        trace = self.trace(run_id)
        run = trace["run"]
        nodes: dict[str, dict[str, Any]] = {}
        totals: dict[str, float] = {}
        for span in trace["spans"]:
            if span["parent_id"] is None:
                continue
            name = str(span["node_name"])
            item = nodes.setdefault(name, {"spans": 0, "completed": 0, "failed": 0, "duration_ms": 0.0, "usage": {}})
            item["spans"] += 1
            item["completed" if span["status"] == "completed" else "failed"] += 1
            duration = _duration_ms(span["started_at"], span["ended_at"])
            item["duration_ms"] += duration
            usage = json.loads(span["usage_json"] or "{}")
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    item["usage"][key] = round(item["usage"].get(key, 0.0) + value, 6)
                    totals[key] = round(totals.get(key, 0.0) + value, 6)
        for item in nodes.values():
            item["duration_ms"] = round(item["duration_ms"], 3)
            item["mean_duration_ms"] = round(item["duration_ms"] / item["spans"], 3)
        return {
            "run_id": run_id,
            "workflow": run["workflow"],
            "status": run["status"],
            "duration_ms": round(_duration_ms(run["started_at"], run["ended_at"]), 3),
            "nodes": nodes,
            "usage": totals,
        }

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


async def _index_chunks(
    ctx: RunContext,
    chunks: list[dict[str, Any]],
    name: str = "default",
    storage: str = "artifact",
    dimensions: int = 128,
    **_: Any,
) -> dict[str, Any]:
    if not chunks:
        raise ValidationError("Cannot index zero chunks.")
    if storage not in {"artifact", "sqlite"}:
        raise ValidationError("Index storage must be artifact or sqlite.")
    if not isinstance(dimensions, int) or not 8 <= dimensions <= 8192:
        raise ValidationError("Index dimensions must be an integer from 8 through 8192.")
    resource_name = _safe_name(name)
    entries = [{"chunk": chunk, "vector": _hashed_vector(chunk["text"], dimensions)} for chunk in chunks]
    storage_bytes = len(_json(entries).encode())
    payload: dict[str, Any] = {
        "name": resource_name,
        "storage": storage,
        "dimensions": dimensions,
        "chunk_count": len(chunks),
        "storage_bytes": storage_bytes,
        "created_at": _now(),
    }
    if storage == "artifact":
        payload["entries"] = entries
    index_id = ctx.project.store.put_artifact(payload, "retrieval-index")
    if storage == "sqlite":
        ctx.project.store.put_index_entries(index_id, entries)
    ctx.project.store.set_resource(resource_name, index_id)
    ctx.usage = {"index_id": index_id, "storage": storage, "dimensions": dimensions, "storage_bytes": storage_bytes}
    return {
        "index_id": index_id,
        "index_info": {
            "storage": storage,
            "dimensions": dimensions,
            "chunks": len(chunks),
            "storage_bytes": storage_bytes,
        },
    }


def _index_entries(ctx: RunContext, index_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = ctx.project.store.get_artifact(index_id)
    entries = payload.get("entries") if payload.get("storage", "artifact") == "artifact" else None
    if entries is None:
        entries = ctx.project.store.get_index_entries(index_id)
    if not isinstance(entries, list) or not entries:
        raise ValidationError(f"Index {index_id} has no retrievable entries.")
    return payload, entries


def _resource_index_id(ctx: RunContext, name: str, resolved_index_id: str | None) -> str:
    index_id = resolved_index_id or ctx.project.store.get_resource(_safe_name(name))
    if index_id is None:
        raise ValidationError(f"Resource {name!r} is not built.")
    return index_id


async def _retrieve_bm25(
    ctx: RunContext,
    question: str,
    name: str = "default",
    top_k: int = 5,
    k1: float = 1.2,
    b: float = 0.75,
    resolved_index_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise ValidationError("Question must be a non-empty string.")
    if not isinstance(top_k, int) or top_k < 1:
        raise ValidationError("top_k must be a positive integer.")
    index_id = _resource_index_id(ctx, name, resolved_index_id)
    _, entries = _index_entries(ctx, index_id)
    ranked = _rank_bm25(question, [entry["chunk"] for entry in entries], top_k, k1, b)
    ctx.usage = {"retrieved_chunks": len(ranked), "index_id": index_id, "strategy": "bm25"}
    return {"chunks": ranked}


async def _retrieve_hash(
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
    index_id = _resource_index_id(ctx, name, resolved_index_id)
    payload, entries = _index_entries(ctx, index_id)
    ranked = _rank_hash(question, entries, int(payload.get("dimensions", 128)), top_k)
    ctx.usage = {
        "retrieved_chunks": len(ranked),
        "index_id": index_id,
        "strategy": "hash",
        "dimensions": payload.get("dimensions"),
    }
    return {"chunks": ranked}


async def _retrieve_hybrid(
    ctx: RunContext,
    question: str,
    name: str = "default",
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_k: int = 60,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
    resolved_index_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    index_id = _resource_index_id(ctx, name, resolved_index_id)
    payload, entries = _index_entries(ctx, index_id)
    chunks = [entry["chunk"] for entry in entries]
    lexical = _rank_bm25(question, chunks, candidate_k, 1.2, 0.75)
    vector = _rank_hash(question, entries, int(payload.get("dimensions", 128)), candidate_k)
    ranked = _fuse_rrf(lexical, vector, top_k, rrf_k, lexical_weight, vector_weight)
    ctx.usage = {
        "retrieved_chunks": len(ranked),
        "index_id": index_id,
        "strategy": "hybrid-rrf",
        "dimensions": payload.get("dimensions"),
        "rrf_k": rrf_k,
    }
    return {"chunks": ranked}


async def _fuse_rankings(
    ctx: RunContext,
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    top_k: int = 5,
    rrf_k: int = 60,
    primary_weight: float = 1.0,
    secondary_weight: float = 1.0,
    **_: Any,
) -> dict[str, Any]:
    ranked = _fuse_rrf(primary, secondary, top_k, rrf_k, primary_weight, secondary_weight)
    ctx.usage = {"retrieved_chunks": len(ranked), "strategy": "rrf", "rrf_k": rrf_k}
    return {"chunks": ranked}


async def _rerank_chunks(
    ctx: RunContext, question: str, chunks: list[dict[str, Any]], top_k: int = 5, **_: Any
) -> dict[str, Any]:
    ranked = _rank_bm25(question, chunks, top_k, 1.2, 0.75)
    ctx.usage = {"retrieved_chunks": len(ranked), "strategy": "bm25-rerank"}
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
            {"chunks": "chunks"}, {"index_id": "index", "index_info": "index_info"}, _index_chunks
        ),
        "retrieval.vector": ComponentDefinition(
            {"question": "string"}, {"chunks": "chunks"}, _retrieve_bm25
        ),
        "retrieval.bm25": ComponentDefinition(
            {"question": "string"}, {"chunks": "chunks"}, _retrieve_bm25
        ),
        "retrieval.hash": ComponentDefinition(
            {"question": "string"}, {"chunks": "chunks"}, _retrieve_hash
        ),
        "retrieval.hybrid": ComponentDefinition(
            {"question": "string"}, {"chunks": "chunks"}, _retrieve_hybrid
        ),
        "retrieval.rrf": ComponentDefinition(
            {"primary": "chunks", "secondary": "chunks"}, {"chunks": "chunks"}, _fuse_rankings
        ),
        "retrieval.rerank": ComponentDefinition(
            {"question": "string", "chunks": "chunks"}, {"chunks": "chunks"}, _rerank_chunks
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
          storage: sqlite
          dimensions: 128
    connections:
      - from: $input.path
        to: load.path
      - from: load.documents
        to: chunk.documents
      - from: chunk.chunks
        to: index.chunks
    output:
      index_id: index.index_id
      index_info: index.index_info

  answer:
    input:
      question: string
    nodes:
      lexical:
        uses: retrieval.bm25
        with:
          name: default
          top_k: 12
      vector:
        uses: retrieval.hash
        with:
          name: default
          top_k: 12
      fuse:
        uses: retrieval.rrf
        with:
          top_k: 5
          rrf_k: 10
      rerank:
        uses: retrieval.rerank
        with:
          top_k: 5
      answer:
        uses: answer.extractive
    connections:
      - from: $input.question
        to: lexical.question
      - from: $input.question
        to: vector.question
      - from: lexical.chunks
        to: fuse.primary
      - from: vector.chunks
        to: fuse.secondary
      - from: $input.question
        to: rerank.question
      - from: fuse.chunks
        to: rerank.chunks
      - from: $input.question
        to: answer.question
      - from: rerank.chunks
        to: answer.chunks
    output:
      answer: answer.answer
      sources: rerank.chunks
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
        inferred_inputs = {
            name: "any"
            for name, parameter in signature.parameters.items()
            if name != "ctx" and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        }
        inputs = getattr(function, "__aisys_inputs__", None) or inferred_inputs
        outputs = getattr(function, "__aisys_outputs__", None) or {"result": "any"}
        if not isinstance(inputs, dict) or not isinstance(outputs, dict) or not outputs:
            raise ValidationError(f"{uses} inputs and outputs must be dictionaries with at least one output.")
        if not all(isinstance(name, str) and isinstance(kind, str) for name, kind in {**inputs, **outputs}.items()):
            raise ValidationError(f"{uses} port names and types must be strings.")
        return ComponentDefinition(inputs, outputs, function)

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
            if node.uses in {"retrieval.vector", "retrieval.bm25", "retrieval.hash", "retrieval.hybrid"}:
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
        details = {
            "squad": ("rajpurkar/squad", "plain_text", "validation", _squad_records),
            "hotpotqa": ("hotpotqa/hotpot_qa", "distractor", "validation", _hotpot_records),
            "coqa": ("stanfordnlp/coqa", "default", "validation", _coqa_records),
            "triviaqa": ("mandarjoshi/trivia_qa", "rc", "validation", _triviaqa_records),
            "duorc": ("ibm-research/duorc", "SelfRC", "validation", _duorc_records),
        }
        if dataset not in details:
            raise ValidationError(f"Public datasets supported in V1.1 are: {', '.join(sorted(details))}.")
        if not 1 <= limit <= 100:
            raise ValidationError("Dataset limit must be between 1 and 100.")
        source, config, split, converter = details[dataset]
        rows, total = _sample_viewer_rows(source, config, split, limit)
        data_dir = self.root / "data" / dataset
        eval_dir = self.root / "evals"
        data_dir.mkdir(parents=True, exist_ok=True)
        eval_dir.mkdir(parents=True, exist_ok=True)
        documents, cases = converter(rows)
        documents_path = data_dir / "documents.jsonl"
        evaluation_path = eval_dir / f"{dataset}.jsonl"
        _write_jsonl(documents_path, documents)
        _write_jsonl(evaluation_path, cases)
        metadata = {
            "dataset": source,
            "config": config,
            "split": split,
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
            if (
                not isinstance(case.get("id"), str)
                or not isinstance(case.get("input"), dict)
                or not isinstance(case.get("expected", {}), dict)
            ):
                raise ValidationError("Each evaluation row needs string id plus object input and expected values.")
        experiment_id = f"exp_{uuid.uuid4().hex[:12]}"
        dataset_hash = _file_hash(path)
        system_hash = _file_hash(self.system_path)
        self.store.create_experiment(experiment_id, safe_name, workflow_name, str(path), dataset_hash, system_hash)
        metric_values: dict[str, list[float]] = {
            "source_recall_at_k": [],
            "hit_rate_at_k": [],
            "first_relevant_rank": [],
            "mrr": [],
            "ndcg_at_k": [],
            **{f"source_recall_at_{limit}": [] for limit in (1, 3, 5, 10)},
            **{f"hit_rate_at_{limit}": [] for limit in (1, 3, 5, 10)},
            **{f"precision_at_{limit}": [] for limit in (1, 3, 5, 10)},
        }
        answer_values: dict[str, list[float]] = {"answer_exact_match": [], "answer_token_f1": []}
        slice_values: dict[str, dict[str, list[float]]] = {}
        durations: list[float] = []
        failed = 0
        for case in cases:
            started = time.perf_counter()
            try:
                run = self.run(workflow_name, case["input"])
                chunks = run["outputs"].get("sources", [])
                retrieved = [chunk.get("document_id") for chunk in chunks]
                expected = {str(value) for value in case.get("expected", {}).get("source_ids", [])}
                scores = _retrieval_scores(retrieved, expected)
                expected_answer = case.get("expected", {}).get("answer")
                answer = run["outputs"].get("answer")
                if isinstance(expected_answer, str) and expected_answer.strip() and isinstance(answer, str):
                    answer_scores = _answer_scores(answer, expected_answer)
                    scores.update(answer_scores)
                    for metric, value in answer_scores.items():
                        answer_values[metric].append(value)
                metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
                slice_name = str(metadata.get("slice", "all"))
                scores["slice"] = slice_name
                for metric, values in metric_values.items():
                    values.append(float(scores[metric]))
                    slice_values.setdefault(slice_name, {}).setdefault(metric, []).append(float(scores[metric]))
                duration_ms = (time.perf_counter() - started) * 1000
                durations.append(duration_ms)
                self.store.add_eval_result(
                    experiment_id,
                    case["id"],
                    run["id"],
                    "completed",
                    duration_ms,
                    scores,
                )
            except Exception as error:
                failed += 1
                duration_ms = (time.perf_counter() - started) * 1000
                durations.append(duration_ms)
                scores = {metric: 0.0 for metric in metric_values}
                scores.update({"retrieved_count": 0.0, "slice": "failed"})
                self.store.add_eval_result(
                    experiment_id,
                    case["id"],
                    None,
                    "failed",
                    duration_ms,
                    scores,
                    str(error),
                )
                for metric, values in metric_values.items():
                    values.append(0.0)
                    slice_values.setdefault("failed", {}).setdefault(metric, []).append(0.0)
        summary = {
            "cases": len(cases),
            "failed_cases": failed,
            **{metric: _mean(values) for metric, values in metric_values.items() if metric != "first_relevant_rank"},
            "mean_first_relevant_rank": _mean(
                [value for value in metric_values["first_relevant_rank"] if value > 0]
            ),
            "answer_cases": len(answer_values["answer_exact_match"]),
            "answer_exact_match": _mean(answer_values["answer_exact_match"]),
            "answer_token_f1": _mean(answer_values["answer_token_f1"]),
            "mean_latency_ms": _mean(durations),
            "p95_latency_ms": _percentile(durations, 0.95),
            "slices": {
                name: {
                    "cases": len(values["source_recall_at_k"]),
                    **{
                        metric: _mean([value for value in scores if value > 0])
                        if metric == "first_relevant_rank"
                        else _mean(scores)
                        for metric, scores in values.items()
                    },
                }
                for name, values in sorted(slice_values.items())
            },
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

    def telemetry(self, run_id: str | None = None) -> dict[str, Any]:
        selected = run_id or self.store.latest_run_id()
        if selected is None:
            raise ValidationError("No runs have been recorded.")
        return self.store.telemetry(selected)

    def leaderboard(self, metric: str = "source_recall_at_k") -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_]+", metric):
            raise ValidationError("Metric names may contain only letters, numbers, and underscores.")
        rows = self.store.connection.execute(
            "SELECT name, workflow, status, started_at, summary_json FROM experiments ORDER BY started_at DESC"
        ).fetchall()
        entries = []
        for row in rows:
            summary = json.loads(row["summary_json"] or "{}")
            value = summary.get(metric)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            entries.append(
                {
                    "name": row["name"],
                    "workflow": row["workflow"],
                    "status": row["status"],
                    "metric": metric,
                    "value": round(float(value), 6),
                    "cases": summary.get("cases", 0),
                    "p95_latency_ms": summary.get("p95_latency_ms", 0.0),
                    "started_at": row["started_at"],
                }
            )
        entries.sort(key=lambda entry: (-entry["value"], entry["p95_latency_ms"], entry["name"]))
        return {"metric": metric, "experiments": entries}

    def gate(
        self,
        candidate_name: str,
        metric: str = "source_recall_at_k",
        minimum: float | None = None,
        baseline_name: str | None = None,
        max_regression: float = 0.0,
    ) -> dict[str, Any]:
        candidate = self.store.experiment(candidate_name)
        value = candidate["summary"].get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"Experiment {candidate_name!r} has no numeric {metric} metric.")
        if minimum is not None and not 0 <= minimum <= 1:
            raise ValidationError("Gate minimum must be between 0 and 1.")
        if max_regression < 0:
            raise ValidationError("Gate max regression must be non-negative.")
        result: dict[str, Any] = {
            "candidate": candidate_name,
            "metric": metric,
            "value": round(float(value), 6),
            "minimum": minimum,
            "baseline": baseline_name,
            "max_regression": max_regression,
            "passed": True,
        }
        if minimum is not None and value < minimum:
            result["passed"] = False
        if baseline_name is not None:
            baseline = self.store.experiment(baseline_name)
            if baseline["dataset_hash"] != candidate["dataset_hash"]:
                raise ValidationError("A regression gate requires experiments from the same dataset snapshot.")
            baseline_value = baseline["summary"].get(metric)
            if not isinstance(baseline_value, (int, float)) or isinstance(baseline_value, bool):
                raise ValidationError(f"Experiment {baseline_name!r} has no numeric {metric} metric.")
            delta = float(value) - float(baseline_value)
            result.update({"baseline_value": round(float(baseline_value), 6), "delta": round(delta, 6)})
            if delta < -max_regression:
                result["passed"] = False
        return result

    def sweep(self, spec_path: str | Path) -> dict[str, Any]:
        path = Path(spec_path)
        if not path.is_absolute():
            path = self.root / path
        try:
            specification = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as error:
            raise ValidationError(f"Cannot read sweep specification {path}: {error}") from error
        if not isinstance(specification, dict):
            raise ValidationError("Sweep specification must be an object.")
        workflow = specification.get("workflow", "answer")
        dataset = specification.get("dataset")
        ingest = specification.get("ingest")
        parameters = specification.get("parameters")
        if not isinstance(dataset, str) or not isinstance(parameters, dict) or not parameters:
            raise ValidationError("Sweep needs dataset and a non-empty parameters mapping.")
        if ingest is not None and (
            not isinstance(ingest, dict)
            or not isinstance(ingest.get("workflow", "ingest"), str)
            or not isinstance(ingest.get("input"), dict)
        ):
            raise ValidationError("Sweep ingest must contain a workflow and object input.")
        keys = sorted(parameters)
        values = [parameters[key] for key in keys]
        if not all(isinstance(value, list) and value for value in values):
            raise ValidationError("Each sweep parameter must have a non-empty list of values.")
        combinations = list(itertools.product(*values))
        if len(combinations) > 64:
            raise ValidationError("Sweep is capped at 64 combinations; split larger grids into focused experiments.")
        original = self.system_path.read_text()
        prefix = _safe_name(str(specification.get("name", path.stem)))
        experiments: list[dict[str, Any]] = []
        try:
            for ordinal, combination in enumerate(combinations, start=1):
                definition = yaml.safe_load(original)
                assignment = dict(zip(keys, combination, strict=True))
                for dotted_path, value in assignment.items():
                    _set_mapping_path(definition, dotted_path, value)
                self.system_path.write_text(yaml.safe_dump(definition, sort_keys=False, allow_unicode=True))
                if ingest is not None:
                    self.run(ingest.get("workflow", "ingest"), ingest["input"])
                result = self.evaluate(workflow, dataset, f"{prefix}-{uuid.uuid4().hex[:8]}-{ordinal}")
                experiments.append({"parameters": assignment, **result})
        finally:
            self.system_path.write_text(original)
            if ingest is not None:
                self.run(ingest.get("workflow", "ingest"), ingest["input"])
        report_dir = self.root / "reports"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / f"{prefix}-sweep.json"
        report = {"name": prefix, "dataset": dataset, "experiments": experiments}
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        return {"report": str(report_path), **report}

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
                    f"- MRR: {summary.get('mrr', 0):.4f}",
                    f"- nDCG@k: {summary.get('ndcg_at_k', 0):.4f}",
                    f"- Mean first relevant rank: {summary.get('mean_first_relevant_rank', 0):.4f}",
                    f"- Mean / p95 latency (ms): {summary.get('mean_latency_ms', 0):.3f} / {summary.get('p95_latency_ms', 0):.3f}",
                    f"- Answer cases / exact match / token F1: {summary.get('answer_cases', 0)} / {summary.get('answer_exact_match', 0):.4f} / {summary.get('answer_token_f1', 0):.4f}",
                    "",
                    "## Retrieval cutoffs",
                    "",
                ]
                + [
                    f"- @{limit}: recall {summary.get(f'source_recall_at_{limit}', 0):.4f}, "
                    f"hit {summary.get(f'hit_rate_at_{limit}', 0):.4f}, "
                    f"precision {summary.get(f'precision_at_{limit}', 0):.4f}"
                    for limit in (1, 3, 5, 10)
                ]
                + [
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


def _rank_bm25(
    question: str, chunks: list[dict[str, Any]], top_k: int, k1: float, b: float
) -> list[dict[str, Any]]:
    # ponytail: in-memory O(chunks × query_terms) scorer; add a persisted ANN index only after a corpus benchmark fails.
    if not 0 < float(k1) <= 5 or not 0 <= float(b) <= 1:
        raise ValidationError("BM25 k1 must be in (0, 5] and b must be in [0, 1].")
    document_frequencies: dict[str, int] = {}
    tokenized = [_tokenize(chunk["text"]) for chunk in chunks]
    for tokens in tokenized:
        for token in set(tokens):
            document_frequencies[token] = document_frequencies.get(token, 0) + 1
    count = len(chunks)
    query = _tokenize(question)
    average_length = sum(len(tokens) for tokens in tokenized) / count if count else 1.0
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, tokens in zip(chunks, tokenized, strict=True):
        term_counts = {token: tokens.count(token) for token in set(tokens)}
        score = 0.0
        for token in set(query):
            frequency = term_counts.get(token, 0)
            if not frequency:
                continue
            idf = math.log(1 + (count - document_frequencies.get(token, 0) + 0.5) / (document_frequencies.get(token, 0) + 0.5))
            denominator = frequency + k1 * (1 - b + b * len(tokens) / average_length)
            score += idf * (frequency * (k1 + 1) / denominator)
        scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**chunk, "score": round(score, 8)} for score, chunk in scored[:top_k]]


def _hashed_vector(text: str, dimensions: int) -> dict[str, float]:
    values: dict[int, float] = {}
    for token in _tokenize(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        values[bucket] = values.get(bucket, 0.0) + (1.0 if digest[4] % 2 else -1.0)
    norm = math.sqrt(sum(value * value for value in values.values())) or 1.0
    return {str(bucket): round(value / norm, 8) for bucket, value in values.items()}


def _rank_hash(
    question: str, entries: list[dict[str, Any]], dimensions: int, top_k: int
) -> list[dict[str, Any]]:
    query = _hashed_vector(question, dimensions)
    scored = [
        (
            sum(float(value) * float(entry["vector"].get(bucket, 0.0)) for bucket, value in query.items()),
            entry["chunk"],
        )
        for entry in entries
    ]
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**chunk, "score": round(score, 8)} for score, chunk in scored[:top_k]]


def _fuse_rrf(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    top_k: int,
    rrf_k: int,
    primary_weight: float,
    secondary_weight: float,
) -> list[dict[str, Any]]:
    if not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValidationError("RRF rrf_k must be a positive integer.")
    if primary_weight < 0 or secondary_weight < 0 or primary_weight + secondary_weight == 0:
        raise ValidationError("RRF weights must be non-negative and at least one must be positive.")
    # ponytail: two rankings cover the intended lexical/vector hybrid; general N-way fusion belongs in a later component manifest.
    scores: dict[str, float] = {}
    chunks: dict[str, dict[str, Any]] = {}
    for ranking, weight in ((primary, primary_weight), (secondary, secondary_weight)):
        for rank, chunk in enumerate(ranking, start=1):
            chunks.setdefault(chunk["id"], chunk)
            scores[chunk["id"]] = scores.get(chunk["id"], 0.0) + weight / (rrf_k + rank)
    return [
        {**chunks[chunk_id], "score": round(score, 8)}
        for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    ]


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "aisys/0.1"})
    context = ssl.create_default_context()
    host_bundle = Path("/etc/ssl/cert.pem")
    if host_bundle.exists():
        context.load_verify_locations(cafile=str(host_bundle))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code == 429 and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise ValidationError(f"Public dataset request failed: {error}") from error
        except Exception as error:
            raise ValidationError(f"Public dataset request failed: {error}") from error
    raise AssertionError("Dataset retry loop exited unexpectedly.")


def _sample_viewer_rows(dataset: str, config: str, split: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    endpoint = "https://datasets-server.huggingface.co/rows"
    parameters = {"dataset": dataset, "config": config, "split": split}
    try:
        initial = _request_json(
            f"{endpoint}?{urllib.parse.urlencode({**parameters, 'offset': 0, 'length': 1})}"
        )
    except ValidationError as error:
        if "429" not in str(error):
            raise
        # ponytail: a single first-rows response is a rate-limit fallback; restore distributed paging when the public API allows it.
        preview = _request_json(
            f"https://datasets-server.huggingface.co/first-rows?{urllib.parse.urlencode(parameters)}"
        )
        rows = [item.get("row", {}) for item in preview.get("rows", [])][:limit]
        if not rows:
            raise ValidationError(f"Dataset {dataset} returned no preview rows.") from error
        return rows, len(rows)
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


def _coqa_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for row in rows:
        story = str(row["story"])
        document_id = f"coqa:{hashlib.sha256(story.encode()).hexdigest()[:16]}"
        documents.setdefault(document_id, {"id": document_id, "title": row.get("source", ""), "text": story})
        questions = row.get("questions", [])
        answers = row.get("answers", {}).get("input_text", [])
        for ordinal, (question, answer) in enumerate(zip(questions, answers, strict=False)):
            if str(answer).lower() == "unknown":
                continue
            cases.append(
                {
                    "id": f"{document_id}:{ordinal}",
                    "input": {"question": question},
                    "expected": {"source_ids": [document_id], "answer": answer},
                    "metadata": {"dataset": "stanfordnlp/coqa", "source": row.get("source", "")},
                }
            )
    return list(documents.values()), cases


def _triviaqa_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for row in rows:
        question_id = str(row["question_id"])
        candidates: list[tuple[str, str]] = []
        for field, text_field in (("entity_pages", "wiki_context"), ("search_results", "search_context")):
            source = row.get(field, {})
            for title, text in zip(source.get("title", []), source.get(text_field, []), strict=False):
                if text:
                    candidates.append((str(title), str(text)))
        answer = row.get("answer", {})
        aliases = [str(value) for value in answer.get("aliases", []) if value]
        if not aliases and answer.get("value"):
            aliases = [str(answer["value"])]
        expected_ids: list[str] = []
        for ordinal, (title, text) in enumerate(candidates):
            document_id = f"triviaqa:{question_id}:{ordinal}"
            documents.setdefault(document_id, {"id": document_id, "title": title, "text": text})
            if any(alias.casefold() in text.casefold() for alias in aliases):
                expected_ids.append(document_id)
        if expected_ids:
            cases.append(
                {
                    "id": f"triviaqa:{question_id}",
                    "input": {"question": row["question"]},
                    "expected": {"source_ids": expected_ids, "answer": answer.get("value", "")},
                    "metadata": {"dataset": "mandarjoshi/trivia_qa"},
                }
            )
    return list(documents.values()), cases


def _duorc_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for row in rows:
        if row.get("no_answer"):
            continue
        plot = str(row["plot"])
        document_id = f"duorc:{hashlib.sha256(str(row['plot_id']).encode()).hexdigest()[:16]}"
        documents.setdefault(document_id, {"id": document_id, "title": row.get("title", ""), "text": plot})
        cases.append(
            {
                "id": f"duorc:{row['question_id']}",
                "input": {"question": row["question"]},
                "expected": {"source_ids": [document_id], "answer": (row.get("answers") or [""])[0]},
                "metadata": {"dataset": "ibm-research/duorc", "title": row.get("title", "")},
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


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def _duration_ms(started_at: str, ended_at: str | None) -> float:
    if ended_at is None:
        return 0.0
    return (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000


def _retrieval_scores(retrieved: list[Any], expected: set[str]) -> dict[str, float]:
    # Source IDs identify documents while a retriever returns chunks; score each document at its first rank.
    ranked = list(dict.fromkeys(str(source) for source in retrieved if isinstance(source, str)))
    rank = next((index + 1 for index, source_id in enumerate(ranked) if source_id in expected), None)
    scores = {
        "source_recall_at_k": len(expected.intersection(ranked)) / len(expected) if expected else 0.0,
        "hit_rate_at_k": 1.0 if rank is not None else 0.0,
        "first_relevant_rank": float(rank or 0),
        "mrr": 1.0 / rank if rank is not None else 0.0,
        "retrieved_count": float(len(ranked)),
    }
    for limit in (1, 3, 5, 10):
        selected = ranked[:limit]
        relevant = len(expected.intersection(selected))
        scores[f"source_recall_at_{limit}"] = relevant / len(expected) if expected else 0.0
        scores[f"hit_rate_at_{limit}"] = 1.0 if relevant else 0.0
        scores[f"precision_at_{limit}"] = relevant / len(selected) if selected else 0.0
    cutoff = len(ranked)
    dcg = sum(1 / math.log2(index + 2) for index, source_id in enumerate(ranked) if source_id in expected)
    ideal = sum(1 / math.log2(index + 2) for index in range(min(len(expected), cutoff)))
    scores["ndcg_at_k"] = dcg / ideal if ideal else 0.0
    return {key: round(value, 6) for key, value in scores.items()}


def _answer_scores(answer: str, expected: str) -> dict[str, float]:
    actual_tokens, expected_tokens = _tokenize(answer), _tokenize(expected)
    overlap = sum((Counter(actual_tokens) & Counter(expected_tokens)).values())
    precision = overlap / len(actual_tokens) if actual_tokens else 0.0
    recall = overlap / len(expected_tokens) if expected_tokens else 0.0
    return {
        "answer_exact_match": 1.0 if " ".join(actual_tokens) == " ".join(expected_tokens) else 0.0,
        "answer_token_f1": round(2 * precision * recall / (precision + recall), 6) if precision + recall else 0.0,
    }


def _set_mapping_path(value: dict[str, Any], dotted_path: str, replacement: Any) -> None:
    if not isinstance(dotted_path, str) or not dotted_path:
        raise ValidationError("Sweep parameter paths must be non-empty strings.")
    target: Any = value
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            raise ValidationError(f"Sweep parameter path {dotted_path!r} does not exist.")
        target = target[part]
    if not isinstance(target, dict) or parts[-1] not in target:
        raise ValidationError(f"Sweep parameter path {dotted_path!r} does not exist.")
    target[parts[-1]] = replacement
