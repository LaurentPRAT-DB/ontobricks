# Property Companion (`_props`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Materialize typed-subject SPO rows in a `_props` companion so Explorer expansion fetches node payloads without scanning the reader-facing SPO union.

**Architecture:** Add a shared `props_select(spo)` projection and backend-specific table naming/materialization. Lakehouse and Lakebase rebuild `_props` in the existing `rebuild_adjacency` lifecycle. Adj-ready expansion optimistically fetches from `_props`; a missing-relation error retries the same adjacency BFS with the current SPO relation.

**Tech Stack:** Python 3.10+, Databricks SQL / Delta, Lakebase Postgres (`psycopg`), pytest, `uv run --frozen`.

## Global Constraints

- Lakehouse and Lakebase only; do not modify `src/back/core/graphdb/neo4j/**`.
- `_props` is an always-TABLE snapshot built from the reader-facing union.
- Preserve the Explorer JSON shape and entity/triple cap semantics.
- Do not issue a `_props` existence probe before the expansion statement.
- Missing `_props` falls back to SPO; unrelated query errors propagate.
- Keep the Refresh adjacency label and API unchanged.

---

### Task 1: Shared property projection and expansion source

**Files:**
- Create: `src/back/core/graphdb/props.py`
- Modify: `src/back/core/graphdb/adjacency.py`
- Create: `tests/units/graphdb/test_props_sql.py`
- Modify: `tests/units/graphdb/test_adjacency_sql.py`

**Interfaces:**
- Produces: `props_select(spo: str) -> str`
- Changes: `expand_and_fetch_sql(..., spo: str, props: str | None = None, ...) -> str`

- [ ] Write tests proving `props_select` keeps every outgoing triple for typed subjects and that `expand_and_fetch_sql` uses `props` only for its final payload join.
- [ ] Run `uv run --frozen pytest -q tests/units/graphdb/test_props_sql.py tests/units/graphdb/test_adjacency_sql.py` and verify failure because the API does not exist.
- [ ] Implement the projection and optional final-fetch relation.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: Lakehouse `_props` lifecycle and fallback

**Files:**
- Modify: `src/back/core/graphdb/GraphDBBackend.py`
- Modify: `src/back/core/graphdb/delta/_table_naming.py`
- Modify: `src/back/core/graphdb/delta/materialize.py`
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py`
- Modify: `tests/units/graphdb/test_graphdb_adjacency_contract.py`
- Modify: `tests/units/graphdb/delta/test_delta_flat_store.py`

**Interfaces:**
- Produces: `GraphDBBackend.props_table_id(table_name: str) -> str`
- Produces: Delta `props_fqn`, `props_suffix`, and `build_props_ctas_sql`
- Behavior: Delta rebuild creates/optimizes `_props`; adj-ready expansion retries with SPO only for missing-relation failures.

- [ ] Add failing contract, CTAS, rebuild, optimized-fetch, and missing-table fallback tests.
- [ ] Run the two focused test modules and verify the expected failures.
- [ ] Implement naming, CTAS, rebuild, optimistic fetch, and guarded fallback.
- [ ] Re-run the focused tests and verify they pass.

### Task 3: Lakebase `_props` lifecycle and fallback

**Files:**
- Modify: `src/back/core/graphdb/lakebase/_adjacency_ddl.py`
- Modify: `src/back/core/graphdb/lakebase/LakebaseFlatStore.py`
- Modify: `tests/units/graphdb/test_lakebase_adjacency.py`

**Interfaces:**
- Produces: `props_phy`, `ensure_props_table`, `rebuild_props_data`, `analyze_props_table`
- Behavior: setup creates a subject index; the existing rebuild transaction truncates/inserts `_props`; expansion uses the same optimistic fallback contract as Delta.

- [ ] Add failing identifier, DDL, transaction-order, optimized-fetch, fallback, and error-propagation tests.
- [ ] Run `uv run --frozen pytest -q tests/units/graphdb/test_lakebase_adjacency.py` and verify failure.
- [ ] Implement the Lakebase lifecycle and fetch path.
- [ ] Re-run the focused test and verify it passes.

### Task 4: Documentation, changelog, and verification

**Files:**
- Modify: `docs/graphdb-integration.md`
- Modify: `docs/architecture.md`
- Modify: `docs/user-guide.md`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

- [ ] Document `_props`, its subject clustering/index, shared refresh lifecycle, staleness, and fallback.
- [ ] Append an English changelog section listing every modified file and focused test evidence.
- [ ] Run `uv run --frozen pytest -q -m "not scenario"`.
- [ ] Update the changelog test line with the exact final result.
- [ ] Read lints for all changed Python files and resolve introduced diagnostics.
