"""Sensor platform for Schellenberg USB stick status."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SchellenbergUsbApi
from .const import (
    DOMAIN,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_HUB,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Schellenberg USB sensor entities."""
    api: SchellenbergUsbApi = entry.runtime_data

    # Create sensor entities for USB stick status
    sensors = [
        SchellenbergConnectionSensor(api, entry),
        SchellenbergVersionSensor(api, entry),
        SchellenbergModeSensor(api, entry),
    ]

    # Find hub subentry id to group sensors
    hub_subentry_id = next(
        (
            s.subentry_id
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_HUB
        ),
        None,
    )
    async_add_entities(sensors, config_subentry_id=hub_subentry_id)


class SchellenbergBaseSensor(SensorEntity):
    """Base class for Schellenberg USB stick sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, api: SchellenbergUsbApi, entry: SchellenbergConfigEntry) -> None:
        """Initialize the sensor."""
        self.api = api
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model="USB Stick",
            sw_version=api.device_version,
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        USB stick sensors are available when the stick is connected.
        """
        return self.api.is_connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STICK_STATUS_UPDATED, self._handle_status_update
            )
        )

    @callback
    def _handle_status_update(self) -> None:
        """Handle status update from API."""
        self.async_write_ha_state()


class SchellenbergConnectionSensor(SchellenbergBaseSensor):
    """Sensor for USB stick connection status."""

    _attr_translation_key = "connection_status"

    def __init__(self, api: SchellenbergUsbApi, entry: SchellenbergConfigEntry) -> None:
        """Initialize the connection sensor."""
        super().__init__(api, entry)
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def native_value(self) -> str:
        """Return the connection status."""
        return "Connected" if self.api.is_connected else "Disconnected"

    @property
    def icon(self) -> str:
        """Return the icon based on connection status."""
        return "mdi:usb" if self.api.is_connected else "mdi:usb-off"


class SchellenbergVersionSensor(SchellenbergBaseSensor):
    """Sensor for USB stick firmware version."""

    _attr_translation_key = "firmware_version"

    def __init__(self, api: SchellenbergUsbApi, entry: SchellenbergConfigEntry) -> None:
        """Initialize the version sensor."""
        super().__init__(api, entry)
        self._attr_unique_id = f"{entry.entry_id}_version"

    @property
    def native_value(self) -> str | None:
        """Return the firmware version."""
        return self.api.device_version

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:chip"


class SchellenbergModeSensor(SchellenbergBaseSensor):
    """Sensor for USB stick operating mode."""

    _attr_translation_key = "operating_mode"

    def __init__(self, api: SchellenbergUsbApi, entry: SchellenbergConfigEntry) -> None:
        """Initialize the mode sensor."""
        super().__init__(api, entry)
        self._attr_unique_id = f"{entry.entry_id}_mode"

    @property
    def native_value(self) -> str | None:
        """Return the operating mode."""
        mode = self.api.device_mode
        if mode:
            return mode.capitalize()
        return None

    @property
    def icon(self) -> str:
        """Return the icon based on mode."""
        mode = self.api.device_mode
        if mode == "listening":
            return "mdi:ear-hearing"
        if mode == "bootloader":
            return "mdi:restart"
        if mode == "initial":
            return "mdi:power"
        return "mdi:help-circle"
