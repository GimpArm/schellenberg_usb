"""Tests for sensor.py's three USB-stick status sensors."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.schellenberg_usb.sensor import (
    SchellenbergConnectionSensor,
    SchellenbergModeSensor,
    SchellenbergVersionSensor,
)


def make_api(**overrides):
    api = MagicMock()
    api.is_connected = overrides.get("is_connected", True)
    api.device_version = overrides.get("device_version", "1.2.3")
    api.device_mode = overrides.get("device_mode", "listening")
    return api


def make_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def test_connection_sensor_reports_connected(hass: HomeAssistant) -> None:
    api = make_api(is_connected=True)
    sensor = SchellenbergConnectionSensor(api, make_entry())
    sensor.hass = hass
    assert sensor.native_value == "connected"
    assert sensor.icon == "mdi:usb"
    assert sensor.available is True


def test_connection_sensor_reports_disconnected(hass: HomeAssistant) -> None:
    api = make_api(is_connected=False)
    sensor = SchellenbergConnectionSensor(api, make_entry())
    sensor.hass = hass
    assert sensor.native_value == "disconnected"
    assert sensor.icon == "mdi:usb-off"
    assert sensor.available is False


def test_version_sensor_reports_api_device_version(hass: HomeAssistant) -> None:
    api = make_api(device_version="9.9.9")
    sensor = SchellenbergVersionSensor(api, make_entry())
    sensor.hass = hass
    assert sensor.native_value == "9.9.9"


def test_mode_sensor_reports_api_device_mode_and_matching_icon(
    hass: HomeAssistant,
) -> None:
    api = make_api(device_mode="listening")
    sensor = SchellenbergModeSensor(api, make_entry())
    sensor.hass = hass
    assert sensor.native_value == "listening"
    assert sensor.icon == "mdi:ear-hearing"


def test_mode_sensor_unknown_mode_falls_back_to_help_icon(
    hass: HomeAssistant,
) -> None:
    api = make_api(device_mode=None)
    sensor = SchellenbergModeSensor(api, make_entry())
    sensor.hass = hass
    assert sensor.native_value is None
    assert sensor.icon == "mdi:help-circle"
