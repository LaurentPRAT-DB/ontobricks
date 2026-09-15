# OBX Import Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Registry OBX imports assign a different domain name while rewriting domain identity and clearing graph runtime only for renamed imports.

**Architecture:** Keep the existing preview/import routes and decision envelope. Extract the new-domain URI formula into one shared helper, prepare renamed version documents inside `SettingsService`, and simplify the Import preview to an always-visible `Import as` field whose value determines whether the existing import action is create, skip, overwrite, or rename.

**Tech Stack:** Python 3.10+, FastAPI, Bootstrap 5, vanilla JavaScript, pytest, `uv run --frozen`.

## Global Constraints

- No Registry domain-row Clone button and no new API route.
- Import remains admin-only.
- A renamed import copies ontology, mappings, settings, and every version present in the OBX.
- A renamed import rewrites domain display name and auto base URI, clears `last_build` and triplestore statistics, and does not copy graph tables or document binaries.
- Overwrite behavior and payloads remain unchanged.
- Domain names use the New Domain CamelCase contract: `[A-Z][A-Za-z0-9]*`, maximum 64 characters.
- UI feedback uses `showNotification`; no native `alert()`, `confirm()`, or `prompt()`.
- Comments, logs, docs, tests, and changelog are English.
- Final verification is `uv run --frozen pytest -q -m "not scenario"`.

## File map

| File | Responsibility |
|------|----------------|
| `src/back/core/helpers/DatabricksHelpers.py` | Shared pure auto-base-URI builder. |
| `src/back/core/helpers/__init__.py` | Export the helper through the existing helpers facade. |
| `src/back/objects/domain/Domain.py` | Delegate new-domain URI generation to the shared helper. |
| `src/back/objects/domain/SettingsService.py` | Suggest import display names and prepare renamed version documents. |
| `src/front/templates/partials/registry/_import_obx_modal.html` | Label the Import-as column and remove touched inline visibility styles. |
| `src/front/static/registry/js/registry.js` | Render/validate Import-as fields and derive import decisions. |
| `tests/units/domain/test_domain_service.py` | Guard existing new-domain URI behavior after extraction. |
| `tests/units/settings/test_settings_obx.py` | Cover preview suggestions, identity rewrite, graph reset, and overwrite preservation. |
| `tests/units/front/test_registry_obx_import_rename.py` | Structural contract for the Import-as UI and decision payload. |
| `docs/import-export.md` | Explain Import-as behavior and graph rebuild requirement. |
| `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log` | Required release changelog entry. |

---

### Task 1: Share the auto base URI formula

**Files:**
- Modify: `src/back/core/helpers/DatabricksHelpers.py`
- Modify: `src/back/core/helpers/__init__.py`
- Modify: `src/back/objects/domain/Domain.py`
- Test: `tests/units/domain/test_domain_service.py`

**Interfaces:**
- Produces: `build_auto_base_uri(domain_name: str, default_domain: str) -> str`
- Consumes: `DEFAULT_BASE_URI` and the current URI sanitization contract.

- [ ] **Step 1: Add a failing regression test**

Import the new helper and add to `TestSaveDomainInfo`:

```python
from back.core.helpers import build_auto_base_uri


def test_shared_auto_base_uri_matches_domain_generation(self):
    domain = _mock_domain()
    result = Domain(domain).save_domain_info({"name": "ClaimsSales"})
    assert result["base_uri"].endswith("/ClaimsSales#")
    assert result["base_uri_auto"] is True
    assert build_auto_base_uri("Claims Sales", "https://example.org") == (
        "https://example.org/Claims_Sales#"
    )
```

- [ ] **Step 2: Run the focused test and verify the extraction seam is absent**

Run:

```bash
uv run --frozen pytest -q tests/units/domain/test_domain_service.py::TestSaveDomainInfo::test_shared_auto_base_uri_matches_domain_generation
```

Expected: FAIL with `ImportError: cannot import name 'build_auto_base_uri'`.

- [ ] **Step 3: Implement the shared helper and delegate from Domain**

Add `import re`, a static method in `DatabricksHelpers`, and export its alias in `back/core/helpers/__init__.py`:

```python
@staticmethod
def build_auto_base_uri(domain_name: str, default_domain: str) -> str:
    base = (default_domain or DEFAULT_BASE_URI).rstrip("/#")
    raw_name = (domain_name or "").strip() or "MyDomain"
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name).strip("_") or "MyDomain"
    return f"{base}/{safe_name}#"
```

In `Domain._resolve_base_uri`, retain custom-URI handling and default resolution, then replace the local name formula with:

```python
return self._sanitize_base_uri(
    build_auto_base_uri(domain_name, default_domain)
)
```

Remove imports that become unused.

- [ ] **Step 4: Run URI tests**

Run:

```bash
uv run --frozen pytest -q tests/units/domain/test_domain_service.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/back/core/helpers/DatabricksHelpers.py src/back/core/helpers/__init__.py \
  src/back/objects/domain/Domain.py tests/units/domain/test_domain_service.py
git commit -m "refactor(domain): share automatic base URI builder"
```

---

### Task 2: Prepare renamed OBX documents

**Files:**
- Modify: `src/back/objects/domain/SettingsService.py`
- Test: `tests/units/settings/test_settings_obx.py`

**Interfaces:**
- Consumes: `build_auto_base_uri(domain_name, default_domain)` and `resolve_default_base_uri(domain, settings)`.
- Produces:
  - `SettingsService._suggest_import_name(svc, display_name: str) -> str`
  - `SettingsService._prepare_renamed_version_doc(doc, version, display_name, base_uri) -> Dict[str, Any]`

- [ ] **Step 1: Add failing backend tests**

Extend `tests/units/settings/test_settings_obx.py`:

```python
def test_rename_new_domain_rewrites_identity_and_clears_graph_runtime():
    session_mgr, settings = _mock_session_settings()
    registry_svc = _make_registry_svc(exists={})
    doc = _fake_doc("Claims", "2")
    doc["info"]["last_build"] = "2026-09-01T10:00:00Z"
    doc["triplestore"] = {"stats": {"triples": 42}}
    doc["assignment"] = {"entities": [{"name": "Claim"}]}
    file_bytes = TestImportRegistryObx()._build_obx_bytes(
        domains=[{"name": "claims", "info": doc["info"], "versions": {"2": doc}}]
    )

    with patch.object(_svc_module, "RegistryService") as rs_cls, patch.object(
        _svc_module, "resolve_default_base_uri", return_value="https://example.org"
    ):
        rs_cls.from_context.return_value = registry_svc
        SettingsService.import_registry_obx_result(
            file_bytes,
            [{"name": "claims", "action": "rename", "new_name": "ClaimsCopy"}],
            session_mgr,
            settings,
        )

    folder, version, raw_doc = registry_svc.write_version.call_args.args
    written = json.loads(raw_doc)
    assert (folder, version) == ("claimscopy", "2")
    assert written["info"]["name"] == "ClaimsCopy"
    assert written["ontology"]["base_uri"] == "https://example.org/ClaimsCopy#"
    assert written["ontology"]["base_uri_auto"] is True
    assert written["info"]["last_build"] == ""
    assert written["triplestore"]["stats"] == {}
    assert written["assignment"] == doc["assignment"]
```

Also add:

```python
def test_overwrite_preserves_identity_and_build_metadata():
    session_mgr, settings = _mock_session_settings()
    registry_svc = _make_registry_svc(
        exists={"claims": True}, listing={"claims": ["1"]}
    )
    doc = _fake_doc("Claims", "1")
    doc["info"]["last_build"] = "2026-09-01T10:00:00Z"
    doc["ontology"] = {"base_uri": "https://source.example/Claims#"}
    file_bytes = TestImportRegistryObx()._build_obx_bytes(
        domains=[{"name": "claims", "info": doc["info"], "versions": {"1": doc}}]
    )

    with patch.object(_svc_module, "RegistryService") as rs_cls:
        rs_cls.from_context.return_value = registry_svc
        SettingsService.import_registry_obx_result(
            file_bytes,
            [{"name": "claims", "action": "overwrite"}],
            session_mgr,
            settings,
        )

    written = json.loads(registry_svc.write_version.call_args.args[2])
    assert written["info"]["name"] == "Claims"
    assert written["ontology"]["base_uri"] == "https://source.example/Claims#"
    assert written["info"]["last_build"] == "2026-09-01T10:00:00Z"

def test_preview_suggests_free_camelcase_import_name():
    session_mgr, settings = _mock_session_settings()
    registry_svc = _make_registry_svc(exists={"claims": True})
    file_bytes = TestImportRegistryObx()._build_obx_bytes(
        domains=[
            {
                "name": "claims",
                "info": {"name": "Claims"},
                "versions": {"1": _fake_doc("Claims", "1")},
            }
        ]
    )

    with patch.object(_svc_module, "RegistryService") as rs_cls:
        rs_cls.from_context.return_value = registry_svc
        result = SettingsService.preview_obx_import_result(
            file_bytes, session_mgr, settings
        )

    assert result["domains"][0]["display_name"] == "Claims"
    assert result["domains"][0]["suggested_new_name"] == "ClaimsImported"
```

- [ ] **Step 2: Run backend tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/settings/test_settings_obx.py
```

Expected: FAIL because preview has no `display_name`, suggestions are folder slugs, and renamed documents are written unchanged.

- [ ] **Step 3: Implement preview naming and rename preparation**

In `SettingsService`:

```python
@staticmethod
def _prepare_renamed_version_doc(
    doc: Dict[str, Any],
    version: str,
    display_name: str,
    base_uri: str,
) -> Dict[str, Any]:
    prepared = copy.deepcopy(doc)

    def rewrite(node: Dict[str, Any]) -> None:
        info = node.setdefault("info", {})
        info["name"] = display_name
        info["last_build"] = ""
        ontology = node.setdefault("ontology", {})
        ontology["base_uri"] = base_uri
        ontology["base_uri_auto"] = True
        node["last_build"] = ""
        node.setdefault("triplestore", {})["stats"] = {}

    rewrite(prepared)
    versions = prepared.get("versions")
    nested = versions.get(version) if isinstance(versions, dict) else None
    if isinstance(nested, dict):
        rewrite(nested)
    return prepared
```

Use this CamelCase normalizer and free-name loop for preview:

```python
@staticmethod
def _camelcase_import_name(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value or "")
    candidate = "".join(part[:1].upper() + part[1:] for part in parts)
    if candidate and candidate[0].isalpha():
        return candidate[:64]
    return "ImportedDomain"

@staticmethod
def _suggest_import_name(svc: RegistryService, display_name: str) -> str:
    base = SettingsService._camelcase_import_name(display_name) + "Imported"
    candidate = base[:64]
    index = 2
    while svc.domain_exists(sanitize_domain_folder(candidate)):
        suffix = str(index)
        candidate = base[: 64 - len(suffix)] + suffix
        index += 1
    return candidate
```

In the rename branch, validate `new_name` against `^[A-Z][A-Za-z0-9]{0,63}$`, resolve the default URI once, call `build_auto_base_uri`, prepare each doc, and pass the prepared JSON to `write_version`. Leave the overwrite branch untouched:

```python
display_name = (decision.get("new_name") or "").strip()
if not re.fullmatch(r"[A-Z][A-Za-z0-9]{0,63}", display_name):
    raise ValidationError(
        "Imported domain name must be CamelCase alphanumeric"
    )
target_folder = sanitize_domain_folder(display_name)
base_uri = build_auto_base_uri(
    display_name,
    resolve_default_base_uri(domain_session, settings),
)
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
uv run --frozen pytest -q tests/units/settings/test_settings_obx.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/back/objects/domain/SettingsService.py tests/units/settings/test_settings_obx.py
git commit -m "feat(registry): prepare renamed OBX imports"
```

---

### Task 3: Replace Rename action with Import as

**Files:**
- Modify: `src/front/templates/partials/registry/_import_obx_modal.html`
- Modify: `src/front/static/registry/js/registry.js`
- Create: `tests/units/front/test_registry_obx_import_rename.py`

**Interfaces:**
- Consumes preview fields: `name`, `display_name`, `suggested_new_name`, and `exists`.
- Produces existing decisions: `{name, action}` or `{name, action: "rename", new_name}`.

- [ ] **Step 1: Add a failing structural contract**

Create `tests/units/front/test_registry_obx_import_rename.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (ROOT / "src/front/static/registry/js/registry.js").read_text()
TEMPLATE = (
    ROOT / "src/front/templates/partials/registry/_import_obx_modal.html"
).read_text()


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
```

- [ ] **Step 2: Run the frontend contract and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_registry_obx_import_rename.py
```

Expected: FAIL because the Import-as column/helper do not exist and Rename is still a radio.

- [ ] **Step 3: Implement Import-as rendering and validation**

In the modal table, add `<th>Import as</th>`. In `renderImportObxPreview`:

- Render source/status/versions as today.
- Render Skip/Overwrite radios only when `d.exists`.
- Render an always-visible `.import-obx-name` input with required, maxlength 64, CamelCase pattern, and invalid feedback.
- Prefill `d.exists ? d.suggested_new_name : d.display_name`.
- Remove the Rename radio and hidden rename field.

Add:

```javascript
function sanitizeImportFolder(name) {
    return name.trim().toLowerCase().replace(/[ -]/g, '_').replace(/[^a-z0-9_]/g, '') ||
        'untitled_domain';
}
```

Before posting, validate every field with `/^[A-Z][A-Za-z0-9]{0,63}$/`, reject duplicate target folders in the same preview, toggle `.is-invalid`, and issue one warning through `showNotification`.

Build each decision with:

```javascript
const importName = row.querySelector('.import-obx-name').value.trim();
const sourceFolder = row.dataset.name;
const targetFolder = sanitizeImportFolder(importName);
if (sourceFolder !== targetFolder) {
    decisions.push({ name: sourceFolder, action: 'rename', new_name: importName });
} else if (row.dataset.exists === '1') {
    decisions.push({
        name: sourceFolder,
        action: row.querySelector('input[type="radio"]:checked')?.value || 'skip'
    });
} else {
    decisions.push({ name: sourceFolder, action: 'overwrite' });
}
```

- [ ] **Step 4: Run frontend tests**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_registry_obx_import_rename.py \
  tests/units/front/test_registry_stacked_obx_modals.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/front/templates/partials/registry/_import_obx_modal.html \
  src/front/static/registry/js/registry.js \
  tests/units/front/test_registry_obx_import_rename.py
git commit -m "feat(registry): add Import as domain naming"
```

---

### Task 4: Documentation, changelog, and verification

**Files:**
- Modify: `docs/import-export.md`
- Create or append: `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

**Interfaces:**
- Produces: user documentation and the mandatory v0.9.0 changelog record.

- [ ] **Step 1: Update user documentation**

In `docs/import-export.md`, document that Import preview exposes **Import as** for every domain; a changed name creates a separate domain, regenerates its URI, and requires graph rebuild. State that Overwrite preserves imported identity/build metadata.

- [ ] **Step 2: Run focused regression tests**

Run:

```bash
uv run --frozen pytest -q tests/units/settings/test_settings_obx.py \
  tests/units/domain/test_domain_service.py \
  tests/units/front/test_registry_obx_import_rename.py \
  tests/units/front/test_registry_stacked_obx_modals.py
```

Expected: PASS.

- [ ] **Step 3: Run lint diagnostics on all edited source files**

Use IDE diagnostics for:

```text
src/back/core/helpers/DatabricksHelpers.py
src/back/core/helpers/__init__.py
src/back/objects/domain/Domain.py
src/back/objects/domain/SettingsService.py
src/front/static/registry/js/registry.js
src/front/templates/partials/registry/_import_obx_modal.html
```

Expected: no newly introduced errors.

- [ ] **Step 4: Run the mandatory non-scenario suite**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: all tests pass.

- [ ] **Step 5: Write the changelog entry with the real test result**

Create or append `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log` using the repository changelog format. Include every modified file and paste the exact test summary from Step 4.

- [ ] **Step 6: Commit documentation and changelog**

```bash
git add docs/import-export.md changelogs/v0.9.0/benoitcayladbx_2026-09-15.log
git commit -m "docs: describe renamed OBX imports"
```
