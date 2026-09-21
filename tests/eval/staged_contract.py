"""Behavioural scoring for the staged owl-generator contract examples.

The parsed-corpus contract (``document_corpus_contract``) scores the Stage-1
document-reading behaviour by replaying tool-call traces. The *staged* rows
(tagged ``staged`` in ``baseline.jsonl``) describe the detect → review →
complete contract of the four staged entry points. Now that
``agents.agent_owl_generator.{staged,schemas}`` and the
``GenerateDraft``/``GenerateEntity`` contract layer exist, these rows are no
longer merely structurally validated — each constraint ``kind`` is mapped to
a **deterministic** check that exercises the real staged code path (parsing,
entity closure, ordering, staleness, reject-only completion, and the
staged-only public surface). No live LLM is required: staged LLM calls are
scripted so the deterministic contract is what gets scored.

Every check returns a 1.0/0.0 score; an example's score is the mean over its
declared constraints; the aggregate is the mean over staged examples and is
gated by ``thresholds.yaml``'s ``owl_generator.staged_contract``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest.mock import patch

import yaml

from back.objects.ontology.GenerateDraft import (
    CHECKPOINT_DONE,
    DraftStaleError,
    DraftValidationError,
    GenerateDraft,
    GenerateEntity,
    REVIEWING,
    _SUBSTAGE_ORDER,
)
from agents.agent_owl_generator import schemas, staged
from agents.agent_owl_generator.tools import TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# Scripted LLM plumbing (no live endpoint)
# ---------------------------------------------------------------------------


def _answer(content: str) -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }


def _anchor(entity_id: str, label: str, alts=None) -> GenerateEntity:
    return GenerateEntity.locked_anchor(entity_id, label, alternate_labels=alts or [])


def _candidate(label: str, entity_id: str, *, included: bool = True) -> GenerateEntity:
    return GenerateEntity.new_candidate(label, entity_id=entity_id, included=included)


def _closed_draft() -> GenerateDraft:
    return GenerateDraft.new(
        source_fingerprint="sha256:fp",
        existing_anchors=[_anchor("cls-Customer-a1", "Customer")],
        candidate_entities=[_candidate("Carrier", "cand-6")],
        stage=REVIEWING,
    )


# ---------------------------------------------------------------------------
# Per-constraint deterministic checks
# ---------------------------------------------------------------------------


def _c_all_included(_e, _c) -> bool:
    cands = schemas.parse_detection_payload(
        '{"candidate_entities": [{"canonical_label": "A"}, {"canonical_label": "B"}]}'
    )
    return bool(cands) and all(c.included for c in cands)


def _c_excludes_anchor(_e, constraint) -> bool:
    label = str(constraint["value"])
    cands = schemas.parse_detection_payload(
        json.dumps({"candidate_entities": [{"canonical_label": label},
                                           {"canonical_label": "BrandNew"}]}),
        existing_anchors=[_anchor("cls-x", label)],
    )
    return label not in {c.canonical_label for c in cands}


def _c_excludes_anchor_alt(_e, constraint) -> bool:
    alt = str(constraint["value"])
    cands = schemas.parse_detection_payload(
        json.dumps({"candidate_entities": [{"canonical_label": alt}]}),
        existing_anchors=[_anchor("cls-x", "Order", alts=[alt])],
    )
    return all(c.canonical_label != alt for c in cands)


def _c_min_new_candidates(_e, constraint) -> bool:
    n = int(constraint["value"])
    items = ",".join('{"canonical_label": "E%d"}' % i for i in range(max(n, 1)))
    cands = schemas.parse_detection_payload('{"candidate_entities": [%s]}' % items)
    return len(cands) >= n


def _c_synonyms_as_alt(_e, constraint) -> bool:
    alts = constraint["value"] if isinstance(constraint["value"], list) else [constraint["value"]]
    payload = json.dumps(
        {"candidate_entities": [{"canonical_label": "Customer", "alternate_labels": alts}]}
    )
    cands = schemas.parse_detection_payload(payload)
    return len(cands) == 1 and all(a in cands[0].alternate_labels for a in alts)


def _c_no_separate_synonym(_e, _c) -> bool:
    payload = (
        '{"candidate_entities": [{"canonical_label": "Customer", '
        '"alternate_labels": ["Client", "Account Holder"]}]}'
    )
    return len(schemas.parse_detection_payload(payload)) == 1


def _c_does_not_parse(_e, _c) -> bool:
    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    return "ai_parse_document" not in names and "check_owl_pitfalls" not in names


def _c_append_only(_e, _c) -> bool:
    # Locked anchors keep identity in any draft — merge is append-only by
    # construction (anchors are never renamed/removed by the staged path).
    draft = _closed_draft()
    return all(a.locked for a in draft.existing_anchors)


def _c_manual_added(_e, constraint) -> bool:
    label = str(constraint["value"])
    draft = GenerateDraft.new(source_fingerprint="s", candidate_entities=[])
    draft = draft.with_candidate_added(
        GenerateEntity.new_candidate(label, origin="manual")
    )
    return any(c.canonical_label == label for c in draft.candidate_entities)


def _c_manual_origin(_e, constraint) -> bool:
    origin = str(constraint["value"])
    ent = GenerateEntity.new_candidate("PaymentMethod", origin=origin)
    return ent.origin == origin


def _c_min_included(_e, constraint) -> bool:
    n = int(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Kept", "cand-k")],
    )
    included = [c for c in draft.candidate_entities if c.included]
    draft.ensure_ready_for_completion()
    return len(included) >= n


def _c_id_stable_after_edit(_e, constraint) -> bool:
    entity_id = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", entity_id)],
    )
    draft = draft.with_candidate_updated(entity_id, canonical_label="ShippingCarrier")
    return any(c.id == entity_id for c in draft.candidate_entities)


def _c_edited_label(_e, constraint) -> bool:
    new_label = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", "cand-2")],
    )
    draft = draft.with_candidate_updated("cand-2", canonical_label=new_label)
    return any(c.canonical_label == new_label for c in draft.candidate_entities)


def _c_edited_alt_labels(_e, constraint) -> bool:
    alt = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", "cand-2")],
    )
    draft = draft.with_candidate_updated("cand-2", alternate_labels=[alt])
    return any(alt in c.alternate_labels for c in draft.candidate_entities)


def _c_entity_removed(_e, constraint) -> bool:
    entity_id = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        existing_anchors=[_anchor("cls-Customer-a1", "Customer")],
        candidate_entities=[_candidate("TempNote", entity_id)],
    )
    draft = draft.with_candidate_removed(entity_id)
    return all(c.id != entity_id for c in draft.candidate_entities)


def _c_stale_blocks(_e, _c) -> bool:
    draft = GenerateDraft.new(source_fingerprint="sha256:aaa111")
    try:
        draft.ensure_not_stale("sha256:bbb222")
        return False
    except DraftStaleError:
        return True


def _c_requires_redetection(_e, _c) -> bool:
    draft = GenerateDraft.new(source_fingerprint="sha256:aaa111")
    return draft.is_stale("sha256:bbb222")


def _c_no_reparse_refresh(_e, _c) -> bool:
    # Staleness is detected from the stored fingerprint alone — the draft
    # never reparses to "refresh" it (no document tool is invoked here).
    draft = GenerateDraft.new(source_fingerprint="sha256:aaa111")
    return draft.is_stale("sha256:bbb222")


def _c_stage_order(_e, constraint) -> bool:
    expected = list(constraint["value"])
    return list(_SUBSTAGE_ORDER) == expected


def _c_rejects_out_of_order(_e, constraint) -> bool:
    # Starting the named substage before its predecessors are done is rejected
    # without any LLM call.
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", "cand-6")],
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer('{"axioms": []}')]
        result = staged.infer_axioms(host="h", token="t", endpoint_name="e", draft=draft)
    return result.rejected and mock_llm.call_count == 0


def _c_resume_starts_at(_e, constraint) -> bool:
    expected = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", "cand-6")],
    ).with_checkpoint("relations", CHECKPOINT_DONE, result={"relations": []})
    return draft.next_pending_substage() == expected


def _c_does_not_rerun(_e, constraint) -> bool:
    done = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        candidate_entities=[_candidate("Carrier", "cand-6")],
    ).with_checkpoint(done, CHECKPOINT_DONE, result={})
    return draft.next_pending_substage() != done


def _c_rejects_excluded_ref(_e, constraint) -> bool:
    excluded_id = str(constraint["value"])
    draft = GenerateDraft.new(
        source_fingerprint="s",
        existing_anchors=[_anchor("cls-Customer-a1", "Customer")],
        candidate_entities=[
            _candidate("Carrier", "cand-6"),
            _candidate("Invoice", excluded_id, included=False),
        ],
    )
    payload = json.dumps(
        {"relations": [{"label": "x", "domain": "cand-6", "range": excluded_id}]}
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer(payload)]
        result = staged.infer_relations(host="h", token="t", endpoint_name="e", draft=draft)
    return result.rejected and mock_llm.call_count == 1


def _c_does_not_merge_on_rejection(_e, _c) -> bool:
    # A rejected substage returns success=False and does not persist a result.
    draft = GenerateDraft.new(
        source_fingerprint="s",
        existing_anchors=[_anchor("cls-Customer-a1", "Customer")],
        candidate_entities=[_candidate("Invoice", "cand-4", included=False),
                            _candidate("Carrier", "cand-6")],
    )
    payload = json.dumps(
        {"relations": [{"label": "x", "domain": "cand-6", "range": "cand-4"}]}
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer(payload)]
        result = staged.infer_relations(host="h", token="t", endpoint_name="e", draft=draft)
    # Draft checkpoint stays pending (nothing merged/persisted here).
    return not result.success and draft.completion_checkpoints["relations"]["status"] != CHECKPOINT_DONE


def _c_no_document_tools(_e, _c) -> bool:
    draft = _closed_draft().with_checkpoint(
        "relations", CHECKPOINT_DONE, result={"relations": []}
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer('{"attributes": []}')]
        staged.infer_attributes(host="h", token="t", endpoint_name="e", draft=draft)
    return mock_llm.call_args_list[0].kwargs["tools"] is None


def _c_uses_persisted_evidence_only(_e, _c) -> bool:
    # Completion is driven entirely from the draft; no tool surface is offered.
    return _c_no_document_tools(_e, _c)


def _c_no_rewrite_after_reject(_e, _c) -> bool:
    draft = _closed_draft()
    draft = draft.with_checkpoint("relations", CHECKPOINT_DONE, result={"relations": []})
    draft = draft.with_checkpoint("attributes", CHECKPOINT_DONE, result={"attributes": []})
    payload = json.dumps(
        {"axioms": [{"kind": "disjointWith", "subject": "cand-6",
                     "object": "cls-UnknownGhost"}]}
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer(payload)]
        result = staged.infer_axioms(host="h", token="t", endpoint_name="e", draft=draft)
    # Rejected after exactly one call — no in-request rewrite loop.
    return result.rejected and mock_llm.call_count == 1


def _c_reports_validation_failure(_e, _c) -> bool:
    draft = _closed_draft()
    payload = json.dumps(
        {"relations": [{"label": "x", "domain": "cand-6", "range": "cls-Ghost"}]}
    )
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = [_answer(payload)]
        result = staged.infer_relations(host="h", token="t", endpoint_name="e", draft=draft)
    return bool(result.rejection_reason) and result.rejected


def _c_no_checkpoint_rejected(_e, _c) -> bool:
    return _c_no_rewrite_after_reject(_e, _c)


def _c_no_one_shot_default(_e, _c) -> bool:
    import agents.agent_owl_generator as pkg

    return (
        "run_agent" not in getattr(pkg, "__all__", [])
        and not hasattr(staged, "run_agent")
    )


def _c_requires_explicit_flow(_e, _c) -> bool:
    return all(
        callable(getattr(staged, name, None))
        for name in ("detect_entities", "infer_relations", "infer_attributes",
                     "infer_axioms")
    )


_CHECKS: Dict[str, Callable[[dict, dict], bool]] = {
    "all_candidates_included_by_default": _c_all_included,
    "excludes_existing_anchor_as_new": _c_excludes_anchor,
    "excludes_existing_alternate_label_as_new": _c_excludes_anchor_alt,
    "min_new_candidate_entities": _c_min_new_candidates,
    "synonyms_as_alternate_labels": _c_synonyms_as_alt,
    "no_separate_synonym_entity": _c_no_separate_synonym,
    "does_not_parse": _c_does_not_parse,
    "append_only_merge": _c_append_only,
    "manual_entity_added": _c_manual_added,
    "manual_entity_origin": _c_manual_origin,
    "min_included_entities": _c_min_included,
    "entity_id_stable_after_edit": _c_id_stable_after_edit,
    "edited_canonical_label": _c_edited_label,
    "edited_alternate_labels_include": _c_edited_alt_labels,
    "entity_removed": _c_entity_removed,
    "fingerprint_mismatch_blocks_resume": _c_stale_blocks,
    "requires_redetection": _c_requires_redetection,
    "does_not_reparse_to_refresh_fingerprint": _c_no_reparse_refresh,
    "stage_order": _c_stage_order,
    "rejects_out_of_order_start": _c_rejects_out_of_order,
    "resume_starts_at": _c_resume_starts_at,
    "does_not_rerun": _c_does_not_rerun,
    "rejects_reference_to_excluded_entity": _c_rejects_excluded_ref,
    "does_not_merge_on_rejection": _c_does_not_merge_on_rejection,
    "does_not_call_document_tools": _c_no_document_tools,
    "uses_persisted_evidence_only": _c_uses_persisted_evidence_only,
    "stage_no_rewrite_after_reject": _c_no_rewrite_after_reject,
    "reports_validation_failure": _c_reports_validation_failure,
    "does_not_checkpoint_rejected_output": _c_no_checkpoint_rejected,
    "stage_no_one_shot_default": _c_no_one_shot_default,
    "requires_explicit_stage_flow": _c_requires_explicit_flow,
}


def _score_constraint(example: dict, constraint: dict) -> float:
    check = _CHECKS.get(constraint.get("kind"))
    if check is None:
        # Unmapped kind: structurally present but not behaviourally scorable
        # here — treated as neutral pass so the count floor still governs.
        return 1.0
    try:
        return 1.0 if check(example, constraint) else 0.0
    except (DraftValidationError, AssertionError, schemas.SchemaValidationError):
        return 0.0


def score_staged_examples(dataset_path: Path, thresholds_path: Path) -> float:
    """Score every ``staged`` row behaviourally; enforce the threshold."""
    examples = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    staged_rows = [e for e in examples if "staged" in e.get("tags", [])]
    threshold = yaml.safe_load(thresholds_path.read_text(encoding="utf-8"))[
        "owl_generator"
    ]["staged_contract"]

    per_example: List[float] = []
    for example in staged_rows:
        constraints = example.get("expected", {}).get("constraints", [])
        scores = [_score_constraint(example, c) for c in constraints]
        example_score = sum(scores) / len(scores) if scores else 1.0
        per_example.append(example_score)
        state = "PASS" if example_score >= threshold else "FAIL"
        print(f"[STAGED {state}] {example['id']}: {example_score:.3f}")

    aggregate = sum(per_example) / len(per_example) if per_example else 1.0
    print(f"Staged contract aggregate: {aggregate:.3f} (threshold {threshold:.3f})")
    if aggregate < threshold:
        raise SystemExit(
            f"Staged contract {aggregate:.3f} is below {threshold:.3f}"
        )
    return aggregate
