"""UI contracts for durable document parse status and retry."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS_JS = REPO_ROOT / "src/front/static/domain/js/domain-documents.js"
DOCUMENTS_HTML = (
    REPO_ROOT / "src/front/templates/partials/domain/_domain_documents.html"
)
WIZARD_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-wizard.js"


def test_documents_ui_renders_all_parse_states():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "parseStatusBadge(" in js
    assert "Parsing" in js
    assert "Ready" in js
    assert "Parse failed" in js
    assert "bi-hourglass-split" in js
    assert "bi-check-circle" in js
    assert "bi-exclamation-triangle" in js


def test_pending_polling_is_single_and_stops_when_terminal():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "parsePollTimer: null" in js
    assert "clearTimeout(this.parsePollTimer)" in js
    assert "files.some(file => file.parse_status === 'pending')" in js
    assert "setTimeout(() => this.refreshList(), 2000)" in js


def test_failed_document_has_retry_action():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "retryParse(filename)" in js
    assert "'/domain/documents/retry-parse'" in js
    assert "JSON.stringify({ filename })" in js
    assert "Retry parsing" in js


def test_status_region_is_accessible():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert 'id="docFileList"' in html
    assert 'aria-live="polite"' in html
    assert "Files are parsed once after upload" in html


def test_document_preview_filename_is_keyboard_accessible():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert 'tabindex="0"' in js
    assert "event.key === 'Enter' || event.key === ' '" in js


def test_wizard_pending_documents_use_hourglass_status_icon():
    js = WIZARD_JS.read_text(encoding="utf-8")
    assert "file.parse_status === 'pending' ? 'bi-hourglass-split'" in js


def test_generate_filters_unready_selected_documents():
    js = WIZARD_JS.read_text(encoding="utf-8")
    assert "getSelectedDocumentFiles()" in js
    assert "file.parse_status === 'ready'" in js
    assert "Document parsing is not ready" in js
    assert "unavailableDocuments.map(file => file.name)" in js
