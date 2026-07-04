"""Test the __init__.py module of Schellenberg USB integration."""

from __future__ import annotations

from types import MappingProxyType
from uuid import UUID
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CMD_UP,
    CONF_BLIND_ID,
    CONF_COMMAND,
    CONF_DEVICE_ID,
    CONF_ENUM,
    CONF_SERIAL_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_TEST_COMMAND,
    SUBENTRY_TYPE_BLIND,
)


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create a mock config entry."""
    entry = ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Schellenberg USB",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        entry_id="test_entry_id",
        state=ConfigEntryState.NOT_LOADED,
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    # Manually add the entry to the internal dict to avoid async_add
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


def test_legacy_blind_id_is_backfilled_once(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test a legacy blind gets one persisted UUID that never changes."""
    from custom_components.schellenberg_usb import _async_backfill_blind_ids

    legacy = ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_ID: "ABC123"}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Legacy blind",
        unique_id="ABC123",
    )
    hass.config_entries.async_add_subentry(mock_config_entry, legacy)

    assert _async_backfill_blind_ids(hass, mock_config_entry)
    saved_blind_id = mock_config_entry.subentries[legacy.subentry_id].data[
        CONF_BLIND_ID
    ]
    assert str(UUID(saved_blind_id)) == saved_blind_id

    assert not _async_backfill_blind_ids(hass, mock_config_entry)
    assert (
        mock_config_entry.subentries[legacy.subentry_id].data[CONF_BLIND_ID]
        == saved_blind_id
    )


def test_backfill_replaces_duplicate_and_missing_blind_ids(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test every blind receives a different valid registry UUID."""
    from custom_components.schellenberg_usb import _async_backfill_blind_ids

    duplicate_id = "11111111-1111-4111-8111-111111111111"
    first = ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_ID: "ABC123", CONF_BLIND_ID: duplicate_id}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="First blind",
        unique_id="ABC123",
    )
    second = ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_ID: "DEF456", CONF_BLIND_ID: duplicate_id}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Second blind",
        unique_id="DEF456",
    )
    third = ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_ID: "789ABC"}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Third blind",
        unique_id="789ABC",
    )
    for subentry in (first, second, third):
        hass.config_entries.async_add_subentry(mock_config_entry, subentry)

    assert _async_backfill_blind_ids(hass, mock_config_entry)
    saved_ids = [
        mock_config_entry.subentries[subentry.subentry_id].data[CONF_BLIND_ID]
        for subentry in (first, second, third)
    ]

    assert saved_ids[0] == duplicate_id
    assert len(set(saved_ids)) == 3
    assert all(str(UUID(blind_id)) == blind_id for blind_id in saved_ids)
    assert not _async_backfill_blind_ids(hass, mock_config_entry)


@pytest.mark.asyncio
async def test_async_setup_entry_basic(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test basic async_setup_entry functionality."""
    from custom_components.schellenberg_usb import async_setup_entry

    with (
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_forward,
    ):
        result = await async_setup_entry(hass, mock_config_entry)

        assert result is True
        mock_forward.assert_called_once_with(mock_config_entry, PLATFORMS)
        # Check that runtime_data was set
        assert mock_config_entry.runtime_data is not None
        assert isinstance(mock_config_entry.runtime_data, SchellenbergUsbApi)


@pytest.mark.asyncio
async def test_subentry_change_listener_reloads_once_and_is_removed_on_unload(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test subentry reload listeners do not accumulate across reloads."""
    from custom_components.schellenberg_usb import async_setup_entry

    with (
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch.object(
            hass.config_entries, "async_reload", new_callable=AsyncMock
        ) as reload_entry,
    ):
        await async_setup_entry(hass, mock_config_entry)
        hass.config_entries.async_add_subentry(
            mock_config_entry,
            ConfigSubentry(
                data=MappingProxyType({CONF_DEVICE_ID: "F2B8D5"}),
                subentry_type=SUBENTRY_TYPE_BLIND,
                title="Sitting room door",
                unique_id="F2B8D5",
            ),
        )
        await hass.async_block_till_done()

        reload_entry.assert_awaited_once_with(mock_config_entry.entry_id)
        assert len(mock_config_entry.update_listeners) == 1
        await mock_config_entry._async_process_on_unload(hass)

    assert mock_config_entry.update_listeners == []


@pytest.mark.asyncio
async def test_async_setup_registers_test_command_service(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test the diagnostic service validates and forwards a command."""
    from custom_components.schellenberg_usb import async_setup

    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api.control_blind = AsyncMock(return_value=True)  # type: ignore[method-assign]
    mock_config_entry.runtime_data = api

    assert await async_setup(hass, {})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_COMMAND,
        {
            CONF_DEVICE_ID: "f2b8d5",
            CONF_ENUM: "23",
            CONF_COMMAND: "open",
        },
        blocking=True,
    )

    api.control_blind.assert_awaited_once_with(  # type: ignore[attr-defined]
        "23", CMD_UP, device_id="F2B8D5", source="service"
    )


@pytest.mark.asyncio
async def test_async_setup_entry_creates_hub_device(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test that async_setup_entry creates a hub device."""
    from custom_components.schellenberg_usb import async_setup_entry

    with (
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
    ):
        result = await async_setup_entry(hass, mock_config_entry)

        assert result is True

        # Check that a hub device was created
        device_registry = dr.async_get(hass)
        hub_device = device_registry.async_get_device(
            identifiers={(DOMAIN, mock_config_entry.entry_id)}
        )
        assert hub_device is not None
        assert hub_device.name == "Schellenberg USB Stick"


@pytest.mark.asyncio
async def test_async_unload_entry(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test async_unload_entry disconnects and cleans up resources."""
    from custom_components.schellenberg_usb import async_setup_entry, async_unload_entry

    with (
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", new_callable=AsyncMock
        ) as mock_unload,
    ):
        # First setup the entry
        await async_setup_entry(hass, mock_config_entry)
        mock_unload.return_value = True

        # Now unload it
        with patch.object(
            SchellenbergUsbApi, "disconnect", new_callable=AsyncMock
        ) as mock_disconnect:
            result = await async_unload_entry(hass, mock_config_entry)

            assert result is True
            mock_unload.assert_called_once_with(mock_config_entry, PLATFORMS)
            mock_disconnect.assert_called_once()
