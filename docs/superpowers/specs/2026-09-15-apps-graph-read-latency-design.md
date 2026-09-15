# Databricks Apps Lakehouse/RT Inline Reads Design

## Goal

Use the configured Lakehouse/RT warehouse for interactive Delta graph reads
inside Databricks Apps while preserving the existing local Kernel path and the
Build warehouse's Thrift-only invariant.

## Context

Lakehouse/RT requires the Statement Execution API (SEA). Local development
uses `databricks-sql-kernel` successfully, but the deployed App cannot download
Kernel result chunks from `us-west-2.storage.cloud.databricks.com`.

The installed `databricks-sql-connector 4.5.0` passes `use_cloud_fetch` to its
Kernel adapter, but `databricks-sql-kernel 1.0.0` does not consume the flag.
Consequently, `use_cloud_fetch=False` does not stop external result downloads
on that path. The temporary fallback to the Medium Build warehouse restores
correctness but loses RT performance.

## Design

### Runtime routing

- Local query warehouse with SEA enabled: keep `use_kernel=True`.
- Databricks Apps query warehouse with SEA enabled: call SEA directly with
  `format=JSON_ARRAY` and `disposition=INLINE`.
- Build warehouse: keep Thrift and never enable SEA/Kernel.
- Non-RT query warehouses: keep the existing Thrift connector path.

`resolve_delta_warehouse_id` must return the configured RT warehouse in Apps.
`resolve_lakehouse_use_sea` must return the persisted setting in Apps instead
of forcing `False`.

### Inline SEA transport

Add a focused `StatementExecutionWarehouse` service implementing:

- `execute_query(statement, statement_timeout_s=None) -> list[dict]`
- `test_connection() -> tuple[bool, str]`

It posts to `/api/2.0/sql/statements` with the configured RT warehouse ID,
M2M bearer token, `JSON_ARRAY`, `INLINE`, a 24 MiB byte limit, and a maximum
50-second initial wait. If still pending, it polls the statement endpoint until
the configured query timeout. It follows `next_chunk_internal_link` links on
the workspace host only; it never follows `external_link`.

The decoder uses the response manifest to restore common Python scalar types:
integers, floats, decimals, booleans, dates, timestamps, and JSON complex
values. Null stays `None`; unknown types stay strings.

`DatabricksClient` selects this service for `auth.is_app_mode and
auth.use_kernel`; every other runtime continues to use `SQLWarehouse`.

### Size and truncation

SEA INLINE has a 25 MiB response limit. Requests use `byte_limit=25165824`
(24 MiB). A `truncated=true` manifest is rejected with `InfrastructureError`
rather than returning an incomplete graph. Existing graph route caps remain
unchanged; measured BigCustomers expansion output is about 3.3 MiB.

### Errors and cancellation

- SEA terminal failures become `InfrastructureError` with the server error in
  internal detail, not user-facing text.
- Client-side timeout triggers `POST /api/2.0/sql/statements/{id}/cancel`,
  then raises `InfrastructureError`.
- External result links are rejected explicitly.
- Authentication uses `DatabricksAuth.get_oauth_token`; no credentials appear
  in logs.

## Non-goals

- Writes or DDL through RT (Build remains Thrift).
- Results above 24 MiB.
- Lakebase migration.
- Network-policy changes.
- UI changes.

## Success criteria

- Deployed graph-read query history shows warehouse
  `0000000002d8ef5d` (`Reyden Engine`, type `REYDEN`).
- Deployed logs contain no Kernel CloudFetch download attempts.
- Local graph reads still instantiate `SQLWarehouse` with `use_kernel=True`.
- Build operations still use warehouse `d2096aa075ad44a3` with Thrift.
- Focused tests and `uv run --frozen pytest -q -m "not scenario"` pass.
