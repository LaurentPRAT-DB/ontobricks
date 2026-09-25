# SPEC: agent_owl_generator

> Required by `.cursor/12-ai-feature-lifecycle.mdc`.
> Design: `docs/superpowers/specs/2026-09-21-incremental-owl-generation-design.md`

## 1. Purpose

`agent_owl_generator` auto-designs an OWL ontology from UC metadata and domain documents. Small inputs still use one LLM Turtle completion. Large documents switch to incremental structured generation: chunked `ai_parse_document` / text windows, mutation tools, then Python Turtle via `OntologyGenerator`. Output is consumed by `back/objects/ontology/OntologyService`.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `agent_owl_generator` |
| `module_path` | `src/agents/agent_owl_generator/` |
| `model_endpoint` | _configured per workspace_ |
| `temperature` | `0.0` (for eval) |
| `max_tokens` | `8192` oneshot Turtle (`_GEN_MAX_TOKENS`); `2048` incremental mutation calls |
| `max_owl_eval_rounds` | `2` (`MAX_OWL_EVAL_ROUNDS`; Stage-1 PGE evaluator retry cap) |
| `max_classes` | `40` (`_DEFAULT_MAX_CLASSES`; overridable via `options["max_classes"]`, `<=0` disables) |
| `mlflow_experiment` | `/Shared/ontobricks/agents/owl_generator` |

## 3. Tool surface

| Tool name | Input schema | Output type | Purpose |
|---|---|---|---|
| `list_documents` | `{}` | `dict` | Discover domain volume files (oneshot). |
| `read_document` | `{"filename": "string"}` | `dict` | Read/parse one file; 80k-char payload cap (oneshot). |
| `get_metadata` | `{}` | `dict` | Table list / truncated columns. |
| `get_table_detail` | `{"table": "string"}` | `dict` | Full column list for one table. |
| `add_entity` | `{"name","label","description","parent","emoji"}` | `dict` | Incremental: add class. |
| `add_attribute` | `{"entity_name","attribute_name","attribute_type","description"}` | `dict` | Incremental: datatype property. |
| `add_relationship` | domain/range/name | `dict` | Incremental: object property (phase B). |
| `set_inheritance` | parent/child | `dict` | Incremental: subclass link. |
| `get_ontology_classes` | `{}` | `dict` | Incremental: name snapshot. |
| `get_ontology_properties` | `{}` | `dict` | Incremental: relationship snapshot. |

## 4. Success criteria

1. **Small CRM guideline (oneshot)**
   - input: 5-entity energy CRM prompt, no large PDF
   - expected: Turtle starting with `@prefix`, 8–25 classes, `finish_reason=stop`
2. **42-page Telecom PDF (incremental)**
   - input: selected PDF > 40k parsed chars
   - expected: mode=`incremental`; concepts from first and last chunks; parseable Turtle from `OntologyGenerator`; no Turtle completion with `finish_reason=length`
3. **Duplicate names across chunks**
   - input: chunk 1 `Customer`, chunk 2 `Customers` + `accountNumber`
   - expected: one `Customer` class; `accountNumber` on that class

## 5. Eval dimensions

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `schema_validity` | RDFLib `parse(serialize())` succeeds | `0.95` | `0.30` | rule-based |
| `class_coverage` | proportion of input tables mapped to a class | `0.80` | `0.20` | rule-based |
| `property_quality` | LLM-judge on property naming + domain/range correctness | `0.80` | `0.25` | `tests/eval/judges/owl_property_judge.py` (to build) |
| `latency_p95` | seconds | `<= 30.0` | `0.10` | wall-clock |
| `cost_per_call` | USD | `<= 0.05` | `0.10` | MLflow usage |
| `incremental_completeness` | first-chunk and last-chunk expected class names present | `0.90` | `0.05` | rule-based |

**Aggregate threshold:** ≥ `0.82` to pass G2 (proposed).

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| **Truncated ontology → empty result.** The final Turtle answer is cut off at the output-token cap (`finish_reason == "length"`); the salvaged remainder fails to parse in every RDF syntax, so `/ontology/parse-owl` lands 0 classes and the Generate wizard polls until timeout. | `OntologyParser` logs `Content appeared truncated`; `rdf_utils.parse_rdf_flexible` fails all formats; session saved with 0 classes. In tests: `finish_reason == "length"` on the text answer. | `max_tokens=8192` (was 4096) + a truncation guard in `engine.run_agent`: a length-capped answer is not accepted — the agent is asked to re-emit the ontology concisely (within `MAX_ITERATIONS`), or the run fails with an explicit "output truncated" error instead of a silent empty ontology. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_truncation.py`. |
| **Over-generation / class explosion.** The model over-decomposes — one class per column or per attribute value (e.g. `VatAmount`, `MeterReading`, `Payment`, `Call`) — emitting ~110 classes for a ~5-entity guideline. The ontology parses fine but downstream **auto-mapping** chunks ~5 classes/chunk with cool-downs, so ~22 chunks overrun the scenario `AUTOMAP_TIMEOUT` (600s) → "Auto-Map produced no entity SQL". | Auto-assign log shows `Chunk N/22` (vs the healthy `N/4`); accepted ontology `owl:Class` count ≫ input entity count. In tests: `_count_owl_classes(content) > max_classes`. | Prompt: replaced the "30–60 classes" size limit with "prefer 8–25, one class per real-world entity, never a class per column/value, hard limit 40". Guard: a class-count check in `engine.run_agent` asks the model (bounded by `_MAX_CONSOLIDATE_ROUNDS=2`) to consolidate above `max_classes` (default 40) before accepting. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_class_cap.py`. |
| **Structural defects survive generation.** Orphan classes, dangling `rdfs:domain`/`rdfs:range`, naming violations, or duplicate classes pass the pitfall-tool loop but break registry import or downstream mapping. | `evaluate_ontology()` reports Tier-1 issues; `_evaluate_ontology_stage()` returns a retry hint. In tests: `tests/units/pge_eval/test_owl_evaluator_stage.py`. | Stage-1 PGE Evaluator after the pitfall loop: deterministic `agents.pge_eval.ontology_metrics.evaluate_ontology` feeds concrete retry hints back to the generator, bounded by `MAX_OWL_EVAL_ROUNDS=2`. Fails open on parse errors. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/pge_eval/`. |
| **Large-document Turtle truncation.** A tens-of-pages spec cannot fit in one 8192-token Turtle completion; oneshot retry-concise still fails. `read_document` also drops text after 80k chars. | Logs: `truncated final answer`, `finish_reason=length`, `tool_read_document ... truncated`. | Incremental mode: `pageRange`/text chunks, mutation tools, Python Turtle. Design: `docs/superpowers/specs/2026-09-21-incremental-owl-generation-design.md`. |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/agent_owl_generator/baseline.jsonl` (not built; needs ≥ 20 examples; mix of single-table, multi-table, and degenerate inputs).
- **Synthetic:** Use `databricks-synthetic-data-generation` against UC sample data.
- **Regression:** `tests/eval/datasets/agent_owl_generator/regression.jsonl` — includes oneshot truncation/class-cap cases plus ≥ 3 incremental examples (large-doc mode switch, cross-chunk entity, duplicate merge).

## 8. MLflow tracing

`@trace_agent` on `run_agent`. Incremental: child span per chunk (`filename`, page range). `@trace_tool` on mutation handlers.

## 9. Plan reference

- Design: `docs/superpowers/specs/2026-09-21-incremental-owl-generation-design.md`
- Implementation plan: `.planning/agents/agent_owl_generator/PLAN.md` (after spec approval)

## 10. Sign-off

- [x] Author has filled sections 4, 5, 6, 7.
- [ ] Baseline eval run URI pasted into PR body.
- [ ] Aggregate threshold ≥ declared value in §5.
- [ ] Reviewer waiver (if applicable): _____
