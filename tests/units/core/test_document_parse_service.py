"""Unit contracts for the durable parsed-document corpus."""

from __future__ import annotations

import json

import pytest

from back.core.databricks import DocumentParseService, ParseStatus

DOCS = "/Volumes/main/ob/docs/domains/sales/V1/documents"


class _MemoryVolume:
    def __init__(self) -> None:
        self.binary: dict[str, bytes] = {}
        self.text: dict[str, str] = {}
        self.directories: set[str] = set()
        self.binary_writes: list[str] = []

    def create_directory(self, path: str):
        self.directories.add(path)
        return True, "created"

    def write_binary_file(self, path: str, data: bytes, overwrite: bool = True):
        self.binary[path] = data
        self.binary_writes.append(path)
        return True, "written"

    def write_file(self, path: str, content: str, overwrite: bool = True):
        self.text[path] = content
        return True, "written"

    def read_file(self, path: str):
        if path in self.text:
            return True, self.text[path], "read"
        if path in self.binary:
            try:
                return True, self.binary[path].decode("utf-8"), "read"
            except UnicodeDecodeError:
                return False, "", "not text"
        return False, "", f"File not found: {path}"

    def read_binary_file(self, path: str):
        if path in self.binary:
            return True, self.binary[path], "read"
        return False, b"", f"File not found: {path}"

    def delete_file(self, path: str):
        existed = path in self.text or path in self.binary
        self.text.pop(path, None)
        self.binary.pop(path, None)
        if existed:
            return True, "deleted"
        return False, f"File not found: {path}"


class _FakeExtractor:
    OUTPUT_SCHEMA_VERSION = "2.0"

    def __init__(self, result: str | None = "parsed markdown") -> None:
        self.result = result
        self.calls: list[str] = []

    def extract(self, path: str):
        self.calls.append(path)
        return self.result


@pytest.fixture
def volume():
    return _MemoryVolume()


@pytest.fixture
def extractor():
    return _FakeExtractor()


@pytest.fixture
def service(volume, extractor):
    with DocumentParseService._state_lock:
        DocumentParseService._path_locks.clear()
        DocumentParseService._active_parses.clear()
    return DocumentParseService(volume, extractor)


def _manifest(volume: _MemoryVolume, filename: str) -> dict:
    return json.loads(volume.text[f"{DOCS}/_parsed/{filename}.json"])


def test_binary_upload_records_pending_then_ready(service, volume, extractor):
    submitted = service.prepare_upload(DOCS, "spec.pdf", b"%PDF")
    assert submitted.uploaded is True
    assert submitted.should_parse is True
    assert submitted.parse_status is ParseStatus.PENDING
    assert _manifest(volume, "spec.pdf")["status"] == "pending"

    manifest = service.parse_pending(DOCS, "spec.pdf")

    assert manifest.status is ParseStatus.READY
    assert volume.text[f"{DOCS}/_parsed/spec.pdf.md"] == "parsed markdown"
    assert extractor.calls == [f"{DOCS}/spec.pdf"]


def test_ready_hash_match_is_noop_and_does_not_extract(service, volume, extractor):
    service.prepare_upload(DOCS, "spec.pdf", b"same bytes")
    service.parse_pending(DOCS, "spec.pdf")
    writes_before = list(volume.binary_writes)

    second = service.prepare_upload(DOCS, "spec.pdf", b"same bytes")

    assert second.no_op is True
    assert second.uploaded is False
    assert second.should_parse is False
    assert volume.binary_writes == writes_before
    assert extractor.calls == [f"{DOCS}/spec.pdf"]


def test_changed_hash_invalidates_and_parses_again(service, extractor):
    service.prepare_upload(DOCS, "spec.pdf", b"first")
    first = service.parse_pending(DOCS, "spec.pdf")
    service.prepare_upload(DOCS, "spec.pdf", b"second")
    second = service.parse_pending(DOCS, "spec.pdf")

    assert first.source_hash != second.source_hash
    assert extractor.calls == [f"{DOCS}/spec.pdf", f"{DOCS}/spec.pdf"]


def test_matching_active_pending_upload_does_not_schedule_twice(service):
    first = service.prepare_upload(DOCS, "spec.pdf", b"same")
    second = service.prepare_upload(DOCS, "spec.pdf", b"same")

    assert first.should_parse is True
    assert second.no_op is True
    assert second.should_parse is False
    assert second.parse_status is ParseStatus.PENDING


def test_failed_parse_keeps_source_and_records_safe_error(
    service, volume, extractor
):
    extractor.result = None
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")

    manifest = service.parse_pending(DOCS, "spec.pdf")

    assert volume.binary[f"{DOCS}/spec.pdf"] == b"%PDF"
    assert manifest.status is ParseStatus.FAILED
    assert manifest.error == "Document parsing failed"
    assert f"{DOCS}/_parsed/spec.pdf.md" not in volume.text


def test_retry_failed_binary_moves_back_to_pending(service, extractor):
    extractor.result = None
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")
    service.parse_pending(DOCS, "spec.pdf")

    retry = service.retry(DOCS, "spec.pdf")

    assert retry.should_parse is True
    assert retry.parse_status is ParseStatus.PENDING


def test_plaintext_is_ready_without_markdown_copy(service, volume, extractor):
    submitted = service.prepare_upload(DOCS, "terms.md", b"Customer places Order")

    assert submitted.parse_status is ParseStatus.READY
    assert submitted.should_parse is False
    assert _manifest(volume, "terms.md")["parser"] == "plaintext"
    assert _manifest(volume, "terms.md")["sidecar_path"] is None
    assert f"{DOCS}/_parsed/terms.md.md" not in volume.text
    assert extractor.calls == []


def test_invalid_utf8_plaintext_records_failed(service, volume):
    submitted = service.prepare_upload(DOCS, "terms.md", b"\xff")

    assert submitted.parse_status is ParseStatus.FAILED
    assert _manifest(volume, "terms.md")["error"] == "Document is not valid UTF-8"


def test_unknown_extension_is_not_sent_to_extractor(service, extractor):
    submitted = service.prepare_upload(DOCS, "archive.bin", b"\x00\x01")

    assert submitted.parse_status is ParseStatus.FAILED
    assert submitted.should_parse is False
    assert extractor.calls == []


@pytest.mark.parametrize("status", [ParseStatus.PENDING, ParseStatus.FAILED])
def test_unavailable_binary_reader_never_extracts(
    service, extractor, status: ParseStatus
):
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")
    if status is ParseStatus.FAILED:
        extractor.result = None
        service.parse_pending(DOCS, "spec.pdf")
    extractor.calls.clear()

    payload = service.read_document(DOCS, "spec.pdf")

    assert payload["parse_status"] == status.value
    assert "error" in payload
    assert extractor.calls == []


def test_ready_binary_reader_uses_sidecar_without_extractor(
    service, extractor
):
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")
    service.parse_pending(DOCS, "spec.pdf")
    extractor.calls.clear()

    payload = service.read_document(DOCS, "spec.pdf")

    assert payload["content"] == "parsed markdown"
    assert payload["parse_status"] == "ready"
    assert payload["parsed_with"] == "ai_parse_document"
    assert extractor.calls == []


def test_reader_applies_prompt_limit_after_loading_sidecar(service, extractor):
    extractor.result = "x" * 20
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")
    service.parse_pending(DOCS, "spec.pdf")

    payload = service.read_document(DOCS, "spec.pdf", max_chars=10)

    assert payload["size"] == 20
    assert payload["truncated"] is True
    assert payload["content"].startswith("x" * 10)


def test_legacy_plaintext_without_manifest_remains_readable(service, volume):
    volume.binary[f"{DOCS}/notes.txt"] = b"legacy text"

    payload = service.read_document(DOCS, "notes.txt")

    assert payload["parse_status"] == "ready"
    assert payload["parsed_with"] == "plaintext"
    assert payload["content"] == "legacy text"


def test_legacy_binary_without_manifest_is_not_ready(service, volume):
    volume.binary[f"{DOCS}/spec.pdf"] = b"%PDF"

    payload = service.read_document(DOCS, "spec.pdf")

    assert payload["parse_status"] == "failed"
    assert "retry" in payload["error"].lower()


def test_delete_artifacts_ignores_missing_files(service, volume):
    service.prepare_upload(DOCS, "spec.pdf", b"%PDF")

    errors = service.delete_artifacts(DOCS, "spec.pdf")

    assert errors == []
    assert f"{DOCS}/_parsed/spec.pdf.json" not in volume.text


@pytest.mark.parametrize("filename", ["", ".", "..", "../escape.pdf", "x/y.pdf"])
def test_filename_cannot_escape_document_directory(service, filename):
    with pytest.raises(ValueError, match="Invalid filename"):
        service.prepare_upload(DOCS, filename, b"x")
