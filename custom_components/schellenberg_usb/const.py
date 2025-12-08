"""Constants for the Schellenberg USB integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export protocol constants from pyschellenberg for convenience
from pyschellenberg import (
    CMD_DOWN,
    CMD_LED_BLINK_1,
    CMD_LED_OFF,
    CMD_LED_ON,
    CMD_PAIR,
    CMD_STOP,
    CMD_UP,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    PAIRING_TIMEOUT,
    VERIFY_TIMEOUT,
)

# Explicitly declare re-exports so ruff doesn't remove them
__all__ = [
    "CMD_DOWN",
    "CMD_LED_BLINK_1",
    "CMD_LED_OFF",
    "CMD_LED_ON",
    "CMD_PAIR",
    "CMD_STOP",
    "CMD_UP",
    "EVENT_STARTED_MOVING_DOWN",
    "EVENT_STARTED_MOVING_UP",
    "EVENT_STOPPED",
    "PAIRING_TIMEOUT",
    "VERIFY_TIMEOUT",
]

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import SchellenbergUsbApi

# The domain of your integration. Should be unique.
DOMAIN = "schellenberg_usb"

# Type alias for config entry with runtime data
type SchellenbergConfigEntry = ConfigEntry[SchellenbergUsbApi]

# Platform for the cover entities
PLATFORMS = ["cover", "sensor", "switch"]

# Subentry types
SUBENTRY_TYPE_LED = "led"
SUBENTRY_TYPE_HUB = "hub"
SUBENTRY_TYPE_BLIND = "blind"

# Configuration keys
CONF_SERIAL_PORT = "serial_port"
CONF_DEVICE_NAME = "device_name"

# Data keys
DATA_API_INSTANCE = "api_instance"
DATA_UNSUB_DISPATCHER = "unsub_dispatcher"

# Dispatcher signals (Home Assistant specific)
SIGNAL_DEVICE_EVENT = f"{DOMAIN}_device_event"
SIGNAL_DEVICE_PAIRED = f"{DOMAIN}_device_paired"
SIGNAL_PAIRING_STARTED = f"{DOMAIN}_pairing_started"
SIGNAL_PAIRING_TIMEOUT = f"{DOMAIN}_pairing_timeout"
SIGNAL_STICK_STATUS_UPDATED = f"{DOMAIN}_stick_status_updated"
SIGNAL_CALIBRATION_COMPLETED = f"{DOMAIN}_calibration_completed"

# Calibration constants (Home Assistant specific)
CALIBRATION_TIMEOUT = 300  # Maximum 5 minutes (300 seconds) for calibration
CONF_OPEN_TIME = "open_time"  # Time it takes to open (up) in seconds
CONF_CLOSE_TIME = "close_time"  # Time it takes to close (down) in seconds
CONF_DEVICE_ID = "device_id"  # Device ID for calibration
