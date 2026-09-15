# Lakehouse Build Initial Loading State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show only a spinner while the Lakehouse Build page loads its initial
triple-store information, then reveal the complete panel atomically or show a
retryable error.

**Architecture:** Server-rendered markup defaults to Loading, with separate
Error and Content containers hidden by Bootstrap. A small JavaScript state
transition function controls those containers; `loadDatabricksBuildInfo()`
uses it only until the first successful response, preserving visible content
during later refreshes.

**Tech Stack:** Jinja HTML, vanilla JavaScript, Bootstrap utilities, pytest
source/markup contracts, browser interception.

## Global Constraints

- Apply only to the Lakehouse Build panel.
- Do not change backend APIs, Lakebase behavior, build-task polling, or
  adjacency-task polling.
- Initial HTTP, JSON, and API-success failures keep controls hidden and expose
  only an error with Retry.
- Use existing Bootstrap and `ob-loading-spinner` classes; add no CSS.
- Keep comments, documentation, tests, and changelog entries in English.

---

### Task 1: Gate Lakehouse Build content behind initial information loading

**Files:**
- Create: `tests/units/front/test_lakehouse_build_loading_state.py`
- Modify: `src/front/templates/partials/dtwin/_query_databricks_build.html`
- Modify: `src/front/static/query/js/query-databricks-build.js`
- Modify: `docs/user-guide.md`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

**Interfaces:**
- Produces: `_setDbxBuildInitialState(state)` where `state` is one of
  `'loading'`, `'ready'`, or `'error'`.
- Preserves: `loadDatabricksBuildInfo()` as the loader used by initial load,
  manual Refresh, post-build refresh, and Retry.

- [ ] **Step 1: Write failing markup and behavior contracts**

Create `test_lakehouse_build_loading_state.py`:

```python
"""Contracts for the Lakehouse Build initial loading state."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_HTML = REPO_ROOT / "src/front/templates/partials/dtwin/_query_databricks_build.html"
BUILD_JS = REPO_ROOT / "src/front/static/query/js/query-databricks-build.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_server_markup_starts_with_spinner_only() -> None:
    html = _read(BUILD_HTML)
    loading = html[html.index('id="dbxBuildLoadingState"') - 80 :]
    loading = loading[: loading.index("</div>") + len("</div>")]
    assert 'role="status"' in loading
    assert 'aria-live="polite"' in loading
    assert "ob-loading-spinner" in loading
    assert "Loading triple store information..." in loading
    assert "d-none" not in loading
    content_line = next(
        line for line in html.splitlines() if 'id="dbxBuildContent"' in line
    )
    assert "d-none" in content_line


def test_server_markup_has_hidden_retryable_error() -> None:
    html = _read(BUILD_HTML)
    error_line = next(
        line for line in html.splitlines() if 'id="dbxBuildLoadError"' in line
    )
    assert "d-none" in error_line
    assert 'role="alert"' in error_line
    assert 'id="dbxBuildLoadRetry"' in html


def test_state_transition_controls_all_three_containers() -> None:
    js = _read(BUILD_JS)
    body = _function_body(
        js,
        "function _setDbxBuildInitialState(",
        "\nasync function loadDatabricksBuildInfo",
    )
    for element_id in (
        "dbxBuildLoadingState",
        "dbxBuildLoadError",
        "dbxBuildContent",
    ):
        assert element_id in body
    assert "state !== 'loading'" in body
    assert "state !== 'error'" in body
    assert "state !== 'ready'" in body


def test_initial_loader_reveals_content_only_after_success() -> None:
    js = _read(BUILD_JS)
    body = _function_body(
        js,
        "async function loadDatabricksBuildInfo(",
        "\nfunction _apiErrorMessage",
    )
    assert "let dbxBuildInfoLoaded = false;" in js
    assert "if (!resp.ok || !data.success)" in body
    assert "throw new Error(" in body
    assert "dbxBuildInfoLoaded = true;" in body
    render = body.index("_applyDbxStorageKind(isViewMode);")
    ready = body.index("_setDbxBuildInitialState('ready')")
    assert render < ready


def test_initial_error_stays_hidden_until_retry_succeeds() -> None:
    js = _read(BUILD_JS)
    body = _function_body(
        js,
        "async function loadDatabricksBuildInfo(",
        "\nfunction _apiErrorMessage",
    )
    assert "const isInitialLoad = !dbxBuildInfoLoaded;" in body
    assert "if (isInitialLoad)" in body
    assert "_setDbxBuildInitialState('loading')" in body
    assert "_setDbxBuildInitialState('error')" in body
    init = js[js.index("document.addEventListener('DOMContentLoaded'") :]
    assert "dbxBuildLoadRetry" in init
    assert "addEventListener('click', loadDatabricksBuildInfo)" in init
```

- [ ] **Step 2: Run the new contract and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_lakehouse_build_loading_state.py
```

Expected: five failures because the new state containers, state function,
loaded flag, strict response handling, and Retry binding do not exist.

- [ ] **Step 3: Make server-rendered Loading the only visible state**

In `_query_databricks_build.html`, replace `dbxBuildLoadingOverlay` with:

```html
<div id="dbxBuildLoadingState" role="status" aria-live="polite">
    <div class="ob-loading-spinner">
        <span class="ob-spinner-label">Loading triple store information...</span>
    </div>
</div>

<div id="dbxBuildLoadError" class="alert alert-danger d-none" role="alert">
    <div class="d-flex align-items-center justify-content-between gap-3">
        <span><i class="bi bi-exclamation-triangle me-1"></i>Could not load triple store information.</span>
        <button type="button" class="btn btn-sm btn-outline-danger" id="dbxBuildLoadRetry">
            <i class="bi bi-arrow-clockwise me-1"></i>Retry
        </button>
    </div>
</div>

<div id="dbxBuildContent" class="d-none">
```

Wrap all existing section-header, progress, log, result, storage, readiness,
and status markup in `dbxBuildContent`, closing it immediately before
`databricksBuildRoot`.

- [ ] **Step 4: Implement explicit initial state transitions**

Add the loaded flag near the existing build flags:

```javascript
let dbxBuildInfoLoaded = false;
```

Before `loadDatabricksBuildInfo()`, add:

```javascript
function _setDbxBuildInitialState(state) {
    const loading = document.getElementById('dbxBuildLoadingState');
    const error = document.getElementById('dbxBuildLoadError');
    const content = document.getElementById('dbxBuildContent');
    loading?.classList.toggle('d-none', state !== 'loading');
    error?.classList.toggle('d-none', state !== 'error');
    content?.classList.toggle('d-none', state !== 'ready');
}
```

Change the loader to capture `const isInitialLoad = !dbxBuildInfoLoaded;`,
enter Loading only when `isInitialLoad`, and reject HTTP/API failures:

```javascript
if (!resp.ok || !data.success) {
    throw new Error(_apiErrorMessage(data, 'Could not load triple store information'));
}
```

After all successful render/button/status updates:

```javascript
if (isInitialLoad) {
    dbxBuildInfoLoaded = true;
    _setDbxBuildInitialState('ready');
}
```

In `catch`, enter Error only for the initial request:

```javascript
if (isInitialLoad) {
    _setDbxBuildInitialState('error');
}
```

Remove the old overlay `finally` block. Bind Retry beside Refresh:

```javascript
document.getElementById('dbxBuildLoadRetry')
    ?.addEventListener('click', loadDatabricksBuildInfo);
```

- [ ] **Step 5: Run focused frontend regressions**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_lakehouse_build_loading_state.py \
  tests/units/front/test_lakehouse_materialization_ui.py \
  tests/units/front/test_timed_build_log_contract.py \
  tests/units/front/test_adjacency_refresh_ui.py
```

Expected: all tests pass.

- [ ] **Step 6: Browser-test all initial states**

Intercept `/dtwin/databricks-build/info` in the running local app and verify:

- a delayed response displays only Loading and exposes no focusable Build,
  Refresh, adjacency, purge, log, storage, readiness, or status content;
- a failed initial response displays Error and Retry while Content stays hidden;
- Retry immediately restores Loading;
- a successful retry renders all information before revealing Content;
- after first success, manual Refresh leaves Content visible while in flight;
- desktop `1440x900` and mobile `375x812` layouts have no overflow;
- no feature-related console or network errors remain after success.

- [ ] **Step 7: Document, lint, and run the full suite**

Update `docs/user-guide.md` to describe spinner-only initial loading and
retryable failure. Append the required English v0.9.0 changelog section with
the exact focused, browser, lint, and full-suite results. Run:

```bash
uv run --frozen pytest -q -m "not scenario"
git diff --check
```

Expected: all non-scenario tests pass and no whitespace errors are reported.

- [ ] **Step 8: Commit the isolated change**

Stage only these files, preserving unrelated working-tree changes:

```bash
git add src/front/templates/partials/dtwin/_query_databricks_build.html \
  src/front/static/query/js/query-databricks-build.js \
  tests/units/front/test_lakehouse_build_loading_state.py \
  docs/user-guide.md \
  docs/superpowers/plans/2026-09-15-lakehouse-build-loading-state.md
git commit -m "fix(front): gate Lakehouse Build content while loading"
```

Stage only this task's changelog section separately if another uncommitted
section remains in the daily log.
