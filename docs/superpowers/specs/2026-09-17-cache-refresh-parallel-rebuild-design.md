# Cache Refresh Parallel Rebuild — Design

**Date:** 2026-09-17  
**Status:** Approved direction (option B) — awaiting implementation plan  
**Backends:** Lakehouse (Delta) primary; Lakebase sequential/transactional. Neo4j unchanged.

## Context

**Refresh cache** calls `store.rebuild_adjacency(graph_name)` (UI
`POST /dtwin/adjacency/refresh`, later the external
`POST /api/v1/digitaltwin/cache/refresh`). It always rebuilds the five
companion snapshots in full:

| Companion | Lakehouse | Lakebase |
|-----------|-----------|----------|
| `_adj_out` | CTAS + OPTIMIZE | TRUNCATE + INSERT |
| `_adj_in` | CTAS + OPTIMIZE | TRUNCATE + INSERT |
| `_entity_search` | CTAS + Bloom + OPTIMIZE | TRUNCATE + INSERT |
| `_entity_search_asserted` | same, from `_data`/`_sync` | same, from `_sync` |
| `_props` | CTAS + OPTIMIZE | TRUNCATE + INSERT |

Today every step is serial. Each companion independently scans the SPO
relation (union view or asserted source). Wall time is the sum of five
warehouse jobs plus OPTIMIZE/ANALYZE.

The button must remain a **complete rebuild**. Incremental / skip-if-unchanged
(option C) is out of scope.

## Decision: option B

Run **independent companion rebuilds concurrently** on Lakehouse so wall
clock approaches the slowest companion instead of the sum.

Do **not**:

- change companion SQL results (`typed_out_select`, `typed_in_select`,
  `entity_search_select`, `props_select`);
- skip OPTIMIZE without a measured follow-up;
- share one Databricks SQL client across threads if the client is not
  documented thread-safe — use one statement client/connection per worker
  (same warehouse, same credentials as the store);
- start option C (CDF / delta-of-deltas).

### Lakehouse

`DeltaFlatStore.rebuild_adjacency` becomes two phases:

1. **Submit five independent jobs** (missing asserted search is skipped as
   today when ids are unresolved):
   - `_adj_out` CTAS + OPTIMIZE
   - `_adj_in` CTAS + OPTIMIZE
   - `_entity_search` CTAS + Bloom + OPTIMIZE
   - `_entity_search_asserted` CTAS + Bloom + OPTIMIZE (if configured)
   - `_props` CTAS + OPTIMIZE
2. **Join.** If any job fails, fail the refresh; do not leave a silent
   partial success. Companions that finished may already be visible
   (same as today's sequential CTAS). `forget_missing_props` runs only
   after `_props` succeeds.

Cap concurrency at **5** (one worker per companion). Log elapsed ms per
companion and total wall time.

Task progress (`run_adjacency_refresh_task`) should mention that indexes
are rebuilding in parallel; no new UI.

### Lakebase

Keep **one transaction, sequential TRUNCATE/INSERT**, then ANALYZE.

Postgres cannot share one cursor across concurrent writers, and splitting
into five autocommit connections would expose mixed old/new companions
without a single commit barrier. Lakebase already commits atomically
today; B must not regress that.

Optional later (not this change): a typed-entity staging table reused by
adj/props (option A). Not required for B.

## Errors

- Unresolved table ids: skip rebuild and warn (unchanged).
- Worker exception: log companion name, cancel remaining futures, re-raise
  so the task manager marks the refresh failed.
- OPTIMIZE / Bloom failures stay best-effort warnings (unchanged).

## Testing

- Unit: Lakehouse `rebuild_adjacency` schedules independent companion
  rebuilds (mock client records overlapping or unordered execution vs
  today's fixed `out → in → search → asserted → props` chain).
- Unit: a failing `_props` CTAS still fails the method and does not call
  `forget_missing_props`.
- Unit: Lakebase path still TRUNCATEs/INSERTs in one `_txn_cursor`.
- Existing adjacency rebuild tests keep asserting SQL shape, not wall
  clock.

## Scope

In: parallel Lakehouse companion rebuild, per-companion timing logs.  
Out: incremental refresh, dropping OPTIMIZE, MCP tool, changing Preview
SQL, Lakebase parallel writers.
