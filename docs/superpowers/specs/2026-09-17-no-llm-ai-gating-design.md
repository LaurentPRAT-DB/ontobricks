# No-LLM AI Gating Design

**Date:** 2026-09-17
**Status:** Approved
**Version:** 0.9.0

## Context

Domains can explicitly select **No LLM** in the shared AI Gateway / Model
Serving picker. Today most ontology and mapping routes reject an empty
`llm_endpoint`, but Graph Chat and graph-metrics interpretation silently
auto-discover another model. Frontend behavior is also inconsistent: Mapping
SQL generation disables itself locally, while other AI controls remain
interactive until the backend rejects the request.

An empty domain endpoint must be a deliberate opt-out. Every domain-scoped LLM
feature must remain visible but unavailable until the user selects and saves an
LLM in Domain Information.

## Decisions

1. `domain.info.llm_endpoint` is the sole authority for in-app,
   domain-scoped LLM availability.
2. Any empty endpoint disables LLM features. Legacy empty domains follow the
   same rule; there is no implicit compatibility auto-discovery.
3. Domain Information's AI tab and LLM picker remain available so users can
   re-enable AI.
4. Deterministic features remain available because they do not call an LLM.
5. Availability gating does not change prompts, tools, agent outputs, or model
   evaluation behavior.

## Scope

The central backend guard covers:

- `POST /ontology/wizard/generate-async`
- `POST /ontology/business-rules/generate-async`
- `POST /ontology/auto-assign-icons`
- `POST /ontology/assistant/chat`
- `POST /ontology/assistant/invoke`
- `POST /mapping/wizard/generate-sql`
- `POST /mapping/auto-assign/start`
- `POST /mapping/auto-assign/single`
- `POST /dtwin/metrics/interpret`
- `POST /dtwin/assistant/chat`
- `POST /dtwin/assistant/chat/stream`

The corresponding frontend controls include Ontology Wizard generation,
Ontology Assistant, Business Rules generation, automatic icon assignment,
Mapping SQL generation, batch and panel auto-mapping, Graph Chat, and Analytics
Interpret.

The following are explicitly out of scope:

- SHACL suggestions, ontology pitfalls, reasoning, and cohort operations,
  which are deterministic.
- Standalone MLflow model artifacts and dormant agents that are not invoked
  through the domain UI.
- Model discovery and the Domain Information AI tab, which are required to
  configure the domain.

## Backend Architecture

### Central guard

Replace the serving-specific contract with a domain-target guard:

```python
require_domain_llm(domain, settings) -> tuple[str, str, str, str]
```

The result is `(host, token, endpoint_name, endpoint_kind)`. The helper:

1. Reads and trims `domain.info.llm_endpoint`.
2. Raises one `ValidationError` when the endpoint is empty.
3. Resolves Databricks host and token through the existing credential helper.
4. Normalizes `llm_endpoint_kind` through the shared LLM target utility.

All scoped routes use this helper before creating a background task or invoking
an agent. This guarantees that a rejected request creates no task and makes no
Foundation Model API call.

The error message is consistent:

> No LLM selected. Select one in Domain Information → AI.

It remains a structured HTTP 400 validation response through the existing
global error handler.

### Endpoint authority

Mapping SQL generation currently accepts `endpoint_name` in its request body.
For domain-scoped execution, the saved domain target becomes authoritative.
The legacy request fields remain accepted for wire compatibility but are
ignored; they cannot bypass an empty domain target or override the saved target.

### Remove fail-open discovery

Graph Chat, streaming Graph Chat, and graph-metrics interpretation no longer
call `_auto_discover_llm_endpoint` when the domain endpoint is empty. The
now-unused helper and its preference tests are removed.

No global setting or fallback model is introduced.

## Frontend Architecture

### Declarative markers

Every control that starts a scoped LLM operation carries:

```html
data-requires-llm
```

This mirrors the existing `data-requires` permission contract and also covers
dynamically rendered controls because event handling and CSS use attribute
selectors.

### Fail-closed page state

`base.html` starts pages in an `llm-unconfigured` state. The consolidated
`/navbar/state` response already contains `domain.info.llm_endpoint`;
`navbar.js` is the only writer that toggles the body state after applying the
latest domain information.

The initial fail-closed state prevents a first-paint window in which AI actions
are briefly available before navbar state resolves.

Saving Domain Information refreshes the navbar state:

- a non-empty endpoint removes `llm-unconfigured`;
- No LLM restores `llm-unconfigured`.

No additional API endpoint or duplicated per-page fetch is added.

### Disabled interaction

The global permissions layer owns the presentation and behavior:

- muted opacity and `not-allowed` cursor;
- `aria-disabled="true"` while unavailable;
- capture-phase blocking for click and keyboard activation;
- guidance through the existing notification center using the exact full text:
  “No LLM selected. Select one in Domain Information → AI.”

The controller does not overwrite a control's native `disabled` state, so LLM
availability composes safely with validation, role, inactive-version, and edit
lock gates. Removing the LLM gate cannot accidentally enable a control that is
disabled for another reason.

The following never receive `data-requires-llm`:

- `#domainLlmBrowse`;
- the AI tab navigation;
- picker search, refresh, selection, clear, and cancel controls;
- Domain Information save controls.

## Data Flow

1. The page renders fail-closed.
2. Navbar state returns the saved domain information.
3. The global controller toggles `body.llm-unconfigured`.
4. Marked controls become available only when the endpoint is non-empty.
5. A scoped API route independently calls `require_domain_llm`.
6. The route either invokes the saved target or returns the common validation
   response without starting work.

Frontend gating improves guidance; backend gating remains the security and
correctness boundary.

## Error Handling

- Empty endpoint: HTTP 400 with the common safe validation message.
- Missing credentials with a selected endpoint: preserve the existing
  Databricks credentials validation error.
- Navbar-state failure: remain fail-closed.
- A stale page that was opened before No LLM was saved: backend rejection still
  prevents invocation.
- A selected endpoint that later becomes unavailable: preserve normal
  invocation error handling; that is distinct from an unconfigured domain.

## Testing

### Backend

- Unit-test `require_domain_llm` for Gateway, Serving, empty endpoint, endpoint
  kind normalization, and missing credentials.
- Parametrize scoped route tests to prove every route rejects an empty endpoint
  before task creation or downstream calls.
- Prove Mapping SQL cannot bypass the domain selection with a request endpoint.
- Replace auto-discovery preference tests with strict empty-endpoint rejection
  tests for Graph Chat, stream, and metrics interpretation.

### Frontend

- Structural tests require `data-requires-llm` on every LLM trigger and forbid
  it on the picker/re-enable path.
- Test fail-closed initial state and navbar-driven enable/disable transitions.
- Test capture-phase click and keyboard blocking without mutating unrelated
  native disabled states.
- Browser-test Domain, Ontology, Mapping, and Knowledge Graph at desktop and
  mobile widths, including No LLM → disabled guidance → select/save LLM →
  enabled behavior.

### Regression

- Run focused API, helper, frontend, and agent transport suites.
- Run `uv run --frozen pytest -q -m "not scenario"`.
- No eval dataset delta is required because prompts, tools, agent logic, and
  generated outputs are unchanged.

## Documentation

Update the README and user guide to state that No LLM is a strict domain-level
opt-out and identify Domain Information → AI as the re-enable path. Record the
change in the v0.9.0 changelog.
