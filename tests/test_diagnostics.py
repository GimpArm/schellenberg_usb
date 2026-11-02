"""Tests for the Home Assistant diagnostics platform (diagnostics.py).

Supplements - does not duplicate - the tests for config_flow.py's own
async_step_copy_diagnostics step: these cover the separate, structured JSON
diagnostics Home Assistant's own "Download diagnostics" mechanism produces,
both for the whole hub (config entry) and for a single blind (device).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DEFAULT_TRAVEL_TIME_SECONDS,
    DOMAIN,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
    SUBENTRY_TYPE_HUB,
)
from custom_components.schellenberg_usb.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)


def _build_hub_entry(hass: HomeAssistant, api: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake-schellenberg"}
    )
    entry.add_to_hass(hass)
    entry.runtime_data = api
    return entry


def _add_blind_subentry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    unique_id: str = "ABCDEF",
    title: str = "Living Room Blind",
    data: dict[str, Any] | None = None,
) -> ConfigSubentry:
    if data is None:
        data = {
            CONF_COMMAND_DEVICE_ID: "ABCDEF",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "ABCDEF",
            CONF_STATUS_ENUM: "10",
        }
    subentry = ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title=title,
        unique_id=unique_id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics(): the whole hub.
# ---------------------------------------------------------------------------


async def test_config_entry_diagnostics_redacts_the_serial_port(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry_data"][CONF_SERIAL_PORT] == REDACTED


async def test_config_entry_diagnostics_reports_stick_state(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["stick"]["is_connected"] is True
    assert result["stick"]["device_mode"] == "listening"


async def test_config_entry_diagnostics_lists_every_blind_subentry(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(hass, entry, unique_id="AAAAAA", title="Living Room")
    _add_blind_subentry(
        hass,
        entry,
        unique_id="BBBBBB",
        title="Kitchen",
        data={CONF_COMMAND_DEVICE_ID: "BBBBBB", CONF_COMMAND_ENUM: "20"},
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    names = {blind["name"] for blind in result["blinds"]}
    assert names == {"Living Room", "Kitchen"}


async def test_blind_diagnostics_includes_identities_and_settings(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    (blind,) = result["blinds"]
    assert blind["command_device_id"] == "ABCDEF"
    assert blind["primary_status_identity"] == ("ABCDEF", "10")
    # Neither CONF_OPEN_TIME nor CONF_CLOSE_TIME was set on this subentry -
    # must fall back to the same default cover.py itself uses, not 0/None.
    assert blind["open_time"] == DEFAULT_TRAVEL_TIME_SECONDS
    assert blind["close_time"] == DEFAULT_TRAVEL_TIME_SECONDS


async def test_blind_diagnostics_includes_last_frame_and_position_data(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(hass, entry)
    connected_api._last_received_messages[("ABCDEF", "10")] = {
        "device_id": "ABCDEF",
        "enum": "10",
        "command": "01",
        "sequence": 1,
    }
    connected_api.record_position_update(
        "ABCDEF",
        source="frame",
        direction="opening",
        previous_position=0,
        new_position=50,
        status="estimated",
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    (blind,) = result["blinds"]
    assert blind["last_matched_frame"]["command"] == "01"
    assert blind["last_position_update"]["new_position"] == 50
    # Only _last_received_messages was populated above, not the separate
    # _last_primary_tracking_messages dict get_last_primary_tracking_frame()
    # reads from - must come back None, not crash or return the wrong frame.
    assert blind["last_primary_frame"] is None


async def test_blind_diagnostics_handles_a_blind_with_no_confirmed_status_identity(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Regression: primary_identity is None whenever
    CONF_STATUS_IDENTITY_SOURCE is STATUS_IDENTITY_SOURCE_UNKNOWN -
    get_last_primary_tracking_frame(*primary_identity) would crash with a
    TypeError (unpacking None) if _blind_diagnostics did not guard this
    explicitly.
    """
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(
        hass,
        entry,
        data={
            CONF_COMMAND_DEVICE_ID: "ABCDEF",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
        },
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    (blind,) = result["blinds"]
    assert blind["primary_status_identity"] is None
    assert blind["last_primary_frame"] is None


# ---------------------------------------------------------------------------
# async_get_device_diagnostics(): one blind, looked up by device.
# ---------------------------------------------------------------------------


async def test_device_diagnostics_returns_the_matching_blind(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_blind_subentry(hass, entry, title="Living Room")
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=blind.subentry_id,
        identifiers={(DOMAIN, "ABCDEF")},
        name="Living Room",
    )

    result = await async_get_device_diagnostics(hass, entry, device)

    assert result["name"] == "Living Room"
    assert result["command_device_id"] == "ABCDEF"


async def test_device_diagnostics_matches_the_right_blind_among_several(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Regression: must look up by actual subentry membership, not just
    return the hub's first blind subentry regardless of which device asked.
    """
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(
        hass,
        entry,
        unique_id="AAAAAA",
        title="Living Room",
        data={CONF_COMMAND_DEVICE_ID: "AAAAAA", CONF_COMMAND_ENUM: "10"},
    )
    kitchen = _add_blind_subentry(
        hass,
        entry,
        unique_id="BBBBBB",
        title="Kitchen",
        data={CONF_COMMAND_DEVICE_ID: "BBBBBB", CONF_COMMAND_ENUM: "20"},
    )
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=kitchen.subentry_id,
        identifiers={(DOMAIN, "BBBBBB")},
        name="Kitchen",
    )

    result = await async_get_device_diagnostics(hass, entry, device)

    assert result["name"] == "Kitchen"
    assert result["command_device_id"] == "BBBBBB"


async def test_device_diagnostics_returns_the_hub_sticks_state(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Regression for audit finding 10: the hub (USB stick) is registered
    as its own device, tied to its own SUBENTRY_TYPE_HUB subentry rather
    than any blind's - see __init__.py's async_setup_entry(). Downloading
    diagnostics from *that* device's own page used to always fall through
    to the generic error below, since the lookup only ever checked
    SUBENTRY_TYPE_BLIND subentries and skipped every other subentry type,
    the hub included, outright.
    """
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(hass, entry)
    hub_subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="Hub",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, hub_subentry)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=hub_subentry.subentry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Schellenberg USB Stick",
    )

    result = await async_get_device_diagnostics(hass, entry, device)

    assert result["is_connected"] is True
    assert result["device_mode"] == "listening"
    # The hub's own stick-state shape (_stick_diagnostics), not a blind's
    # (_blind_diagnostics) - which would have a "name" key instead.
    assert "name" not in result


async def test_device_diagnostics_returns_an_error_for_an_unmatched_device(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Defensive: a device not on any blind subentry of this entry (should
    not happen in practice - Home Assistant only calls this for a device it
    already knows belongs to the entry) must not crash, just report the
    mismatch plainly.
    """
    entry = _build_hub_entry(hass, connected_api)
    _add_blind_subentry(hass, entry)
    other_entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/other"})
    other_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    unrelated_device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, "ZZZZZZ")},
        name="Unrelated",
    )

    result = await async_get_device_diagnostics(hass, entry, unrelated_device)

    assert "error" in result
