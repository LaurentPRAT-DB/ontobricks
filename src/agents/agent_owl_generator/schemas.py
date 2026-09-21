"""Structured schema parsing/validation for the staged owl-generator.

This is the **reject-only** boundary of the staged Generate contract (design:
``docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md``
§Structured Schemas; SPEC §3a/§6a). Each staged LLM answer is parsed here into
a bounded, typed structure:

* :func:`parse_detection_payload` — Stage 1 candidate entities, defaulted to
  ``included=true`` / ``origin=detected``, deduplicated against the locked
  anchors (by id, canonical label, and alternate labels) so an existing
  entity is never re-proposed as new;
* :func:`parse_relations_payload` / :func:`parse_attributes_payload` /
  :func:`parse_axioms_payload` — Stage 3 completion outputs, plus the
  ``*_referenced_ids`` extractors that feed the entity-closure check
  (:meth:`GenerateDraft.validate_references`).

A malformed or out-of-schema answer raises :class:`SchemaValidationError`.
The caller reports that as a failure for the user to retry — it is **never**
fed back into another LLM call within the same request (no rewrite loop).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Sequence, Set

from back.core.logging import get_logger
from back.objects.ontology.GenerateDraft import (
    DraftValidationError,
    GenerateEntity,
    ORIGIN_DETECTED,
    TYPE_CLASS,
    _VALID_TYPE_HINTS,
)

logger = get_logger(__name__)

# A fenced ```json ... ``` (or bare ``` ... ```) block the model sometimes
# wraps its answer in, despite instructions to emit raw JSON.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class SchemaValidationError(DraftValidationError):
    """A staged LLM answer is malformed or violates its structured schema.

    Subclasses :class:`DraftValidationError` so the reject-only closure check
    and the schema check surface through the same 400/validation channel.
    """


# ---------------------------------------------------------------------------
# Transport-level structured output (response_format) for Stage 1
# ---------------------------------------------------------------------------
#
# Live-reliability fix: a prompt-only "JSON only, first character must be {"
# instruction cannot force a compliant model to skip a visible reasoning
# preamble — live reproduction against the user's own endpoint
# (benoit_cayla.ontobricks-todrop.monclaudesonnetamoi) still narrated prose
# ahead of the JSON on 4 of 5 calls despite that strengthened prompt. That
# same endpoint was confirmed (by direct user testing) to honour an
# OpenAI/Databricks-style ``response_format={"type": "json_schema", ...}``
# transport directive and return exactly the schema-shaped JSON, while
# rejecting the simpler ``{"type": "json_object"}`` AND rejecting
# ``response_format`` combined with ``tools`` in the same request. This
# constant is passed as ``response_format`` on Stage 1's single
# schema-enforced finalization call only (never on the tool-gathering
# calls, which need ``tools`` instead — see
# :func:`shared.llm_target.build_llm_request`'s mutual-exclusion guard and
# :mod:`agents.agent_owl_generator.staged`'s two-phase detection flow).
#
# The schema mirrors this module's own parser contract exactly (the fields
# ``parse_detection_payload`` reads) so a strict, additionalProperties=False
# schema can never itself reject a shape the parser would have accepted —
# schemas.py's Python-side validation still runs unconditionally afterwards
# (never trust the wire, even with schema enforcement).
DETECTION_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "detection_candidate_entities",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "candidate_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "canonical_label": {"type": "string"},
                            "description": {"type": "string"},
                            "type_hint": {
                                "type": "string",
                                "enum": [TYPE_CLASS],
                            },
                            "evidence": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source": {"type": "string"},
                                        "excerpt": {"type": "string"},
                                    },
                                    "required": ["source", "excerpt"],
                                    "additionalProperties": False,
                                },
                            },
                            "alternate_labels": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "canonical_label",
                            "description",
                            "type_hint",
                            "evidence",
                            "alternate_labels",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["candidate_entities"],
            "additionalProperties": False,
        },
    },
}


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text or "")
    return match.group(1) if match else (text or "")


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse *text* (fence-tolerant) into a JSON object or reject it."""
    stripped = _strip_fences(text).strip()
    if not stripped:
        raise SchemaValidationError("empty structured output")
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise SchemaValidationError(f"output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaValidationError("structured output must be a JSON object")
    return data


# ---------------------------------------------------------------------------
# Stage 1 — detection candidates
# ---------------------------------------------------------------------------


def _coerce_evidence(raw: Any) -> List[Dict[str, str]]:
    evidence: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                evidence.append(
                    {
                        "source": str(item.get("source", "") or ""),
                        "excerpt": str(item.get("excerpt", "") or ""),
                    }
                )
            elif item:
                evidence.append({"source": "", "excerpt": str(item)})
    return evidence


def _coerce_alt_labels(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(a) for a in raw if a is not None and str(a).strip()]
    return []


def parse_detection_payload(
    text: str,
    *,
    existing_anchors: Sequence[GenerateEntity] = (),
    max_candidates: int | None = None,
) -> List[GenerateEntity]:
    """Parse a Stage-1 detection answer into deduplicated candidate entities.

    Every returned candidate defaults to ``included=true`` / ``origin=detected``
    (the reviewer opts entities out, not in). A candidate is dropped when it
    duplicates a locked anchor — or an earlier candidate — by id or by any
    normalized canonical/alternate label (synonyms are as authoritative as
    the canonical label for dedup). Synonyms stay on the candidate as
    ``alternate_labels`` and are never emitted as separate candidate entities.
    """
    data = parse_json_object(text)
    raw = data.get("candidate_entities")
    if not isinstance(raw, list):
        raise SchemaValidationError(
            "detection output must contain a 'candidate_entities' list"
        )

    anchor_ids: Set[str] = {a.id for a in existing_anchors}
    seen_labels: Set[str] = set()
    for anchor in existing_anchors:
        seen_labels |= anchor.normalized_labels()

    candidates: List[GenerateEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaValidationError(
                "each candidate_entities item must be a JSON object"
            )
        label = str(item.get("canonical_label", "") or "").strip()
        if not label:
            raise SchemaValidationError(
                "candidate entity is missing a 'canonical_label'"
            )
        type_hint = item.get("type_hint") or TYPE_CLASS
        if type_hint not in _VALID_TYPE_HINTS:
            # Stage 1 only ever proposes classes; coerce a stray hint rather
            # than reject an otherwise-usable candidate.
            type_hint = TYPE_CLASS
        try:
            entity = GenerateEntity.new_candidate(
                label,
                description=str(item.get("description", "") or ""),
                type_hint=type_hint,
                evidence=_coerce_evidence(item.get("evidence")),
                alternate_labels=_coerce_alt_labels(item.get("alternate_labels")),
                origin=ORIGIN_DETECTED,
                included=True,
            )
        except DraftValidationError as exc:
            raise SchemaValidationError(str(exc)) from exc

        if entity.id in anchor_ids:
            continue
        labels = entity.normalized_labels()
        if labels & seen_labels:
            # Deduplicated against a locked anchor or an earlier candidate.
            continue
        seen_labels |= labels
        candidates.append(entity)
        if max_candidates and len(candidates) >= max_candidates:
            break
    return candidates


# ---------------------------------------------------------------------------
# Stage 3 — completion outputs
# ---------------------------------------------------------------------------


def _require_list(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    raw = data.get(key)
    if not isinstance(raw, list):
        raise SchemaValidationError(f"output must contain a '{key}' list")
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaValidationError(f"each '{key}' item must be a JSON object")
    return raw


def _require_fields(item: Dict[str, Any], fields: Sequence[str], kind: str) -> None:
    for field_name in fields:
        value = item.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SchemaValidationError(f"{kind} is missing required '{field_name}'")


def parse_relations_payload(text: str) -> Dict[str, Any]:
    """Parse a Stage-3 relations answer: ``{"relations": [{label, domain, range}]}``."""
    data = parse_json_object(text)
    relations = _require_list(data, "relations")
    for rel in relations:
        _require_fields(rel, ("label", "domain", "range"), "relation")
    return {"relations": relations}


def parse_attributes_payload(text: str) -> Dict[str, Any]:
    """Parse a Stage-3 attributes answer: ``{"attributes": [{label, domain, datatype}]}``."""
    data = parse_json_object(text)
    attributes = _require_list(data, "attributes")
    for attr in attributes:
        _require_fields(attr, ("label", "domain", "datatype"), "attribute")
    return {"attributes": attributes}


def parse_axioms_payload(text: str) -> Dict[str, Any]:
    """Parse a Stage-3 axioms answer: ``{"axioms": [{kind, subject, object}]}``."""
    data = parse_json_object(text)
    axioms = _require_list(data, "axioms")
    for axiom in axioms:
        _require_fields(axiom, ("kind", "subject", "object"), "axiom")
    return {"axioms": axioms}


def relations_referenced_ids(result: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for rel in result.get("relations", []):
        for role in ("domain", "range"):
            value = rel.get(role)
            if value:
                ids.add(str(value))
    return ids


def attributes_referenced_ids(result: Dict[str, Any]) -> Set[str]:
    return {
        str(attr["domain"])
        for attr in result.get("attributes", [])
        if attr.get("domain")
    }


def axioms_referenced_ids(result: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for axiom in result.get("axioms", []):
        for role in ("subject", "object"):
            value = axiom.get(role)
            if value:
                ids.add(str(value))
    return ids
