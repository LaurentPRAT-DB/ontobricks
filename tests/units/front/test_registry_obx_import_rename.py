"""Structural contracts for Registry OBX Import-as naming."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (ROOT / "src/front/static/registry/js/registry.js").read_text(
    encoding="utf-8"
)
TEMPLATE = (
    ROOT / "src/front/templates/partials/registry/_import_obx_modal.html"
).read_text(encoding="utf-8")


def test_import_preview_has_import_as_contract():
    assert "<th>Import as</th>" in TEMPLATE
    assert 'class="form-control form-control-sm import-obx-name"' in SCRIPT
    assert 'pattern="[A-Z][A-Za-z0-9]*"' in SCRIPT
    assert "actionRadio(idx, 'rename'" not in SCRIPT


def test_changed_name_builds_rename_decision():
    assert "sanitizeImportFolder(importName)" in SCRIPT
    assert "sourceFolder !== targetFolder" in SCRIPT
    assert "action: 'rename'" in SCRIPT
    assert "new_name: importName" in SCRIPT


def test_import_name_validation_rejects_duplicates():
    assert "IMPORT_NAME_PATTERN" in SCRIPT
    assert "targetFolders.has(targetFolder)" in SCRIPT
    assert "showNotification(" in SCRIPT


def test_import_action_controls_follow_target_name():
    assert "function syncImportActionControls(row)" in SCRIPT
    assert "import-obx-conflict-actions" in SCRIPT
    assert "import-obx-create-action" in SCRIPT
    assert "syncImportActionControls(row)" in SCRIPT
