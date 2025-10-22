"""The Schellenberg USB Stick integration."""

from __future__ import annotations

import logging

from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store

from .api import SchellenbergUsbApi
from .const import (
    CONF_SERIAL_PORT,
    DATA_API_INSTANCE,
    DATA_UNSUB_DISPATCHER,
    DOMAIN,
    PLATFORMS,
    SIGNAL_DEVICE_PAIRED,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_devices"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Schellenberg USB component."""

    async def async_pair_device(call: ServiceCall) -> None:
        """Handle the pair service call."""
        # Find the first loaded config entry for this integration
        entries = hass.config_entries.async_entries(DOMAIN)
        loaded_entries = [
            entry
            for entry in entries
            if entry.state == hass.config_entries.ConfigEntryState.LOADED
        ]

        if not loaded_entries:
            raise ServiceValidationError(
                "No Schellenberg USB integration is currently loaded"
            )

        # Use the first loaded entry
        entry = loaded_entries[0]
        api: SchellenbergUsbApi = entry.runtime_data

        if not api.is_connected:
            raise HomeAssistantError("Schellenberg USB stick is not connected")

        # Start pairing and wait for result
        pairing_result = await api.pair_device_and_wait()

        if not pairing_result:
            raise HomeAssistantError(
                "Pairing timeout - no device responded within 10 seconds. "
                "Please ensure you press the pairing button on your blind motor."
            )

        device_id, device_enum = pairing_result
        # Save the new device using the handle_new_device function
        handle_new_device = hass.data[DOMAIN].get("handle_new_device")
        if handle_new_device:
            # Note: handle_new_device doesn't take device_enum parameter since it will reload
            await handle_new_device(device_id, f"Blind {device_id}")

        _LOGGER.info(
            "Successfully paired device: %s with enum %s", device_id, device_enum
        )

    # Register the pair service
    hass.services.async_register(DOMAIN, "pair", async_pair_device)

    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
    """Set up Schellenberg USB from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    port = entry.data[CONF_SERIAL_PORT]
    api = SchellenbergUsbApi(hass, port)

    # Load paired devices from storage
    storage = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await storage.async_load() or {"devices": []}
    devices = stored_data.get("devices", [])

    # Register existing devices with the API so they're recognized
    api.register_existing_devices(devices)

    # Store API in both hass.data and entry.runtime_data for options flow access
    hass.data[DOMAIN][entry.entry_id] = {DATA_API_INSTANCE: api}
    entry.runtime_data = api

    # Start the connection
    hass.async_create_task(api.connect())

    # Forward setup to the cover platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_new_device_no_reload(
        device_id: str, device_name: str | None = None, device_enum: str | None = None
    ) -> None:
        """Save a newly paired device without reloading.

        This is used by the options flow to avoid interrupting the flow.
        The reload happens after the flow completes.

        Args:
            device_id: The unique device ID from the Schellenberg protocol
            device_name: Optional friendly name for the device
            device_enum: Optional device enumerator. If not provided, uses the last one from API
        """
        _LOGGER.info("Saving new device for %s", device_id)
        current_data = await storage.async_load() or {"devices": []}

        # Check if device already exists
        existing_device = next(
            (d for d in current_data.get("devices", []) if d["id"] == device_id),
            None,
        )

        if existing_device:
            _LOGGER.warning("Device %s already exists", device_id)
            return

        # Add new device with enumerator and name
        if device_enum is None:
            device_enum = api.get_last_device_enum()
        new_device = {
            "id": device_id,
            "enum": device_enum,
            "name": device_name or f"Blind {device_id}",
        }

        if "devices" not in current_data:
            current_data["devices"] = []

        current_data["devices"].append(new_device)
        await storage.async_save(current_data)

        _LOGGER.info(
            "New device %s (%s) saved enum %s (reload will happen separately)",
            device_id,
            device_name,
            device_enum,
        )

    async def handle_new_device(device_id: str, device_name: str | None = None) -> None:
        """Save a newly paired device and reload the integration.

        Args:
            device_id: The unique device ID from the Schellenberg protocol
            device_name: Optional friendly name for the device
        """
        # First save the device
        await handle_new_device_no_reload(device_id, device_name)

        # Then reload
        _LOGGER.info("Reloading integration to add entity for device %s", device_id)
        await hass.config_entries.async_reload(entry.entry_id)

    # Make both functions accessible to options flow
    hass.data[DOMAIN]["handle_new_device"] = handle_new_device
    hass.data[DOMAIN]["handle_new_device_no_reload"] = handle_new_device_no_reload

    # Register listener for newly paired devices (from API auto-discovery)
    unsub = async_dispatcher_connect(hass, SIGNAL_DEVICE_PAIRED, handle_new_device)
    hass.data[DOMAIN][entry.entry_id][DATA_UNSUB_DISPATCHER] = unsub

    @callback
    def _handle_entity_removed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Handle entity removal from entity registry."""
        if event.data["action"] != "remove":
            return

        entity_entry = er.async_get(hass).deleted_entities.get(event.data["entity_id"])
        if not entity_entry or entity_entry.config_entry_id != entry.entry_id:
            return

        # Extract device_id from unique_id
        # Format: schellenberg_{device_id}
        unique_id = entity_entry.unique_id
        if not unique_id or not unique_id.startswith("schellenberg_"):
            return

        device_id = unique_id.replace("schellenberg_", "")

        _LOGGER.info("Entity for device %s removed, cleaning up storage", device_id)

        # Remove device from API's known devices list
        api.remove_known_device(device_id)

        # Remove device from storage
        async def _remove_from_storage() -> None:
            current_data = await storage.async_load() or {"devices": []}
            devices = current_data.get("devices", [])
            updated_devices = [d for d in devices if d["id"] != device_id]

            if len(updated_devices) != len(devices):
                current_data["devices"] = updated_devices
                await storage.async_save(current_data)
                _LOGGER.info("Device %s removed from storage", device_id)

        hass.async_create_task(_remove_from_storage())

    # Listen for entity registry updates
    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _handle_entity_removed)
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        api: SchellenbergUsbApi = data[DATA_API_INSTANCE]
        await api.disconnect()
        unsub = data.get(DATA_UNSUB_DISPATCHER)
        if unsub:
            unsub()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: SchellenbergConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Remove a device from the config entry.

    Called when the user removes a device from the device registry in the UI.
    This allows the device to be deleted from the integration page.

    Args:
        hass: The Home Assistant instance
        config_entry: The config entry for this integration
        device_entry: The device entry being removed

    Returns:
        True if the device should be removed from the config entry, False otherwise
    """
    # Load storage to get list of known devices
    storage = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    current_data = await storage.async_load() or {"devices": []}
    devices = current_data.get("devices", [])

    # Check if this device is in our list
    for identifier in device_entry.identifiers:
        if identifier[0] != DOMAIN:
            continue
        device_id = identifier[1]

        # Find and remove device from storage
        updated_devices = [d for d in devices if d["id"] != device_id]

        if len(updated_devices) != len(devices):
            # Device was found and removed from storage
            current_data["devices"] = updated_devices
            await storage.async_save(current_data)
            _LOGGER.info("Removed device %s from storage", device_id)

            # Also remove from API's known devices list if API is available
            try:
                api: SchellenbergUsbApi = config_entry.runtime_data
                api.remove_known_device(device_id)
            except (AttributeError, KeyError):
                pass

            return True

    # Device not found in our storage, allow removal
    return True
