from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aisys import Project, ValidationError


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

    def test_validator_rejects_cycle(self) -> None:
        Project.init(self.root)
        system = self.root / "system.yaml"
        system.write_text(
            system.read_text().replace(
                "      - from: $input.question\n        to: retrieve.question",
                "      - from: answer.answer\n        to: retrieve.question",
            )
        )
        with self.assertRaisesRegex(ValidationError, "cycle"):
            Project.load(self.root).validate()


if __name__ == "__main__":
    unittest.main()
