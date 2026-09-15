# OBX Import Rename Design

Date: 2026-09-15  
Status: approved for implementation planning  
Scope: Registry → Browse → Import `.obx` only. No clone button on domain rows.

## Problem

Users want a copy of a domain under a new name (latest-or-selected versions via Export, then Import). The import preview already has a Rename action, but it is **disabled when the incoming domain does not exist**. A new domain can only land on its original folder name. Rename also writes the source JSON as-is, so the new folder still carries the old `info.name`, old `base_uri`, and graph build stamps (`last_build`), which point at data that was never copied.

## Goal

On Import preview, every incoming domain has an always-visible **Import as** name. Changing that name creates a **new registry folder**. The copied versions keep ontology and mappings, get a new display name and auto base URI (same formula as creating a domain), and do **not** inherit graph runtime. Overwrite of an existing folder is unchanged.

## Out of scope

- A Clone button (or any new action) on Registry domain rows.
- Copying Unity Catalog document binaries, collaboration threads, permissions, schedules, or materialized graph tables.
- Changing Export.
- Letting non-admins import (route stays admin-only).

## UX

Import step 2 (`#importObxModal` / `renderImportObxPreview` in `src/front/static/registry/js/registry.js`):

| Incoming state | Status badge | Action radios | Import as field |
|----------------|--------------|---------------|-----------------|
| Folder free | New | None required (implicit create) | Prefill a CamelCase display name: `info.name` when it matches `[A-Z][A-Za-z0-9]*`, otherwise TitleCase the folder (`claims` → `Claims`) |
| Folder exists | Exists | Skip (default) / Overwrite | Prefill `suggested_new_name` (CamelCase, free folder) |

- Remove the **Rename** radio. Rename is implied when the sanitized Import as folder differs from the incoming folder.
- **Import as** is always visible: `form-control form-control-sm`, required, `maxlength="64"`, same CamelCase pattern as New Domain (`[A-Z][A-Za-z0-9]*`). Use `invalid-feedback`; do not use `prompt()` / `alert()`.
- Domain column still shows the source folder (and `original_name` when it differs).
- Confirm Import:
  - Invalid CamelCase or empty name: stay on the modal, mark the field invalid, Notification Center warning.
  - Sanitized folder equals source and source is new → `action: "overwrite"` (create).
  - Sanitized folder equals source and source exists → `action` from Skip/Overwrite radios (default skip).
  - Sanitized folder differs → `action: "rename"`, `new_name` = typed display name.
- Existing overwrite confirmation dialog is unchanged.
- Success: existing success notification, close modal, `loadRegistryDomains()`. Do not load the imported domain into the session.

No new modal partial. Keep Bootstrap structure in `_import_obx_modal.html`. Add a column or label **Import as** in the table header. Do not add inline `style=""`; use `d-none` / existing registry CSS if a column width is needed (`--db-*` tokens only).

## Backend

Keep `POST /settings/registry/import` and `POST /settings/registry/import/preview`. No new route.

### Preview (`preview_obx_import_result`)

- Continue returning `name` (sanitized source folder), `original_name`, `exists`, `conflicting_versions`, `incoming_versions`, `info`.
- `suggested_new_name` must be a **CamelCase display name** whose `sanitize_domain_folder` result is free. Do not suggest `claims_imported`. Example: source display `Claims` → `ClaimsImported`, then `ClaimsImported2`, …
- Include `display_name` from `info.name` (fallback: source folder) so the UI can prefill without guessing.

### Import (`import_registry_obx_result`)

Decision handling stays skip / overwrite / rename. Before `write_version` on **rename only** (new target folder):

1. Sanitize `new_name` with `sanitize_domain_folder`. Empty → `ValidationError`. Target exists → same skip-with-error as today (`skipped_rename_conflict`).
2. For each version document (tolerate top-level `info` / `ontology` and nested `versions[ver]`):
   - Set `info.name` (and nested equivalents) to the **typed display name** (trimmed), not the folder slug.
   - Set `ontology.base_uri` and `info` URI fields using the same auto formula as `Domain._resolve_base_uri`: `{default_base}/{SafeName}#` with `base_uri_auto` true. `SafeName` is the display name with `[^a-zA-Z0-9_-]` → `_`.
   - Clear graph runtime: `info.last_build` / top-level `last_build` → `""`; `triplestore.stats` → `{}`. Do not copy or create graph tables. Leave `graph_backend` and mappings intact.
3. Write versions with existing `RegistryService.write_version`. Version numbers stay those in the OBX (Export “latest” ⇒ one version, same number as source).
4. Lifecycle `status` is copied as in the file. Cleared `last_build` makes `has_graph` false until rebuild.

Overwrite must **not** rewrite name, URI, or `last_build`.

Extract a small helper on `SettingsService` (e.g. `_prepare_renamed_version_doc`) rather than duplicating URI logic in the import loop. Reuse `resolve_default_base_uri` + the same sanitization as `Domain._resolve_base_uri` (prefer a shared function if extracting from `Domain` is a one-line move; do not invent a second URI scheme).

Errors: `ValidationError` for empty or unsanitizable `new_name`. If the rename target folder already exists, keep today’s per-row skip (`skipped_rename_conflict` + error string) so a multi-domain file can still import the other rows. The UI must block submit when the typed folder collides with another incoming row in the same preview.

## Permissions

Unchanged: `POST /settings/registry/import` is admin-only. Preview stays available to whoever can open Import today.

## Testing

- `tests/units/settings/test_settings_obx.py`:
  - Rename of a **new** source folder writes to the target folder (today’s `test_rename_writes_to_new_folder` plus a case where source `exists` is false).
  - Renamed docs have new `info.name`, auto `base_uri` containing the new safe name, empty `last_build`, empty triplestore stats; ontology classes / assignment mappings preserved.
  - Overwrite does not rewrite name/URI/`last_build`.
  - `_suggest_rename` / preview `suggested_new_name` is CamelCase and folder-free.
- Front contract (new or extend an existing registry JS test): Import as input is rendered for new domains; no `Rename` radio; `disabled` is not applied to rename-only; confirm payload uses `rename` when the name changed.
- Permission tests: no change unless a new path is added (none planned).

## Success criteria

1. User can export a domain (e.g. latest version) and import it as a different CamelCase name without the source folder existing as a conflict.
2. The new domain appears in Registry Browse with the new name, new auto URI, copied ontology/mappings, no graph until rebuild.
3. Importing onto an existing folder via Overwrite still replaces versions without URI rewrite.
4. `uv run --frozen pytest -q -m "not scenario"` passes.
