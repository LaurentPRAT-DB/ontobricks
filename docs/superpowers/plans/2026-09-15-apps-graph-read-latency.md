# Databricks Apps Lakehouse/RT Inline Reads Plan

> Implement with TDD. The approved design is
> `docs/superpowers/specs/2026-09-15-apps-graph-read-latency-design.md`.

## Task 1: Define the inline SEA transport contract

Files:
- Create `tests/units/core/test_statement_execution_warehouse.py`
- Create `src/back/core/databricks/StatementExecutionWarehouse.py`

Steps:
1. Add failing tests for an INLINE request and manifest-based row decoding.
2. Verify RED.
3. Implement the smallest transport that passes.
4. Add failing tests for pending-state polling and internal chunk pagination.
5. Verify RED, implement, and verify GREEN.
6. Add failing tests for failure, truncation, external links, and timeout
   cancellation.
7. Verify RED, implement, and verify GREEN.

## Task 2: Select the transport in Databricks Apps

Files:
- Modify `tests/units/core/test_databricks_client.py`
- Modify `src/back/core/databricks/DatabricksClient.py`

Steps:
1. Add failing tests proving Apps+SEA selects
   `StatementExecutionWarehouse`, while local+SEA and Thrift select
   `SQLWarehouse`.
2. Verify RED.
3. Add explicit service selection in `DatabricksClient`.
4. Verify GREEN.

## Task 3: Restore the configured RT warehouse in Apps

Files:
- Modify `tests/units/settings/test_delta_warehouse_config.py`
- Modify `src/back/core/helpers/DatabricksHelpers.py`

Steps:
1. Change Apps expectations to the configured RT warehouse and SEA enabled.
2. Verify RED.
3. Remove the temporary Apps-to-Build fallback.
4. Verify GREEN.

## Task 4: Documentation and verification

Files:
- Modify `docs/user-guide.md`
- Modify `docs/architecture.md`
- Modify `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

Steps:
1. Document Apps INLINE SEA, local Kernel, and Build Thrift routing.
2. Run focused unit tests.
3. Run lints on modified Python files.
4. Run `uv run --frozen pytest -q -m "not scenario"`.
5. Record the exact result in the changelog.
