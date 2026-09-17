# AI Gateway + Serving LLM Picker — Design

**Date:** 2026-09-17  
**Status:** Approved for implementation planning  
**Target:** Domain LLM selection and all in-app agent / SQL-wizard calls

## Context

The Domain LLM control lists Databricks **Model Serving** endpoints only
(`GET /api/2.0/serving-endpoints`). Agents and SQL Wizard POST
`/serving-endpoints/{name}/invocations`.

Unity **AI Gateway model services** (UI: AI Gateway → Create Model) live in
Unity Catalog as `catalog.schema.service`. They are listed with
`GET /api/2.1/unity-catalog/model-services?parent=schemas/{catalog}.{schema}`
and invoked at `/ai-gateway/mlflow/v1/chat/completions` with
`"model": "<fqn>"`. Custom services such as `….monclaudesonnetamoi` never
appear in the Serving list.

Existing domains keep a single string `domain.info.llm_endpoint` (Serving
leaf name). That must keep working.

## Decision

Keep **both** catalogs in one picker. **AI Gateway first**, Serving as
legacy. Persist kind explicitly. Branch invocation in one helper.

Do **not** drop Serving, do **not** add a workspace-level Serving vs Gateway
toggle, do **not** hide `system.ai.*`.

## Picker UX

Replace the Domain Information LLM `<select>` with:

- a **read-only** display of the current name (FQN or Serving leaf);
- **Browse** (opens the picker modal; fetch lists on open).
- Refresh lives **inside** the modal (re-fetch both groups).

The picker is a Bootstrap modal (same visual language as
`uc_file_browser_modal.html`: `--db-*` tokens, no inline CSS/JS in the
template).

Layout:

1. Search input (client-side filter on `name` and optional `comment`).
2. Group **AI Gateway** (first): every model service the list API returns
   for schemas the workspace token can see, including `system.ai.*` and
   user-created FQNs.
3. Group **Model Serving (legacy)**: current Serving endpoint list.
4. Click a row to select, close the modal, update the display.

New Domain (`showNewDomainDialog` in `utils.js`) uses the **same** picker
modal (stacked on the create dialog). It must persist `llm_endpoint` and
`llm_endpoint_kind`, not a bare `<select>` of Serving names.

A hidden input remains the save source (same pattern as graph-backend
cards). Mapping UI that only displays `info.llm_endpoint` keeps showing
the stored string (FQN or leaf).

## Listing

Extend `GET /mapping/wizard/llm-endpoints` to return:

```json
{
  "success": true,
  "endpoints": [
    {
      "name": "system.ai.claude-sonnet-4-5",
      "kind": "ai_gateway",
      "comment": ""
    },
    {
      "name": "databricks-claude-sonnet-4-5",
      "kind": "serving",
      "state": "READY"
    }
  ]
}
```

Gateway:

- Reuse `DatabricksClient.get_catalogs` / `get_schemas`.
- For each schema, paginate
  `GET /api/2.1/unity-catalog/model-services?parent=schemas/{catalog}.{schema}`
  (`page_size` 100, follow `next_page_token`).
- Skip catalogs/schemas that 403/404; log and continue.
- The list API can also return services visible through `READ_METADATA` or
  `MANAGE` alone. Resolve the authenticated principal with the existing SCIM
  `/Me` helper, call Unity Catalog effective permissions for each candidate,
  and retain only owner services or services whose effective privileges
  include `EXECUTE`.
- If the effective-permissions check itself is forbidden, exclude that
  candidate rather than presenting a model that may fail when invoked.
- `name` in the payload is the **three-part FQN** (`catalog.schema.service`),
  not the `model-services/…` resource path.

Serving: keep `SQLWizardService.get_model_serving_endpoints`; tag
`kind: serving`.

If Gateway listing fails entirely, still return Serving endpoints. Empty
Gateway group is valid.

## Persistence

Add `domain.info.llm_endpoint_kind`: `"ai_gateway"` | `"serving"` | `""`.

- `llm_endpoint` unchanged: FQN for Gateway, Serving leaf for legacy.
- Save/load through `Domain.update` / `DomainSession` / empty-domain
  defaults, same as `llm_endpoint`.
- Domain save payload from the Information tab and New Domain includes
  both fields.

Missing kind on old domains: if `llm_endpoint` matches
`catalog.schema.service` (exactly two dots, no `/`), treat as
`ai_gateway`; otherwise `serving`. New picks always write kind.

## Invocation

All LLM HTTP goes through one branch in `call_serving_endpoint`
(`src/agents/engine_base.py`). Callers keep passing `endpoint_name`;
kind is resolved from domain info when the caller has it, else from the
legacy name heuristic.

| Kind | URL | Body |
|------|-----|------|
| `serving` | `{host}/serving-endpoints/{name}/invocations` | `messages`, `max_tokens`, optional `temperature`, optional `tools` (unchanged) |
| `ai_gateway` | `{host}/ai-gateway/mlflow/v1/chat/completions` | same fields **plus** `"model": "<fqn>"` |

Auth: existing workspace bearer token. Gateway additionally requires
`USE CATALOG`, `USE SCHEMA`, and `EXECUTE` on the service (enforced by
Databricks; we surface HTTP errors).

Temperature-ban retry (`_UNSUPPORTED_PARAMS`) stays keyed by endpoint
name (FQN or leaf).

`SQLWizardService.call_llm_endpoint` uses the **same** URL/body rule so
text-to-SQL works on a Gateway FQN stored on the domain.

`require_serving_llm` keeps requiring a non-empty `llm_endpoint`; it does
not reject FQNs.

## Auto-discover

Graph Chat `_auto_discover_llm_endpoint` only runs when
`llm_endpoint` is empty. Preference:

1. First listed Gateway service whose name starts with `system.ai.`
2. Any other listed Gateway service
3. Current Serving rule: first READY `databricks-*`, then any READY Serving

Never overwrite a saved `llm_endpoint`. Auto-discover should set kind
in-memory for the call (`ai_gateway` vs `serving`); persist only if the
existing Graph Chat path already writes the discovered name.

## Errors

- Databricks not configured: picker shows empty / load error (same as
  today for Serving).
- Gateway 403 on a schema: skip that schema.
- Invoke 401/403/404: existing agent/SQL error paths; message should
  mention Gateway vs Serving when kind is `ai_gateway`.
- Invoke 400 temperature: existing strip-and-retry.
- Nested New Domain + picker: Cancel on the picker does not close New
  Domain.

## Testing

- Unit: listing merges Gateway FQNs and Serving leaves; Gateway failure
  still returns Serving.
- Unit: `call_serving_endpoint` / SQL Wizard POST Gateway URL + `model`
  when kind is `ai_gateway`; Serving URL when kind is `serving` or
  heuristic says leaf name.
- Unit: missing kind + two-dot FQN → Gateway; `databricks-…` → Serving.
- Unit: auto-discover prefers `system.ai.*` over Serving when both exist.
- Unit: Domain save/load round-trips `llm_endpoint_kind`.
- Front: picker groups + search filter (jsdom or existing front unit
  pattern if present); otherwise a small template/JS contract test that
  the modal ids and hidden fields exist.

No agent prompt or tool-schema change. Eval-gate SPEC/dataset is **not**
required for this transport-only change. MLflow `@trace_llm` stays on
`call_serving_endpoint`.

## Scope

In: picker modal + search, combined list API, `llm_endpoint_kind`,
Gateway chat-completions path, SQL Wizard parity, auto-discover order,
docs help text on the LLM tab.

Out: dropping Serving, workspace toggle, model-provider-service raw
paths (`/ai-gateway/openai/v1`, Anthropic, Gemini), provisioning new
Gateway services from OntoBricks, extra EXECUTE grant checks beyond
the list API.
