"""Structured-schema parsing/validation for the staged owl-generator.

Covers the Stage-1 detection candidate schema and the Stage-3 completion
(relations/attributes/axioms) schemas parsed by
``agents.agent_owl_generator.schemas``. These parsers are the reject-only
boundary for the staged contract: a malformed or out-of-schema LLM answer
raises :class:`SchemaValidationError` (never a silent rewrite), and the
referenced-id extractors feed the Stage-3 entity-closure check.
"""

from __future__ import annotations

import pytest

from back.objects.ontology.GenerateDraft import (
    GenerateEntity,
    ORIGIN_DETECTED,
    ORIGIN_MANUAL,
    TYPE_CLASS,
)
from agents.agent_owl_generator import schemas


# ---------------------------------------------------------------------------
# Detection candidate schema
# ---------------------------------------------------------------------------


class TestParseDetectionPayload:
    def test_parses_minimal_candidate(self):
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        candidates = schemas.parse_detection_payload(payload)
        assert len(candidates) == 1
        c = candidates[0]
        assert isinstance(c, GenerateEntity)
        assert c.canonical_label == "Carrier"
        # Detection defaults every candidate to included and origin=detected.
        assert c.included is True
        assert c.origin == ORIGIN_DETECTED
        assert c.type_hint == TYPE_CLASS
        # A detection-time id is minted (the LLM does not supply one).
        assert c.id.startswith("cand-")

    def test_strips_markdown_fences(self):
        payload = (
            "```json\n"
            '{"candidate_entities": [{"canonical_label": "Order"}]}\n'
            "```"
        )
        candidates = schemas.parse_detection_payload(payload)
        assert [c.canonical_label for c in candidates] == ["Order"]

    def test_malformed_json_is_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_detection_payload("not json at all {{{")

    def test_missing_candidate_list_is_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_detection_payload('{"stuff": []}')

    def test_synonyms_kept_as_alternate_labels_not_separate_entities(self):
        payload = (
            '{"candidate_entities": [{"canonical_label": "Customer", '
            '"alternate_labels": ["Client", "Account Holder"]}]}'
        )
        candidates = schemas.parse_detection_payload(payload)
        assert len(candidates) == 1
        assert candidates[0].alternate_labels == ["Client", "Account Holder"]

    def test_dedup_against_anchor_canonical_label(self):
        anchors = [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")]
        payload = (
            '{"candidate_entities": ['
            '{"canonical_label": "Customer"}, {"canonical_label": "Carrier"}]}'
        )
        candidates = schemas.parse_detection_payload(payload, existing_anchors=anchors)
        assert [c.canonical_label for c in candidates] == ["Carrier"]

    def test_dedup_against_anchor_alternate_label(self):
        anchors = [
            GenerateEntity.locked_anchor(
                "cls-Order-b2", "Order", alternate_labels=["PurchaseOrder"]
            )
        ]
        payload = (
            '{"candidate_entities": ['
            '{"canonical_label": "PurchaseOrder"}, {"canonical_label": "Invoice"}]}'
        )
        candidates = schemas.parse_detection_payload(payload, existing_anchors=anchors)
        assert [c.canonical_label for c in candidates] == ["Invoice"]

    def test_dedup_when_candidate_alt_label_matches_anchor(self):
        anchors = [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")]
        payload = (
            '{"candidate_entities": ['
            '{"canonical_label": "Buyer", "alternate_labels": ["Customer"]}]}'
        )
        candidates = schemas.parse_detection_payload(payload, existing_anchors=anchors)
        assert candidates == []

    def test_dedup_between_candidates(self):
        payload = (
            '{"candidate_entities": ['
            '{"canonical_label": "Carrier"}, {"canonical_label": "carrier"}]}'
        )
        candidates = schemas.parse_detection_payload(payload)
        assert len(candidates) == 1

    def test_empty_candidate_list_is_accepted_not_rejected(self):
        # Live bug fix: the mandatory answer when every grounded entity is
        # already a locked anchor (or nothing new exists) is an explicit
        # empty list — that is a valid, successful parse, not a schema
        # violation.
        candidates = schemas.parse_detection_payload('{"candidate_entities": []}')
        assert candidates == []

    def test_empty_candidate_list_accepted_even_with_anchors_present(self):
        anchors = [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")]
        candidates = schemas.parse_detection_payload(
            '{"candidate_entities": []}', existing_anchors=anchors
        )
        assert candidates == []

    def test_unknown_type_hint_coerced_to_class(self):
        payload = (
            '{"candidate_entities": [{"canonical_label": "Order", '
            '"type_hint": "table"}]}'
        )
        candidates = schemas.parse_detection_payload(payload)
        assert candidates[0].type_hint == TYPE_CLASS

    def test_max_candidates_bounds_result(self):
        items = ",".join(
            '{"canonical_label": "E%d"}' % i for i in range(20)
        )
        payload = '{"candidate_entities": [%s]}' % items
        candidates = schemas.parse_detection_payload(payload, max_candidates=5)
        assert len(candidates) == 5

    def test_evidence_carried_through(self):
        payload = (
            '{"candidate_entities": [{"canonical_label": "Carrier", '
            '"evidence": [{"source": "spec.pdf", "excerpt": "Order ships via Carrier."}]}]}'
        )
        candidates = schemas.parse_detection_payload(payload)
        assert candidates[0].evidence == [
            {"source": "spec.pdf", "excerpt": "Order ships via Carrier."}
        ]


# ---------------------------------------------------------------------------
# Transport-level structured-output schema (response_format json_schema)
#
# Live-reliability fix: prompt-only "JSON only" instructions cannot force a
# compliant model to skip a visible reasoning preamble (observed live: 4/5
# calls against the user's own endpoint still narrated prose before the
# JSON, despite the strengthened prompt). The user confirmed that same
# endpoint DOES honour OpenAI/Databricks-style
# ``response_format={"type": "json_schema", "json_schema": {...}}`` and
# returns exactly the schema-shaped JSON. This constant is the strict
# json_schema Stage 1 passes as that transport-level ``response_format`` on
# its single finalization call — never on the tool-gathering calls, which
# the endpoint rejects when combined with ``tools``.
# ---------------------------------------------------------------------------


class TestDetectionResponseFormat:
    def test_is_a_json_schema_response_format(self):
        rf = schemas.DETECTION_RESPONSE_FORMAT
        assert rf["type"] == "json_schema"
        assert "json_schema" in rf
        assert rf["json_schema"]["strict"] is True

    def test_schema_requires_candidate_entities_array(self):
        schema = schemas.DETECTION_RESPONSE_FORMAT["json_schema"]["schema"]
        assert schema["type"] == "object"
        assert schema["required"] == ["candidate_entities"]
        assert schema["additionalProperties"] is False
        items = schema["properties"]["candidate_entities"]["items"]
        assert items["type"] == "object"

    def test_item_schema_matches_the_parser_contract(self):
        # The exact fields `parse_detection_payload` reads: canonical_label,
        # description, type_hint (class-only for Stage 1), evidence
        # (source/excerpt), alternate_labels.
        items = schemas.DETECTION_RESPONSE_FORMAT["json_schema"]["schema"][
            "properties"
        ]["candidate_entities"]["items"]
        assert set(items["properties"]) == {
            "canonical_label",
            "description",
            "type_hint",
            "evidence",
            "alternate_labels",
        }
        assert set(items["required"]) == set(items["properties"])
        assert items["additionalProperties"] is False
        assert items["properties"]["type_hint"]["enum"] == [TYPE_CLASS]

        evidence_item = items["properties"]["evidence"]["items"]
        assert set(evidence_item["properties"]) == {"source", "excerpt"}
        assert set(evidence_item["required"]) == {"source", "excerpt"}
        assert evidence_item["additionalProperties"] is False

    def test_response_format_produces_an_endpoint_ready_empty_answer(self):
        # Sanity check: an answer that is exactly what the schema demands for
        # zero new candidates must still parse cleanly through the same
        # parser used for every other detection answer.
        candidates = schemas.parse_detection_payload('{"candidate_entities": []}')
        assert candidates == []


# ---------------------------------------------------------------------------
# Completion schemas + referenced-id extraction
# ---------------------------------------------------------------------------


class TestParseRelationsPayload:
    def test_parses_relations(self):
        payload = (
            '{"relations": [{"label": "placesOrder", '
            '"domain": "cls-Customer-a1", "range": "cand-6"}]}'
        )
        result = schemas.parse_relations_payload(payload)
        assert result["relations"][0]["label"] == "placesOrder"

    def test_missing_relations_key_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_relations_payload('{"nope": []}')

    def test_relation_missing_domain_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_relations_payload(
                '{"relations": [{"label": "x", "range": "cand-6"}]}'
            )

    def test_referenced_ids_union_domain_range(self):
        payload = (
            '{"relations": [{"label": "placesOrder", '
            '"domain": "cls-Customer-a1", "range": "cand-6"}]}'
        )
        result = schemas.parse_relations_payload(payload)
        assert schemas.relations_referenced_ids(result) == {
            "cls-Customer-a1",
            "cand-6",
        }


class TestParseAttributesPayload:
    def test_parses_attributes(self):
        payload = (
            '{"attributes": [{"label": "orderDate", '
            '"domain": "cand-6", "datatype": "xsd:date"}]}'
        )
        result = schemas.parse_attributes_payload(payload)
        assert result["attributes"][0]["label"] == "orderDate"

    def test_referenced_ids_are_domains(self):
        payload = (
            '{"attributes": ['
            '{"label": "orderDate", "domain": "cand-6", "datatype": "xsd:date"},'
            '{"label": "name", "domain": "cls-Customer-a1", "datatype": "xsd:string"}]}'
        )
        result = schemas.parse_attributes_payload(payload)
        assert schemas.attributes_referenced_ids(result) == {
            "cand-6",
            "cls-Customer-a1",
        }

    def test_attribute_missing_domain_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_attributes_payload(
                '{"attributes": [{"label": "x", "datatype": "xsd:string"}]}'
            )


class TestParseAxiomsPayload:
    def test_parses_axioms(self):
        payload = (
            '{"axioms": [{"kind": "subClassOf", '
            '"subject": "cand-6", "object": "cls-Customer-a1"}]}'
        )
        result = schemas.parse_axioms_payload(payload)
        assert result["axioms"][0]["kind"] == "subClassOf"

    def test_referenced_ids_union_subject_object(self):
        payload = (
            '{"axioms": [{"kind": "disjointWith", '
            '"subject": "cand-6", "object": "cls-UnknownGhost"}]}'
        )
        result = schemas.parse_axioms_payload(payload)
        assert schemas.axioms_referenced_ids(result) == {
            "cand-6",
            "cls-UnknownGhost",
        }

    def test_axiom_missing_subject_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_axioms_payload(
                '{"axioms": [{"kind": "subClassOf", "object": "cls-Customer-a1"}]}'
            )
