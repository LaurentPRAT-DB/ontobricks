"""Structural contracts for the global fail-closed No-LLM UI gate."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pages_start_with_fail_closed_llm_state():
    assert "llm-unconfigured" in _read("src/front/templates/base.html")


def test_global_gate_uses_navbar_state_and_attribute_markers():
    permissions = _read("src/front/static/global/js/permissions.js")
    navbar = _read("src/front/static/global/js/navbar.js")
    css = _read("src/front/static/global/css/permissions.css")

    assert "updateLlmAvailability" in permissions
    assert "data-requires-llm" in permissions
    assert "event.stopImmediatePropagation()" in permissions
    assert "info.llm_endpoint" in navbar
    assert "llm-unconfigured" in css
    assert "[data-requires-llm]" in css


def test_gate_preserves_owned_aria_state_without_changing_native_disabled():
    permissions = _read("src/front/static/global/js/permissions.js")

    assert "const llmAriaState = new WeakMap();" in permissions
    assert "control.getAttribute('aria-disabled')" in permissions
    assert "control.removeAttribute('aria-disabled')" in permissions
    assert "llmAriaState.delete(control)" in permissions
    assert "control.disabled" not in permissions
    assert "setAttribute('disabled'" not in permissions
    assert "removeAttribute('disabled'" not in permissions


def test_gate_blocks_pointer_and_activation_keys_in_capture_phase():
    permissions = _read("src/front/static/global/js/permissions.js")

    assert "document.addEventListener('click', blockUnavailableLlmControl, true)" in permissions
    assert "document.addEventListener('keydown', blockUnavailableLlmControl, true)" in permissions
    assert "event.key !== 'Enter' && event.key !== ' '" in permissions
    assert "event.preventDefault()" in permissions
    assert (
        "'No LLM selected. Select one in Domain Information → AI.'" in permissions
    )


def test_dynamic_controls_are_synchronized_by_one_observer():
    permissions = _read("src/front/static/global/js/permissions.js")

    assert permissions.count("new MutationObserver(") == 1
    assert "node.matches('[data-requires-llm]')" in permissions
    assert "node.querySelectorAll('[data-requires-llm]')" in permissions
    assert "childList: true, subtree: true" in permissions


def test_navbar_updates_gate_and_fails_closed():
    navbar = _read("src/front/static/global/js/navbar.js")

    assert (
        "window.OB?.updateLlmAvailability("
        "Boolean(data.info?.llm_endpoint?.trim())"
    ) in navbar
    assert "window.OB?.updateLlmAvailability(false)" in navbar


def test_llm_availability_style_follows_competing_permission_rules():
    css = _read("src/front/static/global/css/permissions.css")
    llm_gate = css.index("body.llm-unconfigured [data-requires-llm]")
    last_permission_rule = css.rindex("#editLockBanner .edit-lock-actions")

    assert llm_gate > last_permission_rule
