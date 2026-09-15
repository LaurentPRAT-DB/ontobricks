"""Shared entity-directory SQL for Lakehouse and Lakebase."""

from __future__ import annotations

from typing import Callable

from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL

Escape = Callable[[str], str]

__all__ = [
    "Escape",
    "entity_search_select",
    "is_asserted_only_relation",
    "preview_select_sql",
    "sort_preview_rows",
]


def is_asserted_only_relation(table_name: str) -> bool:
    """Whether *table_name* excludes the inferred companion."""
    leaf = table_name.rsplit(".", 1)[-1]
    return leaf.endswith("_data") or leaf.endswith("_sync")


def entity_search_select(spo: str) -> str:
    """Return one deterministic type/label row per typed subject."""
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
    """Build a bounded Preview lookup against an entity-search table."""
    if int(limit) <= 0:
        raise ValueError("limit must be greater than zero")

    clauses: list[str] = []
    if entity_type:
        clauses.append(f"type_uri = '{escape(entity_type)}'")

    safe_value = escape(value.lower()) if value else ""
    search_label = field in ("label", "any")
    search_id = field in ("id", "any")

    def _match(column: str) -> str:
        if match_type == "exact":
            return f"{column} = '{safe_value}'"
        if match_type == "starts":
            return f"{column} LIKE '{safe_value}%'"
        if match_type == "ends":
            return f"{column} LIKE '%{safe_value}'"
        return f"{column} LIKE '%{safe_value}%'"

    if value:
        text_clauses = []
        if search_label:
            text_clauses.append(_match("label_lc"))
        if search_id:
            text_clauses.append(_match("uri_lc"))
        if text_clauses:
            clauses.append(f"({' OR '.join(text_clauses)})")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return (
        f"SELECT uri, type_uri, label FROM {search_table}{where} "
        f"LIMIT {int(limit)}"
    )


def sort_preview_rows(rows: list[dict]) -> list[dict]:
    """Stable UI order after a warehouse LIMIT (no ORDER BY in SQL)."""
    return sorted(
        rows,
        key=lambda r: (r.get("type") or "", r.get("label") or "", r.get("uri") or ""),
    )
