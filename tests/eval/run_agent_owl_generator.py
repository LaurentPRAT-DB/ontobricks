"""Parsed-document corpus contract evaluation for agent_owl_generator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests" / "eval") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests" / "eval"))

from document_corpus_contract import run_contract  # noqa: E402

DATASET = ROOT / "tests/eval/datasets/agent_owl_generator/baseline.jsonl"
THRESHOLDS = ROOT / "tests/eval/thresholds.yaml"


def _live_runner(
    example: Dict[str, Any], *, host: str, token: str, endpoint: str
) -> Tuple[List[str], str, List[str]]:
    from agents.agent_owl_generator.engine import TOOL_HANDLERS, run_agent

    documents = example["input"]["documents"]
    by_name = {doc["name"]: doc for doc in documents}

    def list_documents(_ctx, **_kwargs):
        files = [
            {
                "name": doc["name"],
                "size": len(doc.get("content", "")),
                "parse_status": doc["parse_status"],
            }
            for doc in documents
        ]
        return json.dumps({"files": files, "count": len(files)})

    def read_document(_ctx, *, filename: str = "", **_kwargs):
        doc = by_name.get(filename)
        if doc is None:
            return json.dumps({"filename": filename, "error": "Document not found"})
        if doc["parse_status"] != "ready":
            return json.dumps(
                {
                    "filename": filename,
                    "parse_status": doc["parse_status"],
                    "error": doc.get("error") or "Document parsing is not ready",
                }
            )
        return json.dumps(
            {
                "filename": filename,
                "parse_status": "ready",
                "content": doc.get("content", ""),
                "size": len(doc.get("content", "")),
                "truncated": bool(doc.get("truncated")),
            }
        )

    originals = {
        "list_documents": TOOL_HANDLERS["list_documents"],
        "read_document": TOOL_HANDLERS["read_document"],
    }
    TOOL_HANDLERS.update(
        {"list_documents": list_documents, "read_document": read_document}
    )
    try:
        result = run_agent(
            host=host,
            token=token,
            endpoint_name=endpoint,
            registry={},
            metadata={"tables": []},
            guidelines=example["input"]["request"],
            options={
                "generation_max_iterations": 1,
                "owl_eval_max_rounds": 0,
                "max_classes": 10,
            },
            base_uri="https://example.test/ontology#",
            selected_docs=[doc["name"] for doc in documents],
        )
    finally:
        TOOL_HANDLERS.update(originals)

    tools_called = [
        step.tool_name
        for step in result.steps
        if step.step_type == "tool_call" and step.tool_name
    ]
    observed_text = " ".join(
        [result.owl_content]
        + [step.content for step in result.steps if step.step_type == "tool_result"]
    )
    return tools_called, observed_text, [doc["name"] for doc in documents]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--host", default=os.getenv("DATABRICKS_HOST"))
    parser.add_argument("--token", default=os.getenv("DATABRICKS_TOKEN"))
    parser.add_argument("--endpoint", default=os.getenv("ONTOBRICKS_LLM_ENDPOINT"))
    parser.add_argument(
        "--mlflow-experiment",
        default="/Shared/ontobricks/agents/owl_generator",
    )
    parser.add_argument(
        "--mlflow-tracking-uri",
        default=os.getenv("MLFLOW_TRACKING_URI", "databricks"),
    )
    args = parser.parse_args()
    if args.live and not (args.host and args.token and args.endpoint):
        parser.error("--live requires host, token, and endpoint")

    live = None
    if args.live:
        live = lambda example: _live_runner(  # noqa: E731
            example, host=args.host, token=args.token, endpoint=args.endpoint
        )
    run_contract(
        agent_name="owl_generator",
        dataset_path=DATASET,
        thresholds_path=THRESHOLDS,
        document_tool="read_document",
        dry_run=not args.live,
        live_runner=live,
        mlflow_experiment=args.mlflow_experiment,
        mlflow_tracking_uri=args.mlflow_tracking_uri,
    )


if __name__ == "__main__":
    main()
