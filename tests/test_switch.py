"""Tests for switch platform."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, State

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.schellenberg_usb.switch import (
    SchellenbergLedSwitch,
    async_setup_entry,
)


@pytest.fixture
def mock_api(hass: HomeAssistant) -> SchellenbergUsbApi:
    """Create a mock API."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connected = True
    api._device_version = "RFTU_V20"
    api.led_on = AsyncMock()
    api.led_off = AsyncMock()
    return api


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create a mock config entry."""
    entry = ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Schellenberg USB",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        entry_id="test_entry_switch",
        state=ConfigEntryState.NOT_LOADED,
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.mark.asyncio
async def test_async_setup_entry_creates_led_switch(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that setup entry creates LED switch."""
    mock_config_entry.runtime_data = mock_api

    mock_add_entities = MagicMock()

    await async_setup_entry(hass, mock_config_entry, mock_add_entities)

    mock_add_entities.assert_called_once()
    entities = mock_add_entities.call_args[0][0]
    assert len(entities) == 1
    assert isinstance(entities[0], SchellenbergLedSwitch)


@pytest.mark.asyncio
async def test_led_switch_initialization(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch initialization."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)

    assert switch.unique_id == "test_entry_switch_led"
    assert switch.is_on is False
    assert switch.available is True
    assert switch.device_info is not None


@pytest.mark.asyncio
async def test_led_switch_turn_on(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test turning LED on."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch.object(switch, "async_write_ha_state") as mock_write:
        await switch.async_turn_on()

    mock_api.led_on.assert_called_once()
    assert switch.is_on is True
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_led_switch_turn_off(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test turning LED off."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass
    switch._is_on = True

    with patch.object(switch, "async_write_ha_state") as mock_write:
        await switch.async_turn_off()

    mock_api.led_off.assert_called_once()
    assert switch.is_on is False
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_led_switch_icon(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch icon changes with state."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)

    assert switch.icon == "mdi:led-off"

    switch._is_on = True
    assert switch.icon == "mdi:led-on"


@pytest.mark.asyncio
async def test_led_switch_availability(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch availability based on connection."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)

    assert switch.available is True

    mock_api._is_connected = False
    assert switch.available is False


@pytest.mark.asyncio
async def test_led_switch_restore_state_on(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch restores on state."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch.object(
        switch, "async_get_last_state", return_value=State("switch.led", "on")
    ):
        await switch.async_added_to_hass()

    assert switch._is_on is True
    mock_api.led_on.assert_called_once()


@pytest.mark.asyncio
async def test_led_switch_restore_state_off(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch restores off state."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch.object(
        switch, "async_get_last_state", return_value=State("switch.led", "off")
    ):
        await switch.async_added_to_hass()

    assert switch._is_on is False
    mock_api.led_off.assert_called_once()


@pytest.mark.asyncio
async def test_led_switch_no_previous_state(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch when no previous state exists."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch.object(switch, "async_get_last_state", return_value=None):
        await switch.async_added_to_hass()

    assert switch._is_on is False
    # Should not call led_on or led_off when no previous state
    mock_api.led_on.assert_not_called()
    mock_api.led_off.assert_not_called()


@pytest.mark.asyncio
async def test_led_switch_status_update_callback(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch handles status update callbacks."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch.object(switch, "async_write_ha_state") as mock_write:
        switch._handle_status_update()
        mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_led_switch_reconnection_restores_state(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch restores state on reconnection."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass
    switch._is_on = True
    switch._was_available = False

    with patch.object(switch, "async_write_ha_state"):
        # Simulate reconnection
        mock_api._is_connected = True
        switch._handle_status_update()

    # Should have queued task to restore hardware state
    await hass.async_block_till_done()
    # led_on should be called as part of restore
    assert mock_api.led_on.call_count >= 1


@pytest.mark.asyncio
async def test_led_switch_no_restore_when_already_connected(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch doesn't restore when already connected."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass
    switch._is_on = True
    switch._was_available = True  # Already was available

    mock_api.led_on.reset_mock()

    with patch.object(switch, "async_write_ha_state"):
        # Connection status unchanged
        mock_api._is_connected = True
        switch._handle_status_update()

    # Should not create restore task
    await hass.async_block_till_done()
    # led_on should not be called since we didn't transition from unavailable
    mock_api.led_on.assert_not_called()


@pytest.mark.asyncio
async def test_led_switch_device_info(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch has correct device info."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)

    assert switch.device_info is not None
    assert switch.device_info["identifiers"] == {(DOMAIN, "test_entry_switch")}
    assert switch.device_info["name"] == "Schellenberg USB Stick"
    assert switch.device_info["manufacturer"] == "Schellenberg"


@pytest.mark.asyncio
async def test_led_switch_subscribes_to_updates(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test LED switch subscribes to status updates."""
    switch = SchellenbergLedSwitch(mock_api, mock_config_entry)
    switch.hass = hass

    with patch(
        "custom_components.schellenberg_usb.switch.async_dispatcher_connect"
    ) as mock_connect:
        with patch.object(switch, "async_get_last_state", return_value=None):
            await switch.async_added_to_hass()

        mock_connect.assert_called_once()
