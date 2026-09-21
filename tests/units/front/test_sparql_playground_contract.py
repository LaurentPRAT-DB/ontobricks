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
QUERY_JS = ROOT / "src/front/static/query/js/query.js"
JS = ROOT / "src/front/static/query/js/query-execute.js"
SIGMA_JS = ROOT / "src/front/static/query/js/query-sigmagraph.js"
MENU = ROOT / "src/front/config/menu_config.json"


def read(path):
    return path.read_text(encoding="utf-8")


def _selector_blocks(css_text, selector):
    css_text = __import__("re").sub(r"/\*.*?\*/", "", css_text, flags=__import__("re").DOTALL)
    blocks = []
    for selectors, declarations in __import__("re").findall(r"([^{}]+)\{([^{}]*)\}", css_text):
        parts = [chunk.strip() for chunk in selectors.split(",")]
        if selector in parts:
            blocks.append(declarations)
    return blocks


def _winning_declaration(css_text, selector, prop):
    import re

    value = None
    for block in _selector_blocks(css_text, selector):
        for match in re.finditer(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", block):
            value = match.group(1).strip()
    return value


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


def test_query_shell_actions_stay_declarative_without_inline_handlers():
    shell = read(SHELL)
    for action in ("switch-domain", "ontology", "discussion"):
        assert f'data-query-action="{action}"' in shell
    assert "onclick=" not in shell


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
    assert (
        _winning_declaration(global_css, "#sparqlResultsContainer", "overflow")
        == "auto"
    )
    assert (
        _winning_declaration(
            global_css, "#sparqlResultsContainer .results-empty-state", "color"
        )
        == "var(--db-text-muted)"
    )


def test_query_shell_action_mappings_are_wired_in_js():
    js = read(QUERY_JS)
    assert "switch-domain" in js
    assert "_openGraphSwitcherModal" in js
    assert "ontology" in js
    assert "OntologyViewer.open" in js
    assert "discussion" in js
    assert "openTwinDiscussion" in js


def test_sparql_controller_has_samples_and_no_automatic_explorer_switch():
    js = read(JS)
    assert "const SPARQLPlayground" in js
    assert "async function init()" in js
    assert "async function execute()" in js
    assert "function applySample(" in js
    assert "function isTripleProjection(" in js
    execute_body = js[js.index("async function execute()") :]
    execute_body = execute_body[: execute_body.index("function displayResults(")]
    assert "SidebarNav.switchTo('sigmagraph')" not in execute_body


def test_query_page_initializes_only_the_selected_language_tab():
    js = read(QUERY_JS)
    assert "initQueryPlayground" in js
    assert "GraphQLPlayground.init()" in js
    assert "SPARQLPlayground.init()" in js


def test_query_reentry_without_pending_tab_does_not_force_graphql():
    js = read(QUERY_JS)
    assert "function getActiveQueryTabName()" in js
    assert "tabName === 'sparql' || tabName === 'graphql'" in js
    init_body = js[js.index("function initQueryPlayground(") :]
    init_body = init_body[: init_body.index("async function _initQueryPage(")]
    assert "const activeTabName = getActiveQueryTabName();" in init_body
    assert "bootstrap.Tab.getOrCreateInstance(tab).show();" in init_body
    assert "if (requestedTab) {" in init_body
    assert "if (activeTabName === 'sparql')" in init_body


def test_execute_failure_does_not_clear_generated_sql():
    js = read(JS)
    execute_body = js[js.index("async function execute()") :]
    execute_body = execute_body[: execute_body.index("function displayResults(")]
    catch_body = execute_body[execute_body.index("} catch (error) {") :]
    catch_body = catch_body[: catch_body.index("} finally {")]
    assert 'displayGeneratedSql("")' not in catch_body
    assert "displayGeneratedSql('')" not in catch_body


def test_explorer_bridge_is_explicit_and_sigma_accepts_query_rows():
    sparql_js = read(JS)
    sigma_js = read(SIGMA_JS)
    assert "SigmaGraph.loadQueryResults" in sparql_js
    assert "loadQueryResults:" in sigma_js
    assert "SidebarNav.switchTo('sigmagraph')" in sparql_js
