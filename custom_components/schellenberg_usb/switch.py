"""Switch platform for Schellenberg USB stick LED control."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import SchellenbergUsbApi
from .const import DOMAIN, SIGNAL_STICK_STATUS_UPDATED, SchellenbergConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Schellenberg USB switch entities."""
    api: SchellenbergUsbApi = entry.runtime_data

    # Create switch entity for USB stick LED
    switches = [SchellenbergLedSwitch(api, entry)]

    async_add_entities(switches)


class SchellenbergLedSwitch(RestoreEntity, SwitchEntity):
    """Switch entity for controlling the USB stick LED."""

    _attr_has_entity_name = True
    _attr_translation_key = "led"

    def __init__(self, api: SchellenbergUsbApi, entry: SchellenbergConfigEntry) -> None:
        """Initialize the LED switch."""
        self.api = api
        self._attr_unique_id = f"{entry.entry_id}_led"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model="USB Stick",
            sw_version=api.device_version,
        )
        self._is_on = False
        self._was_available = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates and restore state."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STICK_STATUS_UPDATED, self._handle_status_update
            )
        )

        # Restore the last state
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == "on"
            _LOGGER.debug("Restored LED switch state: %s", self._is_on)

            # If already connected, restore the hardware state
            if self.api.is_connected:
                await self._restore_hardware_state()

    @callback
    def _handle_status_update(self) -> None:
        """Handle status update from API."""
        # Detect when connection is re-established (transition from unavailable to available)
        is_now_available = self.api.is_connected
        if is_now_available and not self._was_available:
            # Connection restored, restore hardware state
            _LOGGER.debug("USB stick reconnected, restoring LED state")
            self.hass.async_create_task(self._restore_hardware_state())

        self._was_available = is_now_available
        self.async_write_ha_state()

    async def _restore_hardware_state(self) -> None:
        """Restore the hardware LED state to match the entity state."""
        _LOGGER.info(
            "Restoring LED hardware state to: %s", "on" if self._is_on else "off"
        )
        if self._is_on:
            await self.api.led_on()
        else:
            await self.api.led_off()

    @property
    def is_on(self) -> bool:
        """Return True if LED is on."""
        return self._is_on

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.api.is_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the LED on."""
        await self.api.led_on()
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED off."""
        await self.api.led_off()
        self._is_on = False
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        """Return the icon based on LED state."""
        return "mdi:led-on" if self._is_on else "mdi:led-off"
