"""Route contracts for parsed document upload and status."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.routers.internal import domain as routes
from back.core.databricks import ParseManifest, ParseStatus, ParseSubmission

DOCS = "/Volumes/main/ob/docs/domains/sales/V1/documents"


class _Upload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


class _Request:
    def __init__(self, *, uploads=None, json_body=None) -> None:
        self._uploads = uploads or []
        self._json = json_body or {}

    async def form(self):
        uploads = self._uploads

        class _Form:
            @staticmethod
            def getlist(name):
                return uploads if name == "files" else []

        return _Form()

    async def json(self):
        return self._json


class _Volume:
    def __init__(self, items=None) -> None:
        self.items = items or []
        self.deleted = []

    def is_configured(self):
        return True

    def create_directory(self, _path):
        return True, "created"

    def list_directory(self, _path):
        return True, self.items, "listed"

    def delete_file(self, path):
        self.deleted.append(path)
        return True, "deleted"


class _ParseService:
    def __init__(self, submission=None, manifests=None) -> None:
        self.submission = submission
        self.manifests = manifests or {}
        self.prepared = []
        self.retried = []
        self.deleted = []

    def prepare_upload(self, base_path, filename, content):
        self.prepared.append((base_path, filename, content))
        return self.submission

    def status(self, _base_path, filename):
        return self.manifests.get(filename)

    def retry(self, base_path, filename):
        self.retried.append((base_path, filename))
        return self.submission

    def delete_artifacts(self, base_path, filename):
        self.deleted.append((base_path, filename))
        return []


def _manifest(filename: str, status: ParseStatus) -> ParseManifest:
    return ParseManifest(
        schema_version=1,
        filename=filename,
        source_hash="abc",
        parser="ai_parse_document",
        output_schema="2.0",
        status=status,
        parsed_at=None,
        error="Document parsing failed" if status is ParseStatus.FAILED else None,
        sidecar_path=f"_parsed/{filename}.md",
    )


@pytest.fixture
def route_context(monkeypatch):
    domain = SimpleNamespace()
    volume = _Volume()
    monkeypatch.setattr(routes, "get_domain", lambda _manager: domain)
    monkeypatch.setattr(
        routes,
        "Domain",
        lambda _domain: SimpleNamespace(get_documents_volume_path=lambda: DOCS),
    )
    monkeypatch.setattr(
        routes, "make_volume_file_service", lambda _domain, _settings: volume
    )
    return SimpleNamespace(
        domain=domain,
        volume=volume,
        session=SimpleNamespace(),
        settings=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_binary_upload_returns_pending_task(monkeypatch, route_context):
    service = _ParseService(
        ParseSubmission("spec.pdf", True, False, True, ParseStatus.PENDING)
    )
    task_manager = MagicMock()
    task_manager.run_background_task.return_value = SimpleNamespace(id="parse-1")
    monkeypatch.setattr(
        routes, "_make_document_parse_service", lambda *_args, **_kwargs: service
    )
    monkeypatch.setattr(routes, "get_task_manager", lambda: task_manager)

    result = await routes.upload_documents(
        _Request(uploads=[_Upload("spec.pdf", b"%PDF")]),
        route_context.session,
        route_context.settings,
    )

    item = result["results"][0]
    assert item == {
        "filename": "spec.pdf",
        "success": True,
        "uploaded": True,
        "no_op": False,
        "parse_status": "pending",
        "task_id": "parse-1",
        "message": "Uploaded; parsing started",
    }
    task_manager.run_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_identical_ready_upload_returns_noop_without_task(
    monkeypatch, route_context
):
    service = _ParseService(
        ParseSubmission("spec.pdf", False, True, False, ParseStatus.READY)
    )
    task_manager = MagicMock()
    monkeypatch.setattr(
        routes, "_make_document_parse_service", lambda *_args, **_kwargs: service
    )
    monkeypatch.setattr(routes, "get_task_manager", lambda: task_manager)

    result = await routes.upload_documents(
        _Request(uploads=[_Upload("spec.pdf", b"%PDF")]),
        route_context.session,
        route_context.settings,
    )

    item = result["results"][0]
    assert item["uploaded"] is False
    assert item["no_op"] is True
    assert item["parse_status"] == "ready"
    assert "task_id" not in item
    task_manager.run_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_list_hides_internal_directory_and_adds_parse_status(
    monkeypatch, route_context
):
    route_context.volume.items = [
        {"name": "spec.pdf", "is_directory": False, "size": 12},
        {"name": "_parsed", "is_directory": True, "size": 0},
    ]
    service = _ParseService(
        manifests={"spec.pdf": _manifest("spec.pdf", ParseStatus.READY)}
    )
    monkeypatch.setattr(
        routes, "_make_document_parse_service", lambda *_args, **_kwargs: service
    )

    result = await routes.list_documents(
        route_context.session, route_context.settings
    )

    assert len(result["files"]) == 1
    assert result["files"][0]["name"] == "spec.pdf"
    assert result["files"][0]["parse_status"] == "ready"


@pytest.mark.asyncio
async def test_retry_failed_binary_schedules_task(monkeypatch, route_context):
    service = _ParseService(
        ParseSubmission("spec.pdf", False, False, True, ParseStatus.PENDING)
    )
    task_manager = MagicMock()
    task_manager.run_background_task.return_value = SimpleNamespace(id="parse-2")
    monkeypatch.setattr(
        routes, "_make_document_parse_service", lambda *_args, **_kwargs: service
    )
    monkeypatch.setattr(routes, "get_task_manager", lambda: task_manager)

    result = await routes.retry_document_parse(
        _Request(json_body={"filename": "spec.pdf"}),
        route_context.session,
        route_context.settings,
    )

    assert result["success"] is True
    assert result["parse_status"] == "pending"
    assert result["task_id"] == "parse-2"


@pytest.mark.asyncio
async def test_delete_source_also_deletes_sidecars(monkeypatch, route_context):
    service = _ParseService()
    monkeypatch.setattr(
        routes, "_make_document_parse_service", lambda *_args, **_kwargs: service
    )

    result = await routes.delete_document(
        _Request(json_body={"filename": "spec.pdf"}),
        route_context.session,
        route_context.settings,
    )

    assert result["success"] is True
    assert route_context.volume.deleted == [f"{DOCS}/spec.pdf"]
    assert service.deleted == [(DOCS, "spec.pdf")]
