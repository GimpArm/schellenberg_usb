"""Test constants in the Schellenberg USB integration."""

from __future__ import annotations

import pytest

from custom_components.schellenberg_usb.const import (
    CALIBRATION_TIMEOUT,
    CMD_DOWN,
    CMD_LED_BLINK_1,
    CMD_LED_OFF,
    CMD_LED_ON,
    CMD_PAIR,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    DATA_API_INSTANCE,
    DATA_UNSUB_DISPATCHER,
    DOMAIN,
    PAIRING_TIMEOUT,
    PLATFORMS,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_PAIRED,
    SIGNAL_PAIRING_STARTED,
    SIGNAL_PAIRING_TIMEOUT,
    SIGNAL_STICK_STATUS_UPDATED,
    VERIFY_TIMEOUT,
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
    assert CONF_CLOSE_TIME == "close_time"


def test_data_constants() -> None:
    """Test data storage constants."""
    assert DATA_API_INSTANCE == "api_instance"
    assert DATA_UNSUB_DISPATCHER == "unsub_dispatcher"


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
    assert CMD_LED_BLINK_1 == "so1"


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
    assert CALIBRATION_TIMEOUT == 300

    assert VERIFY_TIMEOUT > 0
    assert PAIRING_TIMEOUT > 0
    assert CALIBRATION_TIMEOUT > 0


def test_timeout_relationships() -> None:
    """Test timeout relationships make sense."""
    # Calibration should be longer than pairing
    assert CALIBRATION_TIMEOUT > PAIRING_TIMEOUT
    # Pairing should be longer than verification
    assert PAIRING_TIMEOUT > VERIFY_TIMEOUT
