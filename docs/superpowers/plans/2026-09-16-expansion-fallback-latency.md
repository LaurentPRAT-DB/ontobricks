# Explorer Expansion Fallback Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut Explorer expand latency on graphs that still lack `_props`, and stop Spark depth-0/1 fallback SQL from scanning worse than a deeper BFS.

**Architecture:** Keep adj-ready hops. Remember a missing `_props` table in process memory so the next expand skips the failing warehouse statement. Short-circuit depth 0 to `WHERE subject IN (...)`. Broadcast the tiny `entities` side on Spark payload joins. Invalidate the negative cache from `rebuild_adjacency`.

**Tech Stack:** Python 3.10+, Spark SQL / Delta, Lakebase Postgres, pytest, `uv run --frozen`. Explorer HTTP contract unchanged.

## Global Constraints

- Lakehouse and Lakebase only. Do not modify `src/back/core/graphdb/neo4j/**`.
- Keep Refresh cache (`/dtwin/adjacency/refresh`) and `rebuild_adjacency` as the way `_props` is created.
- Preserve expand caps (`max_entities`, `max_triples + 1` probe row, `_ob_expanded_count`).
- Missing `_props` still falls back to adj+SPO; unrelated SQL errors still propagate.
- Comments, changelogs, this plan: English only.
- Tests: `uv run --frozen pytest -q -m "not scenario"`.
- Do not commit unless the user asks.

## Evidence (do not re-litigate)

BIGCustomers, seed `CUST0039973`: Preview ~870 ms (Starts ≈ Contains). Expand depth 0 / 1 / 2 / 3 medians: 10.85 s / 5.84 s / 2.90 s / 3.71 s. Every expand hit missing `_props` then SPO fallback.

## File map

| File | Responsibility |
|------|----------------|
| `src/back/core/graphdb/props.py` | Negative cache, shared execute-with-fallback helper |
| `src/back/core/graphdb/adjacency.py` | Depth-0 IN filter; Spark `BROADCAST(entities)` |
| `src/back/core/graphdb/delta/DeltaFlatStore.py` | Use helper; clear cache after rebuild |
| `src/back/core/graphdb/lakebase/LakebaseFlatStore.py` | Same |
| `tests/units/graphdb/test_props_sql.py` | Cache + helper tests |
| `tests/units/graphdb/test_adjacency_sql.py` | Depth-0 and broadcast SQL shape |
| `tests/units/graphdb/delta/test_delta_flat_store.py` | Second expand does not retry `_props` |
| `tests/units/graphdb/test_lakebase_adjacency.py` | Same for Lakebase |
| `docs/optimizations.md` | Record measured bottleneck and this wave |

Skip Preview JS/SQL. Skip viz-only payloads until a post-Refresh re-benchmark says Display/depth-3 transfer dominates.

---

### Task 1: Negative `_props` cache and shared fallback execute

**Files:**
- Modify: `src/back/core/graphdb/props.py`
- Modify: `tests/units/graphdb/test_props_sql.py`

**Interfaces:**
- Consumes: existing `is_missing_props_error(exc, props_table) -> bool`
- Produces:
  - `remember_missing_props(props_table: str) -> None`
  - `known_missing_props(props_table: str) -> bool`
  - `forget_missing_props(props_table: str) -> None`
  - `reset_missing_props_cache() -> None` (tests)
  - `execute_expand_with_props_fallback(*, execute_query, sql: str, props_table: str, fallback_sql: str) -> list`

- [ ] **Step 1: Failing tests**

Append to `tests/units/graphdb/test_props_sql.py`:

```python
from unittest.mock import Mock

from back.core.graphdb.props import (
    execute_expand_with_props_fallback,
    forget_missing_props,
    known_missing_props,
    remember_missing_props,
    reset_missing_props_cache,
)


def setup_function() -> None:
    reset_missing_props_cache()


def test_missing_props_cache_is_negative_only():
    assert known_missing_props("g_props") is False
    remember_missing_props("g_props")
    assert known_missing_props("g_props") is True
    forget_missing_props("g_props")
    assert known_missing_props("g_props") is False


def test_execute_skips_props_sql_when_table_is_known_missing():
    execute_query = Mock(return_value=[{"subject": "s"}])
    remember_missing_props("g_props")

    rows = execute_expand_with_props_fallback(
        execute_query=execute_query,
        sql="SELECT FROM g_props",
        props_table="g_props",
        fallback_sql="SELECT FROM g",
    )

    execute_query.assert_called_once_with("SELECT FROM g")
    assert rows == [{"subject": "s"}]


def test_execute_remembers_missing_props_and_retries_fallback():
    execute_query = Mock(
        side_effect=[
            RuntimeError("TABLE_OR_VIEW_NOT_FOUND: g_props"),
            [{"subject": "s"}],
        ]
    )

    rows = execute_expand_with_props_fallback(
        execute_query=execute_query,
        sql="SELECT FROM g_props",
        props_table="g_props",
        fallback_sql="SELECT FROM g",
    )

    assert known_missing_props("g_props") is True
    assert [call.args[0] for call in execute_query.call_args_list] == [
        "SELECT FROM g_props",
        "SELECT FROM g",
    ]
    assert rows == [{"subject": "s"}]


def test_execute_propagates_unrelated_failure():
    execute_query = Mock(side_effect=RuntimeError("permission denied"))
    try:
        execute_expand_with_props_fallback(
            execute_query=execute_query,
            sql="SELECT FROM g_props",
            props_table="g_props",
            fallback_sql="SELECT FROM g",
        )
    except RuntimeError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("expected permission denied")
    assert known_missing_props("g_props") is False
```

- [ ] **Step 2: Run the new tests — expect FAIL**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_props_sql.py
```

Expected: import / attribute errors for the new names.

- [ ] **Step 3: Implement in `props.py`**

Keep `is_missing_props_error` and `props_select`. Add a module-level `set[str]`, the four cache helpers, and:

```python
def execute_expand_with_props_fallback(
    *,
    execute_query,
    sql: str,
    props_table: str,
    fallback_sql: str,
) -> list:
    if not props_table:
        return execute_query(sql) or []
    if known_missing_props(props_table):
        return execute_query(fallback_sql) or []
    try:
        return execute_query(sql) or []
    except Exception as exc:  # noqa: BLE001
        if not is_missing_props_error(exc, props_table):
            raise
        remember_missing_props(props_table)
        logger.info(
            "Property table is unavailable; using SPO expansion fallback: %s",
            exc,
        )
        return execute_query(fallback_sql) or []
```

Import `get_logger` the same way other `graphdb` modules do. Empty `props_table` means “no companion attempted” — run `sql` only.

- [ ] **Step 4: Re-run focused tests — PASS**

```bash
uv run --frozen pytest -q tests/units/graphdb/test_props_sql.py
```

---

### Task 2: Wire the helper into Delta and Lakebase expand

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py` (`expand_and_fetch_subgraph`)
- Modify: `src/back/core/graphdb/lakebase/LakebaseFlatStore.py` (`expand_and_fetch_subgraph`)
- Modify: `tests/units/graphdb/delta/test_delta_flat_store.py`
- Modify: `tests/units/graphdb/test_lakebase_adjacency.py`

**Interfaces:**
- Consumes: `execute_expand_with_props_fallback`, `reset_missing_props_cache`
- Produces: second expand on a missing `_props` graph calls `execute_query` once

- [ ] **Step 1: Failing tests**

In `test_delta_flat_store.py` (and the Lakebase equivalent with `g_v1_props` / `g_v1`):

```python
def test_when_props_is_known_missing_expansion_skips_props_sql(self):
    from back.core.graphdb.props import reset_missing_props_cache

    reset_missing_props_cache()
    store = DeltaFlatStore(MagicMock(), domain=_domain())
    store.table_exists = MagicMock(return_value=True)
    store.execute_query = MagicMock(
        side_effect=[
            RuntimeError(
                "TABLE_OR_VIEW_NOT_FOUND: "
                "cat.sch.triplestore_mydomain_V1_props"
            ),
            [],
            [],
        ]
    )

    store.expand_and_fetch_subgraph(
        "cat.sch.triplestore_mydomain_V1_data", ["http://ex/a"], 1, 50, 100
    )
    store.expand_and_fetch_subgraph(
        "cat.sch.triplestore_mydomain_V1_data", ["http://ex/a"], 1, 50, 100
    )

    assert store.execute_query.call_count == 3
    second_sql = store.execute_query.call_args_list[2].args[0]
    assert "FROM cat.sch.triplestore_mydomain_V1_props triples" not in second_sql
    assert "FROM cat.sch.triplestore_mydomain_V1_data triples" in second_sql
```

Keep the existing first-miss retry test (`call_count == 2`).

- [ ] **Step 2: Run those tests — expect FAIL** (`call_count == 4`)

```bash
uv run --frozen pytest -q \
  tests/units/graphdb/delta/test_delta_flat_store.py::TestExpandAndFetchSubgraph::test_when_props_is_known_missing_expansion_skips_props_sql \
  tests/units/graphdb/test_lakebase_adjacency.py::test_expand_and_fetch_subgraph_skips_props_when_known_missing
```

(Name the Lakebase test `test_expand_and_fetch_subgraph_skips_props_when_known_missing`.)

- [ ] **Step 3: Replace duplicated try/except in both `expand_and_fetch_subgraph` methods**

When `props` is set, build `sql` (with props) and `fallback_sql` (same `expand_and_fetch_sql` call with `props=None`). Then:

```python
rows = execute_expand_with_props_fallback(
    execute_query=self.execute_query,
    sql=sql,
    props_table=props,
    fallback_sql=fallback_sql,
) or []
```

When adj is not ready, keep the existing SPO-only SQL and `execute_query(sql)` (no helper).

- [ ] **Step 4: Focused tests PASS**

```bash
uv run --frozen pytest -q \
  tests/units/graphdb/delta/test_delta_flat_store.py \
  tests/units/graphdb/test_lakebase_adjacency.py
```

---

### Task 3: Forget missing `_props` after a successful rebuild

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py` (`rebuild_adjacency`)
- Modify: `src/back/core/graphdb/lakebase/LakebaseFlatStore.py` (`rebuild_adjacency`)
- Modify: the same two test modules as Task 2

**Interfaces:**
- Consumes: `forget_missing_props(props_table: str)`
- Produces: after `rebuild_adjacency`, the next expand tries `_props` again

- [ ] **Step 1: Failing test (Delta; mirror for Lakebase)**

```python
def test_rebuild_adjacency_clears_missing_props_cache(self):
    from back.core.graphdb.props import (
        known_missing_props,
        remember_missing_props,
        reset_missing_props_cache,
    )

    reset_missing_props_cache()
    store = DeltaFlatStore(MagicMock(), domain=_domain())
    store._client = MagicMock()
    remember_missing_props(store.props_table_id("cat.sch.triplestore_mydomain_V1_data"))
    # Patch CTAS/OPTIMIZE so rebuild does not hit a warehouse.
    store.rebuild_adjacency("cat.sch.triplestore_mydomain_V1_data")
    assert known_missing_props(
        store.props_table_id("cat.sch.triplestore_mydomain_V1_data")
    ) is False
```

If `rebuild_adjacency` currently requires a live client, patch `execute_statement` / `drop_relation` / `optimize_table` as neighboring rebuild tests already do. If no rebuild unit test exists, add the smallest mock that lets the method run to the `forget_missing_props` line.

- [ ] **Step 2: Run — FAIL** (cache still True)

- [ ] **Step 3: Call `forget_missing_props(props)` at the end of a successful `rebuild_adjacency`** in both stores. Do not clear on early `return` when table ids are unresolved.

- [ ] **Step 4: Focused tests PASS**

---

### Task 4: Depth-0 `WHERE subject IN` short-circuit

**Files:**
- Modify: `src/back/core/graphdb/adjacency.py` (`expand_and_fetch_sql`)
- Modify: `tests/units/graphdb/test_adjacency_sql.py`
- Modify: `tests/units/graphdb/delta/test_delta_flat_store.py` (`test_depth_zero_has_no_neighbor_level`)

**Interfaces:**
- Consumes: existing `expand_and_fetch_sql(...)` signature
- Produces: depth 0 SQL with `WHERE subject IN` and no hop CTEs / no `CROSS JOIN entity_stats`

- [ ] **Step 1: Failing tests**

```python
def test_depth_zero_filters_payload_by_subject_in():
    sql = expand_and_fetch_sql(
        flavor="spark",
        adj_out="o",
        adj_in="i",
        spo="g",
        props="g_props",
        selected_uris=["http://ex/a", "http://ex/a"],
        depth=0,
        max_entities=10,
        max_triples=20,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "level_1" not in sql
    assert "CROSS JOIN entity_stats" not in sql
    assert "WHERE" in sql and "subject IN" in sql
    assert "FROM g_props" in sql
    assert "http://ex/a" in sql
    assert "LIMIT 21" in sql
    assert "_ob_expanded_count" in sql
```

Update `test_depth_zero_has_no_neighbor_level` so it still asserts no `level_1_candidates`.

- [ ] **Step 2: Run — FAIL** (current SQL still has `VALUES` + join)

```bash
uv run --frozen pytest -q tests/units/graphdb/test_adjacency_sql.py
```

- [ ] **Step 3: At the top of `expand_and_fetch_sql`, after validation**

If `depth == 0`:

```python
seeds = list(dict.fromkeys(selected_uris))[:max_entities]
in_list = ", ".join(f"'{escape(uri)}'" for uri in seeds)
payload_relation = props or spo
return (
    f"SELECT subject, predicate, object, {len(seeds)} AS _ob_expanded_count "
    f"FROM {payload_relation} "
    f"WHERE subject IN ({in_list}) "
    f"LIMIT {max_triples + 1}"
)
```

Depth ≥ 1 keeps the existing CTE builder.

- [ ] **Step 4: Focused tests PASS**, including existing expand SQL tests.

---

### Task 5: Spark `BROADCAST(entities)` on depth ≥ 1 payload join

**Files:**
- Modify: `src/back/core/graphdb/adjacency.py`
- Modify: `tests/units/graphdb/test_adjacency_sql.py`

**Interfaces:**
- Consumes: `flavor: SqlFlavor`
- Produces: Spark SELECT starts with `/*+ BROADCAST(entities) */`; Postgres SQL has no hint

- [ ] **Step 1: Failing tests**

```python
def test_spark_payload_join_broadcasts_entities():
    sql = expand_and_fetch_sql(
        flavor="spark",
        adj_out="o",
        adj_in="i",
        spo="g",
        selected_uris=["http://ex/a"],
        depth=1,
        max_entities=5,
        max_triples=20,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "/*+ BROADCAST(entities) */" in sql


def test_postgres_payload_join_has_no_broadcast_hint():
    sql = expand_and_fetch_sql(
        flavor="postgres",
        adj_out="o",
        adj_in="i",
        spo="g",
        selected_uris=["http://ex/a"],
        depth=1,
        max_entities=5,
        max_triples=20,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "BROADCAST" not in sql
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: In the final SELECT of the CTE path**

```python
hint = "/*+ BROADCAST(entities) */ " if flavor == "spark" else ""
# SELECT {hint}triples.subject, ...
```

Do not add the hint on the depth-0 short-circuit (no `entities` CTE).

- [ ] **Step 4: `uv run --frozen pytest -q tests/units/graphdb/test_adjacency_sql.py` — PASS**

---

### Task 6: Docs, changelog, verification

**Files:**
- Modify: `docs/optimizations.md` (sections 7, 14–17)
- Modify: `docs/superpowers/plans/2026-09-15-search-transversal-next.md` — mark Preview waves as shipped; point remaining expansion work here
- Create or append: `changelogs/v0.9.0/benoitcayladbx_2026-09-16.log`

**Interfaces:** none

- [ ] **Step 1: Update `docs/optimizations.md`**

  - §7: missing `_props` uses a process-local negative cache; Refresh/Build clears it.
  - §14: record the BIGCustomers medians (Preview ~870 ms; expand 0/1/2/3 as above) and that Starts vs Contains was a wash.
  - §15: after Build/Refresh, confirm `_props` exists before chasing Preview.
  - §17: remove items already shipped (in-app Preview sort, starts-with default, asserted companion, `pg_trgm`, clustering/Bloom). Point remaining expansion work at this plan. Keep N-hop / interned IDs / CSR / viz-only payload as “skip until Display dominates after `_props`”.

- [ ] **Step 2: Changelog section (English)**

Title: **Skip known-missing `_props` and fix depth-0 expand SQL**

Context: Explorer expand on an old Lakehouse graph paid a failing `_props` query every time; Spark depth 0/1 fallback was slower than depth 2.

- [ ] **Step 3: Full unit suite**

```bash
uv run --frozen pytest -q -m "not scenario"
```

Paste the summary line into `Tests:`.

- [ ] **Step 4: Manual re-benchmark (same session as the investigation)**

After **Refresh cache** on BIGCustomers:

1. Confirm logs no longer contain `TABLE_OR_VIEW_NOT_FOUND` for `..._V1_props`.
2. Repeat Preview Starts ×3 and expand depth 0, 1, 2 ×3 with the same seed URI.
3. Record medians in the changelog `Context:` or a follow-up note in `docs/optimizations.md` §14.

If Refresh is not run, the negative cache + depth-0 IN filter still apply; do not claim `_props` wins.

---

## Explicitly not in this plan

| Item | Why |
|------|-----|
| Preview `field: any` / pagination / extra indexes | Measured Preview ~870 ms vs expand 2.9–10.8 s; Starts ≈ Contains |
| `focusEntityByUri` Contains | Not on the timed path |
| Viz-only triple payload | Depth 3 added ~0.8 s vs depth 2 while returning 14k triples; revisit after `_props` |
| Integer URI intern / CSR / N-hop | Same skip list as `2026-09-15-search-transversal-next.md` |
| Auto Refresh from Explorer | Operator still owns Build/Refresh |

## Self-review

- Spec A (cache) → Tasks 1–3
- Spec B (depth 0) → Task 4
- Spec C (Spark broadcast) → Task 5
- Spec D (shared helper) → Task 1–2
- Success / re-benchmark → Task 6
- No Preview or Neo4j tasks
