# No-LLM AI Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an empty domain LLM endpoint a strict opt-out that disables every domain-scoped LLM feature in the UI and rejects direct API invocation.

**Architecture:** Add one `require_domain_llm()` backend guard returning the saved target and use it from every domain-scoped LLM route, including Mapping SQL. Remove Graph auto-discovery. Add declarative `data-requires-llm` markers controlled by a fail-closed body state and one global capture-phase interaction gate.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, vanilla JavaScript, Bootstrap 5, pytest.

## Global Constraints

- `domain.info.llm_endpoint` is the sole authority for domain-scoped LLM availability.
- Any empty endpoint disables AI, including legacy empty domains; no model auto-discovery remains.
- Domain Information → AI and the shared LLM picker always remain usable.
- Deterministic SHACL, reasoning, pitfalls, and cohort features remain enabled.
- The saved endpoint overrides compatibility request fields.
- Use the exact guidance: `Select an LLM in Domain Information → AI.`
- Do not change prompts, tools, agent logic, or output behavior; no eval dataset delta is required.
- Follow TDD for every behavior change.
- Run all commands with `uv run --frozen`.

---

### Task 1: Add the central domain LLM guard

**Files:**
- Modify: `src/back/core/helpers/DatabricksHelpers.py`
- Modify: `src/back/core/helpers/__init__.py`
- Create: `tests/units/core/test_domain_llm_guard.py`

**Interfaces:**
- Consumes: `normalize_llm_endpoint_kind(endpoint_name: str, endpoint_kind: str = "") -> str`
- Produces: `require_domain_llm(domain, settings) -> tuple[str, str, str, str]`
- Compatibility: keep `require_serving_llm()` as a three-value wrapper until Tasks 2 and 3 migrate all callers.

- [ ] **Step 1: Write failing guard tests**

```python
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from back.core.errors import ValidationError
from back.core.helpers.DatabricksHelpers import DatabricksHelpers


def _domain(endpoint="", kind=""):
    return SimpleNamespace(
        info={"llm_endpoint": endpoint, "llm_endpoint_kind": kind},
    )


@patch.object(
    DatabricksHelpers,
    "get_databricks_host_and_token",
    return_value=("https://workspace", "token"),
)
def test_require_domain_llm_returns_normalized_target(_credentials):
    assert DatabricksHelpers.require_domain_llm(
        _domain("main.ai.model", ""), SimpleNamespace()
    ) == ("https://workspace", "token", "main.ai.model", "ai_gateway")


@patch.object(
    DatabricksHelpers,
    "get_databricks_host_and_token",
    return_value=("https://workspace", "token"),
)
def test_require_domain_llm_rejects_empty_endpoint(_credentials):
    with pytest.raises(
        ValidationError,
        match="No LLM selected. Select one in Domain Information → AI.",
    ):
        DatabricksHelpers.require_domain_llm(_domain(), SimpleNamespace())
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/core/test_domain_llm_guard.py
```

Expected: FAIL because `require_domain_llm` does not exist.

- [ ] **Step 3: Implement the guard and temporary wrapper**

```python
@staticmethod
def require_domain_llm(domain, settings) -> tuple[str, str, str, str]:
    host, token = DatabricksHelpers.get_databricks_host_and_token(domain, settings)
    if not host or not token:
        raise ValidationError("Databricks credentials not configured")
    info = domain.info or {}
    endpoint = str(info.get("llm_endpoint") or "").strip()
    if not endpoint:
        raise ValidationError(
            "No LLM selected. Select one in Domain Information → AI."
        )
    kind = normalize_llm_endpoint_kind(
        endpoint, str(info.get("llm_endpoint_kind") or "")
    )
    return host, token, endpoint, kind

@staticmethod
def require_serving_llm(domain, settings) -> tuple[str, str, str]:
    host, token, endpoint, _kind = DatabricksHelpers.require_domain_llm(
        domain, settings
    )
    return host, token, endpoint
```

Import `normalize_llm_endpoint_kind` from `shared.llm_target` and export
`require_domain_llm = DatabricksHelpers.require_domain_llm` from
`back.core.helpers.__init__`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q tests/units/core/test_domain_llm_guard.py
uv run --frozen ruff check src/back/core/helpers/DatabricksHelpers.py tests/units/core/test_domain_llm_guard.py
```

Expected: all tests and Ruff checks pass.

- [ ] **Step 5: Commit**

```bash
git add src/back/core/helpers/DatabricksHelpers.py \
  src/back/core/helpers/__init__.py \
  tests/units/core/test_domain_llm_guard.py
git commit -m "feat(core): add domain LLM availability guard"
```

---

### Task 2: Gate Ontology and Mapping LLM routes

**Files:**
- Modify: `src/api/routers/internal/ontology.py`
- Modify: `src/api/routers/internal/mapping.py`
- Modify: `tests/units/api/test_routes.py`
- Modify: `tests/units/core/test_sql_wizard.py`
- Create: `tests/units/api/test_no_llm_route_contract.py`

**Interfaces:**
- Consumes: `require_domain_llm(domain, settings) -> tuple[str, str, str, str]`
- Produces: guarded Ontology and Mapping routes that use the saved domain target.

- [ ] **Step 1: Write a failing route integration contract**

Create an AST/source-order contract covering these route functions:
`generate_business_rules_async`, `generate_ontology_async`,
`auto_assign_icons`, the two ontology assistant handlers,
`generate_sql_from_prompt`, `start_auto_assign`, and `single_auto_assign`.

```python
import inspect

from api.routers.internal import mapping, ontology


ROUTES = [
    ontology.generate_business_rules_async,
    ontology.generate_ontology_async,
    ontology.auto_assign_icons,
    ontology.ontology_assistant_chat,
    ontology.ontology_assistant_invoke,
    mapping.generate_sql_from_prompt,
    mapping.start_auto_assign,
    mapping.single_auto_assign,
]


def test_every_ontology_and_mapping_llm_route_uses_domain_guard():
    for route in ROUTES:
        source = inspect.getsource(route)
        assert "require_domain_llm(" in source, route.__name__
        if "create_task(" in source:
            assert source.index("require_domain_llm(") < source.index("create_task(")
```

- [ ] **Step 2: Run the contract and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/api/test_no_llm_route_contract.py
```

Expected: FAIL because the routes still use `require_serving_llm` or inline
endpoint checks.

- [ ] **Step 3: Migrate Ontology routes**

Replace every serving-specific or inline resolution with:

```python
host, token, llm_endpoint, _llm_endpoint_kind = require_domain_llm(
    domain, settings
)
```

The current Ontology agent interfaces accept only the endpoint name and infer
kind from its FQN, so `_llm_endpoint_kind` remains intentionally unused. Do not
edit prompts, agent loops, tool definitions, or agent signatures.

Update `tests/units/api/test_routes.py` patches to target
`require_domain_llm` and return four values:

```python
return_value=("https://h", "t", "main.ai.model", "ai_gateway")
```

- [ ] **Step 4: Make the saved domain target authoritative for Mapping SQL**

In `generate_sql_from_prompt`, call `require_domain_llm` after loading the
domain and before constructing `SQLWizardService`. Ignore request
`endpoint_name` and `endpoint_kind`, then pass the saved values:

```python
host, token, endpoint_name, endpoint_kind = require_domain_llm(domain, settings)
sql = service.generate_sql(
    endpoint_name=endpoint_name,
    endpoint_kind=endpoint_kind,
    prompt=prompt,
    schema_context=schema_context,
)
```

Add a regression test that sends `"endpoint_name": "request.override"` while
the domain guard returns `"main.ai.saved"` and asserts the SQL service receives
`"main.ai.saved"`.

- [ ] **Step 5: Migrate Mapping auto-assignment routes**

Replace both inline empty-endpoint checks with `require_domain_llm` before task
creation. Pass the saved endpoint through the existing `llm_endpoint` task
argument. The downstream mapping services do not accept endpoint kind, so they
continue using endpoint-name compatibility inference; do not change agent code.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/api/test_no_llm_route_contract.py \
  tests/units/api/test_routes.py \
  tests/units/core/test_sql_wizard.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/api/routers/internal/ontology.py \
  src/api/routers/internal/mapping.py \
  tests/units/api/test_no_llm_route_contract.py \
  tests/units/api/test_routes.py \
  tests/units/core/test_sql_wizard.py
git commit -m "feat(api): gate ontology and mapping AI routes"
```

---

### Task 3: Remove Graph LLM auto-discovery

**Files:**
- Modify: `src/api/routers/internal/dtwin.py`
- Replace: `tests/units/dtwin/test_llm_auto_discovery.py`
- Modify: `tests/units/api/test_dtwin_assistant_chat.py`

**Interfaces:**
- Consumes: `require_domain_llm(domain, settings) -> tuple[str, str, str, str]`
- Produces: strict guards for metrics interpretation, Graph Chat, and streaming Graph Chat.

- [ ] **Step 1: Replace preference tests with strict rejection tests**

Rename the test module to `tests/units/dtwin/test_no_llm_gating.py` and cover
the three route functions:

```python
import inspect

from api.routers.internal import dtwin


def test_graph_llm_routes_require_saved_domain_target():
    for route in [
        dtwin.interpret_graph_metrics,
        dtwin.dtwin_assistant_chat,
        dtwin.dtwin_assistant_chat_stream,
    ]:
        source = inspect.getsource(route)
        assert "require_domain_llm(" in source
        assert "_auto_discover_llm_endpoint(" not in source
```

Add one TestClient regression for non-stream Graph Chat with an empty domain:
assert HTTP 400, the common guidance message, and no patched `run_chat_agent`
call.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/dtwin/test_no_llm_gating.py \
  tests/units/api/test_dtwin_assistant_chat.py
```

Expected: FAIL because Graph routes still call auto-discovery.

- [ ] **Step 3: Guard all three Graph routes**

Use:

```python
host, token, llm_endpoint, _llm_endpoint_kind = require_domain_llm(
    domain, settings
)
```

Delete fallback branches from metrics interpretation, non-stream chat, and
stream chat. The current Graph agent interfaces infer kind from the FQN, so do
not change agent signatures. Preserve conversation history, action
confirmation, streaming, tracing, and response serialization.

- [ ] **Step 4: Remove dead auto-discovery code**

Delete `_auto_discover_llm_endpoint`, its unused imports, and
`tests/units/dtwin/test_llm_auto_discovery.py`. Keep the new strict gating test.

- [ ] **Step 5: Remove the compatibility guard**

After `rg -n "require_serving_llm" src tests` returns no application callers,
delete `require_serving_llm` from `DatabricksHelpers.py` and its re-export from
`back.core.helpers.__init__`.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/dtwin/test_no_llm_gating.py \
  tests/units/api/test_dtwin_assistant_chat.py \
  tests/units/core/test_domain_llm_guard.py
uv run --frozen ruff check \
  src/api/routers/internal/dtwin.py \
  src/back/core/helpers/DatabricksHelpers.py \
  tests/units/dtwin/test_no_llm_gating.py
```

Expected: all tests and Ruff checks pass.

- [ ] **Step 7: Commit**

```bash
git add src/api/routers/internal/dtwin.py \
  src/back/core/helpers/DatabricksHelpers.py \
  src/back/core/helpers/__init__.py \
  tests/units/api/test_dtwin_assistant_chat.py \
  tests/units/dtwin/test_no_llm_gating.py
git rm tests/units/dtwin/test_llm_auto_discovery.py
git commit -m "feat(dtwin): disable AI without a saved LLM"
```

---

### Task 4: Add the global fail-closed frontend controller

**Files:**
- Modify: `src/front/templates/base.html`
- Modify: `src/front/static/global/js/permissions.js`
- Modify: `src/front/static/global/js/navbar.js`
- Modify: `src/front/static/global/css/permissions.css`
- Create: `tests/units/front/test_no_llm_ui_gate.py`

**Interfaces:**
- Consumes: `/navbar/state` field `domain.info.llm_endpoint`
- Produces: `window.OB.updateLlmAvailability(configured: boolean)` and declarative `[data-requires-llm]` behavior.

- [ ] **Step 1: Write failing structural contracts**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_pages_start_with_fail_closed_llm_state():
    assert "llm-unconfigured" in _read("src/front/templates/base.html")


def test_global_gate_uses_navbar_state_and_attribute_markers():
    permissions = _read("src/front/static/global/js/permissions.js")
    navbar = _read("src/front/static/global/js/navbar.js")
    css = _read("src/front/static/global/css/permissions.css")
    assert "updateLlmAvailability" in permissions
    assert "data-requires-llm" in permissions
    assert "event.stopImmediatePropagation()" in permissions
    assert "info.llm_endpoint" in navbar
    assert "llm-unconfigured" in css
    assert "[data-requires-llm]" in css
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_no_llm_ui_gate.py
```

Expected: FAIL because the global gate does not exist.

- [ ] **Step 3: Render an initial fail-closed body**

Add `llm-unconfigured` to the existing `<body>` class attribute in
`base.html`. Do not gate the AI tab or picker.

- [ ] **Step 4: Implement the global controller**

In `permissions.js`, add one state writer:

```javascript
const llmAriaState = new WeakMap();

function syncLlmControl(control, configured) {
    if (!configured && !llmAriaState.has(control)) {
        llmAriaState.set(control, control.getAttribute('aria-disabled'));
        control.setAttribute('aria-disabled', 'true');
        return;
    }
    if (configured && llmAriaState.has(control)) {
        const previous = llmAriaState.get(control);
        if (previous === null) control.removeAttribute('aria-disabled');
        else control.setAttribute('aria-disabled', previous);
        llmAriaState.delete(control);
    }
}

function updateLlmAvailability(configured) {
    document.body.classList.toggle('llm-unconfigured', !configured);
    document.querySelectorAll('[data-requires-llm]').forEach((control) => {
        syncLlmControl(control, configured);
    });
}
```

Install one capture-phase `click` and `keydown` guard. When the closest
`[data-requires-llm]` is inside `body.llm-unconfigured`, prevent execution,
stop propagation, and call:

```javascript
showNotification(
    'Select an LLM in Domain Information → AI.',
    'warning'
);
```

For keydown, block Enter and Space only. Export the writer without replacing
the frozen permissions object:

```javascript
window.OB = window.OB || {};
window.OB.updateLlmAvailability = updateLlmAvailability;
```

Install one `MutationObserver` on `document.body` that calls
`syncLlmControl(control, !document.body.classList.contains('llm-unconfigured'))`
for a newly added node that matches `[data-requires-llm]` and for matching
descendants. This gives dynamically rendered controls the same ARIA state
without re-running page-specific initialization.

- [ ] **Step 5: Drive the state from navbar data**

In `navbar.js::applyDomainInfo`, call:

```javascript
window.OB?.updateLlmAvailability(Boolean(data.info?.llm_endpoint?.trim()));
```

On missing domain data or navbar-state failure, call
`updateLlmAvailability(false)` so the page remains fail-closed.

- [ ] **Step 6: Add declarative styling**

In `permissions.css` add:

```css
body.llm-unconfigured [data-requires-llm] {
    opacity: 0.5 !important;
    cursor: not-allowed !important;
}
```

Do not use `pointer-events: none`; the capture handler must receive clicks to
show guidance. Do not set native `disabled`; existing validation logic keeps
ownership of that state.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_no_llm_ui_gate.py
node --check src/front/static/global/js/permissions.js
node --check src/front/static/global/js/navbar.js
```

Expected: all checks pass.

- [ ] **Step 8: Commit**

```bash
git add src/front/templates/base.html \
  src/front/static/global/js/permissions.js \
  src/front/static/global/js/navbar.js \
  src/front/static/global/css/permissions.css \
  tests/units/front/test_no_llm_ui_gate.py
git commit -m "feat(front): add global no-LLM availability gate"
```

---

### Task 5: Mark every domain-scoped LLM control

**Files:**
- Modify: `src/front/templates/partials/ontology/_ontology_wizard.html`
- Modify: `src/front/templates/partials/ontology/_ontology_map.html`
- Modify: `src/front/templates/partials/ontology/_ontology_business_rules.html`
- Modify: `src/front/templates/partials/mapping/_mapping_autoassign.html`
- Modify: `src/front/templates/partials/mapping/_mapping_design.html`
- Modify: `src/front/templates/partials/mapping/_mapping_manual.html`
- Modify: `src/front/templates/partials/dtwin/_query_chat.html`
- Modify: `src/front/templates/partials/dtwin/_query_analytics.html`
- Modify: `src/front/static/mapping/js/mapping-shared.js`
- Modify: `tests/units/front/test_no_llm_ui_gate.py`
- Modify: `tests/units/front/test_llm_endpoint_picker.py`

**Interfaces:**
- Consumes: `[data-requires-llm]` global behavior from Task 4.
- Produces: complete marker coverage with an explicit re-enable-path exclusion.

- [ ] **Step 1: Write the failing marker inventory test**

```python
import re


def test_every_llm_trigger_has_declarative_marker():
    files = {
        "src/front/templates/partials/ontology/_ontology_wizard.html":
            ["wizardTopGenerateBtn"],
        "src/front/templates/partials/ontology/_ontology_map.html":
            [
                "mapAutoAssignIcons",
                "mapToggleAssistant",
                "assistantSendBtn",
                "assistant-suggestion",
            ],
        "src/front/templates/partials/ontology/_ontology_business_rules.html":
            ['data-br-action="auto-generate"'],
        "src/front/templates/partials/mapping/_mapping_autoassign.html":
            ["startAutoAssignBtn", "reassignAttrsBtn"],
        "src/front/templates/partials/mapping/_mapping_design.html":
            ["autoMapPanelBtn"],
        "src/front/templates/partials/mapping/_mapping_manual.html":
            ["manualAutoMapBtn"],
        "src/front/templates/partials/dtwin/_query_chat.html":
            ["chatSendBtn", "assistant-suggestion"],
        "src/front/templates/partials/dtwin/_query_analytics.html":
            ["analyticsInterpretBtn"],
    }
    for path, controls in files.items():
        html = _read(path)
        tags = re.findall(r"<(?:button|a)\b[^>]*>", html, flags=re.DOTALL)
        for control in controls:
            assert any(
                control in tag and "data-requires-llm" in tag
                for tag in tags
            ), f"{path}: {control}"
```

Add a separate assertion that the Domain AI tab, `#domainLlmBrowse`, and all
`llm_endpoint_picker_modal.html` controls do not contain the marker.

- [ ] **Step 2: Run the marker tests and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_no_llm_ui_gate.py \
  tests/units/front/test_llm_endpoint_picker.py
```

Expected: FAIL listing the first unmarked LLM trigger.

- [ ] **Step 3: Mark static controls**

Add the boolean attribute `data-requires-llm` to every control in the inventory.
Mark every Ontology Assistant and Graph Chat `.assistant-suggestion` button
because each starts a chat turn. Do not mark history controls, action
confirmation/cancel controls, or deterministic data-quality generation.

- [ ] **Step 4: Mark Mapping SQL controls created through `SQLWizardBase`**

In `SQLWizardBase.setupEventListeners`, annotate the configured generate
button when it exists:

```javascript
const generateButton = document.getElementById(this.ids.generateBtn);
if (generateButton) {
    generateButton.setAttribute('data-requires-llm', '');
}
```

Keep the existing prompt/table/endpoint validation and native `disabled`
management unchanged.

- [ ] **Step 5: Run frontend checks**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_no_llm_ui_gate.py \
  tests/units/front/test_llm_endpoint_picker.py \
  tests/units/front/test_no_backend_ui.py
node --check src/front/static/mapping/js/mapping-shared.js
```

Expected: all checks pass.

- [ ] **Step 6: Commit**

```bash
git add src/front/templates/partials/ontology/_ontology_wizard.html \
  src/front/templates/partials/ontology/_ontology_map.html \
  src/front/templates/partials/ontology/_ontology_business_rules.html \
  src/front/templates/partials/mapping/_mapping_autoassign.html \
  src/front/templates/partials/mapping/_mapping_design.html \
  src/front/templates/partials/mapping/_mapping_manual.html \
  src/front/templates/partials/dtwin/_query_chat.html \
  src/front/templates/partials/dtwin/_query_analytics.html \
  src/front/static/mapping/js/mapping-shared.js \
  tests/units/front/test_no_llm_ui_gate.py \
  tests/units/front/test_llm_endpoint_picker.py
git commit -m "feat(front): mark domain LLM actions"
```

---

### Task 6: Document and verify strict No LLM behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/user-guide.md`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-17.log`

**Interfaces:**
- Consumes: completed backend and frontend gating.
- Produces: documented user behavior and final verification evidence.

- [ ] **Step 1: Update documentation**

Document these exact behaviors:

- No LLM is a strict domain-level opt-out.
- LLM actions remain visible but unavailable.
- Guidance points to Domain Information → AI.
- Selecting and saving an LLM re-enables actions.
- Deterministic reasoning and validation remain available.

- [ ] **Step 2: Browser-test the complete flow**

At desktop and 375 px mobile widths:

1. Select No LLM and save Domain Information.
2. Verify Ontology Generate, Assistant, Auto Icons, Business Rules, Mapping
   Auto-Map/SQL, Graph Chat, and Analytics Interpret are visually unavailable.
3. Activate one unavailable control by mouse and keyboard; verify no network
   call and the notification guidance appears.
4. Verify the AI tab and Browse picker remain usable.
5. Select and save a model; verify marked controls become available without a
   page reload.
6. Confirm no relevant console errors and no failed requests.

- [ ] **Step 3: Run all focused tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/core/test_domain_llm_guard.py \
  tests/units/api/test_no_llm_route_contract.py \
  tests/units/api/test_routes.py \
  tests/units/api/test_dtwin_assistant_chat.py \
  tests/units/core/test_sql_wizard.py \
  tests/units/dtwin/test_no_llm_gating.py \
  tests/units/front/test_no_llm_ui_gate.py \
  tests/units/front/test_llm_endpoint_picker.py
```

Expected: all focused tests pass.

- [ ] **Step 4: Run syntax, lint, and diff checks**

Run:

```bash
uv run --frozen ruff check \
  src/back/core/helpers/DatabricksHelpers.py \
  src/api/routers/internal/ontology.py \
  src/api/routers/internal/mapping.py \
  src/api/routers/internal/dtwin.py \
  tests/units/core/test_domain_llm_guard.py \
  tests/units/api/test_no_llm_route_contract.py \
  tests/units/dtwin/test_no_llm_gating.py
node --check src/front/static/global/js/permissions.js
node --check src/front/static/global/js/navbar.js
node --check src/front/static/mapping/js/mapping-shared.js
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 5: Run the mandatory suite**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: zero failures. Record the exact summary in the changelog.

- [ ] **Step 6: Update the changelog**

Append an English section containing:

- context and strict opt-out behavior;
- numbered backend/frontend/documentation changes;
- complete modified-file list;
- focused, browser, lint, syntax, and mandatory-suite results.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/user-guide.md \
  changelogs/v0.9.0/benoitcayladbx_2026-09-17.log
git commit -m "docs: explain strict No LLM behavior"
```
