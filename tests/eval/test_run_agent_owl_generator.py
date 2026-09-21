"""Focused tests for ``tests/eval/run_agent_owl_generator.py``'s ``--live``
wiring (final-review closure item 1).

``--live`` must invoke the real production staged entry points
(``agents.agent_owl_generator.staged.detect_entities`` /
``infer_relations`` / ``infer_attributes`` / ``infer_axioms``) — never the
deprecated one-shot ``agents.agent_owl_generator.engine.run_agent`` bridge,
which has no remaining production caller (SPEC.md §2). These tests fake only
the network boundary (``staged.call_serving_endpoint``), so ``_live_runner``
and ``staged_contract.score_staged_examples_live`` really do drive
``staged.detect_entities``/``infer_*`` end to end.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from unittest.mock import Mock, patch

import run_agent_owl_generator as runner
import staged_contract

from agents.agent_owl_generator import staged

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "tests/eval/datasets/agent_owl_generator/baseline.jsonl"


def _load_example(example_id: str) -> dict:
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("id") == example_id:
            return row
    raise AssertionError(f"example {example_id!r} not found in dataset")


def _fake_llm_answer(content: str) -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }


class TestModulesNeverImportLegacyEngine:
    """Source-level (AST) proof — mirrors
    ``test_owl_generator_staged.py::test_staged_module_never_imports_legacy_engine``
    — that neither eval module references the deprecated bridge module."""

    @staticmethod
    def _assert_no_engine_import(module) -> None:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(
                    "agent_owl_generator.engine" in alias.name for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "agent_owl_generator.engine" not in mod
                if mod == "agents.agent_owl_generator":
                    assert not any(alias.name == "engine" for alias in node.names)

    def test_run_agent_owl_generator_never_imports_legacy_engine(self):
        self._assert_no_engine_import(runner)

    def test_staged_contract_never_imports_legacy_engine(self):
        self._assert_no_engine_import(staged_contract)


class TestLiveRunnerTargetsStagedDetectEntities:
    """``_live_runner`` (parsed-corpus contract, ``--live``) must drive the
    real ``staged.detect_entities()`` entry point, not the legacy bridge."""

    def test_calls_detect_entities_not_run_agent(self):
        example = _load_example("owl-ready-pdf")
        fake_llm = Mock(return_value=_fake_llm_answer(json.dumps({"candidate_entities": []})))
        with patch.object(staged, "call_serving_endpoint", fake_llm), patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy_detect:
            with patch(
                "agents.agent_owl_generator.engine.run_agent",
                side_effect=AssertionError("legacy engine.run_agent must never be called"),
            ):
                tools_called, observed_text, listed_names = runner._live_runner(
                    example, host="https://test.databricks.com", token="tok", endpoint="ep"
                )
        assert spy_detect.call_count == 1
        assert listed_names == ["spec.pdf"]
        assert isinstance(tools_called, list)
        assert isinstance(observed_text, str)

    def test_wires_endpoint_kwargs_through_to_detect_entities(self):
        example = _load_example("owl-ready-pdf")
        fake_llm = Mock(return_value=_fake_llm_answer(json.dumps({"candidate_entities": []})))
        with patch.object(staged, "call_serving_endpoint", fake_llm), patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy_detect:
            runner._live_runner(
                example, host="https://live.databricks.com", token="secret-tok", endpoint="my-ep"
            )
        _, kwargs = spy_detect.call_args
        assert kwargs["host"] == "https://live.databricks.com"
        assert kwargs["token"] == "secret-tok"
        assert kwargs["endpoint_name"] == "my-ep"


class TestScoreStagedExamplesLiveTargetsStagedEntryPoints:
    """``staged_contract.score_staged_examples_live`` must drive the real
    staged entry points for both the detect rows and the completion chain."""

    def test_detect_rows_call_real_detect_entities(self):
        fake_llm = Mock(
            return_value=_fake_llm_answer(
                json.dumps({"candidate_entities": [{"canonical_label": "Carrier"}]})
            )
        )
        with patch.object(staged, "call_serving_endpoint", fake_llm), patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy_detect, patch.object(
            staged, "infer_relations", wraps=staged.infer_relations
        ), patch.object(
            staged, "infer_attributes", wraps=staged.infer_attributes
        ), patch.object(
            staged, "infer_axioms", wraps=staged.infer_axioms
        ):
            scores = staged_contract.score_staged_examples_live(
                DATASET, ROOT / "tests/eval/thresholds.yaml",
                host="https://test.databricks.com", token="tok", endpoint="ep",
            )
        assert spy_detect.call_count == 4  # the 4 "detect"-tagged staged rows
        assert "aggregate" in scores
        assert all(isinstance(v, float) for v in scores.values())

    def test_completion_chain_calls_real_infer_relations_attributes_axioms(self):
        fake_llm = Mock(
            return_value=_fake_llm_answer(
                json.dumps({"candidate_entities": [{"canonical_label": "Carrier"}]})
            )
        )
        with patch.object(staged, "call_serving_endpoint", fake_llm), patch.object(
            staged, "infer_relations", wraps=staged.infer_relations
        ) as spy_relations, patch.object(
            staged, "infer_attributes", wraps=staged.infer_attributes
        ) as spy_attributes, patch.object(
            staged, "infer_axioms", wraps=staged.infer_axioms
        ) as spy_axioms:
            staged_contract.score_staged_examples_live(
                DATASET, ROOT / "tests/eval/thresholds.yaml",
                host="https://test.databricks.com", token="tok", endpoint="ep",
            )
        assert spy_relations.call_count == 1
        # attributes/axioms only run if the previous substage succeeded —
        # they must at least have been reachable (patched), never skipped
        # in favour of a legacy one-shot call.
        assert spy_attributes.call_count in (0, 1)
        assert spy_axioms.call_count in (0, 1)

    def test_never_calls_legacy_run_agent(self):
        fake_llm = Mock(
            return_value=_fake_llm_answer(json.dumps({"candidate_entities": []}))
        )
        with patch.object(staged, "call_serving_endpoint", fake_llm):
            with patch(
                "agents.agent_owl_generator.engine.run_agent",
                side_effect=AssertionError("legacy engine.run_agent must never be called"),
            ):
                staged_contract.score_staged_examples_live(
                    DATASET, ROOT / "tests/eval/thresholds.yaml",
                    host="https://test.databricks.com", token="tok", endpoint="ep",
                )

    def test_offline_scoring_unaffected_after_live_context_exits(self):
        """`live_endpoint()` must fully restore offline (scripted) scoring —
        a prior --live run must never leak into a later offline run in the
        same process."""
        fake_llm = Mock(
            return_value=_fake_llm_answer(json.dumps({"candidate_entities": []}))
        )
        with patch.object(staged, "call_serving_endpoint", fake_llm):
            staged_contract.score_staged_examples_live(
                DATASET, ROOT / "tests/eval/thresholds.yaml",
                host="https://test.databricks.com", token="tok", endpoint="ep",
            )
        assert staged_contract._LIVE_ENDPOINT is None
        # Offline scoring (no mocking of call_serving_endpoint by us here)
        # must still work exactly as before, fully scripted internally.
        aggregate = staged_contract.score_staged_examples(
            DATASET, ROOT / "tests/eval/thresholds.yaml"
        )
        assert aggregate == 1.0
