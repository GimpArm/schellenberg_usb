"""Test constants in the Schellenberg USB integration."""

from __future__ import annotations

import pytest

from custom_components.schellenberg_usb.const import (
    CALIBRATION_TIMEOUT,
    CMD_DOWN,
    CMD_LED_OFF,
    CMD_LED_ON,
    CMD_PAIR,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SERIAL_PORT,
    DOMAIN,
    MOVE_COMMAND_RESEND_DELAY_SECONDS,
    PAIRING_TIMEOUT,
    PLATFORMS,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_PAIRED,
    SIGNAL_PAIRING_STARTED,
    SIGNAL_PAIRING_TIMEOUT,
    SIGNAL_STICK_STATUS_UPDATED,
    STOP_COMMAND_RESEND_DELAY_SECONDS,
    VERIFY_TIMEOUT,
    resolve_direction_command,
)


def test_domain_constant() -> None:
    """Test DOMAIN constant."""
    assert DOMAIN == "schellenberg_usb"


def test_platforms_constant() -> None:
    """Test PLATFORMS constant includes cover, sensor, and switch."""
    assert "cover" in PLATFORMS
    assert "sensor" in PLATFORMS
    assert "switch" in PLATFORMS


def test_configuration_constants() -> None:
    """Test configuration constants."""
    assert CONF_SERIAL_PORT == "serial_port"
    assert CONF_OPEN_TIME == "open_time"
    assert CONF_DEVICE_NAME == "device_name"
    assert CONF_DEVICE_ID == "device_id"
    assert CONF_DEVICE_ENUM == "device_enum"
    assert CONF_OPEN_TIME_SECONDS == "open_time_seconds"
    assert CONF_CLOSE_TIME_SECONDS == "close_time_seconds"
    assert CONF_CLOSE_TIME == "close_time"


def test_device_commands() -> None:
    """Test device command constants."""
    assert CMD_STOP == "00"
    assert CMD_UP == "01"
    assert CMD_DOWN == "02"
    assert CMD_PAIR == "60"


def test_led_commands() -> None:
    """Test LED command constants."""
    assert CMD_LED_ON == "so+"
    assert CMD_LED_OFF == "so-"


def test_signal_constants() -> None:
    """Test signal constants are properly formatted."""
    assert SIGNAL_DEVICE_EVENT == "schellenberg_usb_device_event"
    assert SIGNAL_DEVICE_PAIRED == "schellenberg_usb_device_paired"
    assert SIGNAL_PAIRING_STARTED == "schellenberg_usb_pairing_started"
    assert SIGNAL_PAIRING_TIMEOUT == "schellenberg_usb_pairing_timeout"
    assert SIGNAL_STICK_STATUS_UPDATED == "schellenberg_usb_stick_status_updated"
    assert SIGNAL_CALIBRATION_COMPLETED == "schellenberg_usb_calibration_completed"


def test_timeout_constants() -> None:
    """Test timeout constants are positive integers."""
    assert VERIFY_TIMEOUT == 5
    assert PAIRING_TIMEOUT == 120
    # Lowered from 300 to 120 for Issue #77 (2026-11-07) - see
    # CALIBRATION_TIMEOUT's own comment in const.py.
    assert CALIBRATION_TIMEOUT == 120

    assert VERIFY_TIMEOUT > 0
    assert PAIRING_TIMEOUT > 0
    assert CALIBRATION_TIMEOUT > 0


def test_timeout_relationships() -> None:
    """Test timeout relationships make sense."""
    # Calibration and pairing are now deliberately equal (both 2 minutes) -
    # see CALIBRATION_TIMEOUT's own comment in const.py for why calibration
    # no longer needs the larger margin over pairing this test used to
    # assert; it still must not end up *shorter*, since a fresh pairing
    # offers no fallback once pairing itself has already succeeded.
    assert CALIBRATION_TIMEOUT >= PAIRING_TIMEOUT
    # Pairing should be longer than verification
    assert PAIRING_TIMEOUT > VERIFY_TIMEOUT


def test_resend_delay_constants() -> None:
    """How long control_blind_with_retry()/control_blind_stop() (api.py)
    wait after their own primary, accepted write before firing each extra
    resend - see either constant's own comment in const.py for why both
    are kept equal.
    """
    # Lowered from 0.3 to 0.25 (2026-11-07, project owner's own direction).
    assert MOVE_COMMAND_RESEND_DELAY_SECONDS == 0.25
    assert STOP_COMMAND_RESEND_DELAY_SECONDS == 0.25

    assert MOVE_COMMAND_RESEND_DELAY_SECONDS > 0
    assert STOP_COMMAND_RESEND_DELAY_SECONDS > 0


def test_resolve_direction_command() -> None:
    """Replaces five independently-duplicated CMD_UP/CMD_DOWN ternaries
    (cover.py's async_open_cover/async_close_cover and config_flow.py's
    motor-test, developer-command, and teach-motor steps) - previously
    covered only indirectly, through each of those five call sites' own
    tests, never as a function in its own right.
    """
    assert resolve_direction_command("open", invert_direction=False) == CMD_UP
    assert resolve_direction_command("open", invert_direction=True) == CMD_DOWN
    assert resolve_direction_command("close", invert_direction=False) == CMD_DOWN
    assert resolve_direction_command("close", invert_direction=True) == CMD_UP


def test_resolve_direction_command_rejects_an_unknown_direction() -> None:
    """Only "open"/"close" are valid - anything else is a programming error
    at the call site, not a value that should silently fall through to
    either command.
    """
    with pytest.raises(ValueError, match="sideways"):
        resolve_direction_command("sideways", invert_direction=False)
