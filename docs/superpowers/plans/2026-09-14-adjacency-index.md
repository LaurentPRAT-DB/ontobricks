# Adjacency Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build per-backend neighbor-array tables (`_adj_out` / `_adj_in`) so Lakebase and Lakehouse (pass-through `_data` VIEW or materialized `_data` TABLE) serve 1-hop and bounded BFS from clustered edge lists instead of scanning the SPO union.

**Architecture:** A shared SQL module emits the typed-neighbor SELECT and dialect-specific expansion CTEs (`spark` vs `postgres`). Lakebase and Lakehouse each materialize **always-TABLE** adjacency objects in their own engine. Traversal uses adj when `supports_adjacency` is true and the tables exist; otherwise SPO fallback. Neo4j is frozen: no new methods on `Neo4jStore`, no adj tables, no UI/API, native Cypher expansion unchanged.

**Tech Stack:** Python 3.10+, Databricks SQL / Delta, Lakebase Postgres (`psycopg`), pytest, `uv run --frozen`.

## Global Constraints

- One engine per domain: Lakehouse queries never go to Lakebase; Lakebase never reads Delta adj tables.
- `_adj_out` / `_adj_in` are always physical tables, even when Lakehouse `_data` is a pass-through VIEW.
- Adj is a snapshot of the current readable SPO relation (`_graph` / Lakebase union view). View-mode Lakehouse adj goes stale when source tables change until the next rebuild.
- Same logical edge shape on both backends: `(src STRING/TEXT, predicate, dst)` out; `(dst, predicate, src)` in.
- Exclude `rdf:type` and `rdfs:label`; keep only `http%` endpoints that themselves have an `rdf:type` assertion (typed instances).
- Preserve Explorer caps (depth ≤ 3, `max_entities`, `max_triples`) and `query_limits` graph statement timeout.
- Fallback to today's SPO SQL when adj tables are missing (domains not rebuilt).
- **Neo4j freeze (hard):** do not modify any file under `src/back/core/graphdb/neo4j/`. Do not add `expand_and_fetch_subgraph` on `GraphDBBackend` (Explorer uses `getattr` — a default method would steal Neo4j onto SQL CTEs). `supports_adjacency` is False by default; only `DeltaFlatStore` and `LakebaseFlatStore` set it True. Refresh-adjacency API returns 400 for `graph_backend=neo4j` / `none`; the button is hidden. Build / reasoning / cohort hooks call `rebuild_adjacency` only when `supports_adjacency`. Neo4j Cypher `expand_entity_neighbors` / `bfs_traversal` / `filter_expand` iterative path stay as today.
- Comments, logs, changelog, this plan: English only. Run tests with `uv run --frozen pytest -q -m "not scenario"`.
- No interned integer IDs, no `TEXT[]` CSR, no second query engine, no process-local LRU.

## File map

| File | Responsibility |
|------|----------------|
| `src/back/core/graphdb/adjacency.py` | Shared SELECT + expansion SQL (`spark` / `postgres`). |
| `src/back/core/graphdb/GraphDBBackend.py` | Default `sql_flavor`, `adjacency_table_ids`, `rebuild_adjacency`, adj-aware expand. |
| `src/back/core/graphdb/delta/_table_naming.py` | `_adj_out` / `_adj_in` FQNs. |
| `src/back/core/graphdb/delta/materialize.py` | Delta CTAS + OPTIMIZE for adj. |
| `src/back/core/graphdb/delta/DeltaFlatStore.py` | Rebuild + expand via adj; keep SPO fallback. |
| `src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py` | Rebuild adj after `_graph` exists. |
| `src/back/core/graphdb/lakebase/_adjacency_ddl.py` | PG CREATE/INDEX/INSERT SELECT. |
| `src/back/core/graphdb/lakebase/LakebaseFlatStore.py` | Rebuild + expand via adj. |
| `src/back/objects/digitaltwin/_build_pipeline.py` | Call `rebuild_adjacency` after bulk load / optimize. |
| `src/back/core/reasoning/ReasoningService.py` | Rebuild adj after materialising inferences (when count > 0). |
| `src/back/core/graph_analysis/CohortBuilder.py` | Same after cohort writes that insert edges. |
| `docs/graphdb-integration.md` | Document `_adj_*` and view-mode staleness. |
| `tests/units/graphdb/test_adjacency_sql.py` | SQL shape tests (no warehouse). |
| `tests/units/graphdb/delta/test_delta_flat_store.py` | Delta expand uses adj FQNs. |
| `tests/units/graphdb/test_lakebase_adjacency.py` | PG DDL + expand SQL (mocked cursor). |

---

### Task 1: Shared adjacency SQL (dialect-aware)

**Files:**
- Create: `src/back/core/graphdb/adjacency.py`
- Create: `tests/units/graphdb/test_adjacency_sql.py`

**Interfaces:**
- Consumes: `RDF_TYPE`, `RDFS_LABEL` from `back.core.graphdb.constants`.
- Produces:
  - `SqlFlavor = Literal["spark", "postgres"]`
  - `typed_out_select(spo: str) -> str`
  - `typed_in_select(spo: str) -> str`
  - `expand_entity_neighbors_sql(adj_out: str, adj_in: str, uris: list[str], escape: Callable[[str], str]) -> str`
  - `expand_and_fetch_sql(*, flavor: SqlFlavor, adj_out: str, adj_in: str, spo: str, selected_uris: list[str], depth: int, max_entities: int, max_triples: int, escape: Callable[[str], str]) -> str`

- [ ] **Step 1: Write failing SQL tests**

```python
from back.core.graphdb.adjacency import (
    expand_and_fetch_sql,
    expand_entity_neighbors_sql,
    typed_in_select,
    typed_out_select,
)
from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL


def test_typed_out_select_excludes_type_and_label():
    sql = typed_out_select("g._graph")
    assert "g._graph" in sql
    assert RDF_TYPE in sql
    assert RDFS_LABEL in sql
    assert "t.object LIKE 'http%'" in sql
    assert "typed.subject = t.object" in sql
    assert "AS src" in sql
    assert "AS dst" in sql


def test_typed_in_select_reverses_endpoints():
    sql = typed_in_select("g._graph")
    assert "t.subject AS src" in sql or "AS src" in sql
    assert "typed.subject = t.subject" in sql


def test_neighbors_sql_unions_out_and_in():
    sql = expand_entity_neighbors_sql(
        "t_adj_out", "t_adj_in", ["http://ex/a"], lambda s: s.replace("'", "''")
    )
    assert "FROM t_adj_out" in sql
    assert "FROM t_adj_in" in sql
    assert "http://ex/a" in sql


def test_spark_expansion_uses_left_anti_join():
    sql = expand_and_fetch_sql(
        flavor="spark",
        adj_out="o",
        adj_in="i",
        spo="g",
        selected_uris=["http://ex/a"],
        depth=2,
        max_entities=10,
        max_triples=100,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "LEFT ANTI JOIN" in sql
    assert "FROM o " in sql or "FROM o\n" in sql or "FROM o t" in sql
    assert "FROM i " in sql or "FROM i t" in sql
    assert "FROM g " in sql or "FROM g triples" in sql
    assert "level_2" in sql
    assert "LIMIT 11" in sql
    assert "LIMIT 101" in sql


def test_postgres_expansion_uses_not_exists():
    sql = expand_and_fetch_sql(
        flavor="postgres",
        adj_out="o",
        adj_in="i",
        spo="g",
        selected_uris=["http://ex/O'Brien"],
        depth=1,
        max_entities=5,
        max_triples=20,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "LEFT ANTI JOIN" not in sql
    assert "NOT EXISTS" in sql
    assert "O''Brien" in sql
    assert "VALUES (" in sql or "VALUES" in sql
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_adjacency_sql.py
```

Expected: FAIL (`ModuleNotFoundError: adjacency`).

- [ ] **Step 3: Implement `adjacency.py`**

Use this SQL contract (Spark `VALUES` already used in `DeltaFlatStore.expand_and_fetch_subgraph`; Postgres `VALUES` is valid too):

```python
"""Shared adjacency / typed-neighbor SQL for Lakehouse (Spark) and Lakebase (Postgres)."""

from __future__ import annotations

from typing import Callable, Literal

from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL

SqlFlavor = Literal["spark", "postgres"]
Escape = Callable[[str], str]


def typed_out_select(spo: str) -> str:
    return (
        f"SELECT DISTINCT t.subject AS src, t.predicate, t.object AS dst "
        f"FROM {spo} t "
        f"INNER JOIN {spo} typed "
        f"ON typed.subject = t.object AND typed.predicate = '{RDF_TYPE}' "
        f"WHERE t.object LIKE 'http%' "
        f"AND t.predicate != '{RDF_TYPE}' "
        f"AND t.predicate != '{RDFS_LABEL}'"
    )


def typed_in_select(spo: str) -> str:
    return (
        f"SELECT DISTINCT t.object AS dst, t.predicate, t.subject AS src "
        f"FROM {spo} t "
        f"INNER JOIN {spo} typed "
        f"ON typed.subject = t.subject AND typed.predicate = '{RDF_TYPE}' "
        f"WHERE t.object LIKE 'http%' "
        f"AND t.predicate != '{RDF_TYPE}' "
        f"AND t.predicate != '{RDFS_LABEL}'"
    )


def expand_entity_neighbors_sql(
    adj_out: str, adj_in: str, uris: list[str], escape: Escape
) -> str:
    in_clause = ", ".join(f"'{escape(u)}'" for u in uris)
    return (
        f"SELECT DISTINCT entity FROM ("
        f"SELECT dst AS entity FROM {adj_out} WHERE src IN ({in_clause}) "
        f"UNION "
        f"SELECT src AS entity FROM {adj_in} WHERE dst IN ({in_clause})"
        f") n"
    )


def _anti_join(flavor: SqlFlavor, level: int) -> str:
    visited = f"visited_{level}"
    if flavor == "spark":
        return (
            f"LEFT ANTI JOIN {visited} "
            f"ON {visited}.entity = candidate.entity"
        )
    return (
        f"WHERE NOT EXISTS ("
        f"SELECT 1 FROM {visited} v WHERE v.entity = candidate.entity)"
    )


def expand_and_fetch_sql(
    *,
    flavor: SqlFlavor,
    adj_out: str,
    adj_in: str,
    spo: str,
    selected_uris: list[str],
    depth: int,
    max_entities: int,
    max_triples: int,
    escape: Escape,
) -> str:
    if not selected_uris:
        raise ValueError("At least one selected URI is required")
    depth = max(0, int(depth))
    max_entities = max(1, int(max_entities))
    max_triples = max(1, int(max_triples))
    seed_values = ", ".join(
        f"('{escape(uri)}')" for uri in dict.fromkeys(selected_uris)
    )
    ctes = [f"level_0(entity) AS (VALUES {seed_values})"]
    for level in range(1, depth + 1):
        previous = f"level_{level - 1}"
        visited_union = " UNION ALL ".join(
            f"SELECT entity FROM level_{prior}" for prior in range(level)
        )
        anti = _anti_join(flavor, level)
        ctes.extend(
            [
                f"visited_{level} AS ({visited_union})",
                (
                    f"level_{level}_candidates AS ("
                    f"SELECT t.dst AS entity FROM {adj_out} t "
                    f"JOIN {previous} frontier ON t.src = frontier.entity "
                    f"UNION ALL "
                    f"SELECT t.src AS entity FROM {adj_in} t "
                    f"JOIN {previous} frontier ON t.dst = frontier.entity)"
                ),
                (
                    f"level_{level} AS ("
                    f"SELECT DISTINCT candidate.entity "
                    f"FROM level_{level}_candidates candidate "
                    f"{anti} "
                    f"LIMIT {max_entities})"
                ),
            ]
        )
    levels = " UNION ALL ".join(
        f"SELECT entity FROM level_{level}" for level in range(depth + 1)
    )
    ctes.extend(
        [
            (
                "entity_probe AS ("
                f"SELECT DISTINCT entity FROM ({levels}) discovered "
                f"LIMIT {max_entities + 1})"
            ),
            (
                "entities AS ("
                f"SELECT entity FROM entity_probe LIMIT {max_entities})"
            ),
            (
                "entity_stats AS ("
                "SELECT COUNT(*) AS _ob_expanded_count FROM entity_probe)"
            ),
        ]
    )
    return (
        "WITH "
        + ", ".join(ctes)
        + " "
        + "SELECT triples.subject, triples.predicate, triples.object, "
        + "stats._ob_expanded_count "
        + f"FROM {spo} triples "
        + "JOIN entities ON entities.entity = triples.subject "
        + "CROSS JOIN entity_stats stats "
        + f"LIMIT {max_triples + 1}"
    )
```

Hops never join SPO. The final SELECT still reads properties from `spo` (`_graph` or the Lakebase union view).

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_adjacency_sql.py
```

Expected: PASS.

- [ ] **Step 5: Commit** (only if the user asked to commit work)

```bash
git add src/back/core/graphdb/adjacency.py tests/units/graphdb/test_adjacency_sql.py
git commit -m "feat(graphdb): shared adjacency SQL for Spark and Postgres"
```

---

### Task 2: GraphDBBackend adjacency contract + SPO fallback

**Files:**
- Modify: `src/back/core/graphdb/GraphDBBackend.py`
- Modify: `src/back/core/graphdb/_starter_kit/ExampleStore.py` (comment the new optional methods)
- Do **not** touch `src/back/core/graphdb/neo4j/**`

**Interfaces:**
- Consumes: `adjacency.expand_entity_neighbors_sql` (only when `supports_adjacency`).
- Produces on `GraphDBBackend`:
  - `supports_adjacency: bool` default `False`
  - `sql_flavor(self) -> Optional[Literal["spark", "postgres"]]` default `None`
  - `adjacency_table_ids(self, table_name: str) -> tuple[str, str]` default `("", "")`
  - `rebuild_adjacency(self, table_name: str) -> None` default no-op
  - `adjacency_ready(self, table_name: str) -> bool` — `supports_adjacency` and both ids exist via `table_exists`
  - `expand_entity_neighbors` — if `adjacency_ready` then adj SQL, else **existing SPO SQL** (Neo4j never hits this: it already overrides with Cypher)
  - **Do not** add `expand_and_fetch_subgraph` on the ABC. Implement it only on `DeltaFlatStore` and `LakebaseFlatStore`.

- [ ] **Step 1: Write failing tests for fallback vs adj**

Add `tests/units/graphdb/test_graphdb_adjacency_contract.py` with a tiny concrete subclass that records `execute_query` SQL and toggles `table_exists`:

```python
class FakeStore(GraphDBBackend):
    sql_flavor = "postgres"  # implement as @property returning "postgres"

    def __init__(self):
        self.queries = []
        self._adj_ready = False

    def adjacency_table_ids(self, table_name: str):
        return ("g_adj_out", "g_adj_in")

    def table_exists(self, table_name: str) -> bool:
        return self._adj_ready and table_name in {"g_adj_out", "g_adj_in"}

    def execute_query(self, query: str):
        self.queries.append(query)
        return []

    # stub remaining abstracts with `raise NotImplementedError` or pass
```

Assert: `_adj_ready=False` → neighbor SQL contains `predicate = '{RDF_TYPE}'` (legacy). `_adj_ready=True` → SQL contains `g_adj_out` and not a scan of object LIKE http% on the SPO table.

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_graphdb_adjacency_contract.py
```

- [ ] **Step 3: Implement defaults on `GraphDBBackend`**

`LakebaseBase.query_dialect` stays `"sql"`. Set `supports_adjacency = True`, `sql_flavor` `"postgres"` on Lakebase and `"spark"` on Delta only.

Keep `expand_and_fetch_subgraph` **off** the ABC. Helper `adjacency.expand_and_fetch_sql` is called from Delta and Lakebase implementations only. Unrebuilt Delta domains keep today's SPO CTE inside `DeltaFlatStore.expand_and_fetch_subgraph` when `adjacency_ready` is false.

- [ ] **Step 4: GREEN** then commit if requested.

---

### Task 3: Lakehouse Delta adj tables (VIEW and TABLE `_data`)

**Files:**
- Modify: `src/back/core/graphdb/delta/_table_naming.py`
- Modify: `src/back/core/graphdb/delta/materialize.py`
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py`
- Modify: `src/back/core/graphdb/delta/DeltaTripleStoreBuildPipeline.py`
- Modify: `tests/units/graphdb/delta/test_delta_flat_store.py`

**Interfaces:**
- Produces: `_table_naming.adj_out_fqn` / `adj_in_fqn` (suffixes `_adj_out`, `_adj_in` on the same base as `_data`).
- `materialize.build_adj_ctas_sql(spo_fqn, adj_fqn, direction: Literal["out","in"]) -> str` using `CLUSTER BY (src)` for out and `CLUSTER BY (dst)` for in.
- `DeltaFlatStore.rebuild_adjacency(table_name)` CTAS both tables from `_sql_relation(table_name)` (the union `_graph` when it exists, else `_data`), then `OPTIMIZE` each.
- `DeltaTripleStoreBuildPipeline.run` calls `store.rebuild_adjacency` **after** `_ensure_graph_view` (and after `_data` OPTIMIZE when not view-mode). In view-mode, still CTAS adj (this is the expensive R2RML-backed snapshot that makes hops cheap).

- [ ] **Step 1: Tests**

```python
def test_adj_fqns():
    domain = _domain()
    assert _table_naming.adj_out_fqn(domain) == (
        "cat.sch.triplestore_mydomain_V1_adj_out"
    )
    assert _table_naming.adj_in_fqn(domain) == (
        "cat.sch.triplestore_mydomain_V1_adj_in"
    )


def test_adj_ctas_clusters_src_for_out():
    sql = materialize.build_adj_ctas_sql("cat.sch.g_graph", "cat.sch.g_adj_out", "out")
    assert "CLUSTER BY (src)" in sql
    assert "CREATE OR REPLACE TABLE cat.sch.g_adj_out USING DELTA" in sql
```

Patch `execute_statement` on a `DeltaFlatStore` and assert `rebuild_adjacency` issues two CTAS + two OPTIMIZE.

- [ ] **Step 2: RED then implement naming + CTAS helpers + `rebuild_adjacency`.**

CTAS must `CREATE OR REPLACE TABLE` (Databricks cannot replace a leftover VIEW of the same name — if a previous experiment created views, `DROP VIEW IF EXISTS` then CTAS, same pattern as `apply_data_relation`).

- [ ] **Step 3: Hook the Delta-only build pipeline** after `_ensure_graph_view`:

```python
store = DeltaFlatStore(self.source_client, domain=self.domain, settings=self.settings)
store.rebuild_adjacency(self.graph_view or self.data_table)
```

Progress message: `"Building adjacency index..."`. Failure fails the build (Explorer would silently fall back, but a failed CTAS means a warehouse error the user must see).

- [ ] **Step 4: GREEN + existing `TestDeltaSingleStatementExpansion` still pass** (SPO fallback when adj `table_exists` is false in those tests — they mock `execute_query` only; set `table_exists` false by default in unit tests).

Update expansion tests: when `table_exists` returns True for `*_adj_out`, generated SQL must mention `_adj_out` / `_adj_in` and must **not** join SPO for hops (only for the final triple fetch).

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_flat_store.py tests/units/graphdb/test_adjacency_sql.py
```

---

### Task 4: Lakebase Postgres adj tables

**Files:**
- Create: `src/back/core/graphdb/lakebase/_adjacency_ddl.py`
- Modify: `src/back/core/graphdb/lakebase/LakebaseFlatStore.py`
- Modify: `src/back/objects/digitaltwin/_build_pipeline.py` (after `optimize_table` in `_apply_full` / equivalent)
- Create: `tests/units/graphdb/test_lakebase_adjacency.py`

**Interfaces:**
- Physical names: `{view_phy(graph)}_adj_out` and `{view_phy(graph)}_adj_in` in the graph schema (`search_path` already set).
- DDL:

```sql
CREATE TABLE IF NOT EXISTS {adj_out} (
    src TEXT NOT NULL,
    predicate TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, predicate, dst)
);
CREATE INDEX IF NOT EXISTS {adj_out}_src_idx ON {adj_out} (src);

CREATE TABLE IF NOT EXISTS {adj_in} (
    dst TEXT NOT NULL,
    predicate TEXT NOT NULL,
    src TEXT NOT NULL,
    PRIMARY KEY (dst, predicate, src)
);
CREATE INDEX IF NOT EXISTS {adj_in}_dst_idx ON {adj_in} (dst);
```

- Rebuild:

```sql
TRUNCATE {adj_out};
INSERT INTO {adj_out} (src, predicate, dst) {typed_out_select(union_view)};
TRUNCATE {adj_in};
INSERT INTO {adj_in} (dst, predicate, src) {typed_in_select(union_view)};
ANALYZE {adj_out};
ANALYZE {adj_in};
```

`union_view` is `_sql_relation(table_name)` (the existing `g_<dom>_v<n>` UNION view over `_sync` + `__app`). `managed_synced` and `app_managed` both rebuild in Postgres from that view — do **not** Lakeflow-sync adj from Delta.

- [ ] **Step 1: Tests with a mocked `_cursor`** capturing executed SQL: rebuild emits TRUNCATE/INSERT/ANALYZE; `expand_entity_neighbors` with adj ready uses `_adj_out`.

- [ ] **Step 2: Implement DDL module + `LakebaseFlatStore.adjacency_table_ids` / `rebuild_adjacency` / `sql_flavor`.**

- [ ] **Step 3: After `self.store.optimize_table(self.graph_name)` in `_build_pipeline.py` (~line 898):**

```python
self.store.rebuild_adjacency(self.graph_name)
```

Also call it on the incremental-apply path if one exists after inserts. If `rebuild_adjacency` is missing (Neo4j), `getattr(..., "rebuild_adjacency", None)` no-op is already the ABC default.

- [ ] **Step 4:**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_lakebase_adjacency.py tests/units/graphdb/test_graphdb_adjacency_contract.py
```

---

### Task 5: Refresh adj after inference and cohort edge writes

**Files:**
- Modify: `src/back/core/reasoning/ReasoningService.py` (`_materialize_inferred` after `optimize_inferred_companion`)
- Modify: `src/back/core/graph_analysis/CohortBuilder.py` (after `insert_triples` that adds graph edges)
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py` `purge_materialized_triples` — rebuild adj after truncate inferred (or TRUNCATE adj rows that only existed in inferred: v1 = full rebuild)
- Test: `tests/units/core/reasoning/` existing materialize tests if present; otherwise a focused mock test

**Rule:** v1 always **full rebuild** of both adj tables from the current SPO relation. Incremental edge insert is out of scope (wrong on delete/purge).

```python
count = self._store.insert_triples(table_name, triples)
    if count > 0:
    optimize_fn = getattr(self._store, "optimize_inferred_companion", None)
    if callable(optimize_fn):
        optimize_fn(table_name)
    if getattr(self._store, "supports_adjacency", False):
        self._store.rebuild_adjacency(table_name)
```

Log: `"Rebuilding adjacency index after %d inferred triples"` (English, `%-style`).

- [ ] **Step 1: Test** that a mocked store has `rebuild_adjacency` called once when insert returns > 0, and not called when 0.

- [ ] **Step 2: Implement hooks.**

- [ ] **Step 3:**

```bash
uv run --frozen pytest -q tests/units/core/reasoning tests/units/graphdb
```

---

### Task 6: Docs + changelog

**Files:**
- Modify: `docs/graphdb-integration.md` (Lakehouse objects table + Lakebase layout)
- Modify: `docs/architecture.md` if it duplicates the UC object table
- Modify: `docs/user-guide.md` — one short note under Knowledge Graph build: adjacency snapshot; view-mode freshness
- Create/append: `changelogs/v0.9.0/<github-user>_2026-09-14.log` (English section)

Document:

| Object | Kind | When | Used by |
|--------|------|------|---------|
| `_adj_out` | Delta TABLE `CLUSTER BY (src)` or PG table btree(`src`) | End of graph build; after inference/cohort writes | Explorer hops, `expand_entity_neighbors` |
| `_adj_in` | Delta TABLE `CLUSTER BY (dst)` or PG btree(`dst`) | same | reverse hops |

Lakehouse `lakehouse_materialization=view`: `_data` stays a live VIEW; `_adj_*` are still tables snapshotted from `_graph`. Interactive hops are fast; **new source rows do not appear in traversal until rebuild**. Retrieve of properties via SPO `_graph` can still see live `_data` — Explorer expansion can therefore disagree with a raw SPARQL scan until rebuild. State this explicitly.

- [ ] **Step 1: Edit docs.**
- [ ] **Step 2: Changelog in English** (title, context, numbered files, Tests line).
- [ ] **Step 3: Full unit suite**

```bash
uv run --frozen pytest -q -m "not scenario"
```

Paste the summary line into the changelog `Tests:` field.

---

### Task 7 (phase 2, same engines): Hop cache

Do **not** start this until Tasks 1–6 are green on a real domain. Same constraint: no second engine.

**Shape:** `{base}_hop_cache` table on the active backend:

- columns: `graph_version TEXT`, `seed_hash TEXT`, `depth INT`, `cap_hash TEXT`, `entities ARRAY<STRING>` (Spark) / `TEXT[]` (PG), `built_at TIMESTAMP`
- cluster / index: `(graph_version, seed_hash, depth)`
- `expand_and_fetch_subgraph`: hash `sorted(selected_uris)+depth+max_entities`; SELECT cache; on miss compute via adj, INSERT
- invalidate: `TRUNCATE` inside `rebuild_adjacency` (covers build + inference rebuilds)

**Files (when starting phase 2):**
- `src/back/core/graphdb/adjacency.py` — `seed_hash(...)` helper
- Delta `materialize.py` + Lakebase `_adjacency_ddl.py`
- Tests for hash stability and truncate-on-rebuild

Buffer cache remains automatic (warehouse disk/result cache if always-on; Postgres `shared_buffers`). No application code.

---

## Rollout / ops

1. Deploy code. Existing SQL graphs keep SPO fallback until rebuilt. **Neo4j domains: zero behaviour change.**
2. Run Knowledge Graph **Build** on Lakehouse (view or table) and Lakebase domains.
3. Confirm UC/PG objects `_adj_out` / `_adj_in` exist; Explorer expand is one statement mentioning those tables (warehouse query history / `log_statement`).
4. After reasoning, confirm adj rebuild in logs.

## Out of scope

- **Entire Neo4j stack** — no edits under `src/back/core/graphdb/neo4j/`, no adj objects, no Explorer SQL CTE, no Refresh adjacency
- Interned node IDs, CSR `INT[]`, Vector Search, LakeGraph product, Redis, FastAPI LRU
- Changing `_data` clustering (separate retrieve optimisation)
- SPARQL translator using adj (property paths later)
- Analytics Lakeflow job (still reads `_data`)
