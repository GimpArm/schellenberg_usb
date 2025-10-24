"""Test the config flow for Schellenberg USB integration."""

from __future__ import annotations

from collections.abc import Generator
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
import serial
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from custom_components.schellenberg_usb.config_flow import SchellenbergUsbConfigFlow
from custom_components.schellenberg_usb.const import CONF_SERIAL_PORT, DOMAIN


@pytest.fixture
def mock_serial_port() -> Generator[MagicMock]:
    """Mock the serial port."""
    with patch("serial.Serial") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield mock


@pytest.mark.asyncio
async def test_async_step_user_success(
    hass: HomeAssistant, mock_serial_port: MagicMock
) -> None:
    """Test successful user flow."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    result = await config_flow.async_step_user()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Simulate user input
    result = await config_flow.async_step_user({CONF_SERIAL_PORT: "/dev/ttyUSB0"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Schellenberg USB (/dev/ttyUSB0)"
    assert result["data"] == {CONF_SERIAL_PORT: "/dev/ttyUSB0"}


@pytest.mark.asyncio
async def test_async_step_user_cannot_connect(hass: HomeAssistant) -> None:
    """Test user flow when connection fails."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    with patch("serial.Serial", side_effect=serial.SerialException("Port not found")):
        result = await config_flow.async_step_user({CONF_SERIAL_PORT: "/dev/ttyUSB0"})

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_async_step_user_unknown_error(hass: HomeAssistant) -> None:
    """Test user flow when unknown error occurs."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    with patch("serial.Serial", side_effect=Exception("Unknown error")):
        result = await config_flow.async_step_user({CONF_SERIAL_PORT: "/dev/ttyUSB0"})

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_async_step_user_duplicate_entry(
    hass: HomeAssistant, mock_serial_port: MagicMock
) -> None:
    """Test user flow when entry already exists."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    # Create an existing entry
    existing_entry = config_entries.ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Existing Entry",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        entry_id="existing_id",
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    existing_entry.add_to_hass(hass)  # type: ignore[attr-defined]

    # Try to create a duplicate
    result = await config_flow.async_step_user({CONF_SERIAL_PORT: "/dev/ttyUSB0"})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_async_step_usb_discovery(
    hass: HomeAssistant, mock_serial_port: MagicMock
) -> None:
    """Test USB discovery flow."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB0",
        vid="16c0",
        pid="05e1",
        serial_number="ABC123",
        manufacturer="Van Ooijen",
        description="Schellenberg USB Device",
    )

    result = await config_flow.async_step_usb(discovery_info)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "usb_confirm"


@pytest.mark.asyncio
async def test_async_step_usb_confirm_success(
    hass: HomeAssistant, mock_serial_port: MagicMock
) -> None:
    """Test USB confirmation flow."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB0",
        vid="16c0",
        pid="05e1",
        serial_number="ABC123",
        manufacturer="Van Ooijen",
        description="Schellenberg USB Device",
    )

    # First step: discovery
    await config_flow.async_step_usb(discovery_info)

    # Second step: confirm
    result = await config_flow.async_step_usb_confirm(
        {CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Van Ooijen Schellenberg USB Device"


@pytest.mark.asyncio
async def test_async_step_usb_confirm_connection_error(hass: HomeAssistant) -> None:
    """Test USB confirmation flow with connection error."""
    config_flow = SchellenbergUsbConfigFlow()
    config_flow.hass = hass

    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB0",
        vid="16c0",
        pid="05e1",
        serial_number="ABC123",
        manufacturer="Van Ooijen",
        description="Schellenberg USB Device",
    )

    # First step: discovery
    await config_flow.async_step_usb(discovery_info)

    # Second step: confirm with connection error
    with patch("serial.Serial", side_effect=serial.SerialException("Port not found")):
        result = await config_flow.async_step_usb_confirm(
            {CONF_SERIAL_PORT: "/dev/ttyUSB0"}
        )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "usb_confirm"
        assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_async_get_options_flow(hass: HomeAssistant) -> None:
    """Test that options flow is available."""
    config_flow = SchellenbergUsbConfigFlow()

    entry = config_entries.ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Test",
        data={},
        options={},
        entry_id="test",
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )

    options_flow = config_flow.async_get_options_flow(entry)
    assert options_flow is not None
