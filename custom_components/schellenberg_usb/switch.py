"""Switch platform for Schellenberg USB stick LED control."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import SchellenbergUsbApi
from .const import (
    DOMAIN,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_HUB,
    SchellenbergConfigEntry,
)
from .runtime_translation_text import (
    runtime_translation_text as _runtime_translation_text,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Schellenberg USB switch entities."""
    api: SchellenbergUsbApi = entry.runtime_data
    # Find hub subentry to attach LED switch so it does not appear as ungrouped
    hub_subentry_id = next(
        (
            s.subentry_id
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_HUB
        ),
        None,
    )
    async_add_entities(
        [SchellenbergLedSwitch(api, entry, hass)],
        config_subentry_id=hub_subentry_id,
    )


class SchellenbergLedSwitch(RestoreEntity, SwitchEntity):
    """Switch entity for controlling the USB stick LED."""

    _attr_has_entity_name = True
    # Plain (non-ClassVar) annotation matching the base Entity class's own
    # `str | None` declaration for _attr_translation_key, so tests may clear
    # it back to None (see make_switch() in tests/test_switch.py) without a
    # mypy assignment error.
    _attr_translation_key: str | None = "led"

    def __init__(
        self,
        api: SchellenbergUsbApi,
        entry: SchellenbergConfigEntry,
        hass: HomeAssistant | None = None,
    ) -> None:
        """Initialize the LED switch.

        `hass` is accepted here (optionally - tests build this entity before
        hass is attached, exactly like async_setup_entry's normal entity
        lifecycle) only to translate the device's "model" string at the
        language Home Assistant is configured in; self.hass is not assigned
        yet at this point in the entity lifecycle either way.
        """
        self.api = api
        self._attr_unique_id = f"{entry.entry_id}_led"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},  # Hub device identifier
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model=_runtime_translation_text(hass, "device_model"),
            sw_version=api.device_version,
        )
        self._is_on = False
        self._was_available = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates and restore state."""
        await super().async_added_to_hass()

        # Restore the stored preference *before* subscribing to the signal
        # below - async_get_last_state() suspends this coroutine, and the
        # stick's own connect() completing while it is suspended used to
        # let _handle_status_update() (once subscribed further down) read
        # self._is_on while it was still __init__'s hardcoded False
        # default, sending one wrong hardware command immediately before
        # this method's own restore below sent the correct one - a race,
        # not a hardware quirk, and exactly a "brief, self-correcting"
        # flicker right at Home Assistant startup (audit finding 19).
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == "on"
            _LOGGER.debug("Restored LED switch state: %s", self._is_on)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STICK_STATUS_UPDATED, self._handle_status_update
            )
        )

        # If there was a stored preference to restore and the stick is
        # already connected, push it to the hardware now too (unchanged
        # from before: nothing is pushed for a brand-new entity with no
        # prior state at all - see test_added_to_hass_defaults_to_off_
        # with_nothing_restored - the eventual reconnect signal handles
        # that case the same way it handles a stick that connects later).
        # _was_available is set first, before the await below rather than
        # after it, so a status signal arriving while this restore is
        # still in flight sees it already matching - not the same gap
        # closed above, but the same race in miniature, between this
        # restore and a concurrent, redundant one of its own rather than
        # between this restore and the __init__ default.
        if last_state is not None and self.api.is_connected:
            self._was_available = True
            await self._restore_hardware_state()

    @callback
    def _handle_status_update(self) -> None:
        """React to the USB stick's connection status changing.

        Fired on every status signal, not just reconnects. On an
        unavailable-to-available transition specifically, schedules a
        detached background task to push the switch's current (or
        just-restored) state back to the hardware, since the stick may
        have missed commands sent while it was disconnected.
        """
        # Detect when connection is re-established (transition from
        # unavailable to available)
        is_now_available = self.api.is_connected
        if is_now_available and not self._was_available:
            # Connection restored, restore hardware state
            _LOGGER.debug("USB stick reconnected, restoring LED state")
            # A background task (see _restore_hardware_state's own docstring:
            # it is meant to run detached) so it is cancelled outright on
            # Home Assistant shutdown instead of being awaited by it like a
            # plain async_create_task() task would be.
            self.hass.async_create_background_task(
                self._restore_hardware_state(),
                f"{DOMAIN} LED hardware state restore",
            )

        self._was_available = is_now_available
        self.async_write_ha_state()

    async def _restore_hardware_state(self) -> None:
        """Restore the hardware LED state to match the entity state.

        Runs detached (as a background task), so it must never raise -
        there is nothing synchronous waiting to catch it. A failure is
        logged instead.
        """
        _LOGGER.info(
            "Restoring LED hardware state to: %s", "on" if self._is_on else "off"
        )
        success = await self.api.led_on() if self._is_on else await self.api.led_off()
        if not success:
            _LOGGER.error(
                "Failed to restore LED hardware state to: %s",
                "on" if self._is_on else "off",
            )

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
        if not await self.api.led_on():
            raise HomeAssistantError(
                _runtime_translation_text(
                    self.hass,
                    "led_on_failed",
                    reason=_runtime_translation_text(self.hass, "command_not_sent"),
                )
            )
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED off."""
        if not await self.api.led_off():
            raise HomeAssistantError(
                _runtime_translation_text(
                    self.hass,
                    "led_off_failed",
                    reason=_runtime_translation_text(self.hass, "command_not_sent"),
                )
            )
        self._is_on = False
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        """Return the icon based on LED state."""
        return "mdi:led-on" if self._is_on else "mdi:led-off"
