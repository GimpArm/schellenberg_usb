"""Extended tests for API module - covering untested functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.api import (
    SchellenbergProtocol,
    SchellenbergUsbApi,
)
from custom_components.schellenberg_usb.const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
)


@pytest.mark.asyncio
async def test_api_control_blind_up(hass: HomeAssistant) -> None:
    """Test sending up command to blind."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.control_blind("10", CMD_UP)

    # Should send command in format: ssXX9AAZZZ
    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args
    assert CMD_UP.encode() in call_args


@pytest.mark.asyncio
async def test_api_control_blind_down(hass: HomeAssistant) -> None:
    """Test sending down command to blind."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.control_blind("11", CMD_DOWN)

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss11" in call_args
    assert CMD_DOWN.encode() in call_args


@pytest.mark.asyncio
async def test_api_control_blind_stop(hass: HomeAssistant) -> None:
    """Test sending stop command to blind."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.control_blind("12", CMD_STOP)

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss12" in call_args
    assert CMD_STOP.encode() in call_args


@pytest.mark.asyncio
async def test_api_control_blind_invalid_action(hass: HomeAssistant) -> None:
    """Test control blind with invalid action."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.control_blind("10", "99")

    # Should not send command for invalid action
    mock_transport.write.assert_not_called()


@pytest.mark.asyncio
async def test_api_led_on(hass: HomeAssistant) -> None:
    """Test turning LED on."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.led_on()

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"so+" in call_args


@pytest.mark.asyncio
async def test_api_led_off(hass: HomeAssistant) -> None:
    """Test turning LED off."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.led_off()

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"so-" in call_args


@pytest.mark.asyncio
async def test_api_led_blink_valid_count(hass: HomeAssistant) -> None:
    """Test blinking LED with valid count."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.led_blink(5)

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"so5" in call_args


@pytest.mark.asyncio
async def test_api_led_blink_invalid_count(hass: HomeAssistant) -> None:
    """Test blinking LED with invalid count."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.led_blink(10)  # Invalid - should be 1-9

    # Should not send command for invalid count
    mock_transport.write.assert_not_called()


@pytest.mark.asyncio
async def test_api_set_upper_endpoint(hass: HomeAssistant) -> None:
    """Test setting upper endpoint for calibration."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.set_upper_endpoint("10")

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args


@pytest.mark.asyncio
async def test_api_set_lower_endpoint(hass: HomeAssistant) -> None:
    """Test setting lower endpoint for calibration."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.set_lower_endpoint("10")

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args


@pytest.mark.asyncio
async def test_api_manual_up(hass: HomeAssistant) -> None:
    """Test manual up command."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.manual_up("10")

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args


@pytest.mark.asyncio
async def test_api_manual_down(hass: HomeAssistant) -> None:
    """Test manual down command."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.manual_down("10")

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args


@pytest.mark.asyncio
async def test_api_allow_pairing_on_device(hass: HomeAssistant) -> None:
    """Test allowing pairing on device."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.allow_pairing_on_device("10")

    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]
    assert b"ss10" in call_args


@pytest.mark.asyncio
async def test_api_echo_on(hass: HomeAssistant) -> None:
    """Test enabling echo."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.echo_on()

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_echo_off(hass: HomeAssistant) -> None:
    """Test disabling echo."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.echo_off()

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_enter_bootloader_mode(hass: HomeAssistant) -> None:
    """Test entering bootloader mode."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.enter_bootloader_mode()

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_enter_initial_mode(hass: HomeAssistant) -> None:
    """Test entering initial mode."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.enter_initial_mode()

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_reboot_stick(hass: HomeAssistant) -> None:
    """Test rebooting the stick."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api.reboot_stick()

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_register_entity(hass: HomeAssistant) -> None:
    """Test registering an entity."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    api.register_entity("device_123", "15")

    assert api._registered_devices["device_123"] == "15"


@pytest.mark.asyncio
async def test_api_properties(hass: HomeAssistant) -> None:
    """Test API properties."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Test initial values
    assert api.is_connected is False
    assert api.device_version is None
    assert api.device_mode is None
    assert api.hub_id is None

    # Set some values
    api._is_connected = True
    api._device_version = "RFTU_V20"
    api._device_mode = "listening"
    api._hub_id = "ABC123"

    # Test properties return correct values
    assert api.is_connected is True
    assert api.device_version == "RFTU_V20"
    assert api.device_mode == "listening"
    assert api.hub_id == "ABC123"


@pytest.mark.asyncio
async def test_api_verify_device_success(hass: HomeAssistant) -> None:
    """Test successful device verification."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.return_value = True
        result = await api.verify_device()

    assert result is True


@pytest.mark.asyncio
async def test_api_verify_device_timeout(hass: HomeAssistant) -> None:
    """Test device verification timeout."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = TimeoutError()
        result = await api.verify_device()

    assert result is False


@pytest.mark.asyncio
async def test_api_verify_device_already_in_progress(hass: HomeAssistant) -> None:
    """Test device verification when already in progress."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Set a future that's not done
    api._verify_future = hass.loop.create_future()

    result = await api.verify_device()

    assert result is False


@pytest.mark.asyncio
async def test_api_get_device_id_success(hass: HomeAssistant) -> None:
    """Test getting device ID successfully."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.return_value = "ABC123DEF"
        result = await api.get_device_id()

    assert result == "ABC123DEF"


@pytest.mark.asyncio
async def test_api_get_device_id_timeout(hass: HomeAssistant) -> None:
    """Test getting device ID with timeout."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = TimeoutError()
        result = await api.get_device_id()

    assert result is None


@pytest.mark.asyncio
async def test_api_get_device_id_already_in_progress(hass: HomeAssistant) -> None:
    """Test getting device ID when already in progress."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Set a future that's not done
    api._device_id_future = hass.loop.create_future()

    result = await api.get_device_id()

    assert result is None


@pytest.mark.asyncio
async def test_api_disconnect_with_retry_task(hass: HomeAssistant) -> None:
    """Test disconnect cancels retry task."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    api._transport = mock_transport

    # Create a mock retry task
    mock_retry_task = MagicMock()
    mock_retry_task.done = MagicMock(return_value=False)
    api._retry_task = mock_retry_task

    await api.disconnect()

    mock_retry_task.cancel.assert_called_once()
    mock_transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_api_update_connection_status(hass: HomeAssistant) -> None:
    """Test updating connection status."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Mock the signal dispatcher
    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        api.update_connection_status(True)

        assert api._is_connected is True
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_api_initialize_next_device_enum_wrap_around(hass: HomeAssistant) -> None:
    """Test enum wraps around at 0xFF."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Set a device with enum at max
    api.register_entity("device_1", "FF")

    result = api.initialize_next_device_enum()

    # Should wrap back to starting value
    assert result == "10"


@pytest.mark.asyncio
async def test_api_initialize_next_device_enum_with_invalid_enum(
    hass: HomeAssistant,
) -> None:
    """Test enum calculation with invalid enum value."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Add a device with invalid enum
    api._registered_devices["device_1"] = "INVALID"

    result = api.initialize_next_device_enum()

    # Should still return starting value
    assert result == "10"


def test_protocol_initialization() -> None:
    """Test protocol initialization."""
    callback = MagicMock()
    api = MagicMock()

    protocol = SchellenbergProtocol(callback, api)

    assert protocol.message_callback == callback
    assert protocol.api == api
    assert protocol.buffer == ""
    assert protocol.transport is None


def test_protocol_connection_made() -> None:
    """Test protocol connection made."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    transport = MagicMock()
    protocol.connection_made(transport)

    assert protocol.transport == transport


def test_protocol_data_received_single_message() -> None:
    """Test protocol receives single message."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    protocol.data_received(b"test_message\n")

    callback.assert_called_once_with("test_message")


def test_protocol_data_received_multiple_messages() -> None:
    """Test protocol receives multiple messages."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    protocol.data_received(b"message1\nmessage2\nmessage3\n")

    assert callback.call_count == 3
    callback.assert_any_call("message1")
    callback.assert_any_call("message2")
    callback.assert_any_call("message3")


def test_protocol_data_received_incomplete_message() -> None:
    """Test protocol buffers incomplete message."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    protocol.data_received(b"incomplete")

    # Should not call callback yet
    callback.assert_not_called()
    assert protocol.buffer == "incomplete"

    # Send rest of message
    protocol.data_received(b"_message\n")

    callback.assert_called_once_with("incomplete_message")


def test_protocol_data_received_empty_lines() -> None:
    """Test protocol ignores empty lines."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    protocol.data_received(b"\n\nmessage\n\n")

    # Should only call callback for non-empty message
    callback.assert_called_once_with("message")


def test_protocol_connection_lost() -> None:
    """Test protocol connection lost."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    protocol.connection_lost(None)

    api.update_connection_status.assert_called_once_with(False)


def test_protocol_connection_lost_with_exception() -> None:
    """Test protocol connection lost with exception."""
    callback = MagicMock()
    api = MagicMock()
    protocol = SchellenbergProtocol(callback, api)

    exc = Exception("Connection error")
    protocol.connection_lost(exc)

    api.update_connection_status.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_api_pair_device_already_pairing(hass: HomeAssistant) -> None:
    """Test pairing when already in progress."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Set a pairing future that's not done
    api._pairing_future = hass.loop.create_future()

    result = await api.pair_device_and_wait()

    assert result is None


@pytest.mark.asyncio
async def test_api_pair_device_success(hass: HomeAssistant) -> None:
    """Test successful device pairing."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.return_value = "device_abc123"
        result = await api.pair_device_and_wait()

    assert result == ("device_abc123", "10")
    mock_transport.write.assert_called()
