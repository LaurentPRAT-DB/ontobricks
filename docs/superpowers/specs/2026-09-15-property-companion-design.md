# Property Companion (`_props`) — Design

**Date:** 2026-09-15  
**Status:** Draft for review  
**Backends:** Lakehouse (Delta) and Lakebase (Postgres) only. **Neo4j is frozen.**

## Problem

Explorer expansion (`expand_and_fetch_subgraph`) already discovers neighbors
from `_adj_out` / `_adj_in`. The same statement then paints the subgraph with:

```sql
SELECT triples.subject, triples.predicate, triples.object, …
FROM <union SPO> triples
JOIN entities ON entities.entity = triples.subject
LIMIT max_triples + 1
```

The hop CTEs hit clustered edge lists. The fetch still scans the reader-facing
union (`_graph` VIEW or Lakebase union view). That union is the leftover
warehouse cost after adjacency.

`_entity_search` does not help: it has one row per instance, not outgoing
triples. Adj does not help: it stores typed entity–entity edges only
(no `rdf:type`, no `rdfs:label`, no literals).

## Goal

A fourth **always-TABLE** companion, `_props`, living alongside `_adj_out`,
`_adj_in`, and `_entity_search`:

- One row per outgoing triple of a **typed** subject (same typed-instance
  filter as `_entity_search` / adj endpoints).
- Shape `(subject, predicate, object)` — identical to SPO.
- Clustered / indexed on `subject` so `subject IN entities` is a point lookup.
- Rebuilt **inside** `rebuild_adjacency(...)` so every existing hook refreshes
  it (full build, Refresh adjacency, reasoning materialize, cohort writes).

The Refresh adjacency **button label and API path stay**. The worker still
calls `store.rebuild_adjacency(graph_name)`; that method materialises four
tables, not three.

When `_props` exists, `expand_and_fetch_sql` joins `_props` instead of the
union SPO. Payload semantics stay: all outgoing triples of discovered
subjects, including dangling entity edges, types, labels, and literals.

## Alternatives considered

| Approach | Trade-off |
|----------|-----------|
| **A. Typed-subject SPO replica `_props` (recommended)** | Same JOIN as today, swap relation. Exact payload. Size ≈ outgoing triples of typed instances (the rows Explorer can fetch). |
| **B. Literal complement + reconstruct from adj** | Smaller table. SQL becomes `UNION` of `adj_out` (src in entities) and `_props`. Risk of dropping untyped-IRI objects that adj excluded. |
| **C. No new table — CLUSTER SPO / use `_data`** | Lakehouse `_data` is already `CLUSTER BY (predicate, subject)` (predicate first). `_graph` remains a VIEW over `_data ∪ _inferred`. Lakebase reads a union view. Does not fix the statement Explorer actually runs. |

Choose **A**. Fetch stays one JOIN. No payload drift vs today’s adj+SPO path.

## Schema

| Column | Type | Meaning |
|--------|------|---------|
| `subject` | STRING/TEXT | Typed instance IRI |
| `predicate` | STRING/TEXT | Predicate IRI |
| `object` | STRING/TEXT | Literal or IRI (including `rdf:type` / `rdfs:label` / entity edges) |

No primary key. Duplicate SPO rows follow the source union (`UNION ALL` may
duplicate across `_data` / `_inferred`; `_props` copies that).

Projection (same reader-facing SPO relation as adj):

```sql
SELECT t.subject, t.predicate, t.object
FROM <spo> t
INNER JOIN (
  SELECT DISTINCT subject FROM <spo> WHERE predicate = '<rdf:type>'
) typed ON typed.subject = t.subject
```

Subjects without `rdf:type` are omitted (Explorer never discovers them via
adj).

## Expansion query

BFS CTEs unchanged (adj). Final fetch becomes:

```sql
SELECT triples.subject, triples.predicate, triples.object, stats._ob_expanded_count
FROM <graph>_props triples
JOIN entities ON entities.entity = triples.subject
CROSS JOIN entity_stats stats
LIMIT <max_triples + 1>
```

No separate existence probe. A missing-table error falls back to today’s
union-SPO join (same pattern as `_entity_search` Preview).

If adj tables are missing, keep the existing full SPO expansion path. Do not
attempt `_props` without adj: rebuild writes all four together; a domain
with `_props` but no adj is not a supported state.

`get_triples_for_subjects` (Neo4j / iterative fallback) is unchanged.

## Lifecycle / staleness

Snapshot, same clock as adjacency and `_entity_search`. View-mode Lakehouse
source changes appear in expansion fetch only after Refresh adjacency or a
full build.

Lakebase: `TRUNCATE`/`INSERT` for adj, entity search, **and** `_props` in the
**same transaction**. `ANALYZE` after commit.

Lakehouse: sequential `CREATE OR REPLACE TABLE` (adj_out, adj_in,
entity_search, props). `CLUSTER BY (subject)`. `OPTIMIZE` remains best-effort.

Lakebase indexes: btree on `subject` (required). Optional `(subject,
predicate)` is out of scope for v1.

## Naming

| Engine | Object |
|--------|--------|
| Lakehouse | `triplestore_<domain>_V<n>_props` |
| Lakebase | `<union-view-phy>_props` (same `_bounded_identifier` helper as `_entity_search`) |

Shared SQL lives in `src/back/core/graphdb/` next to `entity_search.py`
(e.g. `props.py`: `props_select(spo)`, `supports` flag, table-id helpers on
`GraphDBBackend`).

## Out of scope

- Neo4j files under `src/back/core/graphdb/neo4j/`
- Trigram / n-gram / `pg_trgm`
- N-hop precompute
- Type columns on adj, `(src, predicate)` reclustering of adj
- Using `_props` for Preview search
- Incremental per-triple updates
- Renaming Refresh adjacency or `/dtwin/adjacency/refresh`
- Changing Explorer JSON (`subject`, `predicate`, `object`) or caps
  (`depth`, `max_entities`, `max_triples`)
- Asserted-only expand (Inferred unchecked) — same as today: still uses the
  relation `filter_expand` already passes; `_props` is built from the **union**
  snapshot only

## Success

After indexes exist, `expand_and_fetch_subgraph` (adj-ready path) does **not**
read `_graph` / the Lakebase union view. Hops use adj; payload uses `_props`.
Refresh adjacency rebuilds adj_out, adj_in, entity_search, and props together.

## Verification

- SQL-shape tests for `props_select` and expand-fetch joining `_props`.
- Delta CTAS contains `CLUSTER BY (subject)` and the typed-subject join.
- Lakebase rebuild inserts `_props` in the same transaction as adj.
- Missing `_props` falls back to union SPO without raising to the UI.
- Neo4j tree untouched.
- `uv run --frozen pytest -q -m "not scenario"`.
