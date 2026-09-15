# Lakehouse Build Progress Design

## Context

The Lakehouse build page currently shows a percentage bar and one current-step
label. The Lakebase build page already presents a richer task log with named
steps, live elapsed time, final step durations, total duration, status, and log
export. Both builds use `TaskManager`, so their task payloads already share the
same step and timestamp model.

## Goal

Give Lakehouse graph builds the same progress visibility as Lakebase builds
without changing build semantics. Users must be able to see which stage is
running, how long it has been running, how long completed stages took, and the
total build duration.

## User Experience

The Lakehouse Build panel will keep its existing progress bar and add a build
log card matching the Lakebase interaction:

- one row per build stage;
- pending, running, completed, skipped, and failed states;
- a live elapsed timer for the running stage;
- a fixed duration for terminal stages;
- total elapsed or completed duration;
- task status badge;
- Hide and Export controls;
- automatic restoration while an active task is resumed from session storage.

The card appears when a build starts or an active build is restored. It remains
visible when the task completes or fails so the user can inspect and export it.

## Lakehouse Build Stages

The task will report these stages:

1. Preparing mappings and generating queries.
2. Creating the R2RML SQL view.
3. Materializing the Delta data table, or exposing the pass-through data view.
4. Preparing the inferred-triples table.
5. Creating the knowledge graph view.
6. Optimizing the Delta data table.
7. Building adjacency indexes.

Stage 6 is marked `skipped` at runtime in view-only materialization because no
Delta data table exists to optimize. The materialization stage description is
selected before task creation so it accurately names a table copy or a
pass-through view.

## Architecture

### Shared timed task renderer

Extract the backend-neutral build-log behavior from `query-sync.js` into a
small shared renderer loaded by the Digital Twin page. The renderer receives:

- the task payload;
- element IDs for the card, rows, status badge, total, and export control;
- a log title used by text export.

It owns status icons, elapsed-time formatting, the one-second running timer,
total-duration rendering, and plain-text export. Lakebase keeps its current
markup and behavior through this renderer; Lakehouse supplies its own element
IDs. Backend-specific polling remains in the existing scripts.

### Lakehouse task lifecycle

`start_databricks_triplestore_build` seeds all stages in execution order.
`DeltaTripleStoreBuildPipeline` advances the task immediately before each
stage. Progress messages remain available as detail beneath the running row.
View-only optimization is explicitly skipped through `TaskManager` rather than
silently folded into another stage.

No new endpoint or task payload field is required.

## Error and Cancellation Behavior

An exception leaves the active stage failed through the existing
`TaskManager.fail_task` path. The renderer displays the task error beneath that
stage and preserves earlier durations. Cancellation uses the existing terminal
status and displays the elapsed duration of the interrupted stage.

Empty builds still terminate successfully with the existing warning message.
Remaining stages are represented consistently by the task manager's terminal
state handling.

## Testing

- Backend unit tests verify the seeded stage order and materialization-specific
  descriptions.
- Pipeline unit tests verify each operation advances the matching stage and
  view mode skips optimization.
- Frontend contract tests verify Lakehouse includes the timed log card and uses
  the shared renderer.
- Existing Lakebase build-log tests guard against regressions during renderer
  extraction.
- Run the full non-scenario suite with
  `uv run --frozen pytest -q -m "not scenario"`.

## Scope

This change covers interactive Lakehouse graph builds on the Digital Twin Build
page. It does not alter graph data, build scheduling, adjacency-refresh task
presentation, or the external build API.
