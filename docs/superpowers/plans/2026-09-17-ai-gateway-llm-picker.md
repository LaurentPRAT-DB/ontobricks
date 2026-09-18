# AI Gateway + Serving LLM Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each domain select any executable Unity AI Gateway model service through a searchable modal while preserving legacy Model Serving endpoints.

**Architecture:** Add a small shared LLM-target utility that decides the request URL and payload from an endpoint name and persisted kind. Add a Databricks model-service catalog that paginates the workspace-global service list and filters to effective `EXECUTE` with bounded parallel probes; merge those results with legacy Serving endpoints in the existing API. Persist `llm_endpoint_kind`, route every LLM transport by target shape/kind, and replace native selects with one reusable Bootstrap picker.

**Tech Stack:** Python 3.11, FastAPI, `requests`, Databricks Unity Catalog REST API 2.1, Bootstrap 5, vanilla JavaScript, pytest.

## Global Constraints

- Preserve existing `domain.info.llm_endpoint` values and legacy Serving behavior.
- Gateway values are three-part FQNs; Serving values are endpoint leaf names.
- New selections always persist `llm_endpoint_kind` as `ai_gateway` or `serving`.
- Show only Gateway services the authenticated principal can execute.
- AI Gateway appears before Model Serving (legacy); include `system.ai.*`.
- Keep MLflow `@trace_llm` on every agent Foundation Model call.
- Do not change prompts, tool schemas, or eval behavior; no eval dataset delta is required.
- No inline CSS or JavaScript; use existing `--db-*` UI tokens.
- Run commands through `uv run --frozen`.

---

### Task 1: Define the shared LLM target contract

**Files:**
- Create: `src/shared/llm_target.py`
- Create: `tests/units/shared/test_llm_target.py`

**Interfaces:**
- Produces: `normalize_llm_endpoint_kind(endpoint_name: str, endpoint_kind: str = "") -> str`
- Produces: `build_llm_request(host: str, endpoint_name: str, endpoint_kind: str, messages: list[dict], *, max_tokens: int, temperature: float | None, tools: list[dict] | None = None) -> tuple[str, dict]`

- [ ] **Step 1: Write failing target-classification tests**

```python
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
    assert url.endswith("/serving-endpoints/databricks-claude-sonnet-4-5/invocations")
    assert "model" not in payload
    assert "temperature" not in payload
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run --frozen pytest -q tests/units/shared/test_llm_target.py`

Expected: collection fails because `shared.llm_target` does not exist.

- [ ] **Step 3: Implement the pure target utility**

```python
from __future__ import annotations

from typing import Any

AI_GATEWAY = "ai_gateway"
SERVING = "serving"
VALID_LLM_ENDPOINT_KINDS = frozenset({AI_GATEWAY, SERVING})


def normalize_llm_endpoint_kind(endpoint_name: str, endpoint_kind: str = "") -> str:
    kind = str(endpoint_kind or "").strip().lower()
    if kind in VALID_LLM_ENDPOINT_KINDS:
        return kind
    name = str(endpoint_name or "").strip()
    return AI_GATEWAY if name.count(".") == 2 and "/" not in name else SERVING


def build_llm_request(
    host: str,
    endpoint_name: str,
    endpoint_kind: str,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float | None,
    tools: list[dict] | None = None,
) -> tuple[str, dict[str, Any]]:
    name = str(endpoint_name or "").strip()
    kind = normalize_llm_endpoint_kind(name, endpoint_kind)
    payload: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    base = host.rstrip("/")
    if kind == AI_GATEWAY:
        payload["model"] = name
        return f"{base}/ai-gateway/mlflow/v1/chat/completions", payload
    return f"{base}/serving-endpoints/{name}/invocations", payload
```

- [ ] **Step 4: Run the focused tests**

Run: `uv run --frozen pytest -q tests/units/shared/test_llm_target.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/shared/llm_target.py tests/units/shared/test_llm_target.py
git commit -m "feat(llm): define gateway and serving targets"
```

---

### Task 2: Persist the endpoint kind without breaking old domains

**Files:**
- Modify: `src/back/objects/session/DomainSession.py:70-82,1323-1355,1460-1472`
- Modify: `src/back/objects/domain/Domain.py:238-248,368-380,450-462,559-571`
- Modify: `tests/units/domain/test_domain_service.py`
- Modify: `tests/units/domain/test_domain_session.py`

**Interfaces:**
- Consumes: `normalize_llm_endpoint_kind` from Task 1.
- Produces: `domain.info["llm_endpoint_kind"]` in defaults, save/load payloads, and domain information responses.

- [ ] **Step 1: Write failing round-trip tests**

Add to `TestSaveDomainInfo`:

```python
def test_save_llm_endpoint_kind(self):
    domain = _mock_domain()
    result = Domain(domain).save_domain_info(
        {
            "llm_endpoint": "main.ai.monclaudesonnetamoi",
            "llm_endpoint_kind": "ai_gateway",
        }
    )
    assert domain.info["llm_endpoint_kind"] == "ai_gateway"
    assert result["llm_endpoint_kind"] == "ai_gateway"


def test_legacy_llm_endpoint_kind_is_inferred(self):
    domain = _mock_domain()
    domain.info["llm_endpoint"] = "databricks-claude-sonnet-4-5"
    result = Domain(domain).get_domain_info()
    assert result["info"]["llm_endpoint_kind"] == "serving"
```

Add a `DomainSession` test asserting `get_empty_domain()["domain"]["info"]` contains `llm_endpoint_kind == ""` and session serialization restores an explicit `ai_gateway`.

- [ ] **Step 2: Verify the persistence tests fail**

Run: `uv run --frozen pytest -q tests/units/domain/test_domain_service.py tests/units/domain/test_domain_session.py`

Expected: assertions fail because the kind is absent.

- [ ] **Step 3: Add the field to all persistence projections**

Add the empty default:

```python
"llm_endpoint": "",
"llm_endpoint_kind": "",
```

On save, normalize only when an endpoint exists:

```python
llm_endpoint = data.get("llm_endpoint", self._s.info.get("llm_endpoint", ""))
llm_endpoint_kind = (
    normalize_llm_endpoint_kind(
        llm_endpoint,
        data.get(
            "llm_endpoint_kind",
            self._s.info.get("llm_endpoint_kind", ""),
        ),
    )
    if llm_endpoint
    else ""
)
```

Store and return both fields. Apply the same field projection anywhere `Domain.py` or `DomainSession.py` currently projects `llm_endpoint`.

- [ ] **Step 4: Run the persistence tests**

Run: `uv run --frozen pytest -q tests/units/domain/test_domain_service.py tests/units/domain/test_domain_session.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/back/objects/session/DomainSession.py src/back/objects/domain/Domain.py tests/units/domain/test_domain_service.py tests/units/domain/test_domain_session.py
git commit -m "feat(domain): persist LLM endpoint kind"
```

---

### Task 3: List executable AI Gateway model services

**Files:**
- Create: `src/back/core/databricks/ModelServiceCatalog.py`
- Modify: `src/back/core/databricks/DatabricksClient.py`
- Modify: `src/api/routers/internal/mapping.py:400-425`
- Create: `tests/units/core/test_model_service_catalog.py`
- Create: `tests/units/api/test_llm_endpoints.py`

**Interfaces:**
- Produces: `ModelServiceCatalog.list_executable() -> list[dict[str, str]]`
- Produces: `DatabricksClient.get_ai_gateway_model_services() -> list[dict[str, str]]`
- Changes `/mapping/wizard/llm-endpoints` response items to `{name, kind, comment?, state?}`.

- [ ] **Step 1: Write failing pagination and permission-filter tests**

Use a mocked auth object with `host`, `get_headers()`, and mocked `requests.get`. Cover:

```python
def test_lists_all_pages_and_strips_resource_prefix():
    # Global model-service pages return model-services/main.ai.first then
    # model-services/main.ai.second.
    # Effective permissions contain EXECUTE.
    assert catalog.list_executable() == [
        {"name": "main.ai.first", "kind": "ai_gateway", "comment": ""},
        {"name": "main.ai.second", "kind": "ai_gateway", "comment": "routed"},
    ]


def test_excludes_read_metadata_only_service():
    # Effective permissions return READ_METADATA without EXECUTE.
    assert catalog.list_executable() == []


def test_uses_global_listing_without_schema_enumeration():
    client.get_catalogs.side_effect = AssertionError("must not enumerate catalogs")
    assert catalog.list_executable() == []
    assert "parent" not in requests_get.call_args.kwargs["params"]
```

The effective-permissions request must use:

`GET /api/2.1/unity-catalog/effective-permissions/model_service/{fqn}?principal={SCIM /Me email}`

Treat owner equality with the authenticated principal as executable without an extra permission call.

- [ ] **Step 2: Verify catalog tests fail**

Run: `uv run --frozen pytest -q tests/units/core/test_model_service_catalog.py`

Expected: import fails because `ModelServiceCatalog` does not exist.

- [ ] **Step 3: Implement global pagination and effective-EXECUTE filtering**

The implementation must:

```python
page_token = ""
while True:
    # GET global model-services with page_size=100 and view=BASIC.
    # Check effective EXECUTE concurrently with a bounded worker pool.
    # Append executable rows and follow next_page_token until empty.
```

Catch candidate permission failures, log at debug without tokens, and exclude those candidates. A global authentication/listing failure yields the successfully collected pages or `[]`.

- [ ] **Step 4: Write the failing merged-router tests**

```python
def test_llm_endpoints_puts_gateway_before_serving(api_client, mocker):
    mocker.patch.object(
        DatabricksClient,
        "get_ai_gateway_model_services",
        return_value=[{"name": "main.ai.mine", "kind": "ai_gateway", "comment": ""}],
    )
    mocker.patch.object(
        SQLWizardService,
        "get_model_serving_endpoints",
        return_value=[{"name": "legacy", "state": "READY"}],
    )
    payload = api_client.get("/mapping/wizard/llm-endpoints").json()
    assert [row["kind"] for row in payload["endpoints"]] == [
        "ai_gateway",
        "serving",
    ]


def test_gateway_listing_failure_keeps_serving(api_client, mocker):
    mocker.patch.object(
        DatabricksClient,
        "get_ai_gateway_model_services",
        side_effect=RuntimeError("gateway unavailable"),
    )
    # Mock Serving as READY and assert it is returned with kind=serving.
```

- [ ] **Step 5: Extend the facade and merge the API results**

Instantiate `ModelServiceCatalog(self)` lazily in `get_ai_gateway_model_services`. In the router, isolate Gateway and Serving in separate `try` blocks, add `kind: "serving"` to legacy rows, sort each group by case-insensitive name, and concatenate Gateway first.

- [ ] **Step 6: Run catalog and router tests**

Run: `uv run --frozen pytest -q tests/units/core/test_model_service_catalog.py tests/units/api/test_llm_endpoints.py`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/back/core/databricks/ModelServiceCatalog.py src/back/core/databricks/DatabricksClient.py src/api/routers/internal/mapping.py tests/units/core/test_model_service_catalog.py tests/units/api/test_llm_endpoints.py
git commit -m "feat(gateway): list executable model services"
```

---

### Task 4: Route agent and SQL Wizard calls by endpoint kind

**Files:**
- Modify: `src/agents/engine_base.py:64-115`
- Modify: every agent engine/component returned by `rg -l 'call_serving_endpoint\\(' src/agents`
- Modify: top-level `run_agent` signatures under `src/agents/**`
- Modify: `src/back/objects/ontology/Ontology.py`
- Modify: `src/back/objects/mapping/Mapping.py`
- Modify: `src/back/core/agents/AgentClient.py`
- Modify: `src/back/core/sqlwizard/SQLWizardService.py:264-375`
- Modify: `tests/units/agents/test_agent_engine_base.py`
- Modify: `tests/units/core/test_sql_wizard.py`

**Interfaces:**
- Consumes: `build_llm_request` and `normalize_llm_endpoint_kind` from Task 1.
- Changes: `call_serving_endpoint(..., *, endpoint_kind: str = "", ...)`.
- Changes: top-level agent/object/client methods accept trailing optional `endpoint_kind: str = ""` and pass it unchanged.
- Changes: `SQLWizardService.call_llm_endpoint(..., endpoint_kind: str = "")`.

- [ ] **Step 1: Add failing agent transport tests**

```python
@patch("agents.engine_base.call_llm_with_retry")
def test_gateway_uses_chat_completions_and_model(mock_retry):
    mock_retry.return_value.json.return_value = {"choices": []}
    call_serving_endpoint(
        "https://host",
        "tok",
        "main.ai.mine",
        [],
        endpoint_kind="ai_gateway",
    )
    url, _, payload = mock_retry.call_args.args[:3]
    assert url == "https://host/ai-gateway/mlflow/v1/chat/completions"
    assert payload["model"] == "main.ai.mine"


@patch("agents.engine_base.call_llm_with_retry")
def test_explicit_serving_kind_allows_dotted_name(mock_retry):
    mock_retry.return_value.json.return_value = {}
    call_serving_endpoint(
        "https://host", "tok", "legacy.with.dots", [],
        endpoint_kind="serving",
    )
    assert "/serving-endpoints/legacy.with.dots/invocations" in (
        mock_retry.call_args.args[0]
    )
```

- [ ] **Step 2: Add failing SQL Wizard Gateway test**

```python
@patch("back.core.sqlwizard.SQLWizardService.requests.post")
def test_call_llm_endpoint_routes_gateway(mock_post, wizard):
    mock_post.return_value.json.return_value = {
        "choices": [{"message": {"content": "SELECT 1"}}]
    }
    wizard.call_llm_endpoint(
        "main.ai.mine",
        {"system": "s", "user": "u"},
        endpoint_kind="ai_gateway",
    )
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url.endswith("/ai-gateway/mlflow/v1/chat/completions")
    assert payload["model"] == "main.ai.mine"
```

- [ ] **Step 3: Verify transport tests fail**

Run: `uv run --frozen pytest -q tests/units/agents/test_agent_engine_base.py tests/units/core/test_sql_wizard.py`

Expected: calls reject `endpoint_kind` or use the Serving URL.

- [ ] **Step 4: Use `build_llm_request` in both HTTP transports**

In `engine_base.py`, build URL/payload centrally, then remove banned parameters from the returned payload:

```python
url, payload = build_llm_request(
    host,
    endpoint_name,
    endpoint_kind,
    messages,
    max_tokens=max_tokens,
    temperature=temperature,
    tools=tools,
)
for param in _unsupported_params(endpoint_name):
    payload.pop(param, None)
```

In SQL Wizard, pass its two-message list to the same builder. Keep its response parsing and exception conversion unchanged.

- [ ] **Step 5: Propagate the optional kind through every agent layer**

For each top-level `run_agent`, add `endpoint_kind: str = ""` as a trailing argument and pass:

```python
llm_response = call_serving_endpoint(
    host,
    token,
    endpoint_name,
    messages,
    endpoint_kind=endpoint_kind,
    # existing options unchanged
)
```

For PGE planner/generator/critic and ontology responses-agent classes, store `self.endpoint_kind` beside `self.endpoint_name` and forward it at each transport call. Update `Ontology.py`, `Mapping.py`, and `AgentClient.py` signatures/calls similarly. Do not alter prompts, tools, iteration limits, or parsing.

- [ ] **Step 6: Prove no transport call dropped the kind**

Run:

```bash
rg -n 'call_serving_endpoint\\(' src/agents
uv run --frozen pytest -q tests/units/agents tests/agents tests/units/core/test_sql_wizard.py
```

Expected: every production call has `endpoint_kind=...`; all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/agents src/back/objects/ontology/Ontology.py src/back/objects/mapping/Mapping.py src/back/core/agents/AgentClient.py src/back/core/sqlwizard/SQLWizardService.py tests/units/agents tests/agents tests/units/core/test_sql_wizard.py
git commit -m "feat(llm): route calls through Unity AI Gateway"
```

---

### Task 5: Pass domain kind and prefer Gateway during Graph Chat discovery

**Files:**
- Modify: `src/back/core/helpers/DatabricksHelpers.py:561-577`
- Modify: `src/api/routers/internal/ontology.py`
- Modify: `src/api/routers/internal/mapping.py`
- Modify: `src/api/routers/internal/dtwin.py:810-855,2370-2405,2430-2670`
- Modify: `tests/units/api/test_dtwin_chat_pending_action_cache.py`
- Create: `tests/units/dtwin/test_llm_auto_discovery.py`
- Create: `tests/units/core/test_databricks_helpers_llm.py`

**Interfaces:**
- Changes: `require_serving_llm(domain, settings) -> tuple[str, str, str, str]` returns host, token, endpoint name, endpoint kind.
- Changes: `_auto_discover_llm_endpoint(domain, settings) -> tuple[str, str]`.
- Consumes: optional `endpoint_kind` parameters from Task 4.

- [ ] **Step 1: Write failing helper and auto-discovery tests**

```python
def test_require_serving_llm_returns_persisted_kind():
    domain.info = {
        "llm_endpoint": "main.ai.mine",
        "llm_endpoint_kind": "ai_gateway",
    }
    assert require_serving_llm(domain, settings)[2:] == (
        "main.ai.mine",
        "ai_gateway",
    )


def test_auto_discover_prefers_system_gateway(mocker):
    mocker.patch.object(
        DatabricksClient,
        "get_ai_gateway_model_services",
        return_value=[
            {"name": "main.ai.custom", "kind": "ai_gateway"},
            {"name": "system.ai.claude-sonnet-4-5", "kind": "ai_gateway"},
        ],
    )
    # Serving also returns a READY databricks-* endpoint.
    assert _auto_discover_llm_endpoint(domain, settings) == (
        "system.ai.claude-sonnet-4-5",
        "ai_gateway",
    )
```

Also test custom Gateway fallback, then READY `databricks-*`, then any READY Serving, then `("", "")`.

- [ ] **Step 2: Verify tests fail**

Run: `uv run --frozen pytest -q tests/units/core/test_databricks_helpers_llm.py tests/units/dtwin/test_llm_auto_discovery.py`

Expected: tuple-shape and ordering assertions fail.

- [ ] **Step 3: Return and pass kind at all domain call sites**

Read:

```python
endpoint_kind = normalize_llm_endpoint_kind(
    endpoint,
    (domain.info or {}).get("llm_endpoint_kind", ""),
)
return host, token, endpoint, endpoint_kind
```

Update every `require_serving_llm` unpack and every Mapping/Ontology/Graph Chat/SQL Wizard invocation to pass `endpoint_kind`. Update warning text from “serving endpoint” to “LLM endpoint” where Gateway is also valid.

- [ ] **Step 4: Implement Gateway-first auto-discovery**

```python
gateway = client.get_ai_gateway_model_services() or []
system = next(
    (row for row in gateway if row.get("name", "").startswith("system.ai.")),
    None,
)
if system:
    return system["name"], "ai_gateway"
if gateway:
    return gateway[0]["name"], "ai_gateway"
# Preserve existing READY Serving preference and return (name, "serving").
```

Use the returned kind only for the current call. Do not persist an auto-discovered target.

- [ ] **Step 5: Run focused router/helper tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/core/test_databricks_helpers_llm.py \
  tests/units/dtwin/test_llm_auto_discovery.py \
  tests/units/api/test_dtwin_chat_pending_action_cache.py \
  tests/units/mapping
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/back/core/helpers/DatabricksHelpers.py src/api/routers/internal/ontology.py src/api/routers/internal/mapping.py src/api/routers/internal/dtwin.py tests/units/core/test_databricks_helpers_llm.py tests/units/dtwin/test_llm_auto_discovery.py tests/units/api/test_dtwin_chat_pending_action_cache.py tests/units/mapping
git commit -m "feat(domain): propagate Gateway LLM targets"
```

---

### Task 6: Build the reusable searchable picker modal

**Files:**
- Create: `src/front/templates/partials/layout/llm_endpoint_picker_modal.html`
- Create: `src/front/static/global/js/llm-endpoint-picker.js`
- Create: `src/front/static/global/css/llm-endpoint-picker.css`
- Modify: `src/front/templates/base.html`
- Modify: `src/front/templates/partials/domain/_domain_information.html:166-189`
- Modify: `src/front/static/domain/js/domain-information.js:148-198,495-525`
- Modify: `src/front/static/domain/js/domain.js:285-315`
- Modify: `src/front/static/global/js/navbar.js:708-730`
- Modify: `src/front/static/global/js/utils.js:840-955`
- Create: `tests/units/front/test_llm_endpoint_picker.py`
- Modify: `tests/units/api/test_ui_rendering.py`

**Interfaces:**
- Produces: `window.openLlmEndpointPicker({name, kind}) -> Promise<{name: string, kind: string} | null>`.
- Consumes: `/mapping/wizard/llm-endpoints` merged payload from Task 3.
- Produces hidden inputs: `#domainLlmEndpoint`, `#domainLlmEndpointKind`.

- [ ] **Step 1: Write failing template/JS contract tests**

```python
def test_shared_picker_is_loaded_from_base():
    base = (REPO_ROOT / "src/front/templates/base.html").read_text()
    assert "partials/layout/llm_endpoint_picker_modal.html" in base
    assert "global/js/llm-endpoint-picker.js" in base


def test_domain_llm_uses_hidden_name_and_kind():
    html = (
        REPO_ROOT
        / "src/front/templates/partials/domain/_domain_information.html"
    ).read_text()
    assert 'id="domainLlmEndpoint"' in html
    assert 'id="domainLlmEndpointKind"' in html
    assert 'id="domainLlmBrowse"' in html


def test_picker_gateway_group_precedes_serving_group():
    html = (
        REPO_ROOT
        / "src/front/templates/partials/layout/llm_endpoint_picker_modal.html"
    ).read_text()
    assert html.index("llmGatewayList") < html.index("llmServingList")
```

Add source-contract assertions for search input, Refresh, Cancel, escaped `textContent`, and `openLlmEndpointPicker`.

- [ ] **Step 2: Verify UI tests fail**

Run: `uv run --frozen pytest -q tests/units/front/test_llm_endpoint_picker.py tests/units/api/test_ui_rendering.py`

Expected: missing files/IDs fail.

- [ ] **Step 3: Add the shared modal and assets to `base.html`**

The modal contains one search `<input>`, Refresh and Cancel buttons, two section headings and list containers, loading/empty/error status text, and no inline behavior. Load its CSS with the other global CSS and its script after Bootstrap and shared utilities.

- [ ] **Step 4: Implement modal loading, filtering, and selection**

Use one cached array per modal opening. Render Gateway first:

```javascript
const normalizedQuery = searchInput.value.trim().toLowerCase();
const visible = endpoints.filter((endpoint) => {
    const haystack = `${endpoint.name} ${endpoint.comment || ''}`.toLowerCase();
    return haystack.includes(normalizedQuery);
});
renderGroup(gatewayList, visible.filter((item) => item.kind === 'ai_gateway'));
renderGroup(servingList, visible.filter((item) => item.kind === 'serving'));
```

Build rows with `document.createElement` and `textContent`, never `innerHTML` with API data. Resolve the promise only on row selection; resolve `null` on Cancel/close. When opened over New Domain, hiding this modal must restore the create modal’s body/backdrop state.

- [ ] **Step 5: Replace the Domain LLM select**

Render:

```html
<input type="hidden" class="domain-editable" id="domainLlmEndpoint"
       value="{{ domain.llm_endpoint or '' }}">
<input type="hidden" class="domain-editable" id="domainLlmEndpointKind"
       value="{{ domain.llm_endpoint_kind or '' }}">
<input type="text" class="form-control" id="domainLlmEndpointDisplay"
       value="{{ domain.llm_endpoint or '' }}" readonly>
<button type="button" class="btn btn-outline-secondary" id="domainLlmBrowse">
    <i class="bi bi-search me-1"></i>Browse
</button>
```

On selection, update both hidden inputs, the display, their saved-value datasets, and dispatch `change` on `domainLlmEndpoint` so existing dirty/editability logic remains active.

- [ ] **Step 6: Persist kind from both save entry points**

Add `llm_endpoint_kind` beside `llm_endpoint` in `buildDomainInfoPayload`, the fallback payload in `domain.js`, and `showNewDomainDialog`’s resolved object. Replace the New Domain `<select>` with a read-only field + Browse button that calls the shared picker.

- [ ] **Step 7: Run UI tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_llm_endpoint_picker.py \
  tests/units/front/test_new_domain_close_flow.py \
  tests/units/front/test_new_domain_name_validation.py \
  tests/units/front/test_no_backend_ui.py \
  tests/units/api/test_ui_rendering.py
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/front/templates/base.html src/front/templates/partials/layout/llm_endpoint_picker_modal.html src/front/templates/partials/domain/_domain_information.html src/front/static/global/js/llm-endpoint-picker.js src/front/static/global/css/llm-endpoint-picker.css src/front/static/domain/js/domain-information.js src/front/static/domain/js/domain.js src/front/static/global/js/navbar.js src/front/static/global/js/utils.js tests/units/front/test_llm_endpoint_picker.py tests/units/api/test_ui_rendering.py
git commit -m "feat(domain): add searchable LLM picker"
```

---

### Task 7: Document, log, and verify the complete change

**Files:**
- Modify: `README.md`
- Modify: `docs/user-guide.md`
- Create or append: `changelogs/v0.9.0/benoitcayladbx_2026-09-17.log`

**Interfaces:**
- Documents the domain picker, Gateway permissions, FQN behavior, and legacy Serving compatibility.

- [ ] **Step 1: Update user-facing documentation**

Document:

- Domain → Information → LLM → Browse.
- Gateway group includes executable `system.ai.*` and custom model services.
- Required Gateway privileges: `USE CATALOG`, `USE SCHEMA`, `EXECUTE`.
- Serving remains under “Model Serving (legacy)”.
- Existing domains need no migration; endpoint kind is inferred and saved on the next selection/save.

- [ ] **Step 2: Add the mandatory English changelog section**

Use this structure:

```text
AI Gateway model selection

Context:
Domains previously listed and invoked only legacy Databricks Model Serving endpoints.

1. src/shared/llm_target.py — Added explicit AI Gateway and Serving request routing.
2. src/back/core/databricks/ModelServiceCatalog.py — Added executable model-service discovery.
3. src/front/... — Added the searchable grouped LLM picker.
4. tests/... — Added persistence, discovery, transport, and UI coverage.

Modified files:
Copy the complete output of `git diff --name-only` for this feature.

Test result:
Copy the exact summary line and exit code from
`uv run --frozen pytest -q -m "not scenario"`.
```

- [ ] **Step 3: Run lint diagnostics on all edited files**

Read IDE diagnostics for the edited Python, JavaScript, HTML, and CSS files. Fix only newly introduced errors.

- [ ] **Step 4: Run focused non-scenario tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/shared/test_llm_target.py \
  tests/units/core/test_model_service_catalog.py \
  tests/units/core/test_sql_wizard.py \
  tests/units/agents \
  tests/units/domain \
  tests/units/dtwin/test_llm_auto_discovery.py \
  tests/units/front/test_llm_endpoint_picker.py \
  tests/units/api/test_llm_endpoints.py
```

Expected: all tests pass.

- [ ] **Step 5: Run the repository-required suite**

Run: `uv run --frozen pytest -q -m "not scenario"`

Expected: exit code 0. Record the exact passed/skipped/deselected counts in the changelog.

- [ ] **Step 6: Inspect the final diff and verify scope**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; unrelated existing untracked specification/plan files remain untouched.

- [ ] **Step 7: Commit documentation and changelog**

```bash
git add README.md docs/user-guide.md changelogs/v0.9.0/benoitcayladbx_2026-09-17.log
git commit -m "docs(llm): explain AI Gateway model selection"
```
