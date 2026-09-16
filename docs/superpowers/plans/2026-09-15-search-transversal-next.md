# Search / Transversal Next Optimizations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the remaining Asana Search / transversal backlog as three independent, testable waves without touching Neo4j or renaming Refresh adjacency.

**Architecture:** Preview uses the union or asserted `_entity_search` companion,
sorts bounded rows in the application, and defaults to Starts with. Expansion
uses adjacency plus `_props`. This plan is retained as implementation history;
new measured expansion work lives in
`docs/superpowers/plans/2026-09-16-expansion-fallback-latency.md`.

**Tech Stack:** Python 3.10+, Spark SQL / Delta, Lakebase Postgres (`psycopg`, optional `pg_trgm`), Explorer JS in `query-sigmagraph.js`, pytest, `uv run --frozen`.

## Global Constraints

- Lakehouse and Lakebase only. Do not modify `src/back/core/graphdb/neo4j/**`.
- Keep Refresh adjacency label and `/dtwin/adjacency/refresh`.
- Preserve Preview caps (`max_preview=500`, probe `501`) and match types (`contains` / `exact` / `starts` / `ends`).
- Missing-index paths keep SPO / btree fallbacks. Unrelated SQL errors still propagate.
- Comments, changelogs, this plan: English only.
- Tests: `uv run --frozen pytest -q -m "not scenario"`.

## Status

Waves 1 and 2 are shipped: application-side Preview sorting, Starts-with
default, asserted-only search, Lakebase `pg_trgm`, and Lakehouse search
clustering/Bloom.

Wave 3 remains conditional. Do not add type columns or change adjacency layout
without a measured hop filter bottleneck. Skip N-hop tables, interned IDs, CSR,
and process-local neighborhood caches unless a later design approves them.

---

### Wave 1 / Task 1: Sort Preview in Python, not in the warehouse

**Files:**
- Modify: `src/back/core/graphdb/entity_search.py` (`preview_select_sql`)
- Modify: `src/back/core/graphdb/GraphDBBackend.py` (`find_preview_seeds`)
- Modify: `tests/units/graphdb/test_entity_search_sql.py`
- Modify: `tests/units/graphdb/test_graphdb_adjacency_contract.py`

**Interfaces:**
- `preview_select_sql` drops `ORDER BY type_uri, label`.
- `find_preview_seeds` sorts the returned dicts by `(type, label)` after the query (both index and SPO paths that share this helper).

- [ ] **Step 1: Failing tests**

```python
def test_preview_select_sql_has_no_warehouse_order_by():
    sql = preview_select_sql(
        search_table="g_entity_search",
        entity_type="",
        field="any",
        match_type="contains",
        value="ada",
        limit=501,
        escape=lambda s: s.replace("'", "''"),
    )
    assert "ORDER BY" not in sql
    assert "LIMIT 501" in sql
```

Also assert `find_preview_seeds` returns rows ordered by type then label when the mock returns unsorted rows.

- [ ] **Step 2:** `uv run --frozen pytest -q tests/units/graphdb/test_entity_search_sql.py tests/units/graphdb/test_graphdb_adjacency_contract.py` — expect FAIL.
- [ ] **Step 3:** Remove `ORDER BY` from `preview_select_sql`. After collecting preview dicts, `sorted(rows, key=lambda r: (r.get("type") or "", r.get("label") or "", r.get("uri") or ""))`.
- [ ] **Step 4:** Re-run focused tests — PASS.
- [ ] **Step 5:** Changelog + `uv run --frozen pytest -q -m "not scenario"`.

**Success:** Spark/Postgres no longer sort the full match set before `LIMIT`. UI order unchanged.

---

### Wave 1 / Task 2: Default Explorer match to starts-with

**Files:**
- Modify: `src/front/templates/partials/dtwin/_query_sigmagraph.html` (or the match `<select>` partial)
- Modify: `src/front/static/query/js/query-sigmagraph.js` (defaults `'contains'` → `'starts'`, including reset paths ~2758)
- Modify: `tests/units/front/` Explorer search contract (add or extend)

**Interfaces:**
- Unchanged API: `match_type` still accepted. Only the initial UI/default JavaScript value changes.

- [ ] **Step 1:** Failing structural test: HTML selected option is `starts`; JS fallback is `'starts'`; reset after search does not restore `contains`.
- [ ] **Step 2:** Run that front test — FAIL.
- [ ] **Step 3:** Change default selected option and JS fallbacks. Keep `contains` as an explicit choice.
- [ ] **Step 4:** Front tests PASS. Changelog + user-guide one-liner (Search match default).
- [ ] **Step 5:** If browser tools exist, open Explorer Search and confirm Starts with is selected; otherwise note that in changelog.

**Success:** New searches use prefix `LIKE 'x%'` unless the user picks Contains. Lakebase `text_pattern_ops` can apply.

---

### Wave 2 / Task 3: Asserted-only entity-search companion

**Problem:** Unchecking Inferred targets `*_data` / `*_sync`; `is_asserted_only_relation` skips `_entity_search` (union snapshot would leak inferences).

**Approach (pick A, do not implement B without a spec):**

- **A (recommended):** Second physical table `_entity_search_asserted` built from `_data` / `_sync` in the same `rebuild_adjacency` job. Preview uses it iff `is_asserted_only_relation(table_name)`.
- **B:** Filter union `_entity_search` by asserted subjects at query time — still scans inferred rows; reject.

**Files:**
- Modify: `src/back/core/graphdb/entity_search.py` (relation → table id helper)
- Modify: Delta `_table_naming.py` / `materialize.py` / `DeltaFlatStore.py`
- Modify: Lakebase `_adjacency_ddl.py` / `LakebaseFlatStore.py`
- Modify: `find_preview_seeds` to try asserted table with the same missing-table fallback
- Tests: entity-search SQL + Delta rebuild + Lakebase txn + contract (`g_data` uses asserted FQN)

**Success:** Inferred-off Preview is one lookup on the asserted companion, not two SPO scans.

Write a short spec under `docs/superpowers/specs/` before coding if asserted vs union naming needs product sign-off.

---

### Wave 2 / Task 4: Lakebase `pg_trgm` GIN for contains

**Files:**
- Modify: `src/back/core/graphdb/lakebase/_adjacency_ddl.py` (`ensure_entity_search_table`)
- Modify: rebuild to `CREATE EXTENSION IF NOT EXISTS pg_trgm` in `public` (same pgcrypto pattern: not in graph schema)
- Tests: `test_lakebase_adjacency.py` asserts `USING gin` / `gin_trgm_ops` on `label_lc` and `uri_lc`

**SQL shape:**

```sql
CREATE INDEX IF NOT EXISTS <name>_label_trgm_idx
  ON <search> USING gin (label_lc gin_trgm_ops);
```

Keep existing `text_pattern_ops` btrees for starts/exact. `contains` stays `LIKE '%x%'`; planner uses GIN when selectivity allows.

**Failure mode:** If `CREATE EXTENSION` is forbidden, log and skip GIN (btree-only). Test both paths with mocked cursor.

**Success:** Contains Preview on Lakebase can use an index. Rebuild still atomic with adj.

---

### Wave 2 / Task 5: Lakehouse `_entity_search` layout + Bloom

**Files:**
- Modify: `src/back/core/graphdb/delta/materialize.py` `build_entity_search_ctas_sql`
- Tests: `tests/units/graphdb/delta/test_delta_package.py`

Change:

```sql
CLUSTER BY (type_uri, label_lc)
```

After CTAS, best-effort:

```sql
ALTER TABLE <search> SET TBLPROPERTIES (
  'delta.bloomFilter.columns' = 'label_lc,uri_lc'
)
```

If `ALTER` fails, log warning (same as OPTIMIZE). Bloom helps equality more than `%x%`; still worth it with type + starts.

**Success:** Rebuild SQL in tests contains the new `CLUSTER BY` and Bloom property attempt.

---

### Wave 3 / Task 6: Denormalize types onto adjacency

**Only if** Explorer hop filters by class are slow (join to `_entity_search` / SPO). Spec first: columns `src_type`, `dst_type` on `_adj_out` / `_adj_in`, rebuild SELECT joins typed subjects, expansion SQL optional extra `AND dst_type = …` when UI sends a type filter.

Out of scope for v1 if Explorer has no hop-type filter in the expand API.

---

### Wave 3 / Task 7: Cluster adj by `(src, predicate)`

Change `build_adj_ctas_sql` cluster key from `src` / `dst` to `(src, predicate)` / `(dst, predicate)`. Lakebase: composite btree `(src, predicate)`. Helps predicate-filtered hops; undirected Explore gains little.

Do last; rebuilds rewrite both adj tables.

---

## Asana mapping

After each wave, mark the matching subtasks complete on **Search / transversal optimizations**. Parent stays open until waves 1–2 land (wave 3 optional).

## Verification (every task)

- Focused tests red → green
- `uv run --frozen pytest -q -m "not scenario"`
- Changelog English section
- Docs: `docs/graphdb-integration.md` + user-guide if user-visible
