"""Command-line interface for the local Aisys runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import Project, ValidationError


def _project(value: str) -> Project:
    return Project.load(Path(value))


def _json_input(value: str, project: Project, workflow_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    if isinstance(parsed, dict):
        return parsed
    workflow = project.workflows().get(workflow_name)
    if workflow is None:
        raise ValidationError(f"Unknown workflow {workflow_name}.")
    if len(workflow.inputs) != 1:
        raise ValidationError("Use a JSON object for workflows with more than one input.")
    return {next(iter(workflow.inputs)): parsed}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aisys", description="Build and evaluate local AI workflows.")
    parser.add_argument("--project", default=".", help="Project directory (default: current directory).")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create a minimal Aisys project.")
    init.add_argument("path")
    init.add_argument("--name", default="document-qa")

    commands.add_parser("check", help="Validate system.yaml.")
    commands.add_parser("components", help="List built-in components.")

    graph = commands.add_parser("graph", help="Print a Mermaid workflow graph.")
    graph.add_argument("workflow", nargs="?")

    run = commands.add_parser("run", help="Run a workflow.")
    run.add_argument("workflow")
    run.add_argument("--input", required=True, help="JSON input object, or a scalar for one-input workflows.")

    trace = commands.add_parser("trace", help="Show a stored trace.")
    trace.add_argument("run_id", nargs="?", default="latest")

    telemetry = commands.add_parser("telemetry", help="Summarize persisted component timing and usage.")
    telemetry.add_argument("run_id", nargs="?", default="latest")

    dataset = commands.add_parser("dataset", help="Fetch a supported public evaluation dataset.")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    fetch = dataset_commands.add_parser("fetch", help="Fetch and convert a public dataset.")
    fetch.add_argument("dataset", choices=("squad", "hotpotqa", "coqa", "triviaqa", "duorc"))
    fetch.add_argument("--limit", type=int, default=60)

    evaluate = commands.add_parser("eval", help="Evaluate source retrieval against JSONL cases.")
    evaluate.add_argument("workflow")
    evaluate.add_argument("dataset")
    evaluate.add_argument("--name", required=True)

    compare = commands.add_parser("compare", help="Compare two recorded evaluation experiments.")
    compare.add_argument("baseline")
    compare.add_argument("candidate")

    leaderboard = commands.add_parser("leaderboard", help="Rank recorded evaluations by a summary metric.")
    leaderboard.add_argument("--metric", default="source_recall_at_k")

    gate = commands.add_parser("gate", help="Check an evaluation against quality and regression thresholds.")
    gate.add_argument("candidate")
    gate.add_argument("--metric", default="source_recall_at_k")
    gate.add_argument("--minimum", type=float)
    gate.add_argument("--baseline")
    gate.add_argument("--max-regression", type=float, default=0.0)

    sweep = commands.add_parser("sweep", help="Run every configuration in a YAML parameter grid.")
    sweep.add_argument("spec")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            _print({"project": str(Project.init(arguments.path, arguments.name)), "status": "created"})
            return 0
        project = _project(arguments.project)
        if arguments.command == "check":
            _print(project.validate())
        elif arguments.command == "components":
            _print(project.components())
        elif arguments.command == "graph":
            print(project.graph(arguments.workflow))
        elif arguments.command == "run":
            _print(project.run(arguments.workflow, _json_input(arguments.input, project, arguments.workflow)))
        elif arguments.command == "trace":
            run_id = project.store.latest_run_id() if arguments.run_id == "latest" else arguments.run_id
            if run_id is None:
                raise ValidationError("No runs have been recorded.")
            _print(project.store.trace(run_id))
        elif arguments.command == "telemetry":
            run_id = None if arguments.run_id == "latest" else arguments.run_id
            _print(project.telemetry(run_id))
        elif arguments.command == "dataset" and arguments.dataset_command == "fetch":
            _print(project.fetch_dataset(arguments.dataset, arguments.limit))
        elif arguments.command == "eval":
            _print(project.evaluate(arguments.workflow, arguments.dataset, arguments.name))
        elif arguments.command == "compare":
            _print(project.compare(arguments.baseline, arguments.candidate))
        elif arguments.command == "leaderboard":
            _print(project.leaderboard(arguments.metric))
        elif arguments.command == "gate":
            result = project.gate(
                arguments.candidate,
                arguments.metric,
                arguments.minimum,
                arguments.baseline,
                arguments.max_regression,
            )
            _print(result)
            return 0 if result["passed"] else 1
        elif arguments.command == "sweep":
            _print(project.sweep(arguments.spec))
        else:
            parser.error("Unknown command.")
    except ValidationError as error:
        print(f"aisys: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
