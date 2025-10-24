"""Test the __init__.py module of Schellenberg USB integration."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_SERIAL_PORT,
    DATA_API_INSTANCE,
    DATA_UNSUB_DISPATCHER,
    DOMAIN,
    PLATFORMS,
)


@pytest.fixture
async def mock_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create a mock config entry."""
    entry = ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Schellenberg USB",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        entry_id="test_entry_id",
        state=ConfigEntryState.LOADED,
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    return entry


@pytest.mark.asyncio
async def test_async_setup_service_registration(hass: HomeAssistant) -> None:
    """Test that async_setup registers the pair service."""
    from custom_components.schellenberg_usb import async_setup

    result = await async_setup(hass, {})
    assert result is True
    assert hass.services.has_service(DOMAIN, "pair")


@pytest.mark.asyncio
async def test_async_setup_pair_service_no_loaded_entries(
    hass: HomeAssistant,
) -> None:
    """Test pair service raises error when no entries are loaded."""
    from custom_components.schellenberg_usb import async_setup

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(DOMAIN, "pair", {}, blocking=True)

    assert "No Schellenberg USB integration is currently loaded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_async_setup_pair_service_not_connected(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test pair service raises error when stick is not connected."""
    from custom_components.schellenberg_usb import async_setup

    await async_setup(hass, {})

    # Setup a mock API that's not connected
    mock_api = MagicMock(spec=SchellenbergUsbApi)
    mock_api.is_connected = False
    mock_config_entry.runtime_data = mock_api

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(DOMAIN, "pair", {}, blocking=True)

    assert "Schellenberg USB stick is not connected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_async_setup_pair_service_success(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test pair service successfully pairs a device."""
    from custom_components.schellenberg_usb import async_setup

    hass.data.setdefault(DOMAIN, {})

    await async_setup(hass, {})

    # Setup a mock API that's connected
    mock_api = MagicMock(spec=SchellenbergUsbApi)
    mock_api.is_connected = True
    mock_api.pair_device_and_wait = AsyncMock(return_value=("device_123", "0x10"))
    mock_config_entry.runtime_data = mock_api

    # Mock handle_new_device
    async def mock_handle_new_device(
        device_id: str, device_name: str | None = None
    ) -> None:
        pass

    hass.data[DOMAIN]["handle_new_device"] = mock_handle_new_device

    await hass.services.async_call(DOMAIN, "pair", {}, blocking=True)

    mock_api.pair_device_and_wait.assert_called_once()


@pytest.mark.asyncio
async def test_async_setup_pair_service_timeout(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test pair service raises error on pairing timeout."""
    from custom_components.schellenberg_usb import async_setup

    await async_setup(hass, {})

    # Setup a mock API that times out
    mock_api = MagicMock(spec=SchellenbergUsbApi)
    mock_api.is_connected = True
    mock_api.pair_device_and_wait = AsyncMock(return_value=None)
    mock_config_entry.runtime_data = mock_api

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(DOMAIN, "pair", {}, blocking=True)

    assert "Pairing timeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_async_setup_entry_basic(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test basic async_setup_entry functionality."""
    from custom_components.schellenberg_usb import async_setup_entry

    with (
        patch("homeassistant.helpers.storage.Store") as mock_store_class,
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_forward,
    ):
        # Setup mock storage
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(return_value={"devices": []})
        mock_store_class.return_value = mock_storage

        result = await async_setup_entry(hass, mock_config_entry)

        assert result is True
        mock_forward.assert_called_once_with(mock_config_entry, PLATFORMS)
        assert DATA_API_INSTANCE in hass.data[DOMAIN][mock_config_entry.entry_id]


@pytest.mark.asyncio
async def test_async_setup_entry_loads_existing_devices(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test that async_setup_entry loads existing devices from storage."""
    from custom_components.schellenberg_usb import async_setup_entry

    devices = [
        {"id": "device_1", "enum": "0x10", "name": "Blind 1"},
        {"id": "device_2", "enum": "0x11", "name": "Blind 2"},
    ]

    with (
        patch("homeassistant.helpers.storage.Store") as mock_store_class,
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
    ):
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(return_value={"devices": devices})
        mock_store_class.return_value = mock_storage

        with patch.object(
            SchellenbergUsbApi, "register_existing_devices"
        ) as mock_register:
            result = await async_setup_entry(hass, mock_config_entry)

            assert result is True
            mock_register.assert_called_once_with(devices)


@pytest.mark.asyncio
async def test_async_unload_entry(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test async_unload_entry disconnects and cleans up resources."""
    from custom_components.schellenberg_usb import async_setup_entry, async_unload_entry

    with (
        patch("homeassistant.helpers.storage.Store") as mock_store_class,
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", new_callable=AsyncMock
        ) as mock_unload,
    ):
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(return_value={"devices": []})
        mock_store_class.return_value = mock_storage

        # First setup the entry
        await async_setup_entry(hass, mock_config_entry)
        mock_unload.return_value = True

        # Now unload it
        with patch.object(
            SchellenbergUsbApi, "disconnect", new_callable=AsyncMock
        ) as mock_disconnect:
            result = await async_unload_entry(hass, mock_config_entry)

            assert result is True
            mock_unload.assert_called_once_with(mock_config_entry, PLATFORMS)
            mock_disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_async_unload_entry_cleanup_unsubscriber(
    hass: HomeAssistant, mock_config_entry: ConfigEntry
) -> None:
    """Test that async_unload_entry calls the dispatcher unsubscriber."""
    from custom_components.schellenberg_usb import async_setup_entry, async_unload_entry

    with (
        patch("homeassistant.helpers.storage.Store") as mock_store_class,
        patch.object(SchellenbergUsbApi, "connect", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", new_callable=AsyncMock
        ) as mock_unload,
    ):
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(return_value={"devices": []})
        mock_store_class.return_value = mock_storage
        mock_unload.return_value = True

        # Setup the entry
        await async_setup_entry(hass, mock_config_entry)

        # Add a mock unsubscriber
        mock_unsub = MagicMock()
        hass.data[DOMAIN][mock_config_entry.entry_id][DATA_UNSUB_DISPATCHER] = (
            mock_unsub
        )

        # Unload the entry
        with patch.object(SchellenbergUsbApi, "disconnect", new_callable=AsyncMock):
            await async_unload_entry(hass, mock_config_entry)

        mock_unsub.assert_called_once()


@pytest.mark.asyncio
async def test_async_remove_config_entry_device_found(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test async_remove_config_entry_device removes device from storage."""
    from custom_components.schellenberg_usb import async_remove_config_entry_device

    device_entry = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "device_123")},
        name="Test Device",
    )

    with patch("homeassistant.helpers.storage.Store") as mock_store_class:
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(
            return_value={
                "devices": [
                    {"id": "device_123", "enum": "0x10", "name": "Blind 1"},
                ]
            }
        )
        mock_storage.async_save = AsyncMock()
        mock_store_class.return_value = mock_storage

        mock_config_entry.runtime_data = MagicMock()
        mock_config_entry.runtime_data.remove_known_device = MagicMock()

        result = await async_remove_config_entry_device(
            hass, mock_config_entry, device_entry
        )

        assert result is True
        mock_storage.async_save.assert_called_once()
        # Verify device was removed from storage
        saved_data = mock_storage.async_save.call_args[0][0]
        assert saved_data["devices"] == []


@pytest.mark.asyncio
async def test_async_remove_config_entry_device_not_found(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test async_remove_config_entry_device when device not in storage."""
    from custom_components.schellenberg_usb import async_remove_config_entry_device

    device_entry = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "device_999")},
        name="Unknown Device",
    )

    with patch("homeassistant.helpers.storage.Store") as mock_store_class:
        mock_storage = AsyncMock(spec=Store)
        mock_storage.async_load = AsyncMock(
            return_value={
                "devices": [
                    {"id": "device_123", "enum": "0x10", "name": "Blind 1"},
                ]
            }
        )
        mock_store_class.return_value = mock_storage

        result = await async_remove_config_entry_device(
            hass, mock_config_entry, device_entry
        )

        # Device not found, but still allow removal
        assert result is True
