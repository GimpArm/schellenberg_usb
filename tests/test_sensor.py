"""Tests for sensor platform."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.schellenberg_usb.sensor import (
    SchellenbergConnectionSensor,
    SchellenbergModeSensor,
    SchellenbergVersionSensor,
    async_setup_entry,
)


@pytest.fixture
def mock_api(hass: HomeAssistant) -> SchellenbergUsbApi:
    """Create a mock API."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connected = True
    api._device_version = "RFTU_V20"
    api._device_mode = "listening"
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
        entry_id="test_entry_sensor",
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
async def test_async_setup_entry_creates_sensors(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that setup entry creates all sensor entities."""
    mock_config_entry.runtime_data = mock_api

    mock_add_entities = MagicMock()

    await async_setup_entry(hass, mock_config_entry, mock_add_entities)

    # Should create 3 sensors
    mock_add_entities.assert_called_once()
    entities = mock_add_entities.call_args[0][0]
    assert len(entities) == 3
    assert isinstance(entities[0], SchellenbergConnectionSensor)
    assert isinstance(entities[1], SchellenbergVersionSensor)
    assert isinstance(entities[2], SchellenbergModeSensor)


@pytest.mark.asyncio
async def test_connection_sensor_connected(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test connection sensor when connected."""
    sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Connected"
    assert sensor.available is True
    assert sensor.icon == "mdi:usb"


@pytest.mark.asyncio
async def test_connection_sensor_disconnected(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test connection sensor when disconnected."""
    mock_api._is_connected = False
    sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Disconnected"
    assert sensor.available is False
    assert sensor.icon == "mdi:usb-off"


@pytest.mark.asyncio
async def test_version_sensor(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test version sensor."""
    sensor = SchellenbergVersionSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "RFTU_V20"
    assert sensor.available is True
    assert sensor.icon == "mdi:chip"


@pytest.mark.asyncio
async def test_version_sensor_no_version(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test version sensor when version is None."""
    mock_api._device_version = None
    sensor = SchellenbergVersionSensor(mock_api, mock_config_entry)

    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_mode_sensor_listening(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test mode sensor in listening mode."""
    sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Listening"
    assert sensor.available is True
    assert sensor.icon == "mdi:ear-hearing"


@pytest.mark.asyncio
async def test_mode_sensor_bootloader(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test mode sensor in bootloader mode."""
    mock_api._device_mode = "bootloader"
    sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Bootloader"
    assert sensor.icon == "mdi:restart"


@pytest.mark.asyncio
async def test_mode_sensor_initial(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test mode sensor in initial mode."""
    mock_api._device_mode = "initial"
    sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Initial"
    assert sensor.icon == "mdi:power"


@pytest.mark.asyncio
async def test_mode_sensor_unknown(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test mode sensor in unknown mode."""
    mock_api._device_mode = "unknown"
    sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert sensor.native_value == "Unknown"
    assert sensor.icon == "mdi:help-circle"


@pytest.mark.asyncio
async def test_mode_sensor_none(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test mode sensor when mode is None."""
    mock_api._device_mode = None
    sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_sensor_unique_ids(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that sensors have unique IDs."""
    connection_sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)
    version_sensor = SchellenbergVersionSensor(mock_api, mock_config_entry)
    mode_sensor = SchellenbergModeSensor(mock_api, mock_config_entry)

    assert connection_sensor.unique_id == "test_entry_sensor_connection"
    assert version_sensor.unique_id == "test_entry_sensor_version"
    assert mode_sensor.unique_id == "test_entry_sensor_mode"


@pytest.mark.asyncio
async def test_sensor_device_info(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that sensors have correct device info."""
    sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)

    assert sensor.device_info is not None
    assert sensor.device_info["identifiers"] == {(DOMAIN, "test_entry_sensor")}
    assert sensor.device_info["name"] == "Schellenberg USB Stick"
    assert sensor.device_info["manufacturer"] == "Schellenberg"


@pytest.mark.asyncio
async def test_sensor_status_update_callback(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that sensors handle status update callbacks."""
    sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)
    sensor.hass = hass

    with patch.object(sensor, "async_write_ha_state") as mock_write:
        sensor._handle_status_update()
        mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_sensor_async_added_to_hass(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test sensor subscribes to status updates when added to hass."""
    sensor = SchellenbergConnectionSensor(mock_api, mock_config_entry)
    sensor.hass = hass

    with patch(
        "custom_components.schellenberg_usb.sensor.async_dispatcher_connect"
    ) as mock_connect:
        await sensor.async_added_to_hass()
        mock_connect.assert_called_once()
