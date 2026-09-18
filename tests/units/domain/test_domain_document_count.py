"""Domain document counts exclude internal parsed sidecars."""

import importlib
from types import SimpleNamespace

from back.objects.domain import Domain


def test_count_documents_excludes_parsed_directory(monkeypatch):
    domain_module = importlib.import_module("back.objects.domain.Domain")
    session = SimpleNamespace(
        uc_version_path="/Volumes/main/ob/docs/domains/sales/V1",
        databricks={"host": "https://workspace", "token": "token"},
    )
    volume = SimpleNamespace(
        list_directory=lambda _path: (
            True,
            [
                {"name": "spec.pdf", "is_directory": False},
                {"name": "notes.md", "is_directory": False},
                {"name": "_parsed", "is_directory": True},
            ],
            "listed",
        )
    )
    monkeypatch.setattr(
        domain_module,
        "get_databricks_host_and_token",
        lambda _session, _settings: ("https://workspace", "token"),
    )
    monkeypatch.setattr(
        domain_module, "VolumeFileService", lambda **_kwargs: volume
    )

    assert Domain(session).count_documents_in_volume(object()) == 2
