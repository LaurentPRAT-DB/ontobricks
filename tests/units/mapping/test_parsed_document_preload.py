"""Mapping preloads ready documents from the durable parsed corpus."""

from __future__ import annotations

import importlib

from back.objects.mapping import Mapping


class _Volume:
    def list_directory(self, _path):
        return (
            True,
            [
                {"name": "spec.pdf", "is_directory": False},
                {"name": "manual.pdf", "is_directory": False},
                {"name": "_parsed", "is_directory": True},
            ],
            "listed",
        )


class _ParseService:
    def __init__(self, _volume):
        self.calls = []

    def read_document(self, base_path, filename, max_chars=None):
        self.calls.append((base_path, filename, max_chars))
        if filename == "spec.pdf":
            return {
                "filename": filename,
                "content": "Customer maps to crm.customer.",
                "size": 30,
                "truncated": False,
                "parsed_with": "ai_parse_document",
                "parse_status": "ready",
            }
        return {
            "filename": filename,
            "parse_status": "pending",
            "error": "Document parsing is not ready",
        }


def test_fetch_documents_for_agent_uses_ready_sidecar_and_reports_pending(
    monkeypatch,
):
    module = importlib.import_module("back.objects.mapping.Mapping")
    helpers = importlib.import_module("back.core.helpers")
    monkeypatch.setattr(
        helpers,
        "effective_uc_version_path",
        lambda _domain: "/Volumes/main/ob/docs/domains/sales/V1",
    )
    monkeypatch.setattr(module, "VolumeFileService", lambda **_kwargs: _Volume())
    monkeypatch.setattr(module, "DocumentParseService", _ParseService, raising=False)

    documents = Mapping.fetch_documents_for_agent(
        object(), "https://workspace", "token"
    )

    assert documents == [
        {
            "name": "spec.pdf",
            "content": "Customer maps to crm.customer.",
            "parse_status": "ready",
        },
        {
            "name": "manual.pdf",
            "content": "",
            "parse_status": "pending",
            "error": "Document parsing is not ready",
        },
    ]
