from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
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
