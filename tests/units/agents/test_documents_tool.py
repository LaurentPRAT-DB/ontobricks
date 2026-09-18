"""Agent document tools read only the durable parsed corpus."""

from __future__ import annotations

import json

import pytest

from agents.tools import documents as docs
from agents.tools.context import ToolContext


def _ctx(documents=None) -> ToolContext:
    return ToolContext(
        host="https://test.databricks.com",
        token="test-token",
        registry={"catalog": "main", "schema": "ob", "volume": "docs"},
        domain_folder="dom",
        domain_version="1",
        documents=list(documents or []),
    )


class _ParseService:
    def __init__(self, payloads=None, manifests=None) -> None:
        self.payloads = payloads or {}
        self.manifests = manifests or {}
        self.reads = []

    def read_document(self, base_path, filename, max_chars=None):
        self.reads.append((base_path, filename, max_chars))
        return self.payloads[filename]

    def status(self, _base_path, filename):
        return self.manifests.get(filename)


class _Response:
    status_code = 200
    content = b"{}"
    headers = {"content-type": "application/json"}

    @staticmethod
    def json():
        return {
            "contents": [
                {"name": "spec.pdf", "is_directory": False, "file_size": 12},
                {"name": "_parsed", "is_directory": True, "file_size": 0},
            ]
        }

    @staticmethod
    def raise_for_status():
        return None


def test_read_ready_pdf_uses_persisted_sidecar(monkeypatch):
    service = _ParseService(
        payloads={
            "spec.pdf": {
                "filename": "spec.pdf",
                "content": "Page one\n\nPage two",
                "size": 18,
                "truncated": False,
                "parsed_with": "ai_parse_document",
                "parse_status": "ready",
            }
        }
    )
    monkeypatch.setattr(docs, "_parse_service", lambda _ctx: service)

    out = json.loads(docs.tool_read_document(_ctx(), filename="spec.pdf"))

    assert out["content"] == "Page one\n\nPage two"
    assert out["parse_status"] == "ready"
    assert service.reads[0][2] == docs._MAX_DOC_CHARS


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_unavailable_pdf_returns_structured_status_without_extractor(
    monkeypatch, status
):
    service = _ParseService(
        payloads={
            "spec.pdf": {
                "filename": "spec.pdf",
                "parse_status": status,
                "error": "Document parsing is not ready",
            }
        }
    )
    monkeypatch.setattr(docs, "_parse_service", lambda _ctx: service)

    out = json.loads(docs.tool_read_document(_ctx(), filename="spec.pdf"))

    assert out["parse_status"] == status
    assert out["error"] == "Document parsing is not ready"


def test_list_documents_hides_parsed_directory_and_adds_status(monkeypatch):
    manifest = type(
        "Manifest",
        (),
        {"status": type("Status", (), {"value": "ready"})(), "parser": "ai_parse_document", "error": None},
    )()
    service = _ParseService(manifests={"spec.pdf": manifest})
    monkeypatch.setattr(docs, "_parse_service", lambda _ctx: service)
    monkeypatch.setattr(docs.requests, "get", lambda *_args, **_kwargs: _Response())

    out = json.loads(docs.tool_list_documents(_ctx()))

    assert out["files"] == [
        {
            "name": "spec.pdf",
            "size": 12,
            "parse_status": "ready",
            "parser": "ai_parse_document",
        }
    ]


def test_documents_context_separates_unavailable_documents():
    ctx = _ctx(
        [
            {"name": "terms.md", "content": "Asset has Location", "parse_status": "ready"},
            {
                "name": "manual.pdf",
                "content": "",
                "parse_status": "pending",
                "error": "Document parsing is not ready",
            },
        ]
    )

    out = json.loads(docs.tool_get_documents_context(ctx))

    assert [item["name"] for item in out["documents"]] == ["terms.md"]
    assert out["unavailable_documents"] == [
        {
            "name": "manual.pdf",
            "parse_status": "pending",
            "error": "Document parsing is not ready",
        }
    ]
