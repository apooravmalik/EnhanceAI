from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aisys import Project, ValidationError
from aisys.core import _retrieval_scores


class CliEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "demo"
        self.source_root = Path(__file__).resolve().parents[1] / "src"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *arguments: str) -> dict:
        environment = {**os.environ, "PYTHONPATH": str(self.source_root)}
        result = subprocess.run(
            [sys.executable, "-m", "aisys.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_project_lifecycle_and_retrieval_evaluation(self) -> None:
        created = self.cli("init", str(self.root), "--name", "test-retrieval")
        self.assertEqual(created["status"], "created")
        self.assertEqual(self.cli("--project", str(self.root), "check")["status"], "valid")

        documents = self.root / "data" / "sample_documents.jsonl"
        ingested = self.cli("--project", str(self.root), "run", "ingest", "--input", str(documents))
        self.assertEqual(ingested["status"], "completed")
        answer = self.cli(
            "--project",
            str(self.root),
            "run",
            "answer",
            "--input",
            '{"question":"Can I return an opened item?"}',
        )
        self.assertEqual(answer["outputs"]["sources"][0]["document_id"], "returns")

        evaluation = self.cli(
            "--project",
            str(self.root),
            "eval",
            "answer",
            "evals/golden.jsonl",
            "--name",
            "baseline",
        )
        self.assertEqual(evaluation["failed_cases"], 0)
        self.assertEqual(evaluation["source_recall_at_k"], 1.0)
        self.assertEqual(evaluation["source_recall_at_1"], 1.0)
        self.assertEqual(evaluation["mrr"], 1.0)
        self.assertGreaterEqual(evaluation["p95_latency_ms"], 0.0)
        partial_cases = self.root / "evals" / "partial.jsonl"
        partial_cases.write_text(
            '{"id":"partial","input":{"question":"Can I return an opened item?"},'
            '"expected":{"source_ids":["returns","not-in-index"]}}\n'
        )
        partial = self.cli(
            "--project",
            str(self.root),
            "eval",
            "answer",
            "evals/partial.jsonl",
            "--name",
            "partial",
        )
        self.assertEqual(partial["source_recall_at_k"], 0.5)
        self.assertEqual(partial["hit_rate_at_k"], 1.0)
        trace = self.cli("--project", str(self.root), "trace", "latest")
        self.assertEqual(trace["run"]["status"], "completed")
        telemetry = self.cli("--project", str(self.root), "telemetry", "latest")
        self.assertEqual(telemetry["status"], "completed")
        self.assertIn("lexical", telemetry["nodes"])
        leaderboard = self.cli("--project", str(self.root), "leaderboard", "--metric", "mrr")
        self.assertEqual(leaderboard["experiments"][0]["metric"], "mrr")
        gate = self.cli("--project", str(self.root), "gate", "baseline", "--minimum", "1")
        self.assertTrue(gate["passed"])

    def test_validator_rejects_cycle(self) -> None:
        Project.init(self.root)
        system = self.root / "system.yaml"
        system.write_text(
            system.read_text().replace(
                "      - from: $input.question\n        to: lexical.question",
                "      - from: answer.answer\n        to: lexical.question",
            )
        )
        with self.assertRaisesRegex(ValidationError, "cycle"):
            Project.load(self.root).validate()

    def test_document_metrics_do_not_reward_repeated_chunks(self) -> None:
        scores = _retrieval_scores(["gold", "gold", "gold"], {"gold"})
        self.assertEqual(scores["precision_at_3"], 1.0)
        self.assertEqual(scores["ndcg_at_k"], 1.0)

    def test_openai_compatible_semantic_rag_and_answer_aliases(self) -> None:
        embedding_requests = [0]

        class EmbeddingsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/chat/completions":
                    payload = {
                        "choices": [{"message": {"content": "Opened items can be returned within fourteen days."}}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                    }
                elif self.path == "/v1/embeddings":
                    embedding_requests[0] += 1
                    vectors = []
                    for text in request["input"]:
                        tokens = text.lower()
                        vectors.append([1.0, 0.0] if "return" in tokens or "opened" in tokens else [0.0, 1.0])
                    payload = {"data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)], "usage": {"prompt_tokens": len(vectors)}}
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_key = os.environ.get("AISYS_TEST_KEY")
        os.environ["AISYS_TEST_KEY"] = "test-key"
        try:
            self.root.mkdir(parents=True)
            (self.root / "documents.jsonl").write_text(
                '{"id":"returns","text":"Opened items can be returned within fourteen days."}\n'
                '{"id":"shipping","text":"Shipping takes three business days."}\n'
            )
            (self.root / "golden.jsonl").write_text(
                '{"id":"returns","input":{"question":"Can I return an opened item?"},'
                '"expected":{"source_ids":["returns"],"answers":["Opened items can be returned within fourteen days."]}}\n'
            )
            (self.root / "system.yaml").write_text(
                f"""
version: 1
workflows:
  ingest:
    input: {{path: path}}
    nodes:
      load: {{uses: documents.load}}
      chunk: {{uses: documents.chunk, with: {{size: 20, overlap: 0}}}}
      embed: {{uses: embeddings.openai_compatible, with: {{model: mock-embed, api_key_env: AISYS_TEST_KEY, base_url: http://127.0.0.1:{server.server_port}/v1, input_cost_per_million_tokens: 100}}}}
      index: {{uses: retrieval.semantic_index, with: {{name: semantic, model: mock-embed, storage: sqlite}}}}
    connections:
      - {{from: $input.path, to: load.path}}
      - {{from: load.documents, to: chunk.documents}}
      - {{from: chunk.chunks, to: embed.chunks}}
      - {{from: embed.entries, to: index.entries}}
    output: {{index_id: index.index_id}}
  answer:
    input: {{question: string}}
    nodes:
      retrieve: {{uses: retrieval.semantic, with: {{name: semantic, model: mock-embed, api_key_env: AISYS_TEST_KEY, base_url: http://127.0.0.1:{server.server_port}/v1, top_k: 1, input_cost_per_million_tokens: 100}}}}
      answer: {{uses: llm.openai_compatible, with: {{model: mock-chat, api_key_env: AISYS_TEST_KEY, base_url: http://127.0.0.1:{server.server_port}/v1, input_cost_per_million_tokens: 100, output_cost_per_million_tokens: 100}}}}
    connections:
      - {{from: $input.question, to: retrieve.question}}
      - {{from: $input.question, to: answer.question}}
      - {{from: retrieve.chunks, to: answer.chunks}}
    output: {{answer: answer.answer, sources: retrieve.chunks}}
""".lstrip()
            )
            project = Project.load(self.root)
            first_ingest = project.run("ingest", {"path": str(self.root / "documents.jsonl")})
            self.assertEqual(embedding_requests[0], 1)
            project.run("ingest", {"path": str(self.root / "documents.jsonl")})
            self.assertEqual(embedding_requests[0], 1)
            self.assertGreater(project.telemetry(first_ingest["id"])["usage"]["estimated_cost_usd"], 0)
            answer = project.run("answer", {"question": "Can I return an opened item?"})
            self.assertEqual(answer["outputs"]["sources"][0]["document_id"], "returns")
            self.assertGreater(project.telemetry(answer["id"])["usage"]["estimated_cost_usd"], 0)
            evaluation = project.evaluate("answer", self.root / "golden.jsonl", "semantic-aliases")
            self.assertEqual(evaluation["answer_exact_match"], 1.0)
        finally:
            if old_key is None:
                os.environ.pop("AISYS_TEST_KEY", None)
            else:
                os.environ["AISYS_TEST_KEY"] = old_key
            server.shutdown()
            server.server_close()

    def test_hybrid_sqlite_index_custom_ports_and_sweep(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "components.py").write_text(
            "from aisys import component\n"
            "@component(outputs={'question': 'string', 'label': 'string'})\n"
            "def normalize(ctx, question):\n"
            "    return {'question': question.lower(), 'label': 'normalized'}\n"
        )
        documents = self.root / "documents.jsonl"
        documents.write_text(
            '{"id":"returns","text":"Opened items can be returned within fourteen days."}\n'
            '{"id":"shipping","text":"Shipping takes three business days."}\n'
        )
        evaluation = self.root / "golden.jsonl"
        evaluation.write_text(
            '{"id":"returns","input":{"question":"Can I return an opened item?"},'
            '"expected":{"source_ids":["returns"]}}\n'
        )
        system = self.root / "system.yaml"
        system.write_text(
            """
version: 1
workflows:
  ingest:
    input: {path: path}
    nodes:
      load: {uses: documents.load}
      chunk: {uses: documents.chunk, with: {size: 20, overlap: 0}}
      index: {uses: retrieval.index, with: {name: default, storage: sqlite, dimensions: 32}}
    connections:
      - {from: $input.path, to: load.path}
      - {from: load.documents, to: chunk.documents}
      - {from: chunk.chunks, to: index.chunks}
    output: {index_id: index.index_id}
  answer:
    input: {question: string}
    nodes:
      normalize: {uses: python:components:normalize}
      lexical: {uses: retrieval.bm25, with: {name: default, top_k: 2}}
      vector: {uses: retrieval.hash, with: {name: default, top_k: 2}}
      fuse: {uses: retrieval.rrf, with: {top_k: 2, rrf_k: 60}}
      rerank: {uses: retrieval.rerank, with: {top_k: 1}}
    connections:
      - {from: $input.question, to: normalize.question}
      - {from: normalize.question, to: lexical.question}
      - {from: normalize.question, to: vector.question}
      - {from: lexical.chunks, to: fuse.primary}
      - {from: vector.chunks, to: fuse.secondary}
      - {from: normalize.question, to: rerank.question}
      - {from: fuse.chunks, to: rerank.chunks}
    output: {sources: rerank.chunks}
""".lstrip()
        )
        project = Project.load(self.root)
        project.run("ingest", {"path": str(documents)})
        result = project.run("answer", {"question": "Can I return an opened item?"})
        self.assertEqual(result["outputs"]["sources"][0]["document_id"], "returns")
        sweep = self.root / "sweep.yaml"
        sweep.write_text(
            f"""
name: retrieval-grid
workflow: answer
dataset: {evaluation}
ingest:
  workflow: ingest
  input: {{path: {documents}}}
parameters:
  workflows.ingest.nodes.index.with.dimensions: [32, 64]
  workflows.answer.nodes.fuse.with.rrf_k: [10, 60]
""".lstrip()
        )
        results = project.sweep(sweep)
        self.assertEqual(len(results["experiments"]), 4)
        self.assertEqual(project.validate()["status"], "valid")


if __name__ == "__main__":
    unittest.main()
