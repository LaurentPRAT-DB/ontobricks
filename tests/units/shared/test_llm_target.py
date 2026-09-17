"""Tests for shared Databricks LLM target routing."""

from shared.llm_target import (
    AI_GATEWAY,
    SERVING,
    build_llm_request,
    normalize_llm_endpoint_kind,
)


def test_explicit_kind_wins_over_name_shape():
    assert normalize_llm_endpoint_kind("a.b.c", SERVING) == SERVING


def test_legacy_fqn_is_inferred_as_gateway():
    assert normalize_llm_endpoint_kind("main.ai.monclaudesonnetamoi") == AI_GATEWAY
    assert normalize_llm_endpoint_kind("databricks-claude-sonnet-4-5") == SERVING


def test_gateway_request_uses_mlflow_chat_completions():
    url, payload = build_llm_request(
        "https://workspace/",
        "main.ai.monclaudesonnetamoi",
        AI_GATEWAY,
        [{"role": "user", "content": "hello"}],
        max_tokens=256,
        temperature=0.1,
    )

    assert url == "https://workspace/ai-gateway/mlflow/v1/chat/completions"
    assert payload["model"] == "main.ai.monclaudesonnetamoi"


def test_serving_request_keeps_invocations_contract():
    url, payload = build_llm_request(
        "https://workspace",
        "databricks-claude-sonnet-4-5",
        SERVING,
        [],
        max_tokens=256,
        temperature=None,
    )

    assert url.endswith(
        "/serving-endpoints/databricks-claude-sonnet-4-5/invocations"
    )
    assert "model" not in payload
    assert "temperature" not in payload


def test_request_includes_tools_when_provided():
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    _, payload = build_llm_request(
        "https://workspace",
        "main.ai.service",
        AI_GATEWAY,
        [],
        max_tokens=256,
        temperature=0.1,
        tools=tools,
    )

    assert payload["tools"] == tools
