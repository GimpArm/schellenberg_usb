"""Test the API module for Schellenberg USB integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import PAIRING_TIMEOUT


@pytest.mark.asyncio
async def test_api_initialization(hass: HomeAssistant) -> None:
    """Test API initialization."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    assert api.hass == hass
    assert api.port == "/dev/ttyUSB0"
    assert api.is_connected is False
    assert api._registered_devices == {}


@pytest.mark.asyncio
async def test_api_register_existing_devices(hass: HomeAssistant) -> None:
    """Test registering existing devices."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    devices = [
        {"id": "device_1", "enum": "0x10", "name": "Blind 1"},
        {"id": "device_2", "enum": "0x11", "name": "Blind 2"},
    ]

    api.register_existing_devices(devices)

    assert api._registered_devices == {
        "device_1": "0x10",
        "device_2": "0x11",
    }


@pytest.mark.asyncio
async def test_api_remove_known_device(hass: HomeAssistant) -> None:
    """Test removing a known device."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    api._registered_devices = {
        "device_1": "0x10",
        "device_2": "0x11",
    }

    api.remove_known_device("device_1")

    assert api._registered_devices == {"device_2": "0x11"}


@pytest.mark.asyncio
async def test_api_get_last_device_enum(hass: HomeAssistant) -> None:
    """Test getting the last device enum."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    api._last_paired_device_enum = "0x15"

    result = api.get_last_device_enum()

    assert result == "0x15"


@pytest.mark.asyncio
async def test_api_connect_success(hass: HomeAssistant) -> None:
    """Test successful connection."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    with patch(
        "serial_asyncio.create_serial_connection", new_callable=AsyncMock
    ) as mock_create:
        mock_transport = MagicMock()
        mock_protocol = MagicMock()
        mock_create.return_value = (mock_transport, mock_protocol)

        await api.connect()

        assert api._is_connecting is False
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_api_connect_already_connecting(hass: HomeAssistant) -> None:
    """Test connect when already connecting."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connecting = True

    with patch(
        "serial_asyncio.create_serial_connection", new_callable=AsyncMock
    ) as mock_create:
        await api.connect()

        # Should not call create_serial_connection
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_api_disconnect(hass: HomeAssistant) -> None:
    """Test disconnection."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    api._transport = mock_transport

    await api.disconnect()

    mock_transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_api_send_command(hass: HomeAssistant) -> None:
    """Test sending a command."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_protocol = MagicMock()
    api._transport = mock_transport
    api._protocol = mock_protocol
    api._is_connected = True

    api.send_command("test_command")

    mock_protocol.send_command.assert_called_once_with("test_command")


@pytest.mark.asyncio
async def test_api_pair_device_and_wait_timeout(hass: HomeAssistant) -> None:
    """Test pairing with timeout."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connected = True

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = TimeoutError()

        result = await api.pair_device_and_wait()

        assert result is None


@pytest.mark.asyncio
async def test_api_send_command_not_connected(hass: HomeAssistant) -> None:
    """Test sending command when not connected."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connected = False

    # Should not raise but also not send
    with patch("serial_asyncio.create_serial_connection", new_callable=AsyncMock):
        api.send_command("test_command")
        # This would not raise an error, but wouldn't send either


@pytest.fixture
def api_with_mock_transport(hass: HomeAssistant):
    """Create an API with mock transport."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    mock_transport = MagicMock()
    mock_protocol = MagicMock()
    api._transport = mock_transport
    api._protocol = mock_protocol
    api._is_connected = True
    return api


@pytest.mark.asyncio
async def test_api_handle_message(hass: HomeAssistant, api_with_mock_transport) -> None:
    """Test handling incoming messages."""
    api = api_with_mock_transport

    # This would typically be called by the protocol when data arrives
    # Just verify the API can be initialized and has the method
    assert hasattr(api, "_handle_message")
