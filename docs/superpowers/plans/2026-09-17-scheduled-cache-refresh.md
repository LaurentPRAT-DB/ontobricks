# Scheduled Graph Cache Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone Scheduler job that rebuilds a domain's Lakebase or Lakehouse graph cache exactly as the interactive Refresh cache action does.

**Architecture:** Register a fifth `TaskTypeSpec` backed by a focused `cache_refresh.py` executor. The executor resolves the selected domain and write-capable graph store through the existing headless `TaskContext`, validates adjacency support, and invokes `rebuild_adjacency`; the generic scheduler API and persistence remain unchanged. Add one radio and one JavaScript type descriptor to expose the type in the existing Scheduler modal.

**Tech Stack:** Python 3.10+, APScheduler, FastAPI service layer, pytest, Jinja, Bootstrap 5, vanilla JavaScript.

## Global Constraints

- Schedule key is exactly `cache_refresh`; label is exactly `Graph Cache Refresh`; task tag is exactly `scheduled_cache_refresh`.
- The type has no target and normalizes every config to `{}`.
- Only `lakebase` and `databricks` backends are accepted; Databricks graph resolution must use `for_write=True`.
- Cache refresh calls the backend's existing `store.rebuild_adjacency(graph_name)` and does not duplicate companion SQL.
- Neo4j, no graph backend, unknown backends, missing stores, and stores without adjacency support fail before rebuild.
- Existing scheduler semantics remain unchanged: minimum interval 2 minutes, `coalesce=True`, and `max_instances=1`.
- UI uses existing Bootstrap components and Bootstrap Icons; no new CSS or endpoints.
- Tests follow red-green: each new contract must be observed failing before production code is changed.
- Do not modify or discard unrelated working-tree changes, including the existing daily changelog content.

---

### Task 1: Register and execute scheduled graph cache refresh

**Files:**
- Create: `src/back/objects/registry/scheduler_tasks/cache_refresh.py`
- Modify: `src/back/objects/registry/scheduler_tasks/__init__.py`
- Modify: `tests/units/registry/test_scheduler_tasks.py`

**Interfaces:**
- Consumes: `TaskContext.domain`, `TaskContext.snapshot`, `TaskContext.graph_name`, `TaskContext.settings`, and `GraphDBFactory._resolve_graph_backend(domain)`.
- Produces: `cache_refresh.normalize_config(config: Dict[str, Any]) -> Dict[str, Any]`.
- Produces: `cache_refresh.run(ctx: TaskContext) -> RunOutcome`.
- Produces: `TASK_CACHE_REFRESH = "cache_refresh"` and a registered `TaskTypeSpec`.

- [ ] **Step 1: Write failing registry and executor tests**

Update the shipped-types assertion and add focused config/executor coverage:

```python
class TestRegistry:
    def test_the_five_shipped_types_are_registered(self):
        assert set(TASK_TYPES) == {
            "build",
            "cohort",
            "analytics",
            "reasoning",
            "cache_refresh",
        }


class TestCacheRefreshConfig:
    def test_it_takes_no_options(self):
        assert get_task_type("cache_refresh").normalize_config({"unused": True}) == {}


class TestCacheRefreshExecutor:
    def test_lakebase_rebuilds_the_selected_graph(self):
        from back.objects.registry.scheduler_tasks import cache_refresh

        store = MagicMock(supports_adjacency=True)
        ctx = _ctx(
            task_type="cache_refresh",
            domain=object(),
            snapshot=object(),
            graph_name="Acme_V1",
            settings=SimpleNamespace(),
        )
        with patch.object(
            cache_refresh.GraphDBFactory,
            "_resolve_graph_backend",
            return_value="lakebase",
        ), patch.object(cache_refresh, "get_graphdb", return_value=store) as get_store:
            outcome = cache_refresh.run(ctx)

        get_store.assert_called_once_with(ctx.snapshot, ctx.settings, for_write=False)
        store.rebuild_adjacency.assert_called_once_with("Acme_V1")
        assert outcome.status == "success"
        assert outcome.count == 0

    def test_databricks_uses_a_write_capable_store(self):
        from back.objects.registry.scheduler_tasks import cache_refresh

        store = MagicMock(supports_adjacency=True)
        ctx = _ctx(
            task_type="cache_refresh",
            domain=object(),
            snapshot=object(),
            graph_name="Acme_V1",
            settings=SimpleNamespace(),
        )
        with patch.object(
            cache_refresh.GraphDBFactory,
            "_resolve_graph_backend",
            return_value="databricks",
        ), patch.object(cache_refresh, "get_graphdb", return_value=store) as get_store:
            cache_refresh.run(ctx)

        get_store.assert_called_once_with(ctx.snapshot, ctx.settings, for_write=True)

    @pytest.mark.parametrize("backend", ["neo4j", "none", "unexpected"])
    def test_unsupported_backends_fail_before_rebuild(self, backend):
        from back.objects.registry.scheduler_tasks import cache_refresh

        ctx = _ctx(task_type="cache_refresh", domain=object())
        with patch.object(
            cache_refresh.GraphDBFactory,
            "_resolve_graph_backend",
            return_value=backend,
        ), patch.object(cache_refresh, "get_graphdb") as get_store:
            with pytest.raises(ValidationError):
                cache_refresh.run(ctx)

        get_store.assert_not_called()
```

Add the missing-store and unsupported-store contracts:

```python
def test_a_missing_store_fails(self):
    from back.objects.registry.scheduler_tasks import cache_refresh

    ctx = _ctx(
        task_type="cache_refresh",
        domain=object(),
        snapshot=object(),
        settings=SimpleNamespace(),
    )
    with patch.object(
        cache_refresh.GraphDBFactory,
        "_resolve_graph_backend",
        return_value="lakebase",
    ), patch.object(cache_refresh, "get_graphdb", return_value=None):
        with pytest.raises(InfrastructureError):
            cache_refresh.run(ctx)

def test_a_store_without_adjacency_support_never_rebuilds(self):
    from back.objects.registry.scheduler_tasks import cache_refresh

    store = MagicMock(supports_adjacency=False)
    ctx = _ctx(
        task_type="cache_refresh",
        domain=object(),
        snapshot=object(),
        settings=SimpleNamespace(),
    )
    with patch.object(
        cache_refresh.GraphDBFactory,
        "_resolve_graph_backend",
        return_value="lakebase",
    ), patch.object(cache_refresh, "get_graphdb", return_value=store):
        with pytest.raises(ValidationError):
            cache_refresh.run(ctx)

    store.rebuild_adjacency.assert_not_called()
```

Import `InfrastructureError` beside `ValidationError` in the test module.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/registry/test_scheduler_tasks.py
```

Expected: collection/import or assertion failures because `cache_refresh`,
`TASK_CACHE_REFRESH`, and the fifth registry entry do not exist.

- [ ] **Step 3: Implement the minimal executor**

Create `cache_refresh.py` with direct graph-store execution so the scheduler
harness remains the sole TaskManager lifecycle owner:

```python
"""Scheduled graph cache refresh."""

from __future__ import annotations

from typing import Any, Dict

from back.core.errors import InfrastructureError, ValidationError
from back.core.graphdb import get_graphdb
from back.core.graphdb.GraphDBFactory import GraphDBFactory

from .context import RunOutcome, TaskContext

_SUPPORTED_BACKENDS = {"lakebase", "databricks"}


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Cache refresh takes no type-specific options."""
    del config
    return {}


def run(ctx: TaskContext) -> RunOutcome:
    """Rebuild graph companion indexes for the scheduled domain."""
    ctx.progress(5, "Loading domain from registry...")
    backend = GraphDBFactory._resolve_graph_backend(ctx.domain)
    if backend not in _SUPPORTED_BACKENDS:
        raise ValidationError(
            "Cache refresh is only available for lakebase and databricks "
            "graph backends."
        )

    ctx.progress(20, "Opening graph backend")
    store = get_graphdb(
        ctx.snapshot,
        ctx.settings,
        for_write=backend == "databricks",
    )
    if store is None:
        raise InfrastructureError("Graph backend is not configured")
    if not getattr(store, "supports_adjacency", False):
        raise ValidationError(
            f"{backend} backend does not support cache refresh"
        )

    graph_name = ctx.graph_name.strip()
    if not graph_name:
        raise ValidationError("Graph name is not configured")

    ctx.progress(70, f"Rebuilding graph indexes for {graph_name}")
    store.rebuild_adjacency(graph_name)
    return RunOutcome(
        status="success",
        message="Graph cache refresh completed",
        count=0,
        task_result={"mode": "adjacency_only", "backend": backend},
    )
```

Register it in `scheduler_tasks/__init__.py`:

```python
from . import analytics, build, cache_refresh, cohort, reasoning

TASK_CACHE_REFRESH = "cache_refresh"

TASK_TYPES: Dict[str, TaskTypeSpec] = {
    # existing entries...
    TASK_CACHE_REFRESH: TaskTypeSpec(
        key=TASK_CACHE_REFRESH,
        label="Graph Cache Refresh",
        task_tag="scheduled_cache_refresh",
        steps=[
            {"name": "open", "description": "Opening graph backend"},
            {"name": "adjacency", "description": "Rebuilding graph indexes"},
        ],
        normalize_config=cache_refresh.normalize_config,
        run=cache_refresh.run,
    ),
}
```

Export `TASK_CACHE_REFRESH` through `__all__`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q tests/units/registry/test_scheduler_tasks.py
```

Expected: all tests in the file pass.

### Task 2: Expose Graph Cache Refresh in Scheduler settings

**Files:**
- Modify: `src/front/templates/partials/settings/_settings_schedule.html`
- Modify: `src/front/static/config/js/schedule.js`
- Modify: `tests/units/front/test_settings_schedule_page.py`
- Modify: `tests/units/settings/test_schedule_endpoints.py`

**Interfaces:**
- Consumes: backend catalogue key `cache_refresh`.
- Produces: radio `value="cache_refresh"` and `TYPES.cache_refresh`.
- The existing generic `/settings/schedules` request body carries the type; no route changes.

- [ ] **Step 1: Write failing UI and API-catalogue tests**

Extend scheduler UI expectations:

```python
@pytest.mark.parametrize(
    "task_type", ["build", "cohort", "analytics", "reasoning", "cache_refresh"]
)
def test_every_backend_type_has_a_radio(self, template, task_type):
    assert f'name="scheduleType" id="scheduleType' in template
    assert f'value="{task_type}"' in template
```

Extend the listing catalogue:

```python
assert {t["key"] for t in out["task_types"]} == {
    "build",
    "cohort",
    "analytics",
    "reasoning",
    "cache_refresh",
}
```

The existing descriptor test automatically requires
`cache_refresh: {` once Task 1 registers the backend type.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_settings_schedule_page.py \
  tests/units/settings/test_schedule_endpoints.py
```

Expected: failures report the missing `cache_refresh` radio, descriptor, and
catalogue expectation.

- [ ] **Step 3: Add the radio and JavaScript descriptor**

In `_settings_schedule.html`, update the introductory copy to mention Graph
Cache Refresh and add:

```html
<input type="radio" class="btn-check" name="scheduleType"
       id="scheduleTypeCacheRefresh" value="cache_refresh" autocomplete="off">
<label class="btn btn-outline-primary btn-sm" for="scheduleTypeCacheRefresh">
    <i class="bi bi-arrow-repeat me-1"></i> Cache
</label>
```

In `schedule.js`, add:

```javascript
cache_refresh: {
    label: 'graph cache refresh',
    badge: () => badge('secondary', 'arrow-repeat', 'Cache'),
    details: (s) => versionBadge(s.version),
    historyColumns: [],
    readConfig: () => ({}),
    applyConfig: () => {},
},
```

Do not add a type-specific field group or CSS.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_settings_schedule_page.py \
  tests/units/settings/test_schedule_endpoints.py
```

Expected: all tests in both files pass.

### Task 3: Document and verify the completed feature

**Files:**
- Modify: `docs/optimizations.md`
- Modify: `README.md` only if its scheduler section enumerates task types
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-17.log`

**Interfaces:**
- Documents the user-visible Scheduler type and its backend limits.
- Records every modified file and the final mandatory test result.

- [ ] **Step 1: Update operational documentation**

In the scheduler/cache-refresh section of `docs/optimizations.md`, state:

```markdown
Graph Cache Refresh can also run as a standalone Scheduler task for a selected
domain and version. It invokes the same full companion-index rebuild as the
interactive **Refresh cache** action. The task is available for Lakehouse and
Lakebase graphs; Neo4j does not use these companions.
```

If README currently enumerates scheduler job types, add Graph Cache Refresh
to that list. Otherwise leave README unchanged.

- [ ] **Step 2: Run lints for edited files**

Use IDE diagnostics for:

```text
src/back/objects/registry/scheduler_tasks/cache_refresh.py
src/back/objects/registry/scheduler_tasks/__init__.py
tests/units/registry/test_scheduler_tasks.py
tests/units/front/test_settings_schedule_page.py
tests/units/settings/test_schedule_endpoints.py
src/front/static/config/js/schedule.js
src/front/templates/partials/settings/_settings_schedule.html
```

Expected: no new diagnostics.

- [ ] **Step 3: Run the mandatory non-scenario suite**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: exit code 0 with no failures.

- [ ] **Step 4: Append the v0.9.0 changelog section**

Append an English section to the existing file without replacing unrelated
content:

```text
## Add scheduled graph cache refresh

Context: Operators can rebuild Lakehouse or Lakebase graph companion indexes
on a recurring schedule without running a full Knowledge Graph Build.

Changes:

1. src/back/objects/registry/scheduler_tasks/cache_refresh.py
   Add the headless scheduled cache-refresh executor.
2. src/back/objects/registry/scheduler_tasks/__init__.py
   Register Graph Cache Refresh as a scheduler task type.
3. src/front/templates/partials/settings/_settings_schedule.html
   Add the Cache type to the Scheduler modal.
4. src/front/static/config/js/schedule.js
   Add rendering and configuration metadata for cache-refresh schedules.
5. tests/units/registry/test_scheduler_tasks.py
   Cover registration, validation, backend selection, and rebuild execution.
6. tests/units/front/test_settings_schedule_page.py
   Cover Scheduler radio and descriptor wiring.
7. tests/units/settings/test_schedule_endpoints.py
   Cover the expanded task-type catalogue.
8. docs/optimizations.md
   Document scheduled companion-index refresh behavior.

Modified files:
- src/back/objects/registry/scheduler_tasks/cache_refresh.py
- src/back/objects/registry/scheduler_tasks/__init__.py
- src/front/templates/partials/settings/_settings_schedule.html
- src/front/static/config/js/schedule.js
- tests/units/registry/test_scheduler_tasks.py
- tests/units/front/test_settings_schedule_page.py
- tests/units/settings/test_schedule_endpoints.py
- docs/optimizations.md
- changelogs/v0.9.0/benoitcayladbx_2026-09-17.log

```

Include README in the numbered and modified-file lists only if Step 1 changed
it. Add a final `Tests:` line containing the command and the exact summary
emitted in Step 3 before saving.

- [ ] **Step 5: Review the final diff and commit**

Run:

```bash
git diff --check
git status --short
git diff -- \
  src/back/objects/registry/scheduler_tasks/cache_refresh.py \
  src/back/objects/registry/scheduler_tasks/__init__.py \
  src/front/templates/partials/settings/_settings_schedule.html \
  src/front/static/config/js/schedule.js \
  tests/units/registry/test_scheduler_tasks.py \
  tests/units/front/test_settings_schedule_page.py \
  tests/units/settings/test_schedule_endpoints.py \
  docs/optimizations.md
```

Expected: no whitespace errors; only intended feature changes are staged.
Do not stage unrelated pre-existing changes. Then commit:

```bash
git add \
  src/back/objects/registry/scheduler_tasks/cache_refresh.py \
  src/back/objects/registry/scheduler_tasks/__init__.py \
  src/front/templates/partials/settings/_settings_schedule.html \
  src/front/static/config/js/schedule.js \
  tests/units/registry/test_scheduler_tasks.py \
  tests/units/front/test_settings_schedule_page.py \
  tests/units/settings/test_schedule_endpoints.py \
  docs/optimizations.md \
  changelogs/v0.9.0/benoitcayladbx_2026-09-17.log
git commit -m "feat(scheduler): add graph cache refresh"
```
