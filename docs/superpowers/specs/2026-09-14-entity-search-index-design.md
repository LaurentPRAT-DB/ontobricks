# Entity Search Index — Design

**Date:** 2026-09-14  
**Status:** Approved for planning  
**Backends:** Lakehouse (Delta) and Lakebase (Postgres) only. **Neo4j is frozen.**

## Problem

Explorer Search (`POST /dtwin/sync/filter`, `phase: preview`) is the first warehouse query on a search. Today it:

1. Scans the full SPO union (`_graph` / Lakebase union view) with `LOWER(...) LIKE '%value%'` (`find_seed_subjects`).
2. Scans again for `rdf:type` + `rdfs:label` (`get_entity_metadata`).

Adjacency tables (`_adj_out` / `_adj_in`) do not help: they store typed entity–entity edges, not labels.

## Solution

A third **always-TABLE** companion, `_entity_search`, living **alongside** `_adj_out` and `_adj_in`:

- One row per typed instance (`uri`, `type_uri`, `label`, `uri_lc`, `label_lc`).
- Built from the same reader-facing SPO relation as adjacency.
- Rebuilt **inside** `rebuild_adjacency(...)` so every existing hook already refreshes it:
  - full KG build
  - **Refresh adjacency** button / `POST /dtwin/adjacency/refresh`
  - reasoning materialize (when triples change)
  - cohort writes that change the graph

The Refresh adjacency **button label stays**. The worker still calls `store.rebuild_adjacency(graph_name)`; that method now materialises three tables, not two.

## Schema

| Column | Type | Meaning |
|--------|------|---------|
| `uri` | STRING/TEXT PK | Subject IRI |
| `type_uri` | STRING/TEXT | `MIN(object)` among `rdf:type` rows |
| `label` | STRING/TEXT | `MIN(object)` among `rdfs:label` rows, else `''` |
| `uri_lc` | STRING/TEXT | `LOWER(uri)` |
| `label_lc` | STRING/TEXT | `LOWER(label)` |

Subjects without `rdf:type` are omitted (same as today’s metadata filter).

## Preview query

When Preview targets the **union** relation (Explorer “Inferred” checked /
`include_inferred=true`), the backend optimistically runs:

```sql
SELECT uri, type_uri, label
FROM <graph>_entity_search
WHERE <type and/or text predicates on type_uri, uri_lc, label_lc>
ORDER BY type_uri, label
LIMIT 501
```

Match types unchanged: `contains` / `exact` / `starts` / `ends`. Field `any` / `label` / `id` unchanged.

No separate existence probe precedes this query. A missing-table error falls
back to today’s SPO two-step path. Preview also uses that fallback when it
targets asserted-only (`…_data` / `…_sync`, because Inferred is unchecked).

## Lifecycle / staleness

Snapshot, same clock as adjacency. View-mode Lakehouse source changes appear in Search only after Refresh adjacency or a full build.

Lakebase: `TRUNCATE`/`INSERT` for adj **and** entity search in the **same transaction**. `ANALYZE` after commit.

Lakehouse: sequential `CREATE OR REPLACE TABLE` (adj_out, adj_in, entity_search). `OPTIMIZE` remains best-effort.

## Out of scope

- Neo4j files under `src/back/core/graphdb/neo4j/`
- Trigram / n-gram indexes (`pg_trgm`)
- Using `_entity_search` for hop expansion
- Renaming the Refresh adjacency button or API path
- Incremental per-triple index updates

## Success

Preview (Inferred on, after indexes exist) is **one** warehouse statement against `_entity_search`, not two SPO scans. Refresh adjacency rebuilds adj_out, adj_in, and entity_search together.
