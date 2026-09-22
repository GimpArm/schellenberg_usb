"""Tests for __init__.py: the field validators, blind-ID backfill, the
test_command service, and the full async_setup_entry / async_unload_entry
lifecycle.

The full-lifecycle test (see "async_setup_entry / async_unload_entry: full
lifecycle" below) drives hass.config_entries.async_setup()/async_unload() for
real - the auto-created hub subentry, the hub device-registry entry, and
forwarding to the cover/sensor/switch platforms all run unmocked - and
patches only the two SchellenbergUsbApi methods that would otherwise touch a
real serial port (connect/disconnect). It covers a hub with zero saved blind
subentries: all three platforms handle that gracefully (cover.py logs and
returns without adding any entities; sensor.py and switch.py always add
their fixed hub-level entities regardless of blind subentries), so no blind
subentry needs to be set up just to exercise this path. Loading a real blind
subentry through this same full-lifecycle path is not covered here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb import (
    _async_backfill_blind_ids,
    _validate_device_enum,
    _validate_device_id,
    async_setup,
    async_setup_entry,
)
from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CMD_UP,
    CONF_BLIND_ID,
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENUM,
    CONF_SERIAL_PORT,
    DOMAIN,
    SERVICE_TEST_COMMAND,
    SUBENTRY_TYPE_BLIND,
    SUBENTRY_TYPE_HUB,
)


def _stub_status_attrs(api: MagicMock) -> None:
    """Fill in the status properties the service handler logs on every call.

    These are all real @property attributes on SchellenbergUsbApi (verified
    directly against api.py), so MagicMock(spec=SchellenbergUsbApi) allows
    setting them freely even though the real class exposes them read-only.
    """
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    api.transmit_block_reason = None


def make_hub_entry(hass: HomeAssistant, api: MagicMock, port: str = "/dev/ttyUSB0"):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: port})
    entry.add_to_hass(hass)
    entry.runtime_data = api
    return entry


def make_blind_subentry(subentry_id: str, blind_id: str | None) -> MagicMock:
    subentry = MagicMock()
    subentry.subentry_id = subentry_id
    subentry.subentry_type = SUBENTRY_TYPE_BLIND
    subentry.title = f"Blind {subentry_id}"
    subentry.data = {CONF_BLIND_ID: blind_id} if blind_id is not None else {}
    return subentry


def make_hub_subentry(subentry_id: str = "hub1") -> MagicMock:
    subentry = MagicMock()
    subentry.subentry_id = subentry_id
    subentry.subentry_type = SUBENTRY_TYPE_HUB
    subentry.title = "Hub"
    subentry.data = {}
    return subentry


# --- _validate_device_id / _validate_device_enum ---------------------------


def test_validate_device_id_normalizes_case_and_whitespace() -> None:
    assert _validate_device_id(" 5d3e7c ") == "5D3E7C"


def test_validate_device_id_rejects_wrong_length() -> None:
    with pytest.raises(vol.Invalid):
        _validate_device_id("5D3E7")


def test_validate_device_id_rejects_non_hex_characters() -> None:
    with pytest.raises(vol.Invalid):
        _validate_device_id("GGGGGG")


def test_validate_device_enum_normalizes_case() -> None:
    assert _validate_device_enum("a1") == "A1"


def test_validate_device_enum_rejects_wrong_length() -> None:
    with pytest.raises(vol.Invalid):
        _validate_device_enum("A")


# --- _async_backfill_blind_ids ----------------------------------------------


def test_backfill_assigns_blind_id_when_missing(hass: HomeAssistant) -> None:
    subentry = make_blind_subentry("sub1", blind_id=None)
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}

    with patch.object(hass.config_entries, "async_update_subentry") as mock_update:
        changed = _async_backfill_blind_ids(hass, entry)

    assert changed is True
    mock_update.assert_called_once()
    args, kwargs = mock_update.call_args
    assert args[0] is entry
    assert args[1] is subentry
    assert kwargs["data"][CONF_BLIND_ID]  # a fresh, non-empty ID was assigned


def test_backfill_leaves_valid_existing_blind_id_untouched(hass: HomeAssistant) -> None:
    existing_id = "12345678-1234-5678-1234-567812345678"
    subentry = make_blind_subentry("sub1", blind_id=existing_id)
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}

    with patch.object(hass.config_entries, "async_update_subentry") as mock_update:
        changed = _async_backfill_blind_ids(hass, entry)

    assert changed is False
    mock_update.assert_not_called()


def test_backfill_skips_non_blind_subentries(hass: HomeAssistant) -> None:
    entry = MagicMock()
    entry.subentries = {"hub1": make_hub_subentry()}

    with patch.object(hass.config_entries, "async_update_subentry") as mock_update:
        changed = _async_backfill_blind_ids(hass, entry)

    assert changed is False
    mock_update.assert_not_called()


# --- async_setup_entry's early guard ----------------------------------------


async def test_async_setup_entry_ignores_non_hub_entries(hass: HomeAssistant) -> None:
    """A config entry with no CONF_SERIAL_PORT must not try to open a serial
    connection - it returns False rather than constructing an API.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    result = await async_setup_entry(hass, entry)

    assert result is False


# --- async_setup_entry / async_unload_entry: full lifecycle ----------------


async def test_async_setup_entry_full_lifecycle_loads_and_unloads(
    hass: HomeAssistant,
) -> None:
    """End-to-end setup/unload through the real config-entry machinery.

    Only the two methods that would touch a real serial port
    (SchellenbergUsbApi.connect/disconnect) are mocked out - everything else
    (device registry, the auto-created hub subentry, forwarding to the
    cover/sensor/switch platforms, the update-listener registration, and
    unload/disconnect) runs for real. All three platforms handle a hub with
    no saved blind subentries gracefully (cover.py logs and returns; sensor.py
    and switch.py always add their fixed hub-level entities), so no blind
    subentry needs to be set up just to exercise this path.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"})
    entry.add_to_hass(hass)

    with (
        patch.object(
            SchellenbergUsbApi, "connect", AsyncMock(return_value=True)
        ) as mock_connect,
        patch.object(
            SchellenbergUsbApi, "disconnect", AsyncMock(return_value=None)
        ) as mock_disconnect,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert isinstance(entry.runtime_data, SchellenbergUsbApi)
        mock_connect.assert_awaited_once()

        hub_subentries = [
            s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_HUB
        ]
        assert len(hub_subentries) == 1

        device_registry = dr.async_get(hass)
        hub_device = device_registry.async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        assert hub_device is not None
        assert hub_device.config_subentry_id == hub_subentries[0].subentry_id

        entity_registry = er.async_get(hass)
        entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        assert sum(e.domain == "switch" for e in entities) == 1
        assert sum(e.domain == "sensor" for e in entities) == 3
        assert sum(e.domain == "cover" for e in entities) == 0

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        mock_disconnect.assert_awaited_once()

    assert entry.state is ConfigEntryState.NOT_LOADED


# --- test_command service ----------------------------------------------


async def test_service_test_command_success(hass: HomeAssistant) -> None:
    api = MagicMock(spec=SchellenbergUsbApi)
    api.control_blind = AsyncMock(return_value=True)
    _stub_status_attrs(api)
    make_hub_entry(hass, api)

    await async_setup(hass, {})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_COMMAND,
        {CONF_DEVICE_ID: "5d3e7c", CONF_ENUM: "a1", CONF_COMMAND: "open"},
        blocking=True,
    )

    api.control_blind.assert_awaited_once_with(
        "A1", CMD_UP, device_id="5D3E7C", source="service"
    )


async def test_service_test_command_selects_entry_by_config_entry_id(
    hass: HomeAssistant,
) -> None:
    api1 = MagicMock(spec=SchellenbergUsbApi)
    api1.control_blind = AsyncMock(return_value=True)
    _stub_status_attrs(api1)
    make_hub_entry(hass, api1, port="/dev/ttyUSB0")

    api2 = MagicMock(spec=SchellenbergUsbApi)
    api2.control_blind = AsyncMock(return_value=True)
    _stub_status_attrs(api2)
    entry2 = make_hub_entry(hass, api2, port="/dev/ttyUSB1")

    await async_setup(hass, {})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_COMMAND,
        {
            CONF_DEVICE_ID: "5D3E7C",
            CONF_ENUM: "01",
            CONF_COMMAND: "open",
            CONF_CONFIG_ENTRY_ID: entry2.entry_id,
        },
        blocking=True,
    )

    api1.control_blind.assert_not_awaited()
    api2.control_blind.assert_awaited_once()


async def test_service_test_command_rejects_invalid_device_id(
    hass: HomeAssistant,
) -> None:
    await async_setup(hass, {})
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {CONF_DEVICE_ID: "not-hex", CONF_ENUM: "01", CONF_COMMAND: "open"},
            blocking=True,
        )


async def test_service_test_command_fails_when_no_hub_loaded(hass: HomeAssistant) -> None:
    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {CONF_DEVICE_ID: "5D3E7C", CONF_ENUM: "01", CONF_COMMAND: "open"},
            blocking=True,
        )


async def test_service_test_command_raises_when_command_fails(hass: HomeAssistant) -> None:
    api = MagicMock(spec=SchellenbergUsbApi)
    api.control_blind = AsyncMock(return_value=False)
    _stub_status_attrs(api)
    api.transmit_block_reason = "stick busy"
    make_hub_entry(hass, api)

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {CONF_DEVICE_ID: "5D3E7C", CONF_ENUM: "01", CONF_COMMAND: "stop"},
            blocking=True,
        )
