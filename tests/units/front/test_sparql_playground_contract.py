"""Static contract for Knowledge Graph Query -> SPARQL."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
DTWIN = ROOT / "src/front/templates/dtwin.html"
SHELL = ROOT / "src/front/templates/partials/dtwin/_query_query.html"
GRAPHQL = ROOT / "src/front/templates/partials/dtwin/_query_graphql.html"
SPARQL = ROOT / "src/front/templates/partials/dtwin/_query_sparql.html"
CSS = ROOT / "src/front/static/query/css/query-sparql.css"
GLOBAL_QUERY_CSS = ROOT / "src/front/static/global/css/query.css"
MENU = ROOT / "src/front/config/menu_config.json"


def read(path):
    return path.read_text(encoding="utf-8")


def test_query_shell_hosts_graphql_and_sparql_tabs():
    shell = read(SHELL)
    assert "GraphQL" in shell
    assert "SPARQL" in shell
    assert 'id="queryGraphqlTab"' in shell
    assert 'id="querySparqlTab"' in shell
    assert 'id="queryGraphqlPane"' in shell
    assert 'id="querySparqlPane"' in shell
    assert "_query_graphql.html" in shell
    assert "_query_sparql.html" in shell


def test_sparql_workspace_has_editor_results_and_actions():
    html = read(SPARQL)
    for element_id in (
        "sparqlPlaygroundQuery",
        "sparqlSample",
        "sparqlResultLimit",
        "sparqlRunBtn",
        "sparqlResultsContainer",
        "sparqlGeneratedSql",
        "sparqlDownloadBtn",
        "sparqlExploreBtn",
    ):
        assert f'id="{element_id}"' in html
    assert 'style="' not in html
    assert "onclick=" not in html


def test_graphql_ids_are_preserved():
    html = read(GRAPHQL)
    for element_id in (
        "graphqlLoading",
        "graphqlError",
        "graphqlErrorMsg",
        "graphiql-container",
        "graphqlDepthSelect",
        "graphqlOpenNewTab",
    ):
        assert f'id="{element_id}"' in html


def test_dtwin_wires_query_shell_and_css():
    html = read(DTWIN)
    assert "_query_query.html" in html
    assert "query/css/query-sparql.css" in html


def test_menu_keeps_one_query_item():
    menu = read(MENU)
    assert menu.count('"id": "graphql"') == 1
    block = menu[menu.index('"id": "graphql"') : menu.index('"id": "chat"')]
    assert '"label": "Query"' in block


def test_sparql_css_uses_shared_tokens_and_responsive_stack():
    css = read(CSS)
    assert ".sparql-playground" in css
    assert "var(--db-" in css
    assert "@media" in css
    global_css = read(GLOBAL_QUERY_CSS)
    assert "#sparqlResultsContainer > .gridjs-container" in global_css
    assert "#resultsContainer" not in global_css
