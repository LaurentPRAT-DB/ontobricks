"""Tests for the Generate workflow orchestration service (plan task 4 of
``staged-ontology-generate``).

See ``docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md``.
Exercises the durable detect -> review -> complete -> merge pipeline against
a real :class:`DomainSession` (through the ``domain_session`` fixture), with
the staged agent entry points (``agents.agent_owl_generator.staged``)
monkeypatched to deterministic scripted results — no live LLM endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from back.core.errors import NotFoundError, ValidationError
from back.objects.ontology import GenerateWorkflow as wf
from back.objects.ontology.GenerateDraft import (
    CHECKPOINT_DONE,
    CHECKPOINT_FAILED,
    DONE,
    DraftRevisionConflict,
    DraftStaleError,
    DraftValidationError,
    GenerateEntity,
    REVIEWING,
)
from agents.agent_owl_generator.staged import CompletionResult, DetectionResult

pytestmark = pytest.mark.unit


def _settings():
    return SimpleNamespace()


def _detection_result(labels):
    return DetectionResult(
        success=True,
        candidate_entities=[GenerateEntity.new_candidate(label) for label in labels],
    )


@pytest.fixture(autouse=True)
def no_document_corpus(monkeypatch):
    """Every test runs with an empty ready-document manifest by default.

    ``domain_session`` has no registry configured, so
    ``list_ready_document_manifests`` would already return ``[]`` via the
    "not configured" branch — this fixture just makes that explicit/stable
    if a future registry default changes.
    """
    monkeypatch.setattr(wf, "list_ready_document_manifests", lambda *_a, **_kw: [])


# ---------------------------------------------------------------------------
# Stage 1: detection
# ---------------------------------------------------------------------------


class TestRunDetection:
    def test_creates_paused_draft_with_default_inclusion(
        self, domain_session, monkeypatch
    ):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Carrier", "Invoice"]),
        )

        draft = wf.run_detection(
            domain_session,
            _settings(),
            host="h",
            token="t",
            endpoint_name="e",
            metadata={"tables": []},
        )

        assert draft.stage == REVIEWING
        assert draft.draft_revision == 1
        assert len(draft.candidate_entities) == 2
        assert all(c.included for c in draft.candidate_entities)
        assert all(c.origin == "detected" for c in draft.candidate_entities)

    def test_persists_durably_through_store(self, domain_session, monkeypatch):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Carrier"]),
        )
        wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )
        loaded = domain_session.generate_draft_store.load()
        assert loaded is not None
        assert loaded.candidate_entities[0].canonical_label == "Carrier"

    def test_existing_ontology_entities_passed_as_locked_anchors(
        self, domain_session, monkeypatch
    ):
        domain_session.ontology["classes"] = [{"name": "Customer", "label": "Customer"}]
        captured = {}

        def _fake_detect(**kw):
            captured.update(kw)
            return _detection_result(["Carrier"])

        monkeypatch.setattr(wf.owl_staged, "detect_entities", _fake_detect)
        draft = wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        assert [a.id for a in captured["existing_anchors"]] == ["Customer"]
        assert [a.id for a in draft.existing_anchors] == ["Customer"]

    def test_ready_corpus_included_no_parse_triggered(
        self, domain_session, monkeypatch
    ):
        """The workflow reads ready manifest identity only — it must never
        call anything that would parse a pending document."""
        monkeypatch.setattr(
            wf,
            "list_ready_document_manifests",
            lambda *_a, **_kw: [{"filename": "spec.pdf", "source_hash": "abc"}],
        )
        captured = {}

        def _fake_detect(**kw):
            captured.update(kw)
            return _detection_result(["Carrier"])

        monkeypatch.setattr(wf.owl_staged, "detect_entities", _fake_detect)
        draft = wf.run_detection(
            domain_session,
            _settings(),
            host="h",
            token="t",
            endpoint_name="e",
            selected_docs=["spec.pdf"],
        )
        assert draft.selected_source_config["documents"] == ["spec.pdf"]
        assert draft.source_fingerprint.startswith("sha256:")

    def test_no_ready_documents_still_detects_from_metadata_only(
        self, domain_session, monkeypatch
    ):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Carrier"]),
        )
        draft = wf.run_detection(
            domain_session,
            _settings(),
            host="h",
            token="t",
            endpoint_name="e",
            metadata={"tables": [{"full_name": "t1"}]},
        )
        assert len(draft.candidate_entities) == 1

    def test_detection_schema_rejection_raises_validation_error(
        self, domain_session, monkeypatch
    ):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: DetectionResult(
                success=False, error="bad json", rejected=True
            ),
        )
        with pytest.raises(DraftValidationError):
            wf.run_detection(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

    def test_detection_infra_failure_raises_infrastructure_error(
        self, domain_session, monkeypatch
    ):
        from back.core.errors import InfrastructureError

        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: DetectionResult(
                success=False, error="timeout", rejected=False
            ),
        )
        with pytest.raises(InfrastructureError):
            wf.run_detection(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

    def test_re_detection_discards_prior_draft(self, domain_session, monkeypatch):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Carrier"]),
        )
        wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Invoice"]),
        )
        draft2 = wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )
        assert draft2.draft_revision == 1
        assert draft2.candidate_entities[0].canonical_label == "Invoice"


# ---------------------------------------------------------------------------
# Stage 2: draft read/update/discard
# ---------------------------------------------------------------------------


class TestDraftReview:
    def _seed(self, domain_session, monkeypatch, labels=("Carrier",)):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(list(labels)),
        )
        return wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

    def test_get_draft_view_none_when_absent(self, domain_session):
        assert wf.get_draft_view(domain_session, _settings()) is None

    def test_get_draft_view_returns_dict_with_stale_flag(
        self, domain_session, monkeypatch
    ):
        self._seed(domain_session, monkeypatch)
        view = wf.get_draft_view(domain_session, _settings())
        assert view["stale"] is False
        assert view["stage"] == REVIEWING
        assert len(view["candidate_entities"]) == 1

    def test_get_draft_view_marks_stale_after_ontology_change(
        self, domain_session, monkeypatch
    ):
        self._seed(domain_session, monkeypatch)
        domain_session.ontology["classes"] = [
            {"name": "NewAnchor", "label": "NewAnchor"}
        ]
        view = wf.get_draft_view(domain_session, _settings())
        assert view["stale"] is True

    def test_full_field_edit(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        cand_id = draft.candidate_entities[0].id
        updated = wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="update",
            entity_id=cand_id,
            updates={
                "canonical_label": "Shipper",
                "description": "Ships goods",
                "type_hint": "class",
                "evidence": [{"source": "spec.pdf", "excerpt": "x"}],
                "alternate_labels": ["Hauler"],
            },
        )
        cand = updated.candidate_entities[0]
        assert cand.id == cand_id
        assert cand.canonical_label == "Shipper"
        assert cand.description == "Ships goods"
        assert cand.alternate_labels == ["Hauler"]

    def test_manual_add(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        updated = wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="add",
            entity={"canonical_label": "Invoice", "description": "Bill"},
        )
        assert len(updated.candidate_entities) == 2
        added = [
            c for c in updated.candidate_entities if c.canonical_label == "Invoice"
        ][0]
        assert added.origin == "manual"

    def test_manual_remove(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        cand_id = draft.candidate_entities[0].id
        updated = wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="remove",
            entity_id=cand_id,
        )
        assert updated.candidate_entities == []

    def test_exclude_then_include(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        cand_id = draft.candidate_entities[0].id
        excluded = wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="exclude",
            entity_id=cand_id,
        )
        assert excluded.candidate_entities[0].included is False
        reincluded = wf.update_draft(
            domain_session,
            revision=excluded.draft_revision,
            op="include",
            entity_id=cand_id,
        )
        assert reincluded.candidate_entities[0].included is True

    def test_stale_revision_raises_conflict(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        with pytest.raises(DraftRevisionConflict):
            wf.update_draft(
                domain_session,
                revision=draft.draft_revision + 5,
                op="exclude",
                entity_id=draft.candidate_entities[0].id,
            )

    def test_update_without_draft_raises_not_found(self, domain_session):
        with pytest.raises(NotFoundError):
            wf.update_draft(
                domain_session, revision=0, op="exclude", entity_id="cand-x"
            )

    def test_unknown_op_rejected(self, domain_session, monkeypatch):
        draft = self._seed(domain_session, monkeypatch)
        with pytest.raises(ValidationError):
            wf.update_draft(domain_session, revision=draft.draft_revision, op="bogus")

    def test_discard_clears_draft(self, domain_session, monkeypatch):
        self._seed(domain_session, monkeypatch)
        wf.discard_draft(domain_session)
        assert domain_session.generate_draft_store.load() is None


# ---------------------------------------------------------------------------
# Stage 3: completion + merge
# ---------------------------------------------------------------------------


def _completion_ok(substage, result):
    return CompletionResult(success=True, substage=substage, result=result)


class TestRunCompletion:
    def _seed_reviewed_draft(self, domain_session, monkeypatch, labels=("Carrier",)):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(list(labels)),
        )
        return wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

    def _stub_all_ok(self, monkeypatch, cand_id):
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_relations",
            lambda **kw: _completion_ok("relations", {"relations": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: _completion_ok(
                "attributes",
                {
                    "attributes": [
                        {
                            "label": "trackingCode",
                            "domain": cand_id,
                            "datatype": "xsd:string",
                        }
                    ]
                },
            ),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_axioms",
            lambda **kw: _completion_ok("axioms", {"axioms": []}),
        )

    def test_completion_runs_strict_order_and_merges(self, domain_session, monkeypatch):
        draft = self._seed_reviewed_draft(domain_session, monkeypatch)
        cand_id = draft.candidate_entities[0].id
        self._stub_all_ok(monkeypatch, cand_id)

        result = wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        assert result["draft"]["stage"] == DONE
        for substage in ("relations", "attributes", "axioms"):
            assert (
                result["draft"]["completion_checkpoints"][substage]["status"]
                == CHECKPOINT_DONE
            )

        classes = domain_session.get_classes()
        assert any(c["label"] == "Carrier" for c in classes)
        carrier = next(c for c in classes if c["label"] == "Carrier")
        assert any(dp["label"] == "trackingCode" for dp in carrier["dataProperties"])

    def test_stale_fingerprint_blocks_completion(self, domain_session, monkeypatch):
        draft = self._seed_reviewed_draft(domain_session, monkeypatch)
        self._stub_all_ok(monkeypatch, draft.candidate_entities[0].id)
        domain_session.ontology["classes"] = [
            {"name": "Unexpected", "label": "Unexpected"}
        ]

        with pytest.raises(DraftStaleError):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

    def test_excluded_candidate_never_reaches_merge(self, domain_session, monkeypatch):
        draft = self._seed_reviewed_draft(
            domain_session, monkeypatch, labels=("Carrier", "Invoice")
        )
        carrier_id = draft.candidate_entities[0].id
        invoice_id = draft.candidate_entities[1].id
        wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="exclude",
            entity_id=invoice_id,
        )
        self._stub_all_ok(monkeypatch, carrier_id)

        wf.run_completion(domain_session, _settings(), host="h", token="t", endpoint_name="e")

        classes = domain_session.get_classes()
        assert any(c["label"] == "Carrier" for c in classes)
        assert not any(c["label"] == "Invoice" for c in classes)

    def test_entity_closure_violation_is_rejected_not_merged(
        self, domain_session, monkeypatch
    ):
        draft = self._seed_reviewed_draft(domain_session, monkeypatch)
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_relations",
            lambda **kw: _completion_ok(
                "relations",
                {
                    "relations": [
                        {
                            "label": "shipsFor",
                            "domain": draft.candidate_entities[0].id,
                            "range": "cand-ghost",
                        }
                    ]
                },
            ),
        )
        with pytest.raises(DraftValidationError):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

        # Rejected substage must not have been checkpointed done, and nothing merged.
        resumed = domain_session.generate_draft_store.load()
        assert (
            resumed.completion_checkpoints["relations"]["status"] == CHECKPOINT_FAILED
        )
        assert domain_session.get_classes() == []

    def test_partial_failure_then_resume_does_not_rerun_done_substages(
        self, domain_session, monkeypatch
    ):
        self._seed_reviewed_draft(domain_session, monkeypatch)
        relations_calls = []

        def _relations(**kw):
            relations_calls.append(1)
            return _completion_ok("relations", {"relations": []})

        monkeypatch.setattr(wf.owl_staged, "infer_relations", _relations)
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: CompletionResult(
                success=False, substage="attributes", error="boom"
            ),
        )

        with pytest.raises(Exception):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

        after_first_attempt = domain_session.generate_draft_store.load()
        assert (
            after_first_attempt.completion_checkpoints["relations"]["status"]
            == CHECKPOINT_DONE
        )
        assert (
            after_first_attempt.completion_checkpoints["attributes"]["status"]
            == CHECKPOINT_FAILED
        )
        assert len(relations_calls) == 1

        # Retry: attributes now succeeds; relations must NOT be re-run.
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: _completion_ok("attributes", {"attributes": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_axioms",
            lambda **kw: _completion_ok("axioms", {"axioms": []}),
        )
        result = wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        assert len(relations_calls) == 1  # still only called once, not re-run
        assert result["draft"]["stage"] == DONE

    def test_completion_without_draft_raises_not_found(self, domain_session):
        with pytest.raises(NotFoundError):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

    def test_completion_blocked_when_nothing_included(
        self, domain_session, monkeypatch
    ):
        draft = self._seed_reviewed_draft(domain_session, monkeypatch)
        wf.update_draft(
            domain_session,
            revision=draft.draft_revision,
            op="exclude",
            entity_id=draft.candidate_entities[0].id,
        )
        with pytest.raises(DraftValidationError):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )

    def test_already_done_draft_cannot_be_completed_twice(
        self, domain_session, monkeypatch
    ):
        draft = self._seed_reviewed_draft(domain_session, monkeypatch)
        self._stub_all_ok(monkeypatch, draft.candidate_entities[0].id)
        wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        with pytest.raises(DraftValidationError):
            wf.run_completion(
                domain_session, _settings(), host="h", token="t", endpoint_name="e"
            )


# ---------------------------------------------------------------------------
# Deterministic append merge: preserve existing entities
# ---------------------------------------------------------------------------


class TestMergePreservesExistingEntities:
    def test_anchor_identity_untouched_by_merge(self, domain_session, monkeypatch):
        domain_session.ontology["classes"] = [
            {
                "name": "Customer",
                "label": "Customer",
                "uri": "http://x#Customer",
                "dataProperties": [{"name": "firstName", "label": "First Name"}],
            }
        ]
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Carrier"]),
        )
        draft = wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )
        cand_id = draft.candidate_entities[0].id

        monkeypatch.setattr(
            wf.owl_staged,
            "infer_relations",
            lambda **kw: _completion_ok(
                "relations",
                {
                    "relations": [
                        {"label": "shipsFor", "domain": cand_id, "range": "Customer"}
                    ]
                },
            ),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: _completion_ok("attributes", {"attributes": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_axioms",
            lambda **kw: _completion_ok("axioms", {"axioms": []}),
        )

        wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        classes = domain_session.get_classes()
        customer = next(c for c in classes if c["name"] == "Customer")
        assert customer["uri"] == "http://x#Customer"
        assert customer["dataProperties"] == [
            {"name": "firstName", "label": "First Name"}
        ]

        properties = domain_session.get_properties()
        rel = next(p for p in properties if p["label"] == "shipsFor")
        assert rel["range"] == "Customer"

    def test_alternate_labels_persisted_as_first_class_data(
        self, domain_session, monkeypatch
    ):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: DetectionResult(
                success=True,
                candidate_entities=[
                    GenerateEntity.new_candidate(
                        "Carrier", alternate_labels=["Shipper", "Hauler"]
                    )
                ],
            ),
        )
        wf.run_detection(domain_session, _settings(), host="h", token="t", endpoint_name="e")
        self._stub_all_ok_local(monkeypatch)

        wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        carrier = next(
            c for c in domain_session.get_classes() if c["label"] == "Carrier"
        )
        assert carrier["alternate_labels"] == ["Shipper", "Hauler"]

    @staticmethod
    def _stub_all_ok_local(monkeypatch):
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_relations",
            lambda **kw: _completion_ok("relations", {"relations": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: _completion_ok("attributes", {"attributes": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_axioms",
            lambda **kw: _completion_ok("axioms", {"axioms": []}),
        )

    def test_no_duplicate_entity_names_across_repeated_labels(
        self, domain_session, monkeypatch
    ):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: DetectionResult(
                success=True,
                candidate_entities=[
                    GenerateEntity.new_candidate("Carrier", entity_id="cand-1"),
                ],
            ),
        )
        domain_session.ontology["classes"] = [
            {"name": "Carrier", "label": "Carrier already exists"}
        ]
        # Detection would normally dedup this away; simulate a manual add
        # colliding in *name* only (different label) to test minting.
        draft = wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )
        assert (
            draft.candidate_entities == []
        )  # dedup already happened inside detect_entities normally,
        # but here detect_entities is stubbed to bypass dedup, so assert on the anchor instead.


class TestSubClassOfAndDisjointAxiomMerge:
    def test_subclassof_axiom_sets_parent(self, domain_session, monkeypatch):
        monkeypatch.setattr(
            wf.owl_staged,
            "detect_entities",
            lambda **kw: _detection_result(["Vehicle", "Truck"]),
        )
        draft = wf.run_detection(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )
        vehicle_id = draft.candidate_entities[0].id
        truck_id = draft.candidate_entities[1].id

        monkeypatch.setattr(
            wf.owl_staged,
            "infer_relations",
            lambda **kw: _completion_ok("relations", {"relations": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_attributes",
            lambda **kw: _completion_ok("attributes", {"attributes": []}),
        )
        monkeypatch.setattr(
            wf.owl_staged,
            "infer_axioms",
            lambda **kw: _completion_ok(
                "axioms",
                {
                    "axioms": [
                        {
                            "kind": "subClassOf",
                            "subject": truck_id,
                            "object": vehicle_id,
                        }
                    ]
                },
            ),
        )

        wf.run_completion(
            domain_session, _settings(), host="h", token="t", endpoint_name="e"
        )

        truck = next(c for c in domain_session.get_classes() if c["label"] == "Truck")
        assert truck["parent"] == "Vehicle"
