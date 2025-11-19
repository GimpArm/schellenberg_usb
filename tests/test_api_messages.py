"""Tests for API message handling - covering protocol message parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.api import SchellenbergUsbApi


@pytest.mark.asyncio
async def test_handle_message_device_verification_response(hass: HomeAssistant) -> None:
    """Test handling device verification response."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._verify_future = hass.loop.create_future()

    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        api._handle_message("RFTU_V20 F:20180510_DFBD B:1")

    assert api._device_version == "RFTU_V20"
    assert api._device_mode == "initial"
    assert api._verify_future.result() is True
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_device_verification_bootloader_mode(
    hass: HomeAssistant,
) -> None:
    """Test handling device verification with bootloader mode."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._verify_future = hass.loop.create_future()

    with patch("custom_components.schellenberg_usb.api.async_dispatcher_send"):
        api._handle_message("RFTU_V20 F:20180510_DFBD B:0")

    assert api._device_version == "RFTU_V20"
    assert api._device_mode == "bootloader"


@pytest.mark.asyncio
async def test_handle_message_device_verification_unknown_mode(
    hass: HomeAssistant,
) -> None:
    """Test handling device verification with unknown boot mode."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._verify_future = hass.loop.create_future()

    with patch("custom_components.schellenberg_usb.api.async_dispatcher_send"):
        api._handle_message("RFTU_V20 F:20180510_DFBD B:99")

    assert api._device_version == "RFTU_V20"
    assert api._device_mode == "unknown"


@pytest.mark.asyncio
async def test_handle_message_device_verification_no_boot_mode(
    hass: HomeAssistant,
) -> None:
    """Test handling device verification without boot mode."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._verify_future = hass.loop.create_future()

    with patch("custom_components.schellenberg_usb.api.async_dispatcher_send"):
        api._handle_message("RFTU_V20 F:20180510_DFBD")

    assert api._device_version == "RFTU_V20"
    assert api._device_mode == "initial"


@pytest.mark.asyncio
async def test_handle_message_transmit_ack_t1(hass: HomeAssistant) -> None:
    """Test handling transmit acknowledgment t1."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Should not raise any errors
    api._handle_message("t1")


@pytest.mark.asyncio
async def test_handle_message_transmit_ack_t0(hass: HomeAssistant) -> None:
    """Test handling transmit acknowledgment t0."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Should not raise any errors
    api._handle_message("t0")


@pytest.mark.asyncio
async def test_handle_message_transmit_error_with_pending_retry(
    hass: HomeAssistant,
) -> None:
    """Test handling transmit error with pending retry command."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._pending_retry_command = "test_command"

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.create_task") as mock_create_task:
        api._handle_message("tE")

        # Should schedule a retry task
        mock_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_device_id_response(hass: HomeAssistant) -> None:
    """Test handling device ID response."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._device_id_future = hass.loop.create_future()

    # Format: srXXXXXX where XXXXXX is the device ID
    api._handle_message("srABC123")

    assert api._device_id_future.result() == "ABC123"


@pytest.mark.asyncio
async def test_handle_message_pairing_device_id(hass: HomeAssistant) -> None:
    """Test handling pairing device ID message (sl format)."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._pairing_future = hass.loop.create_future()

    # Format: sl00BEXXXXXX where XXXXXX is the device ID
    api._handle_message("sl00BEDEV789")

    assert api._pairing_future.result() == "DEV789"


@pytest.mark.asyncio
async def test_handle_message_device_event_registered_device(
    hass: HomeAssistant,
) -> None:
    """Test handling device event for registered device."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api.register_entity("ABC123", "10")

    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        # Format: ssXXYYYYYYZZZZCCPPRR where XX=enum, YYYYYY=device_id, CC=command
        api._handle_message("ss10ABC123ZZZZ01PP00")

        # Should dispatch event to device
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "schellenberg_usb_device_event_ABC123" in call_args[1]
        assert call_args[2] == "01"  # command


@pytest.mark.asyncio
async def test_handle_message_device_event_unregistered_device(
    hass: HomeAssistant,
) -> None:
    """Test handling device event for unregistered device."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        # Message with unknown device - should still dispatch
        api._handle_message("ss99UNKNOWNZZZZ01PP00")

        # Should dispatch event even for unknown devices
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_malformed_device_event(hass: HomeAssistant) -> None:
    """Test handling malformed device event message."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Should not crash on malformed message
    api._handle_message("ss")
    api._handle_message("ss1")
    api._handle_message("invalid")


@pytest.mark.asyncio
async def test_handle_message_empty_string(hass: HomeAssistant) -> None:
    """Test handling empty message."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Should not crash
    api._handle_message("")


@pytest.mark.asyncio
async def test_handle_message_unknown_format(hass: HomeAssistant) -> None:
    """Test handling message with unknown format."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    # Should not crash on unknown message types
    api._handle_message("unknown_message_format")
    api._handle_message("xyz123")


@pytest.mark.asyncio
async def test_api_stop_pairing_mode_without_delay(hass: HomeAssistant) -> None:
    """Test stopping pairing mode without delay."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    await api._stop_pairing_mode(delay=False)

    mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_stop_pairing_mode_with_delay(hass: HomeAssistant) -> None:
    """Test stopping pairing mode with delay."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport

    with patch("asyncio.sleep") as mock_sleep:
        # Make asyncio.sleep awaitable
        mock_sleep.return_value = None
        await api._stop_pairing_mode(delay=True)

        # Should wait 2 seconds before stopping
        mock_sleep.assert_called_once_with(2)
        mock_transport.write.assert_called_once()


@pytest.mark.asyncio
async def test_api_stop_pairing_mode_oserror(hass: HomeAssistant) -> None:
    """Test stopping pairing mode handles OSError gracefully."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")

    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    mock_transport.write.side_effect = OSError("Connection error")
    api._transport = mock_transport

    # Should not raise error
    await api._stop_pairing_mode(delay=False)
