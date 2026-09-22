"""Contract tests for GraphDBBackend adjacency defaults and SPO fallback."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from unittest.mock import patch

import pytest

from back.core.graphdb.GraphDBBackend import GraphDBBackend
from back.core.graphdb.constants import RDF_TYPE


class FakeStore(GraphDBBackend):
    supports_adjacency = True
    supports_entity_search = True

    def __init__(self) -> None:
        self.queries: List[str] = []
        self._adj_ready = False
        self._search_ready = False
        self._search_asserted_ready = False

    def sql_flavor(self) -> Optional[Literal["spark", "postgres"]]:
        return "postgres"

    def adjacency_table_ids(self, table_name: str) -> tuple[str, str]:
        return ("g_adj_out", "g_adj_in")

    def entity_search_table_id(self, table_name: str) -> str:
        return "g_entity_search"

    def entity_search_asserted_table_id(self, table_name: str) -> str:
        return "g_entity_search_asserted"

    def props_table_id(self, table_name: str) -> str:
        return "g_props"

    def table_exists(self, table_name: str) -> bool:
        if table_name == "g_entity_search":
            return self._search_ready
        if table_name == "g_entity_search_asserted":
            return self._search_asserted_ready
        return self._adj_ready and table_name in {"g_adj_out", "g_adj_in"}

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        self.queries.append(query)
        return []

    def create_table(self, table_name: str) -> None:
        raise NotImplementedError

    def drop_table(self, table_name: str) -> None:
        raise NotImplementedError

    def insert_triples(
        self,
        table_name: str,
        triples: List[Dict[str, str]],
        batch_size: int = 500,
        on_progress=None,
    ) -> int:
        raise NotImplementedError

    def query_triples(self, table_name: str) -> List[Dict[str, str]]:
        raise NotImplementedError

    def count_triples(self, table_name: str) -> int:
        raise NotImplementedError

    def get_status(self, table_name: str) -> Dict[str, Any]:
        raise NotImplementedError

    def get_connection(self) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


def test_expand_neighbors_spo_fallback_when_adj_not_ready():
    store = FakeStore()
    store._adj_ready = False
    store.expand_entity_neighbors("g", {"http://ex/seed"})
    assert len(store.queries) == 1
    sql = store.queries[0]
    assert f"predicate = '{RDF_TYPE}'" in sql
    assert "object LIKE 'http%'" in sql


def test_expand_neighbors_uses_adj_tables_when_ready():
    store = FakeStore()
    store._adj_ready = True
    store.expand_entity_neighbors("g", {"http://ex/seed"})
    assert len(store.queries) == 1
    sql = store.queries[0]
    assert "g_adj_out" in sql
    assert "g_adj_in" in sql
    assert "object LIKE 'http%'" not in sql


def test_adjacency_ready_requires_support_and_tables():
    store = FakeStore()
    assert store.adjacency_ready("g") is False
    store._adj_ready = True
    assert store.adjacency_ready("g") is True

    no_support = FakeStore()
    no_support.supports_adjacency = False
    no_support._adj_ready = True
    assert no_support.adjacency_ready("g") is False


def test_entity_search_ready_requires_support_table_and_union_relation():
    store = FakeStore()
    store._search_ready = True
    assert store.entity_search_ready("g") is True
    assert store.entity_search_ready("g_data") is False
    assert store.entity_search_ready("g_sync") is False
    store._search_asserted_ready = True
    assert store.entity_search_ready("g_data") is True
    assert store.entity_search_ready("g_sync") is True
    store.supports_entity_search = False
    assert store.entity_search_ready("g") is False


def test_find_preview_seeds_uses_entity_search_when_ready():
    store = FakeStore()
    with patch.object(store, "table_exists") as exists:
        store.find_preview_seeds("g", value="ada", limit=501)
    exists.assert_not_called()
    assert len(store.queries) == 1
    assert "g_entity_search" in store.queries[0]
    assert "LIKE '%ada%'" in store.queries[0]


def test_find_preview_seeds_falls_back_when_entity_table_is_missing():
    store = FakeStore()
    with patch.object(
        store,
        "execute_query",
        side_effect=[RuntimeError("TABLE_OR_VIEW_NOT_FOUND"), []],
    ) as execute:
        assert store.find_preview_seeds("g", value="ada", limit=2) == []
    assert execute.call_count == 2
    assert "g_entity_search" in execute.call_args_list[0].args[0]
    assert "g_entity_search" not in execute.call_args_list[1].args[0]


def test_find_preview_seeds_uses_asserted_index_for_asserted_relation():
    store = FakeStore()
    store.find_preview_seeds("g_data", value="ada", limit=2)
    assert len(store.queries) == 1
    assert "g_entity_search_asserted" in store.queries[0]
    assert "FROM g_entity_search " not in store.queries[0]
    assert "ORDER BY" not in store.queries[0]


def test_find_preview_seeds_sorts_index_rows():
    store = FakeStore()
    rows = [
        {"uri": "u2", "type_uri": "T", "label": "b"},
        {"uri": "u1", "type_uri": "T", "label": "a"},
        {"uri": "u3", "type_uri": "A", "label": "z"},
    ]
    with patch.object(store, "execute_query", return_value=rows):
        got = store.find_preview_seeds("g", value="x", limit=10)
    assert [r["uri"] for r in got] == ["u3", "u1", "u2"]


def test_find_preview_seeds_does_not_swallow_index_query_failures():
    store = FakeStore()
    with patch.object(
        store,
        "execute_query",
        side_effect=RuntimeError("permission denied"),
    ), pytest.raises(RuntimeError, match="permission denied"):
        store.find_preview_seeds("g", value="ada", limit=2)


class MinimalStore(GraphDBBackend):
    """Bare concrete subclass to exercise GraphDBBackend defaults."""

    def create_table(self, table_name: str) -> None:
        pass

    def drop_table(self, table_name: str) -> None:
        pass

    def insert_triples(
        self,
        table_name: str,
        triples: List[Dict[str, str]],
        batch_size: int = 500,
        on_progress=None,
    ) -> int:
        return 0

    def query_triples(self, table_name: str) -> List[Dict[str, str]]:
        return []

    def count_triples(self, table_name: str) -> int:
        return 0

    def table_exists(self, table_name: str) -> bool:
        return False

    def get_status(self, table_name: str) -> Dict[str, Any]:
        return {}

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        return []

    def get_connection(self) -> Any:
        return None

    def close(self) -> None:
        pass


def test_graphdb_backend_adjacency_defaults():
    store = MinimalStore()
    assert store.supports_adjacency is False
    assert store.supports_entity_search is False
    assert store.sql_flavor() is None
    assert store.adjacency_table_ids("g") == ("", "")
    assert store.entity_search_table_id("g") == ""
    assert store.props_table_id("g") == ""
    store.rebuild_adjacency("g")
    assert store.adjacency_ready("g") is False
    assert store.entity_search_ready("g") is False


def test_find_subjects_by_type_uses_entity_search_when_ready():
    store = FakeStore()
    store._search_ready = True
    store.find_subjects_by_type("g", "http://ex/Customer", limit=10, offset=5)
    assert len(store.queries) == 1
    sql = store.queries[0]
    assert "g_entity_search" in sql
    assert "type_uri = 'http://ex/Customer'" in sql
    assert "LIMIT 10 OFFSET 5" in sql


def test_find_subjects_by_type_falls_back_when_entity_search_not_ready():
    store = FakeStore()
    store._search_ready = False
    store.find_subjects_by_type("g", "http://ex/Customer", limit=10, offset=0)
    assert len(store.queries) == 1
    sql = store.queries[0]
    assert "g_entity_search" not in sql
    assert "predicate = '" in sql


def test_find_subjects_by_type_falls_back_on_missing_table_error():
    store = FakeStore()
    store._search_ready = True
    with patch.object(
        store,
        "execute_query",
        side_effect=[RuntimeError("TABLE_OR_VIEW_NOT_FOUND"), [{"subject": "http://ex/1"}]],
    ) as execute:
        subs = store.find_subjects_by_type("g", "http://ex/Customer", limit=10, offset=0)
    assert subs == ["http://ex/1"]
    assert execute.call_count == 2
    assert "g_entity_search" in execute.call_args_list[0].args[0]
    assert "g_entity_search" not in execute.call_args_list[1].args[0]
