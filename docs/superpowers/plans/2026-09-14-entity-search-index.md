# Entity Search Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `_entity_search` companion table next to `_adj_out` / `_adj_in` so Explorer Preview (the first Search query) is one bounded lookup instead of two SPO scans.

**Architecture:** Shared SQL builds a one-row-per-typed-entity projection and a Preview SELECT. Lakehouse and Lakebase materialise `_entity_search` **inside** `rebuild_adjacency` (same job as adj). `filter_preview` uses `find_preview_seeds`; the union fast path directly queries the index without an existence round-trip and falls back on a missing-table error. Asserted-only Preview (`include_inferred=false` → `…_data` / `…_sync`) keeps today’s SPO path. Neo4j is frozen.

**Tech Stack:** Python 3.10+, Databricks SQL / Delta, Lakebase Postgres (`psycopg`), pytest, `uv run --frozen`.

**Spec:** `docs/superpowers/specs/2026-09-14-entity-search-index-design.md`

## Global Constraints

- One engine per domain: Lakehouse never reads Lakebase tables; Lakebase never reads Delta tables.
- `_entity_search` is always a physical TABLE, even when Lakehouse `_data` is a VIEW.
- Snapshot of the **reader-facing union** (same SPO `rebuild_adjacency` already uses). Stale until the next rebuild (including **Refresh adjacency**).
- Rebuild is **not** a new API: extend `GraphDBBackend.rebuild_adjacency`. The Refresh adjacency button and `POST /dtwin/adjacency/refresh` stay; they already call that method.
- Fast path only when Preview `table_name` is the union relation. It must not issue a separate table-existence query. Asserted-only relations (`*_data`, `*_sync`) use SPO fallback so unchecking Inferred cannot leak inferred instances.
- Preserve Preview caps (`max_preview=500`, probe `501`), match types (`contains` / `exact` / `starts` / `ends`), fields (`any` / `label` / `id`).
- **Neo4j freeze:** do not modify `src/back/core/graphdb/neo4j/**`. Default `supports_adjacency` remains False; Neo4j never gets `_entity_search`.
- No `pg_trgm`, no n-gram tables, no interned IDs, no second engine.
- Comments, logs, changelog, this plan: English only. Tests: `uv run --frozen pytest -q -m "not scenario"`.

## File map

| File | Responsibility |
|------|----------------|
| `src/back/core/graphdb/entity_search.py` | Shared projection + Preview SQL (`spark`/`postgres` identical for this table). |
| `src/back/core/graphdb/GraphDBBackend.py` | `entity_search_table_id`, `entity_search_ready`, `find_preview_seeds`; `rebuild_adjacency` still the refresh entrypoint. |
| `src/back/core/graphdb/delta/_table_naming.py` | `…_entity_search` FQN. |
| `src/back/core/graphdb/delta/materialize.py` | Delta CTAS + CLUSTER BY `type_uri`. |
| `src/back/core/graphdb/delta/DeltaFlatStore.py` | Rebuild entity search after adj CTAS; resolve FQN. |
| `src/back/core/graphdb/lakebase/_adjacency_ddl.py` | PG CREATE/INDEX/TRUNCATE+INSERT for entity search (same txn as adj). |
| `src/back/core/graphdb/lakebase/LakebaseFlatStore.py` | IDs + rebuild in existing `rebuild_adjacency`. |
| `src/back/objects/digitaltwin/DigitalTwin.py` | `filter_preview` uses `find_preview_seeds`; refresh task progress text names both indexes. |
| `src/back/core/graphdb/_starter_kit/ExampleStore.py` | Comment that rebuild also fills `_entity_search`. |
| `docs/graphdb-integration.md`, `docs/architecture.md`, `docs/user-guide.md` | Snapshot + Refresh adjacency rebuilds three tables. |
| Tests | SQL shape, contract, Delta CTAS, Lakebase txn, filter_preview. |

Do **not** change Refresh adjacency button ids, `data-sync-action`, or `/dtwin/adjacency/refresh`. Existing build / reasoning / cohort hooks already call `rebuild_adjacency`.

---

### Task 1: Shared entity-search SQL

**Files:**
- Create: `src/back/core/graphdb/entity_search.py`
- Create: `tests/units/graphdb/test_entity_search_sql.py`

**Interfaces:**
- Consumes: `RDF_TYPE`, `RDFS_LABEL` from `back.core.graphdb.constants`
- Produces:
  - `is_asserted_only_relation(table_name: str) -> bool`
  - `entity_search_select(spo: str) -> str`
  - `preview_select_sql(*, search_table: str, entity_type: str, field: str, match_type: str, value: str, limit: int, escape: Escape) -> str`

- [ ] **Step 1: Write failing SQL tests**

```python
from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL
from back.core.graphdb.entity_search import (
    entity_search_select,
    is_asserted_only_relation,
    preview_select_sql,
)


def test_asserted_only_suffixes():
    assert is_asserted_only_relation("cat.sch.g_data") is True
    assert is_asserted_only_relation("g_x_v1_sync") is True
    assert is_asserted_only_relation("cat.sch.g_graph") is False
    assert is_asserted_only_relation("Domain_V1") is False


def test_entity_search_select_projects_typed_instances():
    sql = entity_search_select("g._graph")
    assert "g._graph" in sql
    assert sql.count("g._graph") == 2
    assert f"predicate = '{RDF_TYPE}'" in sql
    assert f"predicate = '{RDFS_LABEL}'" in sql
    assert "AS uri" in sql
    assert "AS type_uri" in sql
    assert "AS label" in sql
    assert "AS uri_lc" in sql
    assert "AS label_lc" in sql
    assert "LEFT JOIN" in sql
    assert "GROUP BY subject" in sql


def test_preview_sql_any_contains_and_limit():
    sql = preview_select_sql(
        search_table="g_entity_search",
        entity_type="",
        field="any",
        match_type="contains",
        value="jac",
        limit=501,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "FROM g_entity_search" in sql
    assert "label_lc LIKE '%jac%'" in sql
    assert "uri_lc LIKE '%jac%'" in sql
    assert "LIMIT 501" in sql
    assert "ORDER BY type_uri, label" in sql


def test_preview_sql_type_and_exact_label():
    sql = preview_select_sql(
        search_table="g_entity_search",
        entity_type="http://ex/Person",
        field="label",
        match_type="exact",
        value="Ada",
        limit=10,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "type_uri = 'http://ex/Person'" in sql
    assert "label_lc = 'ada'" in sql
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "uri_lc" not in where


def test_preview_sql_rejects_empty_limit():
    import pytest
    with pytest.raises(ValueError):
        preview_select_sql(
            search_table="t",
            entity_type="",
            field="any",
            match_type="contains",
            value="x",
            limit=0,
            escape=lambda s: s,
        )
```

Tighten `test_preview_sql_type_and_exact_label`: the WHERE clause must contain `label_lc = 'ada'` and must **not** contain `uri_lc` (field=label).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_entity_search_sql.py`  
Expected: `ModuleNotFoundError: entity_search` or collection error.

- [ ] **Step 3: Implement `entity_search.py`**

```python
"""Shared entity-directory SQL for Lakehouse (Spark) and Lakebase (Postgres)."""

from __future__ import annotations

from typing import Callable

from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL

Escape = Callable[[str], str]

__all__ = [
    "Escape",
    "entity_search_select",
    "is_asserted_only_relation",
    "preview_select_sql",
]


def is_asserted_only_relation(table_name: str) -> bool:
    leaf = table_name.rsplit(".", 1)[-1]
    return leaf.endswith("_data") or leaf.endswith("_sync")


def entity_search_select(spo: str) -> str:
    return (
        f"SELECT typed.subject AS uri, typed.object AS type_uri, "
        f"COALESCE(lab.object, '') AS label, "
        f"LOWER(typed.subject) AS uri_lc, "
        f"LOWER(COALESCE(lab.object, '')) AS label_lc "
        f"FROM ("
        f"SELECT subject, MIN(object) AS object FROM {spo} "
        f"WHERE predicate = '{RDF_TYPE}' GROUP BY subject"
        f") typed "
        f"LEFT JOIN ("
        f"SELECT subject, MIN(object) AS object FROM {spo} "
        f"WHERE predicate = '{RDFS_LABEL}' GROUP BY subject"
        f") lab ON lab.subject = typed.subject"
    )


def preview_select_sql(
    *,
    search_table: str,
    entity_type: str,
    field: str,
    match_type: str,
    value: str,
    limit: int,
    escape: Escape,
) -> str:
    if int(limit) <= 0:
        raise ValueError("limit must be > 0")
    clauses: list[str] = []
    if entity_type:
        clauses.append(f"type_uri = '{escape(entity_type)}'")
    safe = escape(value.lower()) if value else ""
    search_label = field in ("label", "any")
    search_id = field in ("id", "any")

    def _like(column: str) -> str:
        if match_type == "exact":
            return f"{column} = '{safe}'"
        if match_type == "starts":
            return f"{column} LIKE '{safe}%'"
        if match_type == "ends":
            return f"{column} LIKE '%{safe}'"
        return f"{column} LIKE '%{safe}%'"

    if value:
        text_parts = []
        if search_label:
            text_parts.append(_like("label_lc"))
        if search_id:
            text_parts.append(_like("uri_lc"))
        if text_parts:
            clauses.append("(" + " OR ".join(text_parts) + ")")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return (
        f"SELECT uri, type_uri, label FROM {search_table}{where} "
        f"ORDER BY type_uri, label LIMIT {int(limit)}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_entity_search_sql.py`  
Expected: all passed.

- [ ] **Step 5: Commit** (if the user asked for commits)

```bash
git add src/back/core/graphdb/entity_search.py tests/units/graphdb/test_entity_search_sql.py
git commit -m "feat: add shared entity-search SQL for Preview lookups"
```

---

### Task 2: Backend contract and Preview helper

**Files:**
- Modify: `src/back/core/graphdb/GraphDBBackend.py`
- Modify: `src/back/core/graphdb/_starter_kit/ExampleStore.py` (comment only)
- Modify: `tests/units/graphdb/test_graphdb_adjacency_contract.py` (extend FakeStore)

**Interfaces:**
- Consumes: `entity_search_table_id`, `preview_select_sql`, `is_asserted_only_relation`
- Produces:
  - `entity_search_table_id(self, table_name: str) -> str` default `""`
  - `entity_search_ready(self, table_name: str) -> bool` (diagnostic contract; not called on the Preview hot path)
  - `find_preview_seeds(self, table_name, entity_type="", field="any", match_type="contains", value="", limit=0) -> List[Dict[str, str]]`  
    Each dict: `uri`, `type` (type URI), `label` — same keys `filter_preview` maps today from metadata (`type` is the full URI; local-name happens in `DigitalTwin.filter_preview`).

- [ ] **Step 1: Write failing contract tests** (add to the existing FakeStore file)

```python
def test_entity_search_ready_requires_support_table_and_union_relation():
    store = FakeStore()
    store._search_ready = True
    assert store.entity_search_ready("g") is True
    assert store.entity_search_ready("g_data") is False
    assert store.entity_search_ready("g_sync") is False
    store.supports_adjacency = False
    assert store.entity_search_ready("g") is False


def test_find_preview_seeds_uses_entity_search_when_ready():
    store = FakeStore()
    store._search_ready = True
    store.find_preview_seeds("g", value="ada", limit=501)
    assert len(store.queries) == 1
    assert "g_entity_search" in store.queries[0]
    assert "LIKE '%ada%'" in store.queries[0]


def test_find_preview_seeds_falls_back_to_spo_when_not_ready():
    store = FakeStore()
    store._search_ready = False
    store.find_preview_seeds("g", value="ada", limit=2)
    assert any("rdfs" in q or "label" in q.lower() or "LIKE" in q for q in store.queries)
    assert all("g_entity_search" not in q for q in store.queries)
```

Extend `FakeStore`:

```python
def entity_search_table_id(self, table_name: str) -> str:
    return "g_entity_search"

def table_exists(self, table_name: str) -> bool:
    if table_name == "g_entity_search":
        return self._search_ready
    return self._adj_ready and table_name in {"g_adj_out", "g_adj_in"}
```

Initialize `_search_ready = False`.

For the fallback test, FakeStore.`execute_query` returns `[]`; `find_preview_seeds` will call `find_seed_subjects` then `get_entity_metadata`. Assert at least two queries and no `g_entity_search`.

- [ ] **Step 2: Run to verify fail**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_graphdb_adjacency_contract.py`  
Expected: `AttributeError: entity_search_ready` (or similar).

- [ ] **Step 3: Implement defaults on `GraphDBBackend`**

Import `is_asserted_only_relation`, `preview_select_sql`.

```python
def entity_search_table_id(self, table_name: str) -> str:
    """Return the `_entity_search` identifier for *table_name*, or ``''``."""
    return ""

def entity_search_ready(self, table_name: str) -> bool:
    if not self.supports_adjacency:
        return False
    if is_asserted_only_relation(table_name):
        return False
    ident = self.entity_search_table_id(table_name)
    return bool(ident) and self.table_exists(ident)

def find_preview_seeds(
    self,
    table_name: str,
    entity_type: str = "",
    field: str = "any",
    match_type: str = "contains",
    value: str = "",
    limit: int = 0,
) -> List[Dict[str, str]]:
    """Return Preview rows ``{uri, type, label}``.

    Uses `_entity_search` when ready; otherwise `find_seed_subjects` +
    `get_entity_metadata`.
    """
    probe = int(limit) if limit else 0
    if self.supports_entity_search and not is_asserted_only_relation(table_name) and probe > 0:
        sql = preview_select_sql(
            search_table=self.entity_search_table_id(table_name),
            entity_type=entity_type,
            field=field,
            match_type=match_type,
            value=value,
            limit=probe,
            escape=self._sql_escape,
        )
        # Production implementation catches only missing-relation errors here
        # and then continues into the SPO fallback. It must not call
        # entity_search_ready(), which would add a warehouse round-trip.
        rows = self.execute_query(sql) or []
        return [
            {
                "uri": r["uri"],
                "type": r.get("type_uri") or "",
                "label": r.get("label") or "",
            }
            for r in rows
        ]
    subjects = list(
        self.find_seed_subjects(
            table_name,
            entity_type=entity_type,
            field=field,
            match_type=match_type,
            value=value,
            limit=probe,
        )
    )
    return self.get_entity_metadata(table_name, subjects)
```

Update `rebuild_adjacency` docstring: “Materialise `_adj_out`, `_adj_in`, and `_entity_search` from the readable SPO relation. Default is a no-op.”

ExampleStore: extend the `rebuild_adjacency` comment: “also CTAS / INSERT `_entity_search`.”

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_graphdb_adjacency_contract.py tests/units/graphdb/test_entity_search_sql.py`  
Expected: all passed.

- [ ] **Step 5: Commit** (if requested)

```bash
git commit -m "feat: add entity-search Preview contract with SPO fallback"
```

---

### Task 3: Lakehouse Delta `_entity_search`

**Files:**
- Modify: `src/back/core/graphdb/delta/_table_naming.py`
- Modify: `src/back/core/graphdb/delta/materialize.py`
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py`
- Modify: `tests/units/graphdb/delta/test_delta_package.py`
- Modify: `tests/units/graphdb/delta/test_delta_flat_store.py` if it asserts rebuild SQL list length

**Interfaces:**
- Consumes: `entity_search_select(spo)`
- Produces: FQN `…_entity_search`; `build_entity_search_ctas_sql(spo_fqn, search_fqn)`; `DeltaFlatStore.rebuild_adjacency` CTAS #3

- [ ] **Step 1: Failing naming + CTAS tests** in `test_delta_package.py`

```python
def test_entity_search_fqn():
    domain = _domain()
    view = _table_naming.view_fqn(domain)
    assert _table_naming.entity_search_fqn(domain) == view + "_entity_search"


def test_entity_search_ctas_has_one_from_and_clusters_type():
    from back.core.graphdb.entity_search import entity_search_select
    sql = materialize.build_entity_search_ctas_sql("cat.sch.g_graph", "cat.sch.g_entity_search")
    assert sql.count(" FROM ") == entity_search_select("cat.sch.g_graph").count(" FROM ")
    assert "CREATE OR REPLACE TABLE cat.sch.g_entity_search" in sql
    assert "CLUSTER BY (type_uri)" in sql
    assert "CREATE OR REPLACE TABLE cat.sch.g_entity_search" in sql
    # The SELECT already includes FROM; CTAS must not append another FROM spo
    assert sql.upper().count(" FROM ") == entity_search_select("cat.sch.g_graph").upper().count(" FROM ")
```

Also assert `AS ` + select, not `AS SELECT ... FROM spo` duplicate. Same regression as adj: **do not** append `FROM {spo}` after `entity_search_select`.

- [ ] **Step 2: Run to verify fail**

Run: `uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_package.py::TestTableNaming -k entity`  
Expected: `AttributeError: entity_search_fqn`.

- [ ] **Step 3: Implement naming + CTAS + rebuild**

`_table_naming.py`: `_SUFFIX_ENTITY_SEARCH = "_entity_search"` plus `entity_search_fqn` / `entity_search_suffix` mirroring `adj_out_fqn`.

Strip `_entity_search` in `DeltaFlatStore.adjacency_table_ids` suffix loop (and inferred-resolution loops) so a query table of that name still resolves adj ids.

```python
def build_entity_search_ctas_sql(spo_fqn: str, search_fqn: str) -> str:
    validate_table_name(spo_fqn)
    validate_table_name(search_fqn)
    select_sql = entity_search_select(spo_fqn)
    return (
        f"CREATE OR REPLACE TABLE {search_fqn} USING DELTA "
        f"CLUSTER BY (type_uri) "
        f"AS {select_sql}"
    )
```

`DeltaFlatStore.entity_search_table_id`: same pattern as `adjacency_table_ids` (domain FQN, else strip known suffixes from `table_name` and append `_entity_search`).

`rebuild_adjacency`: after the adj_out/adj_in loop:

```python
search_fqn = self.entity_search_table_id(table_name)
if search_fqn:
    materialize.drop_relation(self._client, search_fqn, kind="view")
    self._client.execute_statement(
        materialize.build_entity_search_ctas_sql(relation, search_fqn)
    )
    try:
        materialize.optimize_table(self._client, search_fqn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OPTIMIZE entity-search table failed for %s: %s", search_fqn, exc)
```

- [ ] **Step 4: Run Delta tests**

Run: `uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_package.py tests/units/graphdb/delta/test_delta_flat_store.py`  
Expected: all passed. If a rebuild test counted exactly two `execute_statement` calls, update it to three.

- [ ] **Step 5: Commit** (if requested)

---

### Task 4: Lakebase `_entity_search` in the adjacency transaction

**Files:**
- Modify: `src/back/core/graphdb/lakebase/_adjacency_ddl.py`
- Modify: `src/back/core/graphdb/lakebase/LakebaseFlatStore.py`
- Modify: `tests/units/graphdb/test_lakebase_adjacency.py`

**Interfaces:**
- Consumes: `entity_search_select(union_view)`
- Produces: `entity_search_phy(graph_name) -> str`; `ensure_entity_search_table`; `rebuild_entity_search_data`; `analyze` includes the third table. `rebuild_adjacency_data` stays adj-only; `LakebaseFlatStore.rebuild_adjacency` calls entity-search rebuild **in the same `_txn_cursor`**.

- [ ] **Step 1: Failing Lakebase tests**

Add:

```python
def test_entity_search_phy_is_bounded():
    from back.core.graphdb.lakebase import _adjacency_ddl
    name = _adjacency_ddl.entity_search_phy("g")
    assert name.endswith("_entity_search") or "_entity_search" in name
    assert len(name.encode("utf-8")) <= 63


def test_rebuild_adjacency_transaction_includes_entity_search(monkeypatch):
    # Drive LakebaseFlatStore.rebuild_adjacency with a fake txn cursor.
    # Assert TRUNCATE/INSERT for adj_out, adj_in, AND entity_search before commit.
```

Reuse the existing mocked-cursor pattern in `test_lakebase_adjacency.py`. The sequence inside the transaction must include `TRUNCATE`/`INSERT` for the entity-search table. `ANALYZE` for entity search happens **after** commit (same as adj).

- [ ] **Step 2: Run to verify fail**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_lakebase_adjacency.py -k entity`  
Expected: fail on missing `entity_search_phy`.

- [ ] **Step 3: Implement DDL + store**

```python
def entity_search_phy(graph_name: str) -> str:
    return _bounded_identifier(view_phy(graph_name), "_entity_search")


def ensure_entity_search_table(cur: Any, search: str) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {search} (
            uri TEXT NOT NULL PRIMARY KEY,
            type_uri TEXT NOT NULL,
            label TEXT NOT NULL,
            uri_lc TEXT NOT NULL,
            label_lc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {adjacency_index_name(search, 'type')} "
        f"ON {search} (type_uri)"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {adjacency_index_name(search, 'label')} "
        f"ON {search} (label_lc)"
    )


def rebuild_entity_search_data(cur: Any, union_view: str, search: str) -> None:
    from back.core.graphdb.entity_search import entity_search_select
    cur.execute(f"TRUNCATE {search}")
    cur.execute(
        f"INSERT INTO {search} (uri, type_uri, label, uri_lc, label_lc) "
        f"{entity_search_select(union_view)}"
    )
```

`LakebaseFlatStore`:

```python
def entity_search_table_id(self, table_name: str) -> str:
    return _adjacency_ddl.entity_search_phy(table_name)

def rebuild_adjacency(self, table_name: str) -> None:
    validate_table_name(table_name)
    union_view = self._sql_relation(table_name)
    adj_out, adj_in = self.adjacency_table_ids(table_name)
    search = self.entity_search_table_id(table_name)
    with self._cursor() as cur:
        _adjacency_ddl.ensure_adjacency_tables(cur, adj_out, adj_in)
        _adjacency_ddl.ensure_entity_search_table(cur, search)
    with self._txn_cursor() as (_, cur):
        _adjacency_ddl.rebuild_adjacency_data(cur, union_view, adj_out, adj_in)
        _adjacency_ddl.rebuild_entity_search_data(cur, union_view, search)
    with self._cursor() as cur:
        _adjacency_ddl.analyze_adjacency_tables(cur, adj_out, adj_in)
        cur.execute(f"ANALYZE {search}")
```

CREATE TABLE stays outside the rebuild txn (like adj today). Data load is atomic across the three tables.

- [ ] **Step 4: Run Lakebase tests**

Run: `uv run --frozen pytest -q tests/units/graphdb/test_lakebase_adjacency.py`  
Expected: all passed.

- [ ] **Step 5: Commit** (if requested)

---

### Task 5: Preview path + refresh copy

**Files:**
- Modify: `src/back/objects/digitaltwin/DigitalTwin.py` (`filter_preview`, `run_adjacency_refresh_task` messages)
- Test: add `tests/units/dtwin/test_filter_preview_entity_search.py` (or extend an existing DigitalTwin filter test if one exists)

**Interfaces:**
- Consumes: `store.find_preview_seeds(...)`
- Produces: unchanged JSON `{phase, seeds, total, capped}`

- [ ] **Step 1: Failing unit test**

```python
def test_filter_preview_uses_find_preview_seeds_and_maps_local_type():
    class Store:
        def find_preview_seeds(self, table, **kwargs):
            self.kwargs = kwargs
            return [
                {
                    "uri": "http://ex/Ada",
                    "type": "http://ex/Person",
                    "label": "Ada",
                },
                {
                    "uri": "http://ex/Bob",
                    "type": "http://ex/Person",
                    "label": "Bob",
                },
            ]

    out = DigitalTwin.filter_preview(Store(), "g", "", "any", "contains", "a", max_preview=1)
    assert out["capped"] is True
    assert out["total"] == 2
    assert out["seeds"][0]["type"] == "Person"
    assert out["seeds"][0]["type_uri"] == "http://ex/Person"
```

If `uri_local_name` of `http://ex/Person` is `Person`, assert that. Probe limit is `max_preview+1`; store must be called with `limit=2`.

- [ ] **Step 2: Run to verify fail** (preview still calls `find_seed_subjects`)

- [ ] **Step 3: Switch `filter_preview`**

Replace `find_seed_subjects` + `get_entity_metadata` with:

```python
probe_limit = max_preview + 1
rows = store.find_preview_seeds(
    graph_name,
    entity_type=entity_type,
    field=field,
    match_type=match_type,
    value=value,
    limit=probe_limit,
)
```

Keep the same exception wrapping. `capped = len(rows) > max_preview`; `preview_rows = rows[:max_preview]`; map to seeds with `uri_local_name` on type; sort `(type, label)` as today (fast path is already SQL-ordered; keep sort for fallback + stable UI).

`run_adjacency_refresh_task`: progress `Rebuilding adjacency and entity-search indexes for {graph_name}`; complete message can stay `Adjacency refresh completed` (API/UI unchanged) **or** `Graph index refresh completed` — keep **`Adjacency refresh completed`** so existing UI pollers that match the string do not break. Only the in-progress `tm.update_progress` text may mention entity search.

- [ ] **Step 4: Run**

Run: `uv run --frozen pytest -q tests/units/dtwin/test_filter_preview_entity_search.py tests/units/api/test_dtwin_adjacency_refresh.py tests/units/front/test_adjacency_refresh_ui.py`  
Expected: all passed. UI tests still say `Refresh adjacency`.

- [ ] **Step 5: Commit** (if requested)

---

### Task 6: Docs + verification

**Files:**
- Modify: `docs/graphdb-integration.md` (adjacency section: third table + rebuild)
- Modify: `docs/architecture.md` (index snapshot list)
- Modify: `docs/user-guide.md` (Refresh adjacency also rebuilds Search index)
- Changelog via `.claude/skills/changelog/SKILL.md` after implementation, not in this planning-only change unless you implement in the same turn.

- [ ] **Step 1: Document**

State explicitly:

- `_entity_search` is rebuilt by `rebuild_adjacency` together with `_adj_out` / `_adj_in`.
- **Refresh adjacency** is the user action for all three.
- View-mode: Search index is a snapshot; uncheck Inferred → asserted-only SPO fallback, not the union index.
- Neo4j: no table.

- [ ] **Step 2: Full test suite**

Run: `uv run --frozen pytest -q -m "not scenario"`  
Expected: green.

- [ ] **Step 3: Manual check**

1. Rebuild or click **Refresh adjacency**.
2. Explorer Search with Inferred on: warehouse should run **one** Preview statement on `_entity_search`.
3. Uncheck Inferred and search again: SPO fallback (no `_entity_search` in SQL).
4. Neo4j domain: no new tables, no button.

---

## Self-review

- Spec coverage: schema, one-query Preview, same rebuild clock, asserted-only fallback, Neo4j freeze, no button rename — each has a task.
- No trigram scope creep.
- `rebuild_adjacency` remains the single refresh entrypoint; Refresh adjacency UI/API unchanged.
- Preview directly queries `_entity_search`; `entity_search_ready` is not used
  on the hot path because its existence probe would negate the one-query goal.
- Types: `find_preview_seeds` → `{uri, type, label}` with `type` = type URI, matching `get_entity_metadata`.
