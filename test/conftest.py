"""Shared fixtures for Schellenberg USB tests."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.schellenberg_usb.const import CONF_SERIAL_PORT


@pytest.fixture
def mock_serial_port() -> str:
    """Return a mock serial port."""
    return "/dev/ttyUSB0"


@pytest.fixture
def mock_config_entry_data(mock_serial_port: str) -> dict[str, str]:
    """Return mock config entry data."""
    return {CONF_SERIAL_PORT: mock_serial_port}


@pytest.fixture
async def mock_api() -> MagicMock:
    """Create a mock API instance."""
    api = MagicMock()
    api.is_connected = False
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.pair_device_and_wait = AsyncMock()
    api.register_existing_devices = MagicMock()
    api.remove_known_device = MagicMock()
    api.get_last_device_enum = MagicMock(return_value="0x10")
    return api


@pytest.fixture
async def mock_storage(hass: HomeAssistant) -> MagicMock:
    """Create a mock storage instance."""
    storage = MagicMock(spec=Store)
    storage.async_load = AsyncMock(return_value={"devices": []})
    storage.async_save = AsyncMock()
    return storage


@pytest.fixture
def mock_serial() -> Generator[MagicMock]:
    """Mock the serial module."""
    with patch("serial.Serial") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield mock
