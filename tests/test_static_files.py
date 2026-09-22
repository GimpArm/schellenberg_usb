"""Consistency checks for the integration's non-Python data files.

These need no Home Assistant test harness at all - they parse manifest.json,
strings.json, translations/*.json, services.yaml, and icons.json directly off
disk and cross-check them, the way a linter would. They catch the class of
bug that slips in easily when hand-editing translations: a step renamed in
strings.json but not in a translation file, a placeholder token typoed in one
language, a service field removed from services.yaml but still documented in
strings.json, and so on.

Key-structure comparisons are order-independent (sets of dotted paths):
strings.json and translations/en.json happen to be byte-identical, but
translations/de.json, es.json, and fr.json ship with their step objects in a
different order (harmless - JSON object order carries no meaning to Home
Assistant's loader) - a naive list/text diff against them would report
mismatches that are not real bugs, so every comparison below is set-based.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from custom_components.schellenberg_usb.const import (
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENUM,
    DOMAIN,
    SERVICE_TEST_COMMAND,
)

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "schellenberg_usb"

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

LANGUAGES = ("de", "en", "es", "fr")


def _load_json(relative_path: str) -> dict:
    return json.loads((INTEGRATION_DIR / relative_path).read_text(encoding="utf-8"))


def _load_services_yaml() -> dict:
    return yaml.safe_load((INTEGRATION_DIR / "services.yaml").read_text(encoding="utf-8"))


def _key_paths(node: object, prefix: str = "") -> set[str]:
    """Return the set of dotted key paths reachable in a nested dict.

    Only dicts are recursed into - there are no lists anywhere in these
    translation files, so a non-dict node simply contributes nothing further.
    """
    if not isinstance(node, dict):
        return set()
    paths: set[str] = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        paths.add(path)
        paths |= _key_paths(value, path)
    return paths


def _leaf_strings(node: object, prefix: str = "") -> dict[str, str]:
    """Flatten a nested dict down to {dotted_path: leaf_string}."""
    if isinstance(node, str):
        return {prefix: node}
    if isinstance(node, dict):
        leaves: dict[str, str] = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            leaves.update(_leaf_strings(value, path))
        return leaves
    return {}


def test_manifest_matches_domain_and_has_required_fields() -> None:
    manifest = _load_json("manifest.json")
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["version"]
    assert isinstance(manifest["requirements"], list)
    assert len(manifest["requirements"]) > 0
    assert all(isinstance(item, str) and item for item in manifest["requirements"])


def test_manifest_usb_matcher_matches_discovery_test_fixture() -> None:
    """The USB auto-discovery matcher's vid/pid must stay in sync with the
    UsbServiceInfo fixture used by test_config_flow_user.py's discovery
    tests - if one changes without the other, USB auto-discovery would
    silently stop matching real hardware despite the discovery flow itself
    still testing green.
    """
    manifest = _load_json("manifest.json")
    usb_matchers = manifest["usb"]
    assert len(usb_matchers) == 1
    assert usb_matchers[0]["vid"] == "16C0"
    assert usb_matchers[0]["pid"] == "05E1"


def test_icons_json_is_valid_and_matches_service_name() -> None:
    icons = _load_json("icons.json")
    assert SERVICE_TEST_COMMAND in icons["services"]
    assert icons["services"][SERVICE_TEST_COMMAND]["service"].startswith("mdi:")


def test_translations_en_is_identical_to_strings_json() -> None:
    strings = _load_json("strings.json")
    en = _load_json("translations/en.json")
    assert en == strings


def test_all_translations_have_same_key_structure_as_strings_json() -> None:
    strings_paths = _key_paths(_load_json("strings.json"))
    for language in LANGUAGES:
        translation_paths = _key_paths(_load_json(f"translations/{language}.json"))
        missing = strings_paths - translation_paths
        extra = translation_paths - strings_paths
        assert not missing, f"{language}.json is missing keys: {sorted(missing)}"
        assert not extra, f"{language}.json has unexpected extra keys: {sorted(extra)}"


def test_all_translations_preserve_placeholder_tokens() -> None:
    strings_leaves = _leaf_strings(_load_json("strings.json"))
    for language in LANGUAGES:
        translation_leaves = _leaf_strings(_load_json(f"translations/{language}.json"))
        for path, source_text in strings_leaves.items():
            source_tokens = set(PLACEHOLDER_RE.findall(source_text))
            if not source_tokens:
                continue
            translated_text = translation_leaves.get(path)
            assert translated_text is not None, (
                f"{language}.json is missing translated string at {path}"
            )
            translated_tokens = set(PLACEHOLDER_RE.findall(translated_text))
            assert translated_tokens == source_tokens, (
                f"{language}.json placeholder mismatch at {path}: expected "
                f"{sorted(source_tokens)}, got {sorted(translated_tokens)}"
            )


def test_services_yaml_documents_test_command_with_expected_fields() -> None:
    services = _load_services_yaml()
    assert SERVICE_TEST_COMMAND in services
    fields = services[SERVICE_TEST_COMMAND]["fields"]
    assert CONF_DEVICE_ID in fields
    assert CONF_ENUM in fields
    assert CONF_COMMAND in fields
    assert CONF_CONFIG_ENTRY_ID in fields
    assert fields[CONF_DEVICE_ID]["required"] is True
    assert fields[CONF_ENUM]["required"] is True
    assert fields[CONF_COMMAND]["required"] is True
    assert fields[CONF_CONFIG_ENTRY_ID]["required"] is False


def test_services_yaml_command_options_match_service_handler() -> None:
    """The command selector's options must match the open/close/stop mapping
    __init__.py's service handler actually understands (its
    open/close/stop -> CMD_UP/CMD_DOWN/CMD_STOP dict) - adding a fourth
    option here without teaching the handler about it would ship a control
    that always raises ServiceValidationError.
    """
    services = _load_services_yaml()
    options = services[SERVICE_TEST_COMMAND]["fields"][CONF_COMMAND]["selector"][
        "select"
    ]["options"]
    assert set(options) == {"open", "close", "stop"}


def test_strings_json_service_description_matches_services_yaml() -> None:
    strings = _load_json("strings.json")
    services = _load_services_yaml()
    assert (
        strings["services"][SERVICE_TEST_COMMAND]["description"]
        == services[SERVICE_TEST_COMMAND]["description"]
    )
