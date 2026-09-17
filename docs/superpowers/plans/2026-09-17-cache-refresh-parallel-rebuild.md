# Cache Refresh Parallel Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Lakehouse **Refresh cache** wall time by rebuilding independent graph-index companions concurrently while preserving a complete rebuild and Lakebase transaction atomicity.

**Architecture:** `DeltaFlatStore` will express each companion rebuild as one isolated job, execute up to five jobs through a bounded `ThreadPoolExecutor`, and propagate any CTAS failure. The existing `DatabricksClient` is safe for these concurrent calls: the DDL/write rebuild path uses the Databricks SQL connector, whose connection pool is thread-safe — each worker borrows a separate pooled connection. (The Statement Execution API is used only for real-time reads and is not involved in the rebuild DDL workers.) `LakebaseFlatStore` remains unchanged and sequential inside one transaction.

**Tech Stack:** Python 3.12, `concurrent.futures`, Databricks SQL Warehouse, Delta CTAS/OPTIMIZE, pytest.

## Global Constraints

- Refresh remains a complete rebuild; no CDF, incremental update, or unchanged-source shortcut.
- Parallelism applies only to Lakehouse/Databricks; Lakebase remains one sequential transaction.
- Companion SQL and OPTIMIZE/Bloom best-effort semantics remain unchanged.
- At most five companion jobs run concurrently.
- A CTAS failure fails the refresh task; `_props` negative-cache invalidation occurs only after `_props` succeeds.
- Preserve the unrelated uncommitted duplicate-notification fix in `query-databricks-build.js`, its test, and the 2026-09-17 changelog.

---

### Task 1: Isolate one Lakehouse companion rebuild per job

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py:145-192`
- Test: `tests/units/graphdb/delta/test_delta_flat_store.py:100-180`

**Interfaces:**
- Consumes: existing `materialize.build_adj_ctas_sql`, `materialize.build_props_ctas_sql`, `materialize.optimize_table`, and `_rebuild_entity_search_table`.
- Produces: `_rebuild_adjacency_table(relation: str, table: str, direction: str) -> None` and `_rebuild_props_table(relation: str, table: str) -> None`.

- [ ] **Step 1: Add tests for isolated jobs and `_props` invalidation**

Add tests that call the new helpers directly:

```python
def test_rebuild_props_forgets_missing_cache_only_after_success():
    client = MagicMock()
    store = DeltaFlatStore(client, domain=_domain())
    props = store.props_table_id("MyDomain_V1")
    remember_missing_props(props)

    store._rebuild_props_table("cat.sch.graph", props)

    assert known_missing_props(props) is False
    assert any("_props USING DELTA" in c.args[0] for c in client.execute_statement.call_args_list)


def test_rebuild_props_keeps_missing_cache_when_ctas_fails():
    client = MagicMock()
    client.execute_statement.side_effect = RuntimeError("ctas failed")
    store = DeltaFlatStore(client, domain=_domain())
    props = store.props_table_id("MyDomain_V1")
    remember_missing_props(props)

    with pytest.raises(RuntimeError, match="ctas failed"):
        store._rebuild_props_table("cat.sch.graph", props)

    assert known_missing_props(props) is True
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/graphdb/delta/test_delta_flat_store.py \
  -k "rebuild_props_forgets or rebuild_props_keeps"
```

Expected: both tests fail because `_rebuild_props_table` does not exist.

- [ ] **Step 3: Extract the two helpers without changing behavior**

Implement:

```python
def _rebuild_adjacency_table(
    self, relation: str, adj_fqn: str, direction: str
) -> None:
    materialize.drop_relation(self._client, adj_fqn, kind="view")
    self._client.execute_statement(
        materialize.build_adj_ctas_sql(relation, adj_fqn, direction)
    )
    try:
        materialize.optimize_table(self._client, adj_fqn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OPTIMIZE adjacency table failed for %s: %s", adj_fqn, exc)


def _rebuild_props_table(self, relation: str, props: str) -> None:
    materialize.drop_relation(self._client, props, kind="view")
    self._client.execute_statement(materialize.build_props_ctas_sql(relation, props))
    try:
        materialize.optimize_table(self._client, props)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OPTIMIZE property table failed for %s: %s", props, exc)
    forget_missing_props(props)
```

Refactor the existing serial body to call these helpers. Do not add concurrency yet.

- [ ] **Step 4: Run Delta graph-store tests and verify GREEN**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_flat_store.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the isolated-job refactor**

```bash
git add src/back/core/graphdb/delta/DeltaFlatStore.py \
  tests/units/graphdb/delta/test_delta_flat_store.py
git commit -m "refactor(graph): isolate Delta cache rebuild jobs"
```

---

### Task 2: Execute Lakehouse companion jobs concurrently

**Files:**
- Modify: `src/back/core/graphdb/delta/DeltaFlatStore.py:1-192`
- Test: `tests/units/graphdb/delta/test_delta_flat_store.py:100-200`

**Interfaces:**
- Consumes: the isolated helper methods from Task 1.
- Produces: `_run_rebuild_job(name: str, rebuild: Callable[[], None]) -> None`; `rebuild_adjacency` submits a dictionary of zero-argument companion jobs.

- [ ] **Step 1: Add a concurrency regression test**

Use a barrier so the test cannot pass with serial execution:

```python
def test_rebuild_adjacency_runs_all_companion_jobs_concurrently():
    store = DeltaFlatStore(MagicMock(), domain=_domain())
    barrier = threading.Barrier(5, timeout=2)
    calls = []

    def wait_for_peers(*args):
        calls.append(args)
        barrier.wait()

    with patch.object(store, "_rebuild_adjacency_table", side_effect=wait_for_peers), \
         patch.object(store, "_rebuild_entity_search_table", side_effect=wait_for_peers), \
         patch.object(store, "_rebuild_props_table", side_effect=wait_for_peers):
        store.rebuild_adjacency("MyDomain_V1")

    assert len(calls) == 5
```

Import `threading` in the test module.

- [ ] **Step 2: Run the concurrency test and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/graphdb/delta/test_delta_flat_store.py \
  -k "runs_all_companion_jobs_concurrently"
```

Expected: fail with `BrokenBarrierError` because the jobs still run serially.

- [ ] **Step 3: Add the bounded executor and timing wrapper**

Import:

```python
import concurrent.futures
import time
```

Add:

```python
def _run_rebuild_job(self, name: str, rebuild: Callable[[], None]) -> None:
    started = time.perf_counter()
    rebuild()
    logger.info(
        "Graph-index companion rebuilt: %s in %.0f ms",
        name,
        (time.perf_counter() - started) * 1000,
    )
```

Build jobs in `rebuild_adjacency`:

```python
jobs = {
    "adj_out": lambda: self._rebuild_adjacency_table(relation, adj_out, "out"),
    "adj_in": lambda: self._rebuild_adjacency_table(relation, adj_in, "in"),
    "entity_search": lambda: self._rebuild_entity_search_table(relation, search),
    "props": lambda: self._rebuild_props_table(relation, props),
}
if search_asserted and asserted_spo:
    jobs["entity_search_asserted"] = lambda: self._rebuild_entity_search_table(
        asserted_spo, search_asserted
    )
```

Submit and join:

```python
started = time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(jobs))) as executor:
    future_names = {
        executor.submit(self._run_rebuild_job, name, rebuild): name
        for name, rebuild in jobs.items()
    }
    for future in concurrent.futures.as_completed(future_names):
        try:
            future.result()
        except Exception:
            for pending in future_names:
                pending.cancel()
            logger.exception(
                "Graph-index companion rebuild failed: %s", future_names[future]
            )
            raise
logger.info(
    "Graph-index rebuild completed: %s companions in %.0f ms",
    len(jobs),
    (time.perf_counter() - started) * 1000,
)
```

- [ ] **Step 4: Add and verify failure propagation**

Add:

```python
def test_parallel_rebuild_propagates_companion_failure():
    store = DeltaFlatStore(MagicMock(), domain=_domain())

    with patch.object(
        store, "_rebuild_props_table", side_effect=RuntimeError("props failed")
    ):
        with pytest.raises(RuntimeError, match="props failed"):
            store.rebuild_adjacency("MyDomain_V1")
```

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/delta/test_delta_flat_store.py
```

Expected: all tests pass, including the existing best-effort OPTIMIZE tests.

- [ ] **Step 5: Verify Lakebase is still transactionally sequential**

Run:

```bash
uv run --frozen pytest -q tests/units/graphdb/test_lakebase_adjacency.py
```

Expected: all tests pass; no Lakebase production file changed.

- [ ] **Step 6: Commit the parallel execution**

```bash
git add src/back/core/graphdb/delta/DeltaFlatStore.py \
  tests/units/graphdb/delta/test_delta_flat_store.py
git commit -m "perf(graph): rebuild Delta cache companions in parallel"
```

---

### Task 3: Document, verify, and benchmark

**Files:**
- Modify: `docs/optimizations.md:35-40`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-17.log`
- Test: full non-scenario suite

**Interfaces:**
- Consumes: per-companion and total timing logs emitted by Task 2.
- Produces: user-facing operational documentation and measured before/after evidence.

- [ ] **Step 1: Update optimization documentation**

Document that Lakehouse rebuilds full companion snapshots concurrently, that Lakebase preserves one serial transaction, and that timings are logged per companion and in total. Explicitly state that this is not incremental refresh.

- [ ] **Step 2: Run static diagnostics**

Run IDE diagnostics on:

- `src/back/core/graphdb/delta/DeltaFlatStore.py`
- `tests/units/graphdb/delta/test_delta_flat_store.py`

Expected: no new diagnostics.

- [ ] **Step 3: Run the mandatory suite**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: zero failures.

- [ ] **Step 4: Benchmark one real Lakehouse refresh**

Trigger **Refresh cache** on the same Lakehouse domain used for the prior baseline. Record:

- total task wall time;
- each companion elapsed time from logs;
- whether all five companions completed;
- whether search and expansion still return results.

Compare against the latest serial baseline. If warehouse admission queues all five statements and no wall-time gain appears, retain correctness but reduce `max_workers` to 3 and repeat once.

- [ ] **Step 5: Append the changelog**

Add an English section with context, numbered changes, modified files, mandatory-suite result, and live benchmark result to:

`changelogs/v0.9.0/benoitcayladbx_2026-09-17.log`

- [ ] **Step 6: Commit docs and changelog**

```bash
git add docs/optimizations.md \
  changelogs/v0.9.0/benoitcayladbx_2026-09-17.log
git commit -m "docs(graph): document parallel cache refresh"
```
