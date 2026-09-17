# External Cache Refresh API Design

## Context

OntoBricks already exposes a **Refresh cache** action in the UI. Internally,
`POST /dtwin/adjacency/refresh` starts an asynchronous task that rebuilds the
graph companion indexes used by search and traversal. External integrations
cannot currently trigger the same operation.

The external API must expose that existing operation without rebuilding the
knowledge graph itself and without requiring or mutating the browser's active
domain session.

## API Contract

### Start a refresh

`POST /api/v1/digitaltwin/cache/refresh`

Optional query parameters follow the existing external `/build` contract:

- `domain_name` (legacy alias: `project_name`)
- `domain_version` (legacy alias: `project_version`)
- `registry_catalog`
- `registry_schema`
- `registry_volume`

When `domain_name` is omitted, the current session domain is used, matching the
other external digital-twin endpoints.

Successful response:

```json
{
  "success": true,
  "task_id": "task-id",
  "message": "Cache refresh started"
}
```

### Poll a refresh

`GET /api/v1/digitaltwin/cache/refresh/{task_id}`

The response uses the existing `TaskProgressResponse` contract:

- `status`: `pending`, `running`, `completed`, `failed`, or `cancelled`
- `progress`: integer from 0 to 100
- `message`, `result`, and `error` when available

An unknown task identifier returns the existing not-found response.

## Architecture and Data Flow

The external router resolves the requested domain through
`DigitalTwin.resolve_domain`, including version and registry overrides. It then
resolves the configured graph backend with the same backend-selection logic as
the internal route.

For Lakebase and Databricks/Lakehouse backends, the route:

1. Creates a `DomainSnapshot`.
2. Creates an `adjacency_refresh` task with the existing task manager.
3. Starts a daemon worker thread.
4. Calls `DigitalTwin.run_adjacency_refresh_task`.
5. Returns the task identifier immediately.

The shared worker opens the graph store and invokes
`store.rebuild_adjacency(graph_name)`. This is exactly the operation behind the
UI's **Refresh cache** button, so all graph companion structures rebuilt by the
backend remain aligned with UI behavior.

The route does not update the browser session and does not duplicate the
backend rebuild implementation.

## Validation and Errors

- Lakebase and Databricks/Lakehouse are supported.
- Neo4j and domains without a graph backend return a validation error because
  they do not use these companion indexes.
- Unknown or unsupported backend names return a validation error.
- Domain and registry resolution errors use the existing external API error
  mapping.
- Runtime rebuild failures are recorded on the asynchronous task and returned
  by the polling endpoint; the start request remains successful once the task
  has been created.

## Testing

Route-level tests will verify:

1. A Lakebase request resolves the requested domain/version, starts an
   `adjacency_refresh` task, and returns its identifier.
2. A Databricks/Lakehouse request selects the write-capable backend through the
   shared worker.
3. Neo4j, missing, and unknown backends are rejected before task creation.
4. Registry overrides are forwarded to domain resolution.
5. The polling endpoint returns existing task progress and returns not found
   for an unknown task.
6. The external OpenAPI document includes both routes and their response
   schemas.

Tests will be written first and observed failing before production code is
added.

## Scope

This change exposes the graph-index cache refresh through external REST only.
It does not add an MCP tool, refresh process-memory caches, rebuild graph data,
or change the internal UI endpoint.
