# Explorer Expansion Fallback Latency — Design

**Date:** 2026-09-16  
**Status:** Approved for implementation planning  
**Backends:** Lakehouse (Delta) and Lakebase (Postgres) only. **Neo4j is frozen.**

## Evidence

Live `/dtwin/sync/filter` timings on `BIGCustomers` /
`triplestore_bigcustomers_V1` (one seed `CUST0039973`, inferred on):

| Path | Median wall time | Payload |
|------|------------------|---------|
| Preview Starts with | 872 ms | 1 seed, 255 B |
| Preview Contains | 866 ms | same |
| Expand depth 0 | 10.85 s | 14 triples |
| Expand depth 1 | 5.84 s | 21 triples |
| Expand depth 2 | 2.90 s | 23 triples |
| Expand depth 3 | 3.71 s | 14,594 triples |

Alternating depth order did not change the ranking. TTFB ≈ total time
(transfer ~2–3 ms). Every expand logged a missing
`triplestore_bigcustomers_V1_props` then SPO fallback. Starts vs Contains
was noise.

## Problem

1. **Missing `_props` is retried on every expand.** The first warehouse
   statement always fails (`TABLE_OR_VIEW_NOT_FOUND` / `does not exist`),
   then the same BFS runs against the union SPO. Absence is not remembered.
2. **Low-depth fallback SQL is slower than deeper BFS** on Spark. Depth 0
   is a `VALUES` CTE joined to the full graph union plus
   `CROSS JOIN COUNT(*)`. Spark appears to pick a worse plan for the
   simpler statement. Depth 3 is only slightly slower than depth 2 despite
   ~600× more triples, so serialization is not the current bottleneck.
3. **The durable fix is still Refresh cache / Build**, which materializes
   `_props`. Code changes must not replace that; they only stop paying for
   a known-missing table and stop using a self-hurting depth-0/1 join shape
   on the SPO fallback.

## Decision

### A. Process-local negative cache for `_props`

After a classified missing-table error, remember the property-table id in
process memory. Later expands skip the failing statement and go straight to
the adj+SPO fallback SQL.

- Cache **negatives only**. Do not cache “exists”.
- Invalidate the id in `rebuild_adjacency` after a successful rebuild
  (Build, Refresh cache, reasoning, cohort writes).
- Unrelated SQL errors still propagate.
- Tests reset the cache in a fixture.

Do **not** add a `table_exists(_props)` probe on the hot path (warehouse
`SELECT 1` is itself hundreds of ms on Delta).

### B. Depth-0 payload short-circuit

When `depth == 0`, do not emit hop CTEs or join the graph to a `VALUES`
relation. Fetch:

```sql
SELECT subject, predicate, object, <n> AS _ob_expanded_count
FROM <payload>
WHERE subject IN (<deduped seeds, limited to max_entities>)
LIMIT <max_triples + 1>
```

`<payload>` is `_props` when used, else the reader SPO. `<n>` is the
bounded seed count (same cap semantics as today’s `entity_probe`).

### C. Spark small-frontier payload hint (depth ≥ 1)

Keep BFS CTEs. For `flavor="spark"`, add a broadcast hint on the tiny
`entities` side of the payload join:

```sql
SELECT /*+ BROADCAST(entities) */ triples.subject, ...
```

Postgres is unchanged (nested-loop/index lookup is already the intended
plan once `_props` exists).

### D. Shared execute helper

Delta and Lakebase currently duplicate the try/missing/`execute` fallback.
One helper in `props.py` owns: negative cache, error classification, log
line, fallback execute.

## Out of scope

- Neo4j
- Preview pagination, `field: any` split, extra Preview indexes
- Viz-only payloads / interned URI ids / CSR / N-hop tables
- Changing Refresh cache URL or button label
- Auto-running Refresh from Explorer
- Caching Explorer result sets

Revisit viz-only payloads **only if** Display or depth-3 transfer dominates
**after** `_props` exists and depth 0/1 plans are sane.

## Success

- Second expand on a graph without `_props` issues **one** warehouse
  statement (the SPO fallback), not two.
- Refresh cache / Build restores `_props` queries without a process restart.
- Depth-0 SQL has `WHERE subject IN` and no `level_1` / `CROSS JOIN entity_stats`.
- Spark depth ≥ 1 SQL contains `BROADCAST(entities)`.
- Re-benchmark on BIGCustomers after Refresh: expand depth 2 should no
  longer log `TABLE_OR_VIEW_NOT_FOUND` for `_props`.
