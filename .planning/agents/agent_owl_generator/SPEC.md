# SPEC: agent_owl_generator

> Required by `.cursor/12-ai-feature-lifecycle.mdc`.
> Material change in progress: staged Generate (design:
> `docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md`,
> plan: `staged-ontology-generate`). **Task 4 has landed**: the staged agent
> entry points (`detect_entities`, `infer_relations`, `infer_attributes`,
> `infer_axioms` in `agents.agent_owl_generator.staged`) are now wired into a
> durable async workflow (`back.objects.ontology.GenerateWorkflow`) with
> checkpoint persistence and an append-only final merge, exposed via
> `POST /ontology/wizard/generate/detect`, `GET/POST
> /ontology/wizard/generate/draft(/update|/discard)`, and
> `POST /ontology/wizard/generate/complete`
> (`src/api/routers/internal/ontology.py`). `/ontology/wizard/generate-async`
> (the legacy one-shot route) now always returns `410 Gone` and never runs
> generation. The deprecated `run_agent` bridge in
> `agents.agent_owl_generator.engine` has no remaining production caller —
> `Ontology.generate_with_agent`, `AgentClient.run_owl_generator`, and the
> supervisor's `"ontology"` task were all removed, not merely left dormant
> — since the design explicitly requires the post-generation pitfall
> rewrite loop to never run for production Generate. Sections still marked
> **(staged, planned)** describe target contract for Task 5 (the wizard UI),
> which has not landed yet.

## 1. Purpose

`agent_owl_generator` auto-designs an OWL ontology from UC metadata, the
domain's ready parsed-document corpus, and the current ontology. Today it
proposes classes, properties, and relationships in a single LLM-driven step
and returns a structure that conforms to the OntoBricks ontology JSON format
consumed by `back/objects/ontology/OntologyService`.

**(staged — live as of Task 3; wired end-to-end as of Task 4)** The agent is
split into four explicit entry points — `detect_entities`, `infer_relations`,
`infer_attributes`, and `infer_axioms` — driven by a durable, human-reviewed
draft instead of a single unattended call. Detection proposes candidate
entities for human review; completion (relations → attributes → axioms, in
that strict order) consumes only the reviewed, validated entity set and
appends its result to the existing ontology instead of replacing it. The
generic one-shot default is removed from the package's public surface:
`agent_owl_generator.__all__` exposes only the four staged entry points, and
no staged function calls the still-existing legacy `run_agent` bridge — which
itself now has no remaining production caller (Task 4 removed
`Ontology.generate_with_agent`, `AgentClient.run_owl_generator`, and the
supervisor's `"ontology"` task). `back.objects.ontology.GenerateWorkflow`
(Task 4) wires the staged functions into an async workflow with checkpoint
persistence and the final append-only merge against the live ontology — see
§2.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `agent_owl_generator` |
| `module_path` | `src/agents/agent_owl_generator/` |
| `model_endpoint` | Domain-selected AI Gateway or Model Serving endpoint |
| `temperature` | `0.0` (for eval) |
| `max_tokens` | `8192` per call (`_GEN_MAX_TOKENS`; Databricks endpoint output cap) |
| `max_owl_eval_rounds` | `2` (`MAX_OWL_EVAL_ROUNDS`; Stage-1 PGE evaluator retry cap — **(staged, planned)** retained only as a reject/report check, not a rewrite trigger; see §6a) |
| `max_classes` | `40` (`_DEFAULT_MAX_CLASSES`; over-generation guard — accepted ontology is asked to consolidate above this. Overridable via `options["max_classes"]`, `<=0` disables) |
| `mlflow_experiment` | `/Shared/ontobricks/agents/owl_generator` |
| `stage_entry_points` | `detect_entities` (Stage 1), `infer_relations` / `infer_attributes` / `infer_axioms` (Stage 3, strict order) — **live** as of Task 3 (`agents.agent_owl_generator.staged`); wired into the async workflow + checkpoint persistence + append-only merge — **live as of Task 4** (`back.objects.ontology.GenerateWorkflow`, `POST /ontology/wizard/generate/detect` + `.../complete`). `run_agent` (`agents.agent_owl_generator.engine`) is deprecated, not re-exported from the package root, never called by the staged module, and has **no remaining production caller** as of Task 4 (`/wizard/generate-async` now returns `410 Gone`) |

## 3. Tool surface

| Tool name | Input schema | Output type | Purpose |
|---|---|---|---|
| `list_documents` | `{}` | `{files: [{name, size, parse_status}], count}` | Discover source documents without exposing `_parsed`; never starts parsing |
| `read_document` | `{filename: string}` | Ready text payload or structured pending/failed payload | Read only the durable parsed corpus; never calls `DocumentExtractor` |
| `get_metadata` | `{}` | Catalog metadata JSON | Inspect selected table metadata |
| `get_table_detail` | `{table_name: string}` | Columns and table details | Inspect one relevant source table |

The tool surface is unchanged by staging: every stage that reads source
material (`detect_entities` only) uses these same four tools. Completion
entry points (`infer_relations`, `infer_attributes`, `infer_axioms`) —
**live as of Task 3** — do not call `list_documents` or `read_document` at
all — they consume only the persisted, validated Stage 2 entity contract
(canonical label, description, type hint, evidence text, alternate labels
captured during review), never a fresh document or metadata read. This is
the no-reparse contract for completion: evidence captured once during
review is the only lexical grounding completion stages get.

## 3a. Structured schemas (staged — live as of Task 3)

Detection output (`detect_entities`) is a bounded list of candidates, not
free-text OWL:

```json
{
  "candidate_entities": [
    {
      "id": "cand-3f9a1c",
      "canonical_label": "Carrier",
      "description": "Company that ships an Order to a Customer.",
      "type_hint": "class",
      "evidence": [{"source": "spec.pdf", "excerpt": "Order ships via Carrier."}],
      "alternate_labels": ["Shipper", "Freight Company"],
      "origin": "detected",
      "included": true
    }
  ]
}
```

- `included` defaults to `true` for every detected candidate; the human
  reviewer opts entities out, not in.
- `alternate_labels` carries every synonym found in metadata/document
  evidence as a first-class field — never as a second candidate entity and
  never buried only in `description` prose. Alternate labels are also
  treated as lexical evidence by completion (`infer_relations` /
  `infer_attributes` / `infer_axioms` may match naming against alternate
  labels, not only the canonical label).
- Detection never returns a candidate that duplicates an existing ontology
  entity's `id`, canonical label, or any of its alternate labels — those are
  passed in as **locked anchors** and must be deduplicated against, not
  re-proposed.

Completion entry points receive the validated Stage 2 contract (locked
anchors + included candidates, addressed by stable `id`) and return
relations/attributes/axioms that reference only those ids — see the entity
closure rule in §6a.

## 4. Success criteria

Three concrete examples for the current one-shot behavior, still true for
`detect_entities`'s document-reading behavior:

1. Given a ready PDF sidecar containing “Customer places Order,” the agent
   reads `spec.pdf`, grounds the resulting classes/relationship in that text,
   and makes no `ai_parse_document` call.
2. Given one ready glossary and one pending PDF, the agent may use the glossary
   but identifies the PDF as unavailable and does not claim evidence from it.
3. Given only pending or failed documents, the agent reports that document
   parsing is not ready and does not invent document-grounded ontology terms.

Three additional examples for the staged contract **(live as of Task 3;
exercised behaviorally by `tests/eval/staged_contract.py` and
`tests/units/agents/test_owl_generator_staged.py`)**:

4. Given an existing ontology with a `Customer` class and a document
   introducing "Carrier," `detect_entities` proposes `Carrier` as a new,
   `included=true` candidate and does not re-propose `Customer` as new.
5. Given a Stage 2 contract where the user excluded a detected `Invoice`
   candidate, `infer_relations` never emits a relation whose domain or range
   is `Invoice`; if it attempts to, the output is rejected rather than
   applied.
6. Given `completion_checkpoints.relations.status == "done"` and
   `attributes.status == "pending"`, resuming Stage 3 starts at
   `infer_attributes` and does not re-run `infer_relations`.

## 5. Eval dimensions

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `schema_validity` | RDFLib `parse(serialize())` succeeds | `0.95` | `0.30` | rule-based |
| `class_coverage` | proportion of input tables mapped to a class | `0.80` | `0.20` | rule-based |
| `property_quality` | LLM-judge on property naming + domain/range correctness | `0.80` | `0.25` | Planned ontology-quality judge; outside the parsed-corpus contract gate |
| `latency_p95` | seconds | `<= 30.0` | `0.10` | wall-clock |
| `cost_per_call` | USD | `<= 0.05` | `0.15` | MLflow usage |
| `ready_corpus_use` | ready document selected and used | `0.90` | contract | `tests/eval/run_agent_owl_generator.py` |
| `no_parse_safety` | no extractor/parse tool in observed trace | `1.00` | contract | `tests/eval/run_agent_owl_generator.py` |
| `status_disclosure` | unavailable documents are disclosed | `0.90` | contract | `tests/eval/run_agent_owl_generator.py` |
| `sidecar_hiding` | `_parsed` never appears as a source | `1.00` | contract | `tests/eval/run_agent_owl_generator.py` |

**Ontology-quality aggregate threshold:** ≥ `0.82`.
**Parsed-corpus contract threshold:** ≥ `0.90`.

**Staged contract dimensions — live as of Task 3.** The staged entry points
and `tests/eval/staged_contract.py` now exist, so every dimension below is
scored deterministically (scripted LLM responses, no live endpoint needed)
against every `staged`-tagged dataset row, gated by
`tests/eval/thresholds.yaml`'s `owl_generator.staged_contract: 0.95`:

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `stage_default_inclusion` | every detected candidate has `included=true` before review | `1.00` | contract | `tests/eval/staged_contract.py` (deterministic) |
| `stage_anchor_dedup` | no locked anchor (by id, canonical label, or alternate label) is re-proposed as a new candidate | `1.00` | contract | same |
| `stage_entity_closure` | no completion output references an id outside locked anchors ∪ validated new set | `1.00` | contract | same |
| `stage_order_contract` | `attributes` never checkpoints before `relations`; `axioms` never before `attributes` | `1.00` | contract | same |
| `stage_checkpoint_resume` | a resumed Stage 3 run does not re-run a substage already checkpointed `done` | `1.00` | contract | same |
| `stage_stale_fingerprint_block` | resume/update is refused when the source fingerprint changed since detection | `1.00` | contract | same |
| `stage_alternate_label_lexical_use` | synonyms surface as `alternate_labels`, not separate candidate entities | `0.90` | contract | same |
| `stage_no_rewrite_after_reject` | a validation-rejected stage output is reported as a failure, never resubmitted as an in-request rewrite | `1.00` | contract | same |
| `stage_no_one_shot_default` | no entry point applies a full ontology without passing through detect → review → complete | `1.00` | contract | same |

**Staged contract aggregate threshold:** ≥ `0.95`, enforced in
`tests/eval/thresholds.yaml` and CI via `tests/eval/run_agent_owl_generator.py`
(current result: `1.000` over 14 examples — see §10).

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| **Truncated ontology → empty result.** The final Turtle answer is cut off at the output-token cap (`finish_reason == "length"`); the salvaged remainder fails to parse in every RDF syntax, so `/ontology/parse-owl` lands 0 classes and the Generate wizard polls until timeout. | `OntologyParser` logs `Content appeared truncated`; `rdf_utils.parse_rdf_flexible` fails all formats; session saved with 0 classes. In tests: `finish_reason == "length"` on the text answer. | `max_tokens=8192` (was 4096) + a truncation guard in `engine.run_agent`: a length-capped answer is not accepted — the agent is asked to re-emit the ontology concisely (within `MAX_ITERATIONS`), or the run fails with an explicit "output truncated" error instead of a silent empty ontology. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_truncation.py`. |
| **Over-generation / class explosion.** The model over-decomposes — one class per column or per attribute value (e.g. `VatAmount`, `MeterReading`, `Payment`, `Call`) — emitting ~110 classes for a ~5-entity guideline. The ontology parses fine but downstream **auto-mapping** chunks ~5 classes/chunk with cool-downs, so ~22 chunks overrun the scenario `AUTOMAP_TIMEOUT` (600s) → "Auto-Map produced no entity SQL". | Auto-assign log shows `Chunk N/22` (vs the healthy `N/4`); accepted ontology `owl:Class` count ≫ input entity count. In tests: `_count_owl_classes(content) > max_classes`. | Prompt: replaced the "30–60 classes" size limit with "prefer 8–25, one class per real-world entity, never a class per column/value, hard limit 40". Guard: a class-count check in `engine.run_agent` asks the model (bounded by `_MAX_CONSOLIDATE_ROUNDS=2`) to consolidate above `max_classes` (default 40) before accepting. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_class_cap.py`. |
| **Structural defects survive generation.** Orphan classes, dangling `rdfs:domain`/`rdfs:range`, naming violations, or duplicate classes. | `evaluate_ontology()` reports Tier-1 issues. In tests: `tests/units/pge_eval/test_owl_evaluator_stage.py`. | **Legacy `run_agent` bridge only:** the pitfall-tool loop (`tool_check_owl_pitfalls`, called directly after each OWL answer) and the Stage-1 PGE evaluator (`_evaluate_ontology_stage`, bounded by `MAX_OWL_EVAL_ROUNDS=2`) both feed violations back into the LLM loop and ask it to re-emit corrected Turtle — a post-generation **rewrite** loop; unchanged in the deprecated `engine.run_agent` bridge, which is not on the staged path. **Live as of Task 3 for the staged entry points:** this rewrite loop is absent from `agents.agent_owl_generator.staged`. Pitfall rules (naming, closure) are stated in the stage prompts up front (prompt-first, `prompts.py`); `schemas.py`'s parsers and `GenerateDraft.validate_references()` are the deterministic checks and are **reject-only** — a failing stage output sets `rejected=True`/`rejection_reason` and is reported as a failure for the user to retry, never silently patched by an automatic re-prompt within the same request. Tests: `tests/units/agents/test_owl_generator_staged.py` (`*_rejected_no_rewrite`), `tests/eval/staged_contract.py` (`stage_no_rewrite_after_reject`). |
| **Repeated warehouse parsing.** Generate invokes `ai_parse_document` while reading a source. | Parsed-corpus eval observes an extractor/parse call. | Agent document tools have no extractor dependency and return only persisted corpus content. Completion entry points additionally never call a document tool at all (§3, live as of Task 3 — `tools=None` on every `infer_*` LLM call). |
| **Corpus not ready.** The agent treats pending/failed files as evidence. | Tool payload has `parse_status != ready`; response claims document evidence. | Return a structured unavailable payload and require status disclosure in evals. |
| **Internal sidecar exposed.** `_parsed` appears in the document list. | Listed filename contains `_parsed`. | Filter internal directories before tool results are built. |
| **Entity injection.** A completion substage (`infer_relations`/`infer_attributes`/`infer_axioms`) references or introduces an entity id outside the locked anchors plus the validated Stage 2 set. | Server-side validation of every referenced id against the closed entity set before checkpointing (`_run_completion()` + `GenerateDraft.validate_references()`). | **Live as of Task 3**, with a second, workflow-level closure re-check added in Task 4 (`GenerateWorkflow.run_completion`, after each substage returns, before checkpointing — defense-in-depth even if the substage's own internal check is bypassed). Reject the substage output outright (`rejected=True`); mark that checkpoint `FAILED` and do not merge; surface a retryable failure for that substage only (resume re-runs only the failed/incomplete stage — see §3a checkpoint persistence, live as of Task 4). |
| **Stale draft resumed against a changed source.** The user (or a retry) continues a draft after the selected metadata, ready-document manifests, or existing ontology identities changed since detection. | Recomputed `source_fingerprint` mismatches the draft's stored value. | **Live (Task 2 draft layer, exercised by the staged contract).** Invalidate the draft for resume/update (`GenerateDraft.ensure_not_stale`); require an explicit re-run of `detect_entities`; never silently reuse stale candidates or silently reparse to "refresh" the fingerprint. |
| **Out-of-order or duplicated substage execution.** A retry or race starts `infer_axioms` before `infer_attributes` is checkpointed `done`, or re-runs a substage already `done`. | `completion_checkpoints` status inspected before every substage starts (`staged._ordering_error()`, fails before any LLM call). | **Live as of Task 3.** Refuse to start a substage unless its predecessor is `done`; skip any substage already `done` on resume (`GenerateDraft.next_pending_substage()`). |
| **Replace instead of append.** The final merge deletes or renames a pre-existing entity instead of appending validated new entities and enriching anchors. | Merge diff shows a removed or renamed pre-existing entity id. | **Live as of Task 4** (`GenerateWorkflow.merge_draft_into_ontology`). Merge is append-only by construction: existing classes/properties/`dataProperties` are read and only ever appended to (new classes/properties/`dataProperties`/axioms added; an existing class's unset `parent` may be set from a `subClassOf` axiom) — no existing entity's `name`/content is ever removed or overwritten. New candidate entities are minted a fresh, collision-free class `name` from their `canonical_label`; their detection-time `id` is the merge-time join key only, not carried into the live ontology. Tested by `tests/units/ontology/test_generate_workflow.py::TestMergePreservesExistingEntities`. |
| **Duplicate merge on crash/retry.** A crash or draft-revision conflict between the merge's own `domain.save()` and the draft store recording that fact causes a resumed `run_completion` to re-run the merge and duplicate every appended class/relation/axiom. | Retrying `POST /wizard/generate/complete` on an already-(partially)-merged draft adds a second `Carrier2`/duplicate relation/duplicate axiom instead of a no-op. | **Fixed in the Task 4 review pass** — two layers of defense: (1) a durable `GenerateDraft.merge_checkpoint`, independent of `stage`/`completion_checkpoints`, checked before ever calling the merge again — once `done`, `run_completion` trusts it and never re-merges, regardless of what `stage` says; (2) `merge_draft_into_ontology` is itself idempotent (defense in depth): every added class is tagged `generated_from=<candidate id>` and skipped on a repeat call, relations are deduped by `(domain, range, label)`, binary axioms (`disjointWith`/`equivalentClass`) by `(type, subject, sorted(objects))`, and an exact-repeat `subClassOf` is a no-op. Tested by `tests/units/ontology/test_generate_workflow.py::TestIdempotentMerge` (crash-window resume, checkpoint-done short-circuit, repeated-complete-call rejection). |
| **Silent type-hint mismatch at merge.** An included candidate carries a non-`class` `type_hint` (`object_property`/`data_property`) that the merge cannot yet place into the ontology model, but it gets silently merged as a (wrongly-shaped) class anyway. | A candidate hinted as a property appears as a top-level `owl:Class` after merge instead of being rejected. | **Fixed in the Task 4 review pass.** `merge_draft_into_ontology` rejects (raises `DraftValidationError`, atomic — nothing is merged) any included candidate whose `type_hint != "class"` before any mutation begins; the reviewer must edit the hint back to `class` or exclude the candidate. Tested by `tests/units/ontology/test_generate_workflow.py::TestTypeHintValidationInMerge`. |
| **Silently dropped conflicting `subClassOf`.** A generated `subClassOf` axiom for an entity that already has a *different* parent is silently ignored instead of surfaced — the ontology model supports only one `parent` per class, so this is unrepresentable multi-inheritance. | Merge diff shows the axiom present in the substage result but no trace of it (no parent change, no error, no log the user sees). | **Fixed in the Task 4 review pass.** An exact repeat (same subject+parent) is still a silent no-op (idempotency); a *different* parent now raises `DraftValidationError` explicitly ("multiple parent classes ... are not supported"), never a silent drop. Tested by `tests/units/ontology/test_generate_workflow.py::TestSubClassOfAndDisjointAxiomMerge`. |

## 6a. Prompt-first pitfall handling and append guarantees

Cross-reference for §6's staged rows and §3a's entity-closure rule. Prompt-
first pitfalls, entity closure, and no-reparse are **live as of Task 3**;
append-only merge is **live as of Task 4**:

- **Prompt-first, not rewrite-after.** Pitfall constraints (naming rules,
  orphan-class avoidance, domain/range completeness, duplicate-class
  avoidance, class-count guidance) are stated in the stage system prompts.
  `evaluate_ontology()` / pitfall checks remain as deterministic validation
  but are reject-only: a failing output is never fed back into another LLM
  call within the same request to be silently rewritten.
- **Entity closure.** Every completion substage's output is validated
  against the closed set (locked anchors ∪ included Stage 2 candidates,
  identified by stable `id`) before it is checkpointed. An output that
  references an unknown id is rejected, not merged, and not patched.
- **Append-only merge.** The final merge (`GenerateWorkflow.
  merge_draft_into_ontology`, live as of Task 4) never deletes or renames a
  pre-existing ontology entity. New candidate entities are added as new
  classes/properties/`dataProperties`; their detection-time `id` is used only
  to join relations/attributes/axioms output to the right newly-minted class
  name during this one merge, not carried forward as the entity's identity.
  Existing (anchor) entities may gain new relations/attributes/axioms/an
  unset `parent` (from a `subClassOf` axiom) but their `name`/`uri`/existing
  `dataProperties` are read-only during merge. Included candidates whose
  `type_hint` is not `class` are rejected outright rather than silently
  merged as a wrongly-shaped class.
- **Idempotent/atomic merge.** A durable `GenerateDraft.merge_checkpoint`
  (independent of `stage`) is checked before the merge ever runs again, so a
  crash or draft-revision conflict between the merge's own `domain.save()`
  and that checkpoint being recorded cannot cause `run_completion` to
  duplicate the merge on resume. `merge_draft_into_ontology` is additionally
  idempotent by construction — classes/relations/binary axioms are deduped
  by stable identity keys, and an exact-repeat `subClassOf` is a no-op —
  as defense in depth if it is ever called twice for the same draft. A
  `subClassOf` axiom that conflicts with an entity's *already-different*
  parent (multi-inheritance the model cannot represent) is rejected
  explicitly, never silently dropped.
- **First-class synonyms in OWL.** `alternate_labels` round-trips through
  OWL export/import as `skos:altLabel` triples (`OntologyGenerator`/
  `OntologyParser`), not merely as an in-app session field.
- **No-reparse.** `detect_entities` is the only entry point that reads
  documents, and only through `list_documents`/`read_document` against
  `ready` manifests. Completion entry points read only the persisted Stage 2
  contract. No entry point ever triggers `ai_parse_document`.
- **Lifecycle-gated merge.** `POST /wizard/generate/complete` persists into
  the loaded version's design (`domain.save()`), so it is subject to the
  same status/single-editor-lock gate as every other `/ontology/` write
  (`shared.fastapi.main._is_status_gated_edit`) — blocked on a locked/
  PUBLISHED version or while another user holds the edit lock. Stage 1
  detect and Stage 2 draft read/update/discard only touch the session-scoped
  draft and remain exempt, exactly like the rest of the wizard.

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/agent_owl_generator/baseline.jsonl` — 10
  parsed-corpus material-change cases (ready, pending, failed, mixed, empty,
  boundary, and adversarial corpus states — scored by
  `tests/eval/run_agent_owl_generator.py`) plus 3 legacy schema-shape seed
  cases and 14 staged-contract material-change cases (tagged `staged`;
  documented contract examples for detection, default inclusion, manual
  add/edit/remove, alternate labels, excluded-entity rejection,
  locked-anchor append/dedup, strict relations→attributes→axioms ordering,
  checkpoint recovery, stale-source invalidation, no-reparse, no
  post-rejection rewrite after a deterministic validation failure, and no
  one-shot generation path/default — see §3a/§6a). As of Task 3, the
  `staged` cases are scored **behaviorally** by
  `tests/eval/run_agent_owl_generator.py` (via `tests/eval/staged_contract.py`):
  each constraint `kind` maps to a deterministic check that exercises the
  real `detect_entities` / `infer_relations` / `infer_attributes` /
  `infer_axioms` / `GenerateDraft` code paths with scripted LLM responses
  (no live endpoint required). Detection-tagged rows specifically drive the
  real `staged.detect_entities()` orchestrator end-to-end — existing
  anchors/metadata/corpus are built from that row's own `input`, only
  `staged.call_serving_endpoint` is scripted, and a `get_metadata` tool
  round-trip is dispatched for real through `dispatch_tool`/`TOOL_HANDLERS`
  (offline-safe) — so the observed tool surface/dispatch (`does_not_parse`)
  and dedup outcome (`excludes_existing_anchor_as_new`, etc.) reflect the
  production code path, not a hand-written call to
  `schemas.parse_detection_payload` alone. The runner still structurally
  validates every staged row first (`_validate_staged_examples`): a floor of
  14 examples,
  unique ids, a present `input.stage`, non-empty `expected.constraints`, and
  mandatory coverage of the `stage_no_rewrite_after_reject` and
  `stage_no_one_shot_default` constraint kinds, so neither review-flagged
  gap can silently regress out of the dataset.
- **Planning mirror:** `.planning/agents/agent_owl_generator/eval/dataset.jsonl`.
- **Regression:** `tests/eval/datasets/agent_owl_generator/regression.jsonl` (empty until first production failure).

## 8. MLflow tracing

Existing: `@trace_agent` on the entry point in `src/agents/agent_owl_generator/`. Verify `@trace_tool` is on each tool handler.

**Live as of Task 3.** Each of the four staged entry points
(`detect_entities`, `infer_relations`, `infer_attributes`, `infer_axioms`)
has its own `@trace_agent(name=..., stage=...)` span
(`owl_generator.detect` / `.relations` / `.attributes` / `.axioms`). Every
staged span additionally carries `draft_id`, `draft_revision`, and `stage`
(`detect` | `relations` | `attributes` | `axioms`) as **span attributes**
(`Span.set_attributes`) when the caller passes them — not MLflow trace-level
`tags` (`mlflow.set_trace_tag`/trace `tags=`), which this module does not
use — so a resumed run's traces can be correlated across the checkpointed
substages, and so an eval harness can assert stage-order and
no-rewrite-after-reject directly from the trace without re-deriving it from
prose output. **Live as of Task 4:** `GenerateWorkflow.run_completion` passes
the persisted draft's `draft_id`/`draft_revision` into every `infer_*` call,
so a resumed run's spans are correlated by the real draft identity, not just
forwarded-but-unused kwargs.

Each Stage-1 tool dispatch (`detect_entities`'s `list_documents` /
`read_document` / `get_metadata` / `get_table_detail` calls) additionally
gets its own `@trace_tool` **TOOL** span via a local wrapper
(`staged._dispatch_detection_tool`), carrying the tool name (input) and an
`ok`/`error` status (output) derived from the dispatched JSON result. This is
scoped to the staged detection path only — the shared `dispatch_tool`
helper (used by ~10 other agent engines) and the shared tool handlers are
unchanged.

## 9. Plan reference

`docs/superpowers/plans/2026-09-18-parsed-document-corpus.md` (parsed-corpus
tool contract, still in force for `detect_entities`'s document reads).

`docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md`
(staged Generate design — full target contract for this SPEC's `(staged,
planned)` sections; plan: `staged-ontology-generate`).

## 10. Sign-off

- [x] Author has filled sections 4, 5, 6, 7.
- [x] Baseline eval (parsed-corpus contract, pre-existing, unaffected by this
      revision): `https://fe-vm-bcayla-demos.cloud.databricks.com/ml/experiments/1426639566663818/runs/c8ce3cae2014408e9451cc508068fe0d` (`judge_score=0.965`).
- [x] Post-change eval (parsed-corpus contract, pre-existing, unaffected by
      this revision): `https://fe-vm-bcayla-demos.cloud.databricks.com/ml/experiments/1426639566663818/runs/6ef43cc56fb34cb1b74208fd81ae06a9` (`judge_score=1.000`).
- [x] Pre-change baseline for **this** material change (staged-ontology-generate,
      Task 1 + fixes): dry-run contract validation only —
      `uv run --frozen pytest -q -m "not scenario"` +
      `uv run --frozen python tests/eval/run_agent_owl_generator.py`
      → 14 staged examples structurally validated (coverage-checked for
      `stage_no_rewrite_after_reject` and `stage_no_one_shot_default`); all
      10 parsed-corpus cases PASS, aggregate `1.000` (stub judge, no MLflow
      run is created in dry-run mode). No runtime/prompt code changed in
      this revision, so this dry-run result **is** the pre-change baseline
      for the unchanged parsed-corpus contract.
- [ ] Live MLflow baseline run for this material change: **NOT RUN — BLOCKED.**
      `tests/eval/run_agent_owl_generator.py --live` requires
      `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, and `ONTOBRICKS_LLM_ENDPOINT`;
      none are configured in this environment (`--live requires host,
      token, and endpoint`, exit from `argparse`). No run URI exists for
      this attempt and none is fabricated here. Recorded to run when a
      configured environment is available.
- [x] Post-change staged-contract eval (Task 3 — `detect_entities` /
      `infer_relations` / `infer_attributes` / `infer_axioms` now exist):
      `uv run --frozen python tests/eval/run_agent_owl_generator.py` →
      all 14 staged examples PASS, staged-contract aggregate `1.000`
      (threshold `0.950`, deterministic/scripted, no live endpoint); all 10
      parsed-corpus cases unaffected, aggregate `1.000` (threshold `0.900`).
      Live MLflow eval run for the staged dimensions is blocked for the same
      credential reason as the pre-change baseline above; no run URI is
      fabricated here.
- [ ] Baseline eval run URI pasted into PR body.
- [x] Aggregate threshold ≥ declared value in §5 (staged `1.000` ≥ `0.950`;
      parsed-corpus `1.000` ≥ `0.900`; deterministic scoring, not a live
      MLflow judge run — see the blocked item above).
- [x] Task 4 (async workflow, checkpoint persistence, append-only merge, API
      routes): no new eval dimension needed — §5's `stage_*` dimensions
      already exercise the underlying `staged.py`/`GenerateDraft` behavior
      Task 4 wires together; `tests/units/ontology/test_generate_workflow.py`
      (31 tests) and `tests/units/api/test_generate_routes.py` (10 tests)
      cover the new orchestration/route layer itself.
      `uv run --frozen python tests/eval/run_agent_owl_generator.py` →
      unchanged: all 14 staged examples PASS, aggregate `1.000` (threshold
      `0.950`); all 10 parsed-corpus cases PASS, aggregate `1.000`
      (threshold `0.900`). Live MLflow eval run blocked for the same
      credential reason as prior tasks; no run URI fabricated.
- [ ] Reviewer waiver recorded in the PR, if used.
