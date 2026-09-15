# Lakehouse Build Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Lakehouse graph-build stages with the same live elapsed times, final durations, status, total time, hide control, and text export as Lakebase builds.

**Architecture:** Extract the existing Lakebase timed task log into a backend-neutral browser component and instantiate it for both build panels. Seed the Lakehouse task from a pure materialization-aware step factory, then make `DeltaTripleStoreBuildPipeline` advance or skip every declared stage in execution order.

**Tech Stack:** Python 3.11, FastAPI, `TaskManager`, vanilla JavaScript, Jinja2, Bootstrap 5, pytest.

## Global Constraints

- Preserve existing graph-build semantics and API payloads.
- Keep Lakebase build-log behavior unchanged.
- Mark Delta optimization skipped when materialization is `view`.
- Do not add dependencies.
- Run commands through `uv run --frozen`.
- Do not run scenario tests.

---

### Task 1: Materialization-aware Lakehouse task stages

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py`
- Modify: `src/api/routers/internal/dtwin.py`
- Test: `tests/units/graphdb/delta/test_delta_build_pipeline.py`

**Interfaces:**
- Produces: `lakehouse_build_steps(materialization: str) -> list[dict[str, str]]`.
- Consumed by: `start_databricks_triplestore_build`.

- [ ] **Step 1: Write failing tests for both materialization modes**

Add tests asserting that `lakehouse_build_steps("table")` and
`lakehouse_build_steps("view")` return seven ordered stages, with only the
materialization description differing:

```python
@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "materialize_description"),
    [
        ("table", "Materializing Delta table in Unity Catalog"),
        ("view", "Exposing pass-through data view"),
    ],
)
def test_lakehouse_build_steps_name_materialization_mode(
    mode: str, materialize_description: str
) -> None:
    steps = lakehouse_build_steps(mode)
    assert [step["name"] for step in steps] == [
        "prepare",
        "view",
        "materialize",
        "inferred",
        "graph_view",
        "optimize",
        "adjacency",
    ]
    assert steps[2]["description"] == materialize_description
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_build_pipeline.py
```

Expected: collection fails because `lakehouse_build_steps` is not defined.

- [ ] **Step 3: Implement the pure step factory**

Add this module-level function:

```python
def lakehouse_build_steps(materialization: str) -> list[dict[str, str]]:
    materialize_description = (
        "Exposing pass-through data view"
        if materialization == "view"
        else "Materializing Delta table in Unity Catalog"
    )
    return [
        {"name": "prepare", "description": "Preparing mappings and generating queries"},
        {"name": "view", "description": "Creating the R2RML SQL view"},
        {"name": "materialize", "description": materialize_description},
        {"name": "inferred", "description": "Preparing inferred-triples table"},
        {"name": "graph_view", "description": "Creating knowledge graph view"},
        {"name": "optimize", "description": "Optimizing Delta table"},
        {"name": "adjacency", "description": "Building adjacency indexes"},
    ]
```

Resolve materialization once in `start_databricks_triplestore_build` and pass
the factory result to `tm.create_task(..., steps=...)`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_build_pipeline.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the backend task contract**

```bash
git add src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py \
  src/api/routers/internal/dtwin.py \
  tests/units/graphdb/delta/test_delta_build_pipeline.py
git commit -m "feat(dtwin): define Lakehouse build stages"
```

---

### Task 2: Align pipeline transitions with every declared stage

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py`
- Test: `tests/units/graphdb/delta/test_delta_build_pipeline.py`

**Interfaces:**
- Consumes: the seven-stage order returned by `lakehouse_build_steps`.
- Produces: one task transition per operation, including `skip_step` for view-only optimization.

- [ ] **Step 1: Write failing transition tests**

Extend `_minimal_run_pipeline` with an ordered event log and assert table mode
advances into `inferred`, `graph_view`, `optimize`, and `adjacency`. Add a view
mode assertion that optimization calls `skip_step` and never calls
`materialize.optimize_table`:

```python
@pytest.mark.unit
def test_view_build_skips_optimize_stage() -> None:
    pipe = _minimal_run_pipeline(materialization="view")

    with (
        patch("back.core.graphdb.delta.DeltaTripleStoreBuildPipeline.DeltaFlatStore"),
        patch(
            "back.core.graphdb.delta.DeltaTripleStoreBuildPipeline."
            "materialize.optimize_table"
        ) as optimize,
    ):
        pipe.run()

    pipe.tm.skip_step.assert_called_once_with(
        pipe.task_id, "Optimization not needed for pass-through view"
    )
    optimize.assert_not_called()
```

The table-mode test must assert four `advance_step` calls after materialization
with the messages:

```python
[
    "Preparing inferred-triples table...",
    "Creating knowledge graph view...",
    "Optimizing Delta table...",
    "Building adjacency indexes...",
]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_build_pipeline.py
```

Expected: transition assertions fail because inferred, graph-view, and
optimization stages are currently folded into the final stage.

- [ ] **Step 3: Advance each stage in the orchestrator**

In `run`, advance immediately before inferred preparation and graph-view
creation. Advance into optimization after graph-view creation. In table mode,
optimize and then advance into adjacency. In view mode, call:

```python
self.tm.skip_step(
    self.task_id,
    "Optimization not needed for pass-through view",
)
```

Remove `advance_step` from `_rebuild_adjacency_index`, because the orchestrator
now activates that stage. Keep the existing phase timing and failure handling.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_build_pipeline.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit pipeline stage reporting**

```bash
git add src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py \
  tests/units/graphdb/delta/test_delta_build_pipeline.py
git commit -m "feat(dtwin): report Lakehouse build stage timings"
```

---

### Task 3: Extract the shared timed task-log renderer

**Files:**
- Create: `src/front/static/query/js/timed-task-log.js`
- Modify: `src/front/static/query/js/query-sync.js`
- Modify: `src/front/static/query/css/query-sync.css`
- Modify: `src/front/templates/dtwin.html`
- Modify: `src/front/templates/partials/dtwin/_query_sync.html`
- Create: `tests/units/front/test_timed_build_log_contract.py`

**Interfaces:**
- Produces: `window.TimedTaskLog.create(config)`.
- Instance methods: `show()`, `hide()`, `render(task)`, `export()`.
- Config IDs: `cardId`, `listId`, `totalId`, `badgeId`, `exportButtonId`.
- Config copy: `title`, `filenamePrefix`.

- [ ] **Step 1: Write failing frontend contract tests**

Assert that `dtwin.html` loads `timed-task-log.js` before both backend build
scripts, that `query-sync.js` calls `TimedTaskLog.create`, and that generic
styles are scoped by `.timed-task-log-card` instead of `#syncBuildLogCard`.

```python
def test_shared_renderer_loads_before_backend_build_scripts() -> None:
    html = DTWIN_HTML.read_text(encoding="utf-8")
    assert html.index("timed-task-log.js") < html.index("query-databricks-build.js")
    assert html.index("timed-task-log.js") < html.index("query-sync.js")


def test_lakebase_uses_shared_timed_renderer() -> None:
    js = QUERY_SYNC_JS.read_text(encoding="utf-8")
    assert "TimedTaskLog.create" in js
    assert 'cardId: "syncBuildLogCard"' in js
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_timed_build_log_contract.py
```

Expected: tests fail because the module and generic card class do not exist.

- [ ] **Step 3: Implement the shared renderer**

Move status mapping, row rendering, elapsed timer, total duration, cached task,
and text download behavior from `query-sync.js` into
`timed-task-log.js`. Expose only:

```javascript
window.TimedTaskLog = Object.freeze({
    create: function (config) {
        return {
            show: show,
            hide: hide,
            render: render,
            export: exportLog,
        };
    },
});
```

Use the existing global `escapeHtml`, `formatDuration`,
`computeTaskDuration`, and `showNotification` utilities. Scope the one-second
timer query to the configured list ID so multiple cards can coexist.

- [ ] **Step 4: Delegate the Lakebase wrappers**

Instantiate the component once in `query-sync.js`:

```javascript
const _syncTimedBuildLog = TimedTaskLog.create({
    cardId: "syncBuildLogCard",
    listId: "syncBuildLogList",
    totalId: "syncBuildLogTotal",
    badgeId: "syncBuildLogBadge",
    exportButtonId: "syncBuildLogExport",
    title: "OntoBricks — Knowledge Graph Build Log",
    filenamePrefix: "digital-twin-build",
});
```

Keep the public `showBuildLog`, `hideBuildLog`, `renderBuildLog`, and
`exportBuildLog` wrappers so existing callers and event handlers do not change.
Add `.timed-task-log-card` to the Lakebase card and update CSS selectors to use
that class.

- [ ] **Step 5: Run frontend and existing build-log tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_timed_build_log_contract.py \
  tests/units/front/test_button_design_contract.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit the shared renderer**

```bash
git add src/front/static/query/js/timed-task-log.js \
  src/front/static/query/js/query-sync.js \
  src/front/static/query/css/query-sync.css \
  src/front/templates/dtwin.html \
  src/front/templates/partials/dtwin/_query_sync.html \
  tests/units/front/test_timed_build_log_contract.py
git commit -m "refactor(front): share timed build log renderer"
```

---

### Task 4: Render timed progress in the Lakehouse Build panel

**Files:**
- Modify: `src/front/templates/partials/dtwin/_query_databricks_build.html`
- Modify: `src/front/static/query/js/query-databricks-build.js`
- Test: `tests/units/front/test_timed_build_log_contract.py`

**Interfaces:**
- Consumes: `window.TimedTaskLog.create(config)` from Task 3.
- Produces: Lakehouse elements `dbxBuildLogCard`, `dbxBuildLogList`,
  `dbxBuildLogTotal`, `dbxBuildLogBadge`, `dbxBuildLogExport`, and
  `dbxBuildLogHide`.

- [ ] **Step 1: Write failing Lakehouse integration contracts**

Assert all six IDs exist, the card has `.timed-task-log-card`, and
`query-databricks-build.js` renders on every task poll and restored active
task:

```python
def test_lakehouse_build_panel_has_timed_log_card() -> None:
    html = DBX_BUILD_HTML.read_text(encoding="utf-8")
    for element_id in (
        "dbxBuildLogCard",
        "dbxBuildLogList",
        "dbxBuildLogTotal",
        "dbxBuildLogBadge",
        "dbxBuildLogExport",
        "dbxBuildLogHide",
    ):
        assert f'id="{element_id}"' in html
    assert "timed-task-log-card" in html


def test_lakehouse_poll_renders_full_task_log() -> None:
    js = DBX_BUILD_JS.read_text(encoding="utf-8")
    assert 'cardId: "dbxBuildLogCard"' in js
    assert "_dbxTimedBuildLog.render(task)" in js
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_timed_build_log_contract.py
```

Expected: Lakehouse markup and renderer assertions fail.

- [ ] **Step 3: Add the Lakehouse log card**

Add markup matching the Lakebase card below `dbxBuildProgressArea`, using the
Lakehouse IDs above, the title `Building Lakehouse Graph`, and the same intro,
badge, total, Export, Hide, and row-list structure.

- [ ] **Step 4: Wire build start, polling, resume, and terminal rendering**

Create `_dbxTimedBuildLog` with title
`OntoBricks — Lakehouse Graph Build Log` and filename prefix
`lakehouse-graph-build`. Call `show()` before starting and restoring a running
build. Call `render(task)` on every poll before terminal handling. Bind Hide
and Export buttons during page initialization. Leave adjacency refresh on its
existing compact progress display.

- [ ] **Step 5: Run frontend contracts and Lakehouse UI regressions**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_timed_build_log_contract.py \
  tests/units/front/test_lakehouse_materialization_ui.py \
  tests/units/front/test_adjacency_refresh_ui.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit Lakehouse progress UI**

```bash
git add src/front/templates/partials/dtwin/_query_databricks_build.html \
  src/front/static/query/js/query-databricks-build.js \
  tests/units/front/test_timed_build_log_contract.py
git commit -m "feat(front): show timed Lakehouse build steps"
```

---

### Task 5: Documentation, changelog, and verification

**Files:**
- Modify: `docs/user-guide.md`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

**Interfaces:**
- Documents: the visible Lakehouse stage log and export behavior.

- [ ] **Step 1: Update user documentation**

In the Lakehouse Build guidance, state that builds display named stages with
live elapsed time, completed durations, total duration, and downloadable logs.

- [ ] **Step 2: Run lint diagnostics on changed files**

Use IDE diagnostics for all changed Python, JavaScript, HTML, and CSS files.
Expected: no new diagnostics.

- [ ] **Step 3: Run focused backend and frontend suites**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/graphdb/delta/test_delta_build_pipeline.py \
  tests/units/front/test_timed_build_log_contract.py \
  tests/units/front/test_lakehouse_materialization_ui.py \
  tests/units/front/test_adjacency_refresh_ui.py
```

Expected: all tests pass.

- [ ] **Step 4: Run the mandatory non-scenario suite**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: all tests pass.

- [ ] **Step 5: Append the v0.9.0 changelog section**

Record the context, numbered file-level changes, complete modified-file list,
and exact focused/full test results in English.

- [ ] **Step 6: Verify the final diff**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended feature files and the user's
pre-existing unrelated changes are present.

- [ ] **Step 7: Commit documentation and changelog**

```bash
git add docs/user-guide.md \
  changelogs/v0.9.0/benoitcayladbx_2026-09-15.log
git commit -m "docs(dtwin): document Lakehouse build progress"
```
