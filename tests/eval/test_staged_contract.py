"""Focused tests for the staged-contract eval harness.

Review fix (Task 3 findings, item 1): the *detection*-tagged constraint
checks in ``staged_contract.py`` must exercise the real
``staged.detect_entities()`` orchestrator — built from each dataset
example's actual ``input.metadata`` / ``input.corpus`` /
``input.existing_ontology`` — instead of calling
``schemas.parse_detection_payload`` directly with a hand-written literal.
``_c_does_not_parse`` must be based on the tool surface/dispatch actually
observed on that real run, not only a static ``TOOL_DEFINITIONS`` name
check. The deterministic/offline eval stays offline: only
``staged.call_serving_endpoint`` is scripted.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import staged_contract

from agents.agent_owl_generator import staged
from agents.engine_base import AgentStep

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


def _constraint(example: dict, kind: str) -> dict:
    return next(c for c in example["expected"]["constraints"] if c["kind"] == kind)


class TestDetectionChecksExerciseRealOrchestrator:
    """Detection-tagged constraints must drive the real staged entry point."""

    def test_excludes_anchor_check_calls_detect_entities(self):
        example = _load_example("staged-detect-new-entities-001")
        constraint = _constraint(example, "excludes_existing_anchor_as_new")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_excludes_anchor(example, constraint) is True
        assert spy.call_count == 1

    def test_all_included_check_calls_detect_entities(self):
        example = _load_example("staged-default-inclusion-001")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_all_included(example, {}) is True
        assert spy.call_count == 1

    def test_synonyms_check_calls_detect_entities(self):
        example = _load_example("staged-alternate-labels-001")
        constraint = _constraint(example, "synonyms_as_alternate_labels")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_synonyms_as_alt(example, constraint) is True
        assert spy.call_count == 1

    def test_min_new_candidates_uses_examples_own_anchors(self):
        """The real orchestrator call must be seeded from the example's
        ``existing_ontology``, not a literal baked into the check."""
        example = _load_example("staged-detect-new-entities-001")
        constraint = _constraint(example, "min_new_candidate_entities")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_min_new_candidates(example, constraint) is True
        assert spy.call_count == 1
        _, kwargs = spy.call_args
        anchor_labels = {a.canonical_label for a in kwargs["existing_anchors"]}
        assert anchor_labels == {"Customer"}

    def test_excludes_anchor_alt_check_calls_detect_entities(self):
        example = _load_example("staged-locked-anchor-dedup-001")
        constraint = _constraint(example, "excludes_existing_alternate_label_as_new")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_excludes_anchor_alt(example, constraint) is True
        assert spy.call_count == 1


class TestDoesNotParseObservesProductionPath:
    """``does_not_parse`` must react to what actually happened, not only the
    static tool-definition list."""

    def test_detects_a_banned_tool_actually_dispatched(self):
        """Even if a bug caused ``ai_parse_document`` to be dispatched
        despite never appearing in ``TOOL_DEFINITIONS``, the check must
        catch it from the observed run — a purely static name check on
        ``TOOL_DEFINITIONS`` would incorrectly pass here."""
        example = _load_example("staged-detect-new-entities-001")
        constraint = _constraint(example, "does_not_parse")
        fake_result = staged.DetectionResult(
            success=True,
            candidate_entities=[],
            steps=[
                AgentStep(
                    step_type="tool_call", content="{}", tool_name="ai_parse_document"
                )
            ],
        )
        with patch.object(staged, "detect_entities", return_value=fake_result):
            assert staged_contract._c_does_not_parse(example, constraint) is False

    def test_passes_for_the_real_detection_run(self):
        example = _load_example("staged-detect-new-entities-001")
        constraint = _constraint(example, "does_not_parse")
        with patch.object(
            staged, "detect_entities", wraps=staged.detect_entities
        ) as spy:
            assert staged_contract._c_does_not_parse(example, constraint) is True
        assert spy.call_count == 1

    def test_completion_variant_still_checks_tools_is_none(self):
        """The one non-detection ``does_not_parse`` row (completion stage)
        keeps being verified via the real completion orchestrator."""
        example = _load_example("staged-no-reparse-completion-001")
        constraint = _constraint(example, "does_not_parse")
        assert staged_contract._c_does_not_parse(example, constraint) is True
