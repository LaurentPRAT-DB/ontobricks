"""Shared property-companion SQL for Lakehouse and Lakebase."""

from __future__ import annotations

from back.core.graphdb.constants import RDF_TYPE

__all__ = ["is_missing_props_error", "props_select"]


def is_missing_props_error(exc: Exception, props_table: str) -> bool:
    """Whether *exc* reports that the attempted property table is missing."""
    message = str(exc).lower()
    return props_table.lower() in message and any(
        marker in message
        for marker in (
            "table_or_view_not_found",
            "does not exist",
            "undefined table",
        )
    )


def props_select(spo: str) -> str:
    """Return all outgoing triples whose subject is a typed instance."""
    return (
        f"SELECT t.subject, t.predicate, t.object "
        f"FROM {spo} t "
        f"INNER JOIN ("
        f"SELECT DISTINCT subject FROM {spo} "
        f"WHERE predicate = '{RDF_TYPE}'"
        f") typed ON typed.subject = t.subject"
    )
