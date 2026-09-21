"""Prompt-first stage prompts for the staged owl-generator.

Per the design's *Prompt-First Pitfall Handling (Rewrite Loop Removed)*
section and SPEC §6a: pitfall constraints (naming rules, orphan avoidance,
domain/range completeness, class-count guidance) are stated **up front** in
each stage's system prompt instead of being corrected after the fact by a
post-generation rewrite loop. Deterministic validation still runs afterwards
but is reject-only (see :mod:`agents.agent_owl_generator.schemas` and
:meth:`GenerateDraft.validate_references`).

Every prompt is JSON-output only — the staged agent produces bounded
structured records addressed by stable entity id, never free-text Turtle.
"""

from __future__ import annotations

from typing import Sequence

from back.objects.ontology.GenerateDraft import GenerateDraft, GenerateEntity

# Shared naming constraints, stated once and injected into every stage prompt.
_NAMING_RULES = """\
# NAMING RULES (CRITICAL — NO EXCEPTIONS)
• Classes: PascalCase (Customer, SalesOrder).
• Object/data properties: lowerCamelCase (placesOrder, orderDate).
• No spaces, underscores, hyphens, or escapes in local names.
• NEVER embed the domain or range class name inside a property name
  (❌ hasPersonName / orderContainsItem — ✅ hasName / contains)."""


# ---------------------------------------------------------------------------
# Stage 1 — detection
# ---------------------------------------------------------------------------


def _anchor_lines(existing_anchors: Sequence[GenerateEntity]) -> str:
    lines = []
    for anchor in existing_anchors:
        alt = ", ".join(anchor.alternate_labels)
        suffix = f" (also known as: {alt})" if alt else ""
        lines.append(f"  • {anchor.canonical_label} [{anchor.id}]{suffix}")
    return "\n".join(lines)


def build_detection_system_prompt(
    *, existing_anchors: Sequence[GenerateEntity] = ()
) -> str:
    """System prompt for Stage 1 candidate-entity detection.

    Lists the locked anchors (canonical + alternate labels) so the model
    deduplicates against them and never re-proposes an existing entity.
    """
    anchors_block = _anchor_lines(existing_anchors)
    if anchors_block:
        anchors_section = (
            "# EXISTING ONTOLOGY ENTITIES (LOCKED ANCHORS — DO NOT RE-PROPOSE)\n"
            "These already exist. Never return any of them (or any of their "
            "alternate labels) as a NEW candidate:\n"
            f"{anchors_block}\n"
        )
    else:
        anchors_section = (
            "# EXISTING ONTOLOGY ENTITIES\nThe ontology is currently empty.\n"
        )

    return f"""\
You are an ontology engineer performing ENTITY DETECTION only.

Read the selected table metadata and the READY parsed documents (use your
tools) and propose the real-world ENTITIES (classes) the domain needs. You do
NOT design relations, attributes, or axioms — that happens in a later, human-
reviewed stage. Propose one class per real-world entity; never a class per
column or per attribute value.

{anchors_section}
# SYNONYMS
Any synonym you find in metadata comments or document text (e.g. "Client" for
"Customer") is an ALTERNATE LABEL of a single candidate — put it in
`alternate_labels`, NEVER as a separate candidate entity and NEVER only in the
description prose.

{_NAMING_RULES}

# OUTPUT (JSON ONLY — NO PROSE, NO CODE FENCES)
Return a single JSON object:
{{"candidate_entities": [
  {{"canonical_label": "Carrier",
    "description": "Company that ships an Order to a Customer.",
    "type_hint": "class",
    "evidence": [{{"source": "spec.pdf", "excerpt": "Order ships via Carrier."}}],
    "alternate_labels": ["Shipper", "Freight Company"]}}
]}}
Prefer 8–25 candidates for a typical domain; hard cap 40. Ground every
candidate in the metadata or a ready document."""


def build_detection_user_prompt(
    *, guidelines: str, selected_tables: Sequence[str], selected_docs: Sequence[str]
) -> str:
    parts = []
    if selected_tables:
        parts.append(f"Selected tables: {', '.join(selected_tables)}")
    if selected_docs:
        parts.append(f"Selected documents: {', '.join(selected_docs)}")
    parts.append(
        "Guidelines: "
        + (guidelines or "Detect the core entities of the domain.")
    )
    parts.append(
        "Use get_metadata / get_table_detail to inspect tables and "
        "list_documents / read_document to read READY documents, then return "
        "the candidate_entities JSON."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Stage 3 — completion (relations / attributes / axioms)
# ---------------------------------------------------------------------------


def _entity_catalog(draft: GenerateDraft) -> str:
    """The closed, id-addressed entity set completion may reference.

    Only locked anchors and *included* candidates appear — excluded
    candidates are deliberately absent (they are outside the closure and any
    reference to them is rejected). Alternate labels are surfaced as lexical
    evidence for naming heuristics.
    """
    lines = []
    for entity in (*draft.existing_anchors, *draft.candidate_entities):
        if not entity.included:
            continue
        alt = ", ".join(entity.alternate_labels)
        alt_suffix = f" | alt: {alt}" if alt else ""
        desc = f" — {entity.description}" if entity.description else ""
        lines.append(f"  • [{entity.id}] {entity.canonical_label}{alt_suffix}{desc}")
    return "\n".join(lines)


_CLOSURE_RULE = (
    "You MUST reference entities ONLY by the ids listed above. NEVER invent a "
    "new entity or a new id. Any output referencing an id not listed will be "
    "rejected."
)


def build_relations_system_prompt() -> str:
    return f"""\
You are an ontology engineer inferring OBJECT-PROPERTY RELATIONS between a
fixed, closed set of entities.

{_CLOSURE_RULE}

{_NAMING_RULES}
• At most ONE relation between any ordered pair; choose the most natural
  direction. Never create bidirectional relations.

# OUTPUT (JSON ONLY — NO PROSE, NO CODE FENCES)
{{"relations": [
  {{"label": "placesOrder", "domain": "<entity_id>", "range": "<entity_id>",
    "evidence": "short justification"}}
]}}"""


def build_attributes_system_prompt() -> str:
    return f"""\
You are an ontology engineer inferring DATATYPE ATTRIBUTES for a fixed, closed
set of entities.

{_CLOSURE_RULE}

{_NAMING_RULES}
• Each attribute has an xsd datatype (xsd:string, xsd:integer, xsd:date, …).
• Exclude surrogate keys, audit columns, and foreign keys already carried by
  relations.

# OUTPUT (JSON ONLY — NO PROSE, NO CODE FENCES)
{{"attributes": [
  {{"label": "orderDate", "domain": "<entity_id>", "datatype": "xsd:date",
    "evidence": "short justification"}}
]}}"""


def build_axioms_system_prompt() -> str:
    return f"""\
You are an ontology engineer inferring lightweight OWL AXIOMS over a fixed,
closed set of entities.

{_CLOSURE_RULE}

# RULES
• kind ∈ {{subClassOf, disjointWith, equivalentClass}}.
• Prefer OWL 2 EL-like modelling; add an axiom only when justified.
• Never make a class disjoint from its own subclass.

# OUTPUT (JSON ONLY — NO PROSE, NO CODE FENCES)
{{"axioms": [
  {{"kind": "subClassOf", "subject": "<entity_id>", "object": "<entity_id>"}}
]}}"""


def _prior_results_block(draft: GenerateDraft, substages: Sequence[str]) -> str:
    lines = []
    for substage in substages:
        checkpoint = draft.completion_checkpoints.get(substage) or {}
        if checkpoint.get("status") == "done" and checkpoint.get("result"):
            lines.append(
                f"Already computed {substage}: {checkpoint['result']}"
            )
    return "\n".join(lines)


def build_relations_user_prompt(draft: GenerateDraft) -> str:
    return (
        "Closed entity set (reference ONLY these ids):\n"
        f"{_entity_catalog(draft)}\n\n"
        "Return the relations JSON."
    )


def build_attributes_user_prompt(draft: GenerateDraft) -> str:
    prior = _prior_results_block(draft, ("relations",))
    prior_section = f"\n{prior}\n" if prior else "\n"
    return (
        "Closed entity set (reference ONLY these ids):\n"
        f"{_entity_catalog(draft)}\n"
        f"{prior_section}"
        "\nReturn the attributes JSON."
    )


def build_axioms_user_prompt(draft: GenerateDraft) -> str:
    prior = _prior_results_block(draft, ("relations", "attributes"))
    prior_section = f"\n{prior}\n" if prior else "\n"
    return (
        "Closed entity set (reference ONLY these ids):\n"
        f"{_entity_catalog(draft)}\n"
        f"{prior_section}"
        "\nReturn the axioms JSON."
    )
