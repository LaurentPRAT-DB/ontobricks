"""Contract tests for the shared timed task-log renderer wiring."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DTWIN_HTML = REPO_ROOT / "src/front/templates/dtwin.html"
QUERY_SYNC_JS = REPO_ROOT / "src/front/static/query/js/query-sync.js"
QUERY_SYNC_CSS = REPO_ROOT / "src/front/static/query/css/query-sync.css"
SYNC_PARTIAL = REPO_ROOT / "src/front/templates/partials/dtwin/_query_sync.html"


def test_shared_renderer_loads_before_backend_build_scripts() -> None:
    html = DTWIN_HTML.read_text(encoding="utf-8")
    assert html.index("timed-task-log.js") < html.index("query-databricks-build.js")
    assert html.index("timed-task-log.js") < html.index("query-sync.js")


def test_lakebase_uses_shared_timed_renderer() -> None:
    js = QUERY_SYNC_JS.read_text(encoding="utf-8")
    assert "TimedTaskLog.create" in js
    assert 'cardId: "syncBuildLogCard"' in js


def test_generic_log_styles_use_shared_card_class() -> None:
    css = QUERY_SYNC_CSS.read_text(encoding="utf-8")
    assert ".timed-task-log-card .sync-build-log" in css
    assert "#syncBuildLogCard .sync-build-log" not in css


def test_lakebase_log_card_declares_shared_class() -> None:
    html = SYNC_PARTIAL.read_text(encoding="utf-8")
    assert 'id="syncBuildLogCard"' in html
    assert 'class="card d-none mb-3 timed-task-log-card"' in html
