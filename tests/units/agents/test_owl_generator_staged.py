"""Staged orchestrator contracts for ``agents.agent_owl_generator.staged``.

The staged Generate agent exposes four explicit entry points —
``detect_entities`` (Stage 1) and ``infer_relations`` / ``infer_attributes`` /
``infer_axioms`` (Stage 3, strict order). These tests pin the behavioural
contract from the design
(``docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md``)
and SPEC (``.planning/agents/agent_owl_generator/SPEC.md`` §3/§3a/§6a):

* detection returns bounded structured candidates, defaulted to included,
  deduplicated against locked anchors, using only the no-reparse doc tools;
* completion consumes only the validated draft contract (locked anchors +
  included candidates by stable id), never document tools, never new
  entities; an out-of-closure reference is rejected (reject-only, no
  in-request rewrite);
* strict relations -> attributes -> axioms ordering is enforced at the
  interface;
* every stage carries a distinct trace identity;
* no staged entry point performs one-shot full-ontology generation.

``call_serving_endpoint`` is patched with scripted responses so no live
endpoint is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import agents.tracing as tracing_mod
from back.objects.ontology.GenerateDraft import (
    CHECKPOINT_DONE,
    GenerateDraft,
    GenerateEntity,
    REVIEWING,
)
from agents.agent_owl_generator import staged
from agents.agent_owl_generator import prompts


# ---------------------------------------------------------------------------
# Scripted LLM responses
# ---------------------------------------------------------------------------


def _answer(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _tool_call(name: str, args: str = "{}", call_id: str = "tc-1") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {"name": name, "arguments": args},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _detect(responses):
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = responses
        result = staged.detect_entities(
            host="https://test.databricks.com",
            token="tok",
            endpoint_name="dbx-llm",
            metadata={"tables": []},
            guidelines="Generate a CRM ontology.",
            options={},
            existing_anchors=[
                GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")
            ],
            registry={"catalog": "main", "schema": "ob", "volume": "documents"},
        )
    return result, mock_llm


def _draft(*, candidates=None, anchors=None, relations_done=False, attributes_done=False):
    draft = GenerateDraft.new(
        source_fingerprint="sha256:fp",
        existing_anchors=anchors
        or [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")],
        candidate_entities=candidates
        or [GenerateEntity.new_candidate("Carrier", entity_id="cand-6")],
        stage=REVIEWING,
    )
    if relations_done:
        draft = draft.with_checkpoint(
            "relations", CHECKPOINT_DONE, result={"relations": []}
        )
    if attributes_done:
        draft = draft.with_checkpoint(
            "attributes", CHECKPOINT_DONE, result={"attributes": []}
        )
    return draft


# ---------------------------------------------------------------------------
# Stage 1: detection
# ---------------------------------------------------------------------------


class TestDetectEntities:
    def test_returns_default_included_candidates(self):
        payload = (
            '{"candidate_entities": [{"canonical_label": "Carrier"}, '
            '{"canonical_label": "Invoice"}]}'
        )
        result, _ = _detect([_answer(payload)])
        assert result.success is True
        assert [c.canonical_label for c in result.candidate_entities] == [
            "Carrier",
            "Invoice",
        ]
        assert all(c.included for c in result.candidate_entities)

    def test_deduplicates_against_locked_anchor(self):
        payload = (
            '{"candidate_entities": [{"canonical_label": "Customer"}, '
            '{"canonical_label": "Carrier"}]}'
        )
        result, _ = _detect([_answer(payload)])
        assert [c.canonical_label for c in result.candidate_entities] == ["Carrier"]

    def test_only_uses_no_reparse_tool_surface(self):
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        _, mock_llm = _detect([_answer(payload)])
        sent_tools = mock_llm.call_args_list[0].kwargs["tools"]
        names = {t["function"]["name"] for t in sent_tools}
        assert names <= {
            "list_documents",
            "read_document",
            "get_documents_context",
            "get_metadata",
            "get_table_detail",
        }
        # Never the parser or the pitfall-rewrite tool.
        assert "ai_parse_document" not in names
        assert "check_owl_pitfalls" not in names

    def test_truncated_then_complete_recovers(self):
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        result, mock_llm = _detect(
            [_answer('{"candidate_entities": [{"canonical', "length"), _answer(payload)]
        )
        assert result.success is True
        assert [c.canonical_label for c in result.candidate_entities] == ["Carrier"]

    def test_malformed_final_output_fails_without_rewrite(self):
        # A non-truncated malformed answer is rejected (reject-only), and the
        # agent does not enter a rewrite loop asking the LLM to try again.
        result, mock_llm = _detect([_answer("this is not json")])
        assert result.success is False
        assert result.rejected is True
        assert mock_llm.call_count == 1

    def test_trace_identity_is_stage_specific(self):
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        _, mock_llm = _detect([_answer(payload)])
        assert mock_llm.call_args_list[0].kwargs["trace_name"] == "owl_generator.detect"

    def test_empty_candidate_list_is_a_successful_result_not_a_rejection(self):
        # Live bug (Stage-1 zero-new-candidate case): every selected table's
        # core entity is already a locked anchor, so the model's ONLY
        # contract-compliant answer is the empty list — that must succeed,
        # not be treated as malformed/rejected.
        payload = '{"candidate_entities": []}'
        result, mock_llm = _detect([_answer(payload)])
        assert result.success is True
        assert result.rejected is False
        assert result.candidate_entities == []
        assert mock_llm.call_count == 1


# ---------------------------------------------------------------------------
# Stage 3: relations
# ---------------------------------------------------------------------------


class TestInferRelations:
    def _run(self, responses, draft=None):
        draft = draft or _draft()
        with patch.object(staged, "call_serving_endpoint") as mock_llm:
            mock_llm.side_effect = responses
            result = staged.infer_relations(
                host="h", token="t", endpoint_name="e", draft=draft
            )
        return result, mock_llm

    def test_valid_relation_within_closure_succeeds(self):
        payload = (
            '{"relations": [{"label": "shipsTo", '
            '"domain": "cand-6", "range": "cls-Customer-a1"}]}'
        )
        result, mock_llm = self._run([_answer(payload)])
        assert result.success is True
        assert result.substage == "relations"
        assert result.result["relations"][0]["label"] == "shipsTo"

    def test_completion_never_uses_document_tools(self):
        payload = '{"relations": []}'
        _, mock_llm = self._run([_answer(payload)])
        assert mock_llm.call_args_list[0].kwargs["tools"] is None

    def test_reference_outside_closure_rejected_no_rewrite(self):
        payload = (
            '{"relations": [{"label": "shipsTo", '
            '"domain": "cand-6", "range": "cls-Ghost-999"}]}'
        )
        result, mock_llm = self._run([_answer(payload)])
        assert result.success is False
        assert result.rejected is True
        assert "cls-Ghost-999" in result.rejection_reason
        # Reject-only: exactly one LLM call, no rewrite feedback loop.
        assert mock_llm.call_count == 1

    def test_excluded_candidate_reference_rejected(self):
        draft = _draft(
            candidates=[
                GenerateEntity.new_candidate("Carrier", entity_id="cand-6"),
                GenerateEntity.new_candidate(
                    "Invoice", entity_id="cand-4", included=False
                ),
            ]
        )
        payload = (
            '{"relations": [{"label": "billedVia", '
            '"domain": "cand-6", "range": "cand-4"}]}'
        )
        result, mock_llm = self._run([_answer(payload)], draft=draft)
        assert result.rejected is True
        assert "cand-4" in result.rejection_reason
        assert mock_llm.call_count == 1

    def test_trace_identity(self):
        _, mock_llm = self._run([_answer('{"relations": []}')])
        assert mock_llm.call_args_list[0].kwargs["trace_name"] == "owl_generator.relations"

    def test_truncated_then_complete_recovers(self):
        result, mock_llm = self._run(
            [_answer('{"relations": [', "length"), _answer('{"relations": []}')]
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# Stage 3: attributes (ordering + closure)
# ---------------------------------------------------------------------------


class TestInferAttributes:
    def _run(self, responses, draft):
        with patch.object(staged, "call_serving_endpoint") as mock_llm:
            mock_llm.side_effect = responses
            result = staged.infer_attributes(
                host="h", token="t", endpoint_name="e", draft=draft
            )
        return result, mock_llm

    def test_blocked_when_relations_not_done(self):
        draft = _draft(relations_done=False)
        result, mock_llm = self._run([_answer('{"attributes": []}')], draft)
        assert result.success is False
        assert result.rejected is True
        # Fails fast at the ordering gate — the LLM is never called.
        assert mock_llm.call_count == 0

    def test_succeeds_when_relations_done(self):
        draft = _draft(relations_done=True)
        payload = (
            '{"attributes": [{"label": "orderDate", '
            '"domain": "cand-6", "datatype": "xsd:date"}]}'
        )
        result, mock_llm = self._run([_answer(payload)], draft)
        assert result.success is True
        assert result.substage == "attributes"

    def test_unknown_domain_rejected(self):
        draft = _draft(relations_done=True)
        payload = (
            '{"attributes": [{"label": "x", '
            '"domain": "cand-nope", "datatype": "xsd:string"}]}'
        )
        result, mock_llm = self._run([_answer(payload)], draft)
        assert result.rejected is True
        assert mock_llm.call_count == 1

    def test_trace_identity(self):
        draft = _draft(relations_done=True)
        _, mock_llm = self._run([_answer('{"attributes": []}')], draft)
        assert (
            mock_llm.call_args_list[0].kwargs["trace_name"]
            == "owl_generator.attributes"
        )


# ---------------------------------------------------------------------------
# Stage 3: axioms (ordering + no-rewrite-after-reject)
# ---------------------------------------------------------------------------


class TestInferAxioms:
    def _run(self, responses, draft):
        with patch.object(staged, "call_serving_endpoint") as mock_llm:
            mock_llm.side_effect = responses
            result = staged.infer_axioms(
                host="h", token="t", endpoint_name="e", draft=draft
            )
        return result, mock_llm

    def test_blocked_when_attributes_not_done(self):
        draft = _draft(relations_done=True, attributes_done=False)
        result, mock_llm = self._run([_answer('{"axioms": []}')], draft)
        assert result.rejected is True
        assert mock_llm.call_count == 0

    def test_succeeds_when_attributes_done(self):
        draft = _draft(relations_done=True, attributes_done=True)
        payload = (
            '{"axioms": [{"kind": "subClassOf", '
            '"subject": "cand-6", "object": "cls-Customer-a1"}]}'
        )
        result, mock_llm = self._run([_answer(payload)], draft)
        assert result.success is True
        assert result.substage == "axioms"

    def test_orphan_reference_rejected_no_rewrite(self):
        draft = _draft(relations_done=True, attributes_done=True)
        payload = (
            '{"axioms": [{"kind": "disjointWith", '
            '"subject": "cand-6", "object": "cls-UnknownGhost"}]}'
        )
        result, mock_llm = self._run([_answer(payload)], draft)
        assert result.success is False
        assert result.rejected is True
        assert "cls-UnknownGhost" in result.rejection_reason
        assert mock_llm.call_count == 1


# ---------------------------------------------------------------------------
# No one-shot default path
# ---------------------------------------------------------------------------


class TestNoOneShotDefault:
    def test_staged_module_does_not_expose_run_agent(self):
        # The one-shot generator must not be reachable via the staged surface.
        assert not hasattr(staged, "run_agent")

    def test_staged_functions_do_not_reference_run_agent(self):
        import inspect

        source = inspect.getsource(staged)
        assert "run_agent" not in source

    def test_staged_functions_do_not_reference_pitfall_rewrite_loop(self):
        # Review fix (item 4): the legacy one-shot bridge's post-generation
        # check_owl_pitfalls -> fix rewrite loop (agent_owl_generator.engine)
        # must never be reachable from the staged path, even indirectly.
        import inspect

        source = inspect.getsource(staged)
        for banned in (
            "tool_check_owl_pitfalls",
            "check_owl_pitfalls",
            "_evaluate_ontology_stage",
            "owl_eval_max_rounds",
            "MAX_OWL_EVAL_ROUNDS",
        ):
            assert banned not in source

    def test_staged_module_never_imports_legacy_engine(self):
        # Checks actual import statements (AST), not prose: the module
        # docstring legitimately *names* agents.agent_owl_generator.engine
        # to document that it is deliberately not imported.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(staged))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(
                    "agent_owl_generator.engine" in alias.name
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "agent_owl_generator.engine" not in module
                if module == "agents.agent_owl_generator":
                    assert not any(alias.name == "engine" for alias in node.names)

    def test_public_entry_points_exist(self):
        for name in (
            "detect_entities",
            "infer_relations",
            "infer_attributes",
            "infer_axioms",
        ):
            assert callable(getattr(staged, name))


# ---------------------------------------------------------------------------
# Prompt-first pitfalls + lexical alternate labels
# ---------------------------------------------------------------------------


class TestPrompts:
    def test_detection_prompt_lists_anchors_for_dedup(self):
        anchors = [
            GenerateEntity.locked_anchor(
                "cls-Customer-a1", "Customer", alternate_labels=["Client"]
            )
        ]
        text = prompts.build_detection_system_prompt(existing_anchors=anchors)
        assert "Customer" in text
        # Alternate labels are surfaced so the model dedups against synonyms.
        assert "Client" in text

    def test_relations_prompt_includes_alternate_labels_as_lexical_evidence(self):
        draft = _draft(
            candidates=[
                GenerateEntity.new_candidate(
                    "Carrier",
                    entity_id="cand-6",
                    alternate_labels=["Shipper", "Freight Company"],
                )
            ]
        )
        text = prompts.build_relations_user_prompt(draft)
        assert "cand-6" in text
        assert "Shipper" in text

    def test_prompts_carry_pitfall_naming_rules_up_front(self):
        # Prompt-first: naming rules live in the stage prompt, not in a
        # post-generation rewrite loop.
        text = prompts.build_relations_system_prompt()
        assert "lowerCamelCase" in text

    def test_completion_prompt_excludes_excluded_candidates(self):
        draft = _draft(
            candidates=[
                GenerateEntity.new_candidate("Carrier", entity_id="cand-6"),
                GenerateEntity.new_candidate(
                    "Invoice", entity_id="cand-4", included=False
                ),
            ]
        )
        text = prompts.build_relations_user_prompt(draft)
        assert "cand-6" in text
        assert "cand-4" not in text


# ---------------------------------------------------------------------------
# Live Stage-1 detection failure fix: explicit zero-new-candidate contract
# ---------------------------------------------------------------------------
#
# Root cause: a session with every selected table's core entity already a
# locked anchor gives the model no NEW grounded candidate. The prompt said
# "JSON only" but never explicitly defined what to return in that case, so
# the model replied with prose/refusal instead of a structured answer, and
# `detect_entities` correctly (but unhelpfully, from the user's perspective)
# rejected it with "output is not valid JSON". The fix is prompt-only — the
# reject-only architecture and schema (which already accepts an empty list)
# are unchanged.


class TestZeroCandidateContract:
    def test_system_prompt_states_the_exact_empty_json_contract(self):
        anchors = [
            GenerateEntity.locked_anchor("cls-Customer-a1", "Customer"),
            GenerateEntity.locked_anchor("cls-Order-b2", "Order"),
        ]
        text = prompts.build_detection_system_prompt(existing_anchors=anchors)
        # The exact, byte-literal JSON the model must emit when nothing new
        # is grounded — not a paraphrase the model could reinterpret.
        assert '{"candidate_entities": []}' in text

    def test_system_prompt_states_anchors_are_context_not_candidates(self):
        anchors = [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")]
        text = prompts.build_detection_system_prompt(existing_anchors=anchors)
        lowered = text.lower()
        assert "not candidates" in lowered or "never candidates" in lowered

    def test_system_prompt_forbids_prose_refusal_or_fence_on_empty_result(self):
        anchors = [GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")]
        text = prompts.build_detection_system_prompt(existing_anchors=anchors)
        lowered = text.lower()
        assert "no explanation" in lowered or "no prose" in lowered
        assert "refus" in lowered  # "refusal"/"refuse"

    def test_system_prompt_states_the_zero_candidate_contract_even_with_no_anchors(
        self,
    ):
        # The contract must be stated unconditionally, not only appended to
        # the "existing anchors" branch — a from-scratch domain with zero
        # anchors could still legitimately ground nothing new from a sparse
        # source.
        text = prompts.build_detection_system_prompt(existing_anchors=())
        assert '{"candidate_entities": []}' in text

    def test_system_prompt_forbids_a_reasoning_preamble_before_the_json(self):
        # Live investigation finding: against a real endpoint with a large
        # multi-table source, the model gathered context via tools
        # correctly, then — once forced onto its final (no-tools) turn —
        # wrote a visible step-by-step "Analysis:" preamble before (never
        # reaching) the JSON, which fails to parse. The prompt must forbid
        # this explicitly, not just forbid "explaining an empty result".
        text = prompts.build_detection_system_prompt(existing_anchors=())
        lowered = text.lower()
        assert "first character" in lowered
        assert "analysis" in lowered or "reasoning" in lowered


# ---------------------------------------------------------------------------
# Review fix (item 2): per-tool MLflow tracing for detection tool dispatch
# ---------------------------------------------------------------------------


def _fake_mlflow(spans):
    """Minimal fake ``mlflow`` module recording every span opened/closed,
    across span types (AGENT/LLM/TOOL), so both ``trace_agent`` (already
    wraps ``detect_entities``) and the new per-tool ``trace_tool`` span can
    be exercised in the same scripted run."""

    class _Span:
        def __init__(self, name, span_type):
            self.name = name
            self.span_type = span_type
            self.inputs: dict = {}
            self.outputs: dict = {}

        def set_inputs(self, data):
            self.inputs.update(data)

        def set_outputs(self, data):
            self.outputs.update(data)

        def set_attributes(self, data):
            self.inputs.update(data)

    class _CM:
        def __init__(self, span):
            self._span = span

        def __enter__(self_inner):
            return self_inner._span

        def __exit__(self_inner, *exc):
            spans.append(self_inner._span)
            return False

    def _start_span(name=None, span_type=None):
        return _CM(_Span(name, span_type))

    mock = MagicMock()
    mock.start_span.side_effect = _start_span

    class _SpanType:
        AGENT = "AGENT"
        LLM = "LLM"
        TOOL = "TOOL"

    entities = MagicMock()
    entities.SpanType = _SpanType
    return mock, entities


class TestPerToolTracing:
    """Each Stage-1 tool dispatch gets its own MLflow TOOL span (tool name +
    ok/error status visible on the trace), scoped to the staged detection
    path only — the shared ``dispatch_tool`` helper (used by ~10 other agent
    engines) and the shared tool handlers are untouched."""

    def test_tool_dispatch_gets_a_named_tool_span_with_status(self):
        spans: list = []
        mock_mlflow, mock_entities = _fake_mlflow(spans)
        tracing_mod._TRACING_READY = True
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                with patch.object(staged, "call_serving_endpoint") as mock_llm:
                    mock_llm.side_effect = [
                        _tool_call("get_metadata"),
                        _answer(payload),
                    ]
                    result = staged.detect_entities(
                        host="https://test.databricks.com",
                        token="tok",
                        endpoint_name="dbx-llm",
                        metadata={"tables": [{"name": "orders", "columns": []}]},
                        guidelines="Generate a CRM ontology.",
                        existing_anchors=[
                            GenerateEntity.locked_anchor("cls-Customer-a1", "Customer")
                        ],
                        registry={"catalog": "main", "schema": "ob", "volume": "documents"},
                    )
        finally:
            tracing_mod._TRACING_READY = False

        assert result.success is True
        tool_spans = [s for s in spans if s.span_type == "TOOL"]
        assert len(tool_spans) == 1
        assert tool_spans[0].name == "tool:get_metadata"
        assert tool_spans[0].inputs["tool_name"] == "get_metadata"
        assert tool_spans[0].outputs["status"] == "ok"

    def test_status_reflects_a_tool_error_result(self):
        spans: list = []
        mock_mlflow, mock_entities = _fake_mlflow(spans)
        tracing_mod._TRACING_READY = True
        payload = '{"candidate_entities": [{"canonical_label": "Carrier"}]}'
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                # get_table_detail with no table_name argument fails cleanly
                # (returns a JSON error), never raises.
                result, _ = _detect(
                    [_tool_call("get_table_detail"), _answer(payload)]
                )
        finally:
            tracing_mod._TRACING_READY = False

        assert result.success is True
        tool_spans = [s for s in spans if s.span_type == "TOOL"]
        assert len(tool_spans) == 1
        assert tool_spans[0].outputs["status"] == "error"

    def test_no_handler_behaviour_change_when_traced(self):
        """The dispatched tool result is byte-identical whether or not
        tracing is enabled — tracing is observability only."""
        from agents.agent_owl_generator.tools import TOOL_HANDLERS
        from agents.agent_owl_generator.staged import _build_context
        from agents.engine_base import dispatch_tool

        ctx = _build_context(
            host="https://test",
            token="t",
            registry={},
            metadata={"tables": [{"name": "orders", "columns": []}]},
            domain_name=None,
            domain_folder=None,
            domain_version=None,
            warehouse_id=None,
            selected_tables=None,
        )
        direct = dispatch_tool(TOOL_HANDLERS, ctx, "get_metadata", {}, trace_name="x")

        spans: list = []
        mock_mlflow, mock_entities = _fake_mlflow(spans)
        tracing_mod._TRACING_READY = True
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                traced = staged._dispatch_detection_tool(
                    ctx, "get_metadata", {}, trace_name="x"
                )
        finally:
            tracing_mod._TRACING_READY = False

        assert traced == direct
