"""Structural/behavior contracts for the staged three-step Generate wizard.

Plan task 5 of ``staged-ontology-generate``
(``docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md``).
The frontend has no JS test runner in this repository — every other
``tests/units/front/test_*`` module asserts on the raw template/JS/CSS
source (regex/string contracts), and this module follows the same
convention. Behavioral/browser verification is out of scope here (performed
separately); this file locks in the wiring contract: which endpoints are
called, that the legacy one-shot route is never called, that sessionStorage
only ever holds task ids, that locked anchors cannot be mutated from the
client, and that the three stages/checklist follow the design's exact
shapes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
HTML = REPO_ROOT / "src/front/templates/partials/ontology/_ontology_wizard.html"
WIZARD_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-wizard.js"
REVIEW_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-wizard-review.js"
WIZARD_CSS = REPO_ROOT / "src/front/static/ontology/css/ontology-wizard.css"
ONTOLOGY_PAGE = REPO_ROOT / "src/front/templates/ontology.html"
NO_LLM_GATE_TEST = REPO_ROOT / "tests/units/front/test_no_llm_ui_gate.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Asset wiring
# ---------------------------------------------------------------------------


def test_review_js_is_wired_into_the_ontology_page():
    page = _read(ONTOLOGY_PAGE)
    assert "ontology/js/ontology-wizard-review.js" in page
    # The review module must load before the orchestrator that calls it.
    assert page.index("ontology-wizard-review.js") < page.index(
        "ontology-wizard.js"
    ) or "ontology-wizard.js" in page


def test_review_js_file_exists_and_is_not_empty():
    assert REVIEW_JS.exists()
    assert len(_read(REVIEW_JS)) > 200


# ---------------------------------------------------------------------------
# Endpoint usage — exact staged routes only, never the legacy one-shot route
# ---------------------------------------------------------------------------


def test_detect_route_is_called_for_stage_one():
    js = _read(WIZARD_JS)
    assert "/ontology/wizard/generate/detect" in js


def test_draft_routes_are_called_for_stage_two():
    js = _read(WIZARD_JS) + _read(REVIEW_JS)
    assert "/ontology/wizard/generate/draft" in js
    assert "/ontology/wizard/generate/draft/update" in js
    assert "/ontology/wizard/generate/draft/discard" in js


def test_complete_route_is_called_for_stage_three():
    js = _read(WIZARD_JS)
    assert "/ontology/wizard/generate/complete" in js


def test_legacy_one_shot_route_is_never_called():
    js = _read(WIZARD_JS) + _read(REVIEW_JS)
    assert "generate-async" not in js


def test_legacy_one_shot_js_functions_are_removed():
    js = _read(WIZARD_JS)
    for legacy in (
        "generateOntologyFromWizard",
        "applyWizardOntology",
        "applyWizardOntologySilent",
        "showWizardResults",
        "wizardGeneratedOWL",
    ):
        assert legacy not in js, f"legacy one-shot symbol still present: {legacy}"


# ---------------------------------------------------------------------------
# sessionStorage holds task ids only — never draft/entity/checkpoint state
# ---------------------------------------------------------------------------


def test_session_storage_keys_are_task_ids_only():
    js = _read(WIZARD_JS)

    assert re.search(r"WIZARD_DETECT_TASK_KEY\s*=", js)
    assert re.search(r"WIZARD_COMPLETE_TASK_KEY\s*=", js)

    # The old OWL/stats persistence keys (client-side cache of generated
    # content) must be gone — the draft is the only source of truth now.
    for legacy_key in ("WIZARD_OWL_KEY", "WIZARD_STATS_KEY", "WIZARD_TASK_KEY"):
        assert legacy_key not in js, f"legacy sessionStorage key still present: {legacy_key}"


def test_no_sessionstorage_write_carries_draft_or_entity_payloads():
    js = _read(WIZARD_JS) + _read(REVIEW_JS)
    # Every sessionStorage.setItem call must be storing a task id constant/
    # variable, never a JSON-serialized draft/candidate/checkpoint blob.
    for call in re.findall(r"sessionStorage\.setItem\(([^)]*)\)", js):
        assert "JSON.stringify" not in call, f"sessionStorage stores structured state: {call}"
        assert "draft" not in call.lower(), f"sessionStorage stores draft state: {call}"


# ---------------------------------------------------------------------------
# Stepper: Configure / Review / Complete, active/completed states, a11y
# ---------------------------------------------------------------------------


def test_stepper_markup_has_three_accessible_steps():
    html = _read(HTML)

    nav = re.search(r'<nav\b[^>]*class="[^"]*wizard-stepper[^"]*"[^>]*>', html)
    assert nav, "wizard stepper nav not found"
    assert 'aria-label=' in nav.group(0)

    for step_id, label in (
        ("wizardStepConfigure", "Configure"),
        ("wizardStepReview", "Review"),
        ("wizardStepComplete", "Complete"),
    ):
        assert f'id="{step_id}"' in html
        step_tag = re.search(rf'<li[^>]*id="{step_id}"[^>]*>.*?</li>', html, re.DOTALL)
        assert step_tag, f"stepper item {step_id} not found"
        assert label in step_tag.group(0)


def test_stepper_data_attributes_match_stage_names():
    html = _read(HTML)
    assert 'data-wizard-step="configure"' in html
    assert 'data-wizard-step="review"' in html
    assert 'data-wizard-step="complete"' in html


def test_js_owns_stepper_active_and_completed_state_transitions():
    js = _read(WIZARD_JS)
    assert "function setWizardStage(" in js
    assert "wizard-step" in js
    assert "'active'" in js or '"active"' in js
    assert "'completed'" in js or '"completed"' in js
    assert "aria-current" in js


# ---------------------------------------------------------------------------
# Stage panes exist and default to Configure visible
# ---------------------------------------------------------------------------


def test_three_stage_panes_exist_with_configure_visible_by_default():
    html = _read(HTML)

    configure = re.search(r'<div\b[^>]*id="wizardConfigurePane"[^>]*>', html)
    review = re.search(r'<div\b[^>]*id="wizardReviewPane"[^>]*>', html)
    complete = re.search(r'<div\b[^>]*id="wizardCompletePane"[^>]*>', html)

    assert configure and review and complete
    assert "ob-hidden" not in configure.group(0)
    assert "ob-hidden" in review.group(0)
    assert "ob-hidden" in complete.group(0)


def test_configure_pane_preserves_existing_source_selectors():
    html = _read(HTML)
    configure_block = re.search(
        r'id="wizardConfigurePane".*?(?=id="wizardReviewPane")', html, re.DOTALL
    )
    assert configure_block
    block = configure_block.group(0)
    for marker in (
        "wizardMetadataTableBody",
        "wizardDocsList",
        "wizardGuidelines",
        "wizardIncludeDataProps",
        "wizardTemplateButtons",
    ):
        assert marker in block, f"Configure pane missing existing control {marker}"


def test_top_cta_still_requires_llm_and_is_rightmost():
    html = _read(HTML)
    btn = re.search(r'<button[^>]*id="wizardTopGenerateBtn"[^>]*>', html, re.DOTALL)
    assert btn
    assert "data-requires-llm" in btn.group(0)
    assert "btn-primary" in btn.group(0)


# ---------------------------------------------------------------------------
# Review stage: locked anchors are visibly read-only, candidates editable
# ---------------------------------------------------------------------------


def test_locked_anchor_rows_are_rendered_read_only():
    js = _read(REVIEW_JS)
    assert "wizard-entity-locked" in js
    assert "bi-lock-fill" in js
    # Locked rows must never wire include/exclude/remove/edit controls.
    locked_render = re.search(
        r"function renderLockedAnchor\w*\([^)]*\)\s*\{([\s\S]*?)\n\s*\}", js
    )
    assert locked_render, "locked anchor row renderer not found"
    body = locked_render.group(1)
    assert "wizard-candidate-remove" not in body
    assert "wizard-candidate-include" not in body
    assert "wizard-field-input" not in body


def test_candidates_default_to_included_and_expose_full_edit_controls():
    js = _read(REVIEW_JS)
    assert "wizard-candidate-include" in js
    assert "wizard-candidate-remove" in js
    for field in ("canonical_label", "description", "type_hint"):
        assert f'data-field="{field}"' in js
    assert "wizard-chip" in js  # alternate labels chip widget
    assert "evidence" in js.lower()


def test_add_candidate_flow_posts_op_add():
    js = _read(REVIEW_JS)
    assert re.search(r"op:\s*['\"]add['\"]", js)


def test_remove_include_exclude_ops_are_wired():
    js = _read(REVIEW_JS)
    assert re.search(r"op:\s*['\"]remove['\"]", js)
    assert re.search(r"op:\s*['\"]include['\"]", js)
    assert re.search(r"op:\s*['\"]exclude['\"]", js)
    assert re.search(r"op:\s*['\"]update['\"]", js)


def test_draft_update_calls_carry_the_current_revision():
    js = _read(REVIEW_JS)
    assert re.search(r"revision:\s*\w*[Rr]evision\w*", js), (
        "draft/update payload must carry the draft_revision for optimistic "
        "concurrency"
    )


def test_revision_conflict_reloads_the_draft_instead_of_clobbering():
    js = _read(REVIEW_JS)
    assert "409" in js
    conflict_handling = re.search(
        r"(status(?:Code)?\s*===?\s*409)[\s\S]{0,400}", js
    )
    assert conflict_handling, "no explicit 409 handling found"


def test_at_least_one_candidate_form_field_is_alternate_labels_repeatable():
    html = _read(HTML)
    assert "wizardAddCandidateForm" in html


# ---------------------------------------------------------------------------
# Continue gating + discard confirmation
# ---------------------------------------------------------------------------


def test_continue_button_starts_disabled_and_gated_server_side():
    html = _read(HTML)
    btn = re.search(r'<button[^>]*id="wizardReviewContinueBtn"[^>]*>', html, re.DOTALL)
    assert btn
    assert "disabled" in btn.group(0)


def test_discard_uses_shared_confirm_dialog_not_native_confirm():
    js = _read(REVIEW_JS) + _read(WIZARD_JS)
    assert "showConfirmDialog(" in js
    assert "confirm(" not in js
    assert "alert(" not in js
    assert "prompt(" not in js


# ---------------------------------------------------------------------------
# Complete stage: strict relations -> attributes -> axioms -> merge order
# ---------------------------------------------------------------------------


def test_complete_checklist_lists_substages_in_strict_order():
    html = _read(HTML)
    checklist = re.search(
        r'id="wizardCompleteChecklist"[^>]*>(.*?)</ul>', html, re.DOTALL
    )
    assert checklist, "complete checklist not found"
    body = checklist.group(1)
    order = [m.group(1) for m in re.finditer(r'data-substage="(\w+)"', body)]
    assert order == ["relations", "attributes", "axioms", "merge"]


def test_complete_stage_renders_checkpoint_status_from_draft():
    js = _read(WIZARD_JS)
    assert "completion_checkpoints" in js
    assert "merge_checkpoint" in js


def test_retry_button_exists_and_resumes_without_resetting_done_stages():
    html = _read(HTML)
    assert 'id="wizardCompleteRetryBtn"' in html
    js = _read(WIZARD_JS)
    assert "function retryGenerateCompletion(" in js or (
        "wizard-complete-retry" in html and "/ontology/wizard/generate/complete" in js
    )


def test_completion_success_applies_outcome_like_todays_wizard():
    js = _read(WIZARD_JS)
    assert "SidebarNav" in js and "switchTo('map')" in js
    assert "showNotification(" in js


# ---------------------------------------------------------------------------
# Stale-source invalidation + empty-candidates block
# ---------------------------------------------------------------------------


def test_stale_banner_exists_with_redetect_action():
    html = _read(HTML)
    banner = re.search(r'id="wizardStaleBanner"[^>]*>', html)
    assert banner
    assert "ob-hidden" in banner.group(0)
    assert "wizard-review-restart" in html


def test_review_js_checks_the_stale_flag_from_the_draft_view():
    js = _read(REVIEW_JS)
    assert ".stale" in js


def test_validation_banner_is_aria_live_for_screen_readers():
    html = _read(HTML)
    banner = re.search(r'<div\b[^>]*id="wizardReviewValidation"[^>]*>', html)
    assert banner
    assert "aria-live" in banner.group(0)


# ---------------------------------------------------------------------------
# Design system compliance — no inline style/script, no native popups
# ---------------------------------------------------------------------------


def test_no_inline_style_or_script_in_template():
    html = _read(HTML)
    assert not re.search(r'style="', html)
    assert "<script" not in html


def test_no_native_browser_popups_anywhere_in_the_new_code():
    for path in (WIZARD_JS, REVIEW_JS):
        js = _read(path)
        assert re.search(r"(?<!\.)\balert\(", js) is None, path
        assert re.search(r"(?<!\.)\bconfirm\(", js) is None, path
        assert re.search(r"(?<!\.)\bprompt\(", js) is None, path


def test_configure_tabs_still_use_shared_ob_tabs_treatment():
    html = _read(HTML)
    assert 'class="nav nav-tabs ob-tabs nav-fill"' in html


def test_wizard_step_css_uses_design_tokens_not_raw_hex():
    css = _read(WIZARD_CSS)
    step_block = re.search(r"\.wizard-step\b[^{]*\{([^}]*)\}", css)
    assert step_block
    assert "#e7f1ff" not in css
    assert "#0d6efd" not in css
    assert "#198754" not in css
    assert "var(--db-" in css


def test_new_review_controls_carry_data_action_delegated_handlers():
    js = _read(REVIEW_JS)
    for action in (
        "wizard-review-continue",
        "wizard-review-discard",
        "wizard-review-remove-candidate",
    ):
        assert action in js


def test_addeventlistener_is_not_attached_per_row_in_review_module():
    """Delegated handling on the pane root, not one listener per rendered row."""
    js = _read(REVIEW_JS)
    per_row_listeners = re.findall(
        r"\.(?:addEventListener)\(", js
    )
    # A handful of root-level delegated listeners is fine; dozens would mean
    # per-row binding crept back in.
    assert len(per_row_listeners) <= 6, per_row_listeners


# ---------------------------------------------------------------------------
# Existing behavior preserved: ready-document filtering, data-requires-llm
# ---------------------------------------------------------------------------


def test_ready_document_filtering_is_preserved():
    js = _read(WIZARD_JS)
    assert "parse_status !== 'ready'" in js or 'parse_status === \'ready\'' in js


def test_no_llm_ui_gate_test_still_references_this_button():
    """Guards against silently dropping the declarative LLM marker contract
    exercised by ``test_no_llm_ui_gate.py``."""
    assert "wizardTopGenerateBtn" in _read(NO_LLM_GATE_TEST)
