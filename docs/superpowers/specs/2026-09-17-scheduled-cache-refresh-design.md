# Scheduled Graph Cache Refresh — Design

**Date:** 2026-09-17  
**Status:** Approved direction — awaiting implementation plan  
**Scope:** Scheduler task type only (UI + registry + executor)

## Context

The Scheduler already runs four job kinds (`build`, `cohort`, `analytics`,
`reasoning`) through a type registry: a `TaskTypeSpec` plus a row in the
settings modal. **Refresh cache** in the Knowledge Graph UI is a separate
path: `POST /dtwin/adjacency/refresh` starts an `adjacency_refresh` task
that calls `store.rebuild_adjacency(graph_name)`.

Operators need the same companion-index rebuild on a timer (and Run now),
without a full Knowledge Graph Build. This spec adds a fifth schedule type
that performs that operation.

## Decision

Add a standalone scheduler type:

| Field | Value |
|-------|--------|
| Key | `cache_refresh` |
| Label | Graph Cache Refresh |
| Task tag | `scheduled_cache_refresh` |
| Target | Domain only (`needs_target=False`) |
| Config | Empty dict — no extra options |

Users pick domain, version (`latest` or a stored version), interval
(minimum 2 minutes, unchanged), and enabled. No post-build hook; a Build
schedule still only rebuilds the graph. Cache refresh is a separate job
that can run on its own clock.

## Architecture

Follow the existing plugin seam. Do **not** change `BuildScheduler`:

1. New module `src/back/objects/registry/scheduler_tasks/cache_refresh.py`
   with `normalize_config` and `run`.
2. Register a `TaskTypeSpec` in `scheduler_tasks/__init__.py`.
3. Settings modal radio + `TYPES.cache_refresh` descriptor in `schedule.js`.
4. Listing catalogue (`task_type_catalog`) picks the type up automatically.

### Executor

`run(ctx)` owns the TaskManager lifecycle (same as `build` / `cohort`),
rather than calling `DigitalTwin.run_adjacency_refresh_task`. That wrapper
starts/completes the same task the harness already created, which would
double-complete unless we set `delegates_task_lifecycle=True`. Direct
rebuild keeps one owner of the scheduled task.

Steps:

1. Load the domain via `TaskContext` (headless registry load).
2. Resolve backend with `GraphDBFactory._resolve_graph_backend(domain)`.
3. Reject `neo4j`, `none`, and unknown backends with `ValidationError`
   (same message class as the UI route: only Lakebase and Databricks /
   Lakehouse).
4. Open the graph store (`ctx.graph_store`, `for_write` for Databricks).
5. Fail if the store is missing or `supports_adjacency` is false.
6. Resolve graph name (`ctx.graph_name` / `effective_graph_name`).
7. Call `store.rebuild_adjacency(graph_name)`.
8. Return `RunOutcome(status="success", message=..., count=0)`.

`count` stays 0: rebuild does not report a meaningful row counter today
(the UI task result is `{mode: adjacency_only, backend}`). History shows
status, duration, and message. No extra history columns.

Lakehouse vs Lakebase behavior is entirely inside `rebuild_adjacency`
(including any later parallel Lakehouse companions). The scheduler does
not reimplement SQL.

### Backend selection

Use the same write-capable Databricks store the UI worker uses
(`get_graphdb(..., for_write=backend == "databricks")`). `TaskContext.graph_store`
currently calls `get_graphdb` without `for_write`. The executor must not
silently use a read-only Delta path on Lakehouse. Either:

- pass `for_write=True` when backend is `databricks` from the executor, or
- add an optional `for_write` on `TaskContext` used only by this type.

Prefer an explicit `get_graphdb` call in `cache_refresh.run` so other
scheduled types stay unchanged.

### Concurrency

Existing APScheduler defaults apply: `coalesce=True`, `max_instances=1`
per job id (`sched_cache_refresh_<domain>__`). A long rebuild overlapping
the next interval is coalesced; Run now uses a distinct one-shot job id
(existing `run_schedule_now` behavior).

A scheduled refresh and a UI Refresh cache can still run concurrently
against the same graph. That is already true for two UI clicks; no new
global mutex.

## UI

Scheduler tab (`_settings_schedule.html` + `schedule.js`):

- Fifth type radio: Cache (icon `arrow-repeat`, value `cache_refresh`).
- Descriptor: badge, version badge in details, empty `readConfig` /
  `applyConfig`, no extra history columns, no type-specific field group.
- Intro copy lists Graph Cache Refresh with the other job kinds.
- No new settings endpoints; generic `/settings/schedules` already
  forwards `task_type`.

## Errors

| Condition | Result |
|-----------|--------|
| Domain / version missing | Existing headless load `NotFoundError` |
| Neo4j / none / unknown backend | `ValidationError` before `rebuild_adjacency` |
| Store missing / no adjacency | `ValidationError` or `InfrastructureError` |
| Rebuild exception | Harness marks the schedule run `error` |

Start of a scheduled run still succeeds at save/queue time; runtime
failures land on `last_status` / history like other types.

## Testing

- Registry: `TASK_TYPES` includes `cache_refresh`; catalogue JSON includes
  it; only cohort needs a target.
- Config: `normalize_config` always returns `{}`.
- Executor: Lakebase/Databricks path calls `rebuild_adjacency` with the
  resolved graph name; Neo4j never calls it.
- Databricks path uses `for_write=True`.
- Settings page tests: radio + `TYPES.cache_refresh` descriptor.
- Listing test: catalogue keys include `cache_refresh`.

TDD: failing tests first, then registration, executor, UI wiring.

## Scope

In: fifth scheduler type, settings radio/descriptor, headless
`rebuild_adjacency` for Lakebase and Lakehouse.

Out: MCP tool, external REST (already specified separately), attaching
refresh to Build schedules, process-memory cache flush, Neo4j adjacency,
changing `rebuild_adjacency` SQL.
