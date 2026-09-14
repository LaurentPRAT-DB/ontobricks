"""Shared adjacency / typed-neighbor SQL for Lakehouse (Spark) and Lakebase (Postgres)."""

from __future__ import annotations

from typing import Callable, Literal

from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL

SqlFlavor = Literal["spark", "postgres"]
Escape = Callable[[str], str]

__all__ = [
    "Escape",
    "SqlFlavor",
    "expand_and_fetch_sql",
    "expand_entity_neighbors_sql",
    "typed_in_select",
    "typed_out_select",
]


def typed_out_select(spo: str) -> str:
    return (
        f"SELECT DISTINCT t.subject AS src, t.predicate, t.object AS dst "
        f"FROM {spo} t "
        f"INNER JOIN {spo} typed "
        f"ON typed.subject = t.object AND typed.predicate = '{RDF_TYPE}' "
        f"WHERE t.object LIKE 'http%' "
        f"AND t.predicate != '{RDF_TYPE}' "
        f"AND t.predicate != '{RDFS_LABEL}'"
    )


def typed_in_select(spo: str) -> str:
    return (
        f"SELECT DISTINCT t.object AS dst, t.predicate, t.subject AS src "
        f"FROM {spo} t "
        f"INNER JOIN {spo} typed "
        f"ON typed.subject = t.subject AND typed.predicate = '{RDF_TYPE}' "
        f"WHERE t.object LIKE 'http%' "
        f"AND t.predicate != '{RDF_TYPE}' "
        f"AND t.predicate != '{RDFS_LABEL}'"
    )


def expand_entity_neighbors_sql(
    adj_out: str, adj_in: str, uris: list[str], escape: Escape
) -> str:
    if not uris:
        raise ValueError("At least one URI is required")
    in_clause = ", ".join(f"'{escape(u)}'" for u in uris)
    return (
        f"SELECT DISTINCT entity FROM ("
        f"SELECT dst AS entity FROM {adj_out} WHERE src IN ({in_clause}) "
        f"UNION ALL "
        f"SELECT src AS entity FROM {adj_in} WHERE dst IN ({in_clause})"
        f") n"
    )


def _anti_join(flavor: SqlFlavor, level: int) -> str:
    visited = f"visited_{level}"
    if flavor == "spark":
        return (
            f"LEFT ANTI JOIN {visited} "
            f"ON {visited}.entity = candidate.entity"
        )
    return (
        f"WHERE NOT EXISTS ("
        f"SELECT 1 FROM {visited} v WHERE v.entity = candidate.entity)"
    )


def expand_and_fetch_sql(
    *,
    flavor: SqlFlavor,
    adj_out: str,
    adj_in: str,
    spo: str,
    selected_uris: list[str],
    depth: int,
    max_entities: int,
    max_triples: int,
    escape: Escape,
) -> str:
    if not selected_uris:
        raise ValueError("At least one selected URI is required")
    depth = max(0, int(depth))
    max_entities = max(1, int(max_entities))
    max_triples = max(1, int(max_triples))
    seed_values = ", ".join(
        f"('{escape(uri)}')" for uri in dict.fromkeys(selected_uris)
    )
    ctes = [f"level_0(entity) AS (VALUES {seed_values})"]
    for level in range(1, depth + 1):
        previous = f"level_{level - 1}"
        visited_union = " UNION ALL ".join(
            f"SELECT entity FROM level_{prior}" for prior in range(level)
        )
        anti = _anti_join(flavor, level)
        ctes.extend(
            [
                f"visited_{level} AS ({visited_union})",
                (
                    f"level_{level}_candidates AS ("
                    f"SELECT t.dst AS entity FROM {adj_out} t "
                    f"JOIN {previous} frontier ON t.src = frontier.entity "
                    f"UNION ALL "
                    f"SELECT t.src AS entity FROM {adj_in} t "
                    f"JOIN {previous} frontier ON t.dst = frontier.entity)"
                ),
                (
                    f"level_{level} AS ("
                    f"SELECT DISTINCT candidate.entity "
                    f"FROM level_{level}_candidates candidate "
                    f"{anti} "
                    f"LIMIT {max_entities})"
                ),
            ]
        )
    levels = " UNION ALL ".join(
        f"SELECT entity FROM level_{level}" for level in range(depth + 1)
    )
    ctes.extend(
        [
            (
                "entity_probe AS ("
                f"SELECT DISTINCT entity FROM ({levels}) discovered "
                f"LIMIT {max_entities + 1})"
            ),
            (
                "entities AS ("
                f"SELECT entity FROM entity_probe LIMIT {max_entities})"
            ),
            (
                "entity_stats AS ("
                "SELECT COUNT(*) AS _ob_expanded_count FROM entity_probe)"
            ),
        ]
    )
    return (
        "WITH "
        + ", ".join(ctes)
        + " "
        + "SELECT triples.subject, triples.predicate, triples.object, "
        + "stats._ob_expanded_count "
        + f"FROM {spo} triples "
        + "JOIN entities ON entities.entity = triples.subject "
        + "CROSS JOIN entity_stats stats "
        + f"LIMIT {max_triples + 1}"
    )
