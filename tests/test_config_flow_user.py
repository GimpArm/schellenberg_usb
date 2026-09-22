"""Tests for config_flow.py's SchellenbergUsbConfigFlow (the top-level hub
setup flow: manual entry + USB auto-discovery), via the real Home Assistant
config-flow manager (hass.config_entries.flow).

This is the one flow class in config_flow.py tested through the full
manager rather than by direct instantiation: unlike the blind subentry
flow, it needs no pre-existing parent entry, and its duplicate-detection
(async_set_unique_id / _abort_if_unique_id_configured) only behaves
correctly when driven by the real manager.
"""

from __future__ import annotations

from unittest.mock import patch

import serial
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.const import CONF_SERIAL_PORT, DOMAIN

CHECK_PORT = "custom_components.schellenberg_usb.config_flow.check_serial_port"


async def test_user_step_success_creates_entry(hass: HomeAssistant) -> None:
    with patch(CHECK_PORT):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    assert result["title"] == "Schellenberg USB (/dev/ttyUSB0)"


async def test_user_step_cannot_connect_shows_error(hass: HomeAssistant) -> None:
    with patch(CHECK_PORT, side_effect=serial.SerialException("boom")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_unexpected_error_shows_unknown(hass: HomeAssistant) -> None:
    with patch(CHECK_PORT, side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_step_duplicate_port_aborts(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        unique_id="/dev/ttyUSB0",
    )
    existing.add_to_hass(hass)

    with patch(CHECK_PORT):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_usb_discovery_confirm_creates_entry(hass: HomeAssistant) -> None:
    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB0",
        vid="16C0",
        pid="05E1",
        serial_number="ABC123",
        manufacturer="van ooijen",
        description="Schellenberg USB Stick",
    )
    with patch(CHECK_PORT):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "usb"}, data=discovery_info
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "usb_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "van ooijen Schellenberg USB Stick"


async def test_usb_discovery_already_configured_aborts(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        unique_id="ABC123",
    )
    existing.add_to_hass(hass)

    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB0",
        vid="16C0",
        pid="05E1",
        serial_number="ABC123",
        manufacturer="van ooijen",
        description="Schellenberg USB Stick",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "usb"}, data=discovery_info
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
