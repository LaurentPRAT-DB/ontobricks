# SPEC: agent_owl_generator

> Required by `.cursor/12-ai-feature-lifecycle.mdc`.

## 1. Purpose

`agent_owl_generator` auto-designs an OWL ontology from UC metadata. Given a catalog/schema/table set, it proposes classes, properties, and relationships in a single LLM-driven step, returning a structure that conforms to the OntoBricks ontology JSON format consumed by `back/objects/ontology/OntologyService`.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `agent_owl_generator` |
| `module_path` | `src/agents/agent_owl_generator/` |
| `model_endpoint` | Domain-selected AI Gateway or Model Serving endpoint |
| `temperature` | `0.0` (for eval) |
| `max_tokens` | `8192` per call (`_GEN_MAX_TOKENS`; Databricks endpoint output cap) |
| `max_owl_eval_rounds` | `2` (`MAX_OWL_EVAL_ROUNDS`; Stage-1 PGE evaluator retry cap) |
| `max_classes` | `40` (`_DEFAULT_MAX_CLASSES`; over-generation guard — accepted ontology is asked to consolidate above this. Overridable via `options["max_classes"]`, `<=0` disables) |
| `mlflow_experiment` | `/Shared/ontobricks/agents/owl_generator` |

## 3. Tool surface

| Tool name | Input schema | Output type | Purpose |
|---|---|---|---|
| `list_documents` | `{}` | `{files: [{name, size, parse_status}], count}` | Discover source documents without exposing `_parsed`; never starts parsing |
| `read_document` | `{filename: string}` | Ready text payload or structured pending/failed payload | Read only the durable parsed corpus; never calls `DocumentExtractor` |
| `get_metadata` | `{}` | Catalog metadata JSON | Inspect selected table metadata |
| `get_table_detail` | `{table_name: string}` | Columns and table details | Inspect one relevant source table |

## 4. Success criteria

1. Given a ready PDF sidecar containing “Customer places Order,” the agent
   reads `spec.pdf`, grounds the resulting classes/relationship in that text,
   and makes no `ai_parse_document` call.
2. Given one ready glossary and one pending PDF, the agent may use the glossary
   but identifies the PDF as unavailable and does not claim evidence from it.
3. Given only pending or failed documents, the agent reports that document
   parsing is not ready and does not invent document-grounded ontology terms.

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

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| **Truncated ontology → empty result.** The final Turtle answer is cut off at the output-token cap (`finish_reason == "length"`); the salvaged remainder fails to parse in every RDF syntax, so `/ontology/parse-owl` lands 0 classes and the Generate wizard polls until timeout. | `OntologyParser` logs `Content appeared truncated`; `rdf_utils.parse_rdf_flexible` fails all formats; session saved with 0 classes. In tests: `finish_reason == "length"` on the text answer. | `max_tokens=8192` (was 4096) + a truncation guard in `engine.run_agent`: a length-capped answer is not accepted — the agent is asked to re-emit the ontology concisely (within `MAX_ITERATIONS`), or the run fails with an explicit "output truncated" error instead of a silent empty ontology. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_truncation.py`. |
| **Over-generation / class explosion.** The model over-decomposes — one class per column or per attribute value (e.g. `VatAmount`, `MeterReading`, `Payment`, `Call`) — emitting ~110 classes for a ~5-entity guideline. The ontology parses fine but downstream **auto-mapping** chunks ~5 classes/chunk with cool-downs, so ~22 chunks overrun the scenario `AUTOMAP_TIMEOUT` (600s) → "Auto-Map produced no entity SQL". | Auto-assign log shows `Chunk N/22` (vs the healthy `N/4`); accepted ontology `owl:Class` count ≫ input entity count. In tests: `_count_owl_classes(content) > max_classes`. | Prompt: replaced the "30–60 classes" size limit with "prefer 8–25, one class per real-world entity, never a class per column/value, hard limit 40". Guard: a class-count check in `engine.run_agent` asks the model (bounded by `_MAX_CONSOLIDATE_ROUNDS=2`) to consolidate above `max_classes` (default 40) before accepting. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_class_cap.py`. |
| **Structural defects survive generation.** Orphan classes, dangling `rdfs:domain`/`rdfs:range`, naming violations, or duplicate classes pass the pitfall-tool loop but break registry import or downstream mapping. | `evaluate_ontology()` reports Tier-1 issues; `_evaluate_ontology_stage()` returns a retry hint. In tests: `tests/units/pge_eval/test_owl_evaluator_stage.py`. | Stage-1 PGE Evaluator after the pitfall loop: deterministic `agents.pge_eval.ontology_metrics.evaluate_ontology` feeds concrete retry hints back to the generator, bounded by `MAX_OWL_EVAL_ROUNDS=2`. Fails open on parse errors. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/pge_eval/`. |
| **Repeated warehouse parsing.** Generate invokes `ai_parse_document` while reading a source. | Parsed-corpus eval observes an extractor/parse call. | Agent document tools have no extractor dependency and return only persisted corpus content. |
| **Corpus not ready.** The agent treats pending/failed files as evidence. | Tool payload has `parse_status != ready`; response claims document evidence. | Return a structured unavailable payload and require status disclosure in evals. |
| **Internal sidecar exposed.** `_parsed` appears in the document list. | Listed filename contains `_parsed`. | Filter internal directories before tool results are built. |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/agent_owl_generator/baseline.jsonl` (10 material-change cases covering ready, pending, failed, mixed, empty, boundary, and adversarial corpus states).
- **Planning mirror:** `.planning/agents/agent_owl_generator/eval/dataset.jsonl`.
- **Regression:** `tests/eval/datasets/agent_owl_generator/regression.jsonl` (empty until first production failure).

## 8. MLflow tracing

Existing: `@trace_agent` on the entry point in `src/agents/agent_owl_generator/`. Verify `@trace_tool` is on each tool handler.

## 9. Plan reference

`docs/superpowers/plans/2026-09-18-parsed-document-corpus.md`.

## 10. Sign-off

- [x] Author has filled sections 4, 5, 6, 7.
- [ ] Baseline eval run URI pasted into PR body.
- [ ] Aggregate threshold ≥ declared value in §5.
- [ ] Reviewer waiver recorded in the PR, if used.
