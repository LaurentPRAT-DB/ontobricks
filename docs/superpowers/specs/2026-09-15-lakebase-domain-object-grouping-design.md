# Lakebase Domain Object Grouping Design

**Date:** 2026-09-15  
**Status:** Approved for implementation planning

## Context

Settings → Lakebase → Objects already groups the reader view, `_sync` bulk
table, and `__app` writable companion under one domain/version card. The
domain-owned `_adj_in`, `_adj_out`, and `_entity_search` index tables are
currently shown as independent cards because the grouping helper does not
recognize their suffixes.

## Decision

Extend the existing client-side object-name normalization so these physical
objects resolve to the same domain/version base:

| Physical name | Group base |
|---------------|------------|
| `<base>` | `<base>` |
| `<base>_sync` | `<base>` |
| `<base>__app` | `<base>` |
| `<base>_adj_in` | `<base>` |
| `<base>_adj_out` | `<base>` |
| `<base>_entity_search` | `<base>` |

The Lakebase objects API remains unchanged. Grouping stays in the frontend,
next to the current `_sync` and `__app` normalization, because this is a
presentation concern and avoids adding derived API fields.

## UI Behavior

- One collapsed card represents each domain/version base.
- The card contains its reader view, bulk table, writable companion, and all
  available graph-index tables.
- The object-count badge includes the graph-index tables.
- Individual Drop actions remain available for every object.
- **Delete all objects for this domain** includes `_adj_in`, `_adj_out`, and
  `_entity_search`.
- Existing view-first deletion ordering is preserved so dependent views are
  removed before tables.
- Unrelated objects whose names do not end with a recognized suffix remain in
  their own cards.

## Error Handling

No API or deletion error behavior changes. Individual and grouped deletion
continue using the existing confirmation modal, progress state, sequential
drop requests, and notification-center messages.

If only some index tables exist, the card displays and deletes only those
returned by the API.

## Testing

Add a frontend contract covering:

1. All three index suffixes normalize to the owning domain/version base.
2. The domain card count includes the index tables.
3. Group deletion receives the index tables as domain-owned objects.
4. An unrelated table name is not incorrectly regrouped.

Run the focused frontend tests, then:

```text
uv run --frozen pytest -q -m "not scenario"
```

## Out of Scope

- Changing the Lakebase objects API response.
- Renaming physical Postgres tables.
- Changing index creation or refresh behavior.
- Changing Lakehouse object grouping.
- Changing Neo4j behavior.
