"""Tests for API message handling - covering protocol message parsing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.api import (
    TRANSMIT_MAX_RETRIES,
    SchellenbergUsbApi,
)


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
async def test_busy_burst_keeps_existing_retry_task(
    hass: HomeAssistant,
) -> None:
    """Test duplicate busy responses cannot postpone a scheduled retry forever."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport
    payload = "ss089010000"
    await api.send_command(payload)

    release_retry = asyncio.Event()
    real_sleep = asyncio.sleep

    async def _wait_for_release(_: float) -> None:
        await release_retry.wait()

    with patch(
        "custom_components.schellenberg_usb.api.asyncio.sleep",
        side_effect=_wait_for_release,
    ):
        api._handle_message("tE")
        first_retry = api._retry_task
        assert first_retry is not None
        await real_sleep(0)

        api._handle_message("tE")

        assert api._retry_task is first_retry
        assert not first_retry.cancelled()
        release_retry.set()
        await first_retry

    assert mock_transport.write.call_count == 2


@pytest.mark.asyncio
async def test_busy_retry_stops_after_max_attempts(hass: HomeAssistant) -> None:
    """Test a permanently busy stick cannot create an infinite retry loop."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    api._transport = mock_transport
    payload = "ss0D9020000"
    await api.send_command(payload)

    with patch(
        "custom_components.schellenberg_usb.api.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        for _ in range(TRANSMIT_MAX_RETRIES):
            api._handle_message("tE")
            retry_task = api._retry_task
            assert retry_task is not None
            await retry_task

        api._handle_message("tE")

    assert mock_transport.write.call_count == TRANSMIT_MAX_RETRIES + 1
    assert api._pending_retry_command is None
    assert api._retry_task is None
    assert api._transmit_busy is False
    assert api._transmit_lock.locked() is False


@pytest.mark.asyncio
async def test_serial_write_failure_releases_transmit_state(
    hass: HomeAssistant,
) -> None:
    """Test write errors always release the transmit lock and busy marker."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    mock_transport = MagicMock()
    mock_transport.is_closing = MagicMock(return_value=False)
    mock_transport.write.side_effect = OSError("serial failure")
    api._transport = mock_transport

    assert await api.send_command("ss089010000") is False

    assert api._transmit_busy is False
    assert api._transmit_lock.locked() is False
    assert api._pending_retry_command is None


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

        # Calibration receives the ID-only signal and the cover receives an exact signal.
        assert mock_send.call_count == 2
        assert mock_send.call_args_list[0].args == (
            hass,
            "schellenberg_usb_device_event_ABC123",
            "01",
        )
        assert mock_send.call_args_list[1].args == (
            hass,
            "schellenberg_usb_device_event_ABC123_10",
            "01",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_enum", ["08", "0D"])
async def test_handle_message_preserves_leading_zero_status_enum(
    hass: HomeAssistant,
    status_enum: str,
) -> None:
    """Test leading-zero status enums are normalized and matched exactly."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api.register_entity(
        "3720B8",
        status_enum.removeprefix("0"),
        "Sitting room",
        command_device_id="F2B8D5",
        command_enum="08",
    )

    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        api._handle_message(f"ss{status_enum}3720B8ZZZZ01PP00")

    assert ("3720B8", status_enum) in api._registered_entity_keys
    assert mock_send.call_count == 2
    assert mock_send.call_args_list[1].args == (
        hass,
        f"schellenberg_usb_device_event_3720B8_{status_enum}",
        "01",
    )
    last_received = api.get_last_received("3720B8", status_enum.removeprefix("0"))
    assert last_received is not None
    assert last_received["enum"] == status_enum


@pytest.mark.asyncio
async def test_handle_message_requires_exact_status_pair(
    hass: HomeAssistant,
) -> None:
    """Test a matching ID with another enum does not reach the cover."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api.register_entity(
        "3720B8",
        "08",
        "Sitting room",
        command_device_id="F2B8D5",
        command_enum="23",
    )

    with patch(
        "custom_components.schellenberg_usb.api.async_dispatcher_send"
    ) as mock_send:
        api._handle_message("ss133720B8ZZZZ01PP00")

    mock_send.assert_called_once_with(
        hass,
        "schellenberg_usb_device_event_3720B8",
        "01",
    )
    last_received = api.get_last_received("3720B8", "13")
    assert last_received is not None
    assert last_received["device_id"] == "3720B8"
    assert last_received["enum"] == "13"
    assert last_received["command"] == "01"


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
