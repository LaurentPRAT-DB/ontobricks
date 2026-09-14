"""DigitalTwin Preview contracts for the entity-search fast path."""

from __future__ import annotations

import pytest

from back.objects.digitaltwin.DigitalTwin import DigitalTwin


pytestmark = pytest.mark.unit


def test_filter_preview_uses_index_rows_and_preserves_response_shape() -> None:
    class Store:
        kwargs: dict

        def find_preview_seeds(self, table_name: str, **kwargs):
            assert table_name == "g"
            self.kwargs = kwargs
            return [
                {
                    "uri": "http://ex/Ada",
                    "type": "http://ex/Person",
                    "label": "Ada",
                },
                {
                    "uri": "http://ex/Bob",
                    "type": "http://ex/Person",
                    "label": "Bob",
                },
            ]

    store = Store()
    result = DigitalTwin.filter_preview(
        store,
        "g",
        entity_type="",
        field="any",
        match_type="contains",
        value="a",
        max_preview=1,
    )

    assert store.kwargs["limit"] == 2
    assert result["capped"] is True
    assert result["total"] == 2
    assert result["seeds"] == [
        {
            "uri": "http://ex/Ada",
            "type": "Person",
            "type_uri": "http://ex/Person",
            "label": "Ada",
        }
    ]
