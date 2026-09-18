"""Version-copy contracts for parsed document sidecars."""

from unittest.mock import MagicMock

from back.objects.registry.RegistryService import RegistryCfg, RegistryService


def _service(uc):
    service = RegistryService(
        RegistryCfg("cat", "sch", "vol"), uc, store=MagicMock()
    )
    service._resolved_domains_folder = "domains"
    return service


def test_copy_version_documents_copies_sources_and_parsed_sidecars():
    uc = MagicMock()
    uc.list_directory.side_effect = [
        (
            True,
            [
                {"name": "spec.pdf", "is_directory": False},
                {"name": "_parsed", "is_directory": True},
            ],
            "listed",
        ),
        (
            True,
            [
                {"name": "spec.pdf.md", "is_directory": False},
                {"name": "spec.pdf.json", "is_directory": False},
            ],
            "listed",
        ),
    ]
    uc.read_binary_file.side_effect = [
        (True, b"%PDF", "read"),
        (True, b"markdown", "read"),
        (True, b"{}", "read"),
    ]
    uc.write_binary_file.return_value = (True, "written")
    uc.create_directory.return_value = (True, "created")
    service = _service(uc)

    copied, errors = service.copy_version_documents("sales", "1", "2")

    assert copied == 1
    assert errors == []
    written = [call.args[0] for call in uc.write_binary_file.call_args_list]
    assert written == [
        "/Volumes/cat/sch/vol/domains/sales/V1/documents/spec.pdf".replace(
            "/V1/", "/V2/"
        ),
        "/Volumes/cat/sch/vol/domains/sales/V2/documents/_parsed/spec.pdf.md",
        "/Volumes/cat/sch/vol/domains/sales/V2/documents/_parsed/spec.pdf.json",
    ]


def test_copy_version_documents_keeps_legacy_missing_parsed_directory_successful():
    uc = MagicMock()
    uc.list_directory.side_effect = [
        (True, [{"name": "notes.txt", "is_directory": False}], "listed"),
        (False, [], "Directory not found"),
    ]
    uc.read_binary_file.return_value = (True, b"notes", "read")
    uc.write_binary_file.return_value = (True, "written")
    service = _service(uc)

    copied, errors = service.copy_version_documents("sales", "1", "2")

    assert copied == 1
    assert errors == []
