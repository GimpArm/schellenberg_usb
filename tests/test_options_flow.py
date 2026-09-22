"""Tests for options_flow.py's SchellenbergOptionsFlowHandler, via the real
Home Assistant options-flow manager (hass.config_entries.options).
"""

from __future__ import annotations

from unittest.mock import patch

import serial
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.const import CONF_SERIAL_PORT, DOMAIN

CHECK_PORT = "custom_components.schellenberg_usb.options_flow.check_serial_port"


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        unique_id="/dev/ttyUSB0",
    )
    entry.add_to_hass(hass)
    return entry


async def test_same_port_is_a_noop_save(hass: HomeAssistant) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    with patch(CHECK_PORT) as mock_check:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )
    mock_check.assert_not_called()
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_new_port_that_opens_updates_entry_and_reloads(
    hass: HomeAssistant,
) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(CHECK_PORT) as mock_check:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB1"}
        )

    mock_check.assert_called_once_with("/dev/ttyUSB1")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SERIAL_PORT] == "/dev/ttyUSB1"


async def test_new_port_that_fails_to_open_shows_cannot_connect(
    hass: HomeAssistant,
) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(CHECK_PORT, side_effect=serial.SerialException("boom")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB1"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_SERIAL_PORT] == "/dev/ttyUSB0"  # unchanged


async def test_new_port_unexpected_error_shows_unknown(hass: HomeAssistant) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(CHECK_PORT, side_effect=RuntimeError("boom")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB1"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
