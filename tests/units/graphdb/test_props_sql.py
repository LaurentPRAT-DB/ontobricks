"""SQL shape tests for the property companion projection."""

from back.core.graphdb.constants import RDF_TYPE
from back.core.graphdb.props import props_select


def test_props_select_keeps_all_triples_for_typed_subjects():
    sql = props_select("g._graph")

    assert "SELECT t.subject, t.predicate, t.object" in sql
    assert sql.count("g._graph") == 2
    assert "INNER JOIN" in sql
    assert "typed.subject = t.subject" in sql
    assert f"predicate = '{RDF_TYPE}'" in sql
    assert "t.object LIKE 'http%'" not in sql
    assert "t.predicate !=" not in sql
