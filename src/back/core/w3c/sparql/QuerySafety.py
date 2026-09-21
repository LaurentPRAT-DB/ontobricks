"""Shared safety checks for user-supplied SPARQL."""

from __future__ import annotations

import re

from back.core.errors import ValidationError

_MUTATING_SPARQL = re.compile(
    r"\b(DROP|DELETE|INSERT|CREATE|CLEAR|LOAD|COPY|MOVE|ADD)\b",
    re.IGNORECASE,
)


def require_read_only_sparql(query: str) -> str:
    """Return stripped SPARQL text or reject an empty/mutating query."""
    text = query.strip() if isinstance(query, str) else ""
    if not text:
        raise ValidationError("No SPARQL query provided")
    if _MUTATING_SPARQL.search(text):
        raise ValidationError(
            "SPARQL execution is read-only; mutating operations are not allowed."
        )
    return text
