"""Test config-flow menu translation coverage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.schellenberg_usb.config_flow import (
    DEVELOPER_TOOLS_MENU_OPTIONS,
)

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "schellenberg_usb"
EXPECTED_MENU_OPTIONS = {
    "user": {"pair_test", "pair_device", "manual"},
    "manual_next": {"test_motor", "save_manual"},
    "test_success": {"calibration_close", "manual_times"},
    "reconfigure": {"edit", "test_existing", "developer_tools", "calibrate"},
    "developer_tools": set(DEVELOPER_TOOLS_MENU_OPTIONS),
}


def _load_json(path: Path) -> dict[str, Any]:
    """Load one integration translation document."""
    return json.loads(path.read_text(encoding="utf-8"))


def _leaf_paths(value: Any, prefix: str = "") -> set[str]:
    """Return all leaf paths in a nested translation document."""
    if not isinstance(value, dict):
        return {prefix}
    return {
        leaf
        for key, child in value.items()
        for leaf in _leaf_paths(child, f"{prefix}.{key}" if prefix else key)
    }


def test_developer_tools_runtime_fallback_labels_are_non_empty() -> None:
    """Every runtime Developer Tools action has a visible fallback label."""
    assert DEVELOPER_TOOLS_MENU_OPTIONS["reset_stick"] == (
        "Reset stick / reconnect serial"
    )
    assert all(
        isinstance(label, str) and label.strip()
        for label in DEVELOPER_TOOLS_MENU_OPTIONS.values()
    )


def test_all_config_subentry_menu_options_have_labels() -> None:
    """Every runtime menu option must have a non-empty label in every locale."""
    paths = [
        COMPONENT_DIR / "strings.json",
        *sorted((COMPONENT_DIR / "translations").glob("*.json")),
    ]

    for path in paths:
        steps = _load_json(path)["config_subentries"]["blind"]["step"]
        for step_id, expected_options in EXPECTED_MENU_OPTIONS.items():
            labels = steps[step_id]["menu_options"]
            assert set(labels) == expected_options, f"{path.name}: {step_id}"
            assert all(
                isinstance(label, str) and label.strip() for label in labels.values()
            ), f"{path.name}: {step_id} contains a blank label"


def test_locale_files_cover_all_source_translation_keys() -> None:
    """Partial locale files must not hide newer English flow labels."""
    source_paths = _leaf_paths(_load_json(COMPONENT_DIR / "strings.json"))

    for path in sorted((COMPONENT_DIR / "translations").glob("*.json")):
        missing = source_paths - _leaf_paths(_load_json(path))
        assert not missing, f"{path.name} is missing: {sorted(missing)}"
