"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import SchellenbergUsbApi
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_LED,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)
DEFAULT_TRAVEL_TIME = 60.0  # seconds, a sensible default


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Schellenberg cover entities."""
    try:
        _LOGGER.info("Cover platform async_setup_entry called for: %s", entry.entry_id)
        _LOGGER.debug("Entry data: %s", entry.data)

        # Only hub entries should reach here
        if CONF_SERIAL_PORT not in entry.data:
            _LOGGER.warning(
                "Cover platform called for non-hub entry %s, ignoring", entry.entry_id
            )
            return
        # This is a hub entry - set up all paired device covers from subentries
        _LOGGER.info("Setting up cover for hub entry: %s", entry.title)
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        api = entry.runtime_data

        # Get paired devices from subentries
        subentries = entry.subentries.values()
        _LOGGER.info("Hub has %d subentries (paired devices)", len(entry.subentries))

        if not entry.subentries:
            _LOGGER.info("No subentries (paired devices) found for hub")
            return

        _LOGGER.info("Loading %d paired Schellenberg devices", len(entry.subentries))

        for subentry in subentries:
            # Skip LED subentry; handled by switch platform
            if subentry.subentry_type == SUBENTRY_TYPE_LED:
                continue
            device_id = subentry.data.get("device_id")
            device_enum = subentry.data.get("device_enum")
            device_name = subentry.title

            if not device_id or not device_enum:
                # This subentry lacks motor identification info; it's likely a non-motor type
                # or pairing is incomplete. Downgrade to debug to avoid user confusion.
                _LOGGER.debug(
                    "Skipping subentry %s (type=%s) missing device_id/device_enum",
                    subentry.subentry_id,
                    getattr(subentry, "subentry_type", "unknown"),
                )
                continue

            # Check if entity already exists to avoid duplicates.
            # NOTE: The cover entity sets its unique_id to f"schellenberg_{device_id}" in the entity class.
            # We must use the same pattern here; previously this used f"{device_id}_cover" which never matched
            # and caused a new entity to be created on every reload, losing the restored position (defaulting to 0).
            entity_unique_id = f"schellenberg_{device_id}"
            existing_entity_id = entity_registry.async_get_entity_id(
                "cover", DOMAIN, entity_unique_id
            )
            if existing_entity_id:
                # Entity registry entry already exists (e.g. after reload). We still need
                # to create a new entity object so Home Assistant can manage runtime state.
                entry_entity = entity_registry.entities[existing_entity_id]
                if entry_entity.config_subentry_id != subentry.subentry_id:
                    _LOGGER.info(
                        "Updating existing cover entity %s to subentry %s",
                        existing_entity_id,
                        subentry.subentry_id,
                    )
                    entity_registry.async_update_entity(
                        existing_entity_id,
                        config_subentry_id=subentry.subentry_id,
                    )
                _LOGGER.debug(
                    "Re-instantiating cover entity object for existing registry entry %s",
                    existing_entity_id,
                )

            # Create or get device in device registry
            # Link device to both hub entry AND subentry
            device = device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                config_subentry_id=subentry.subentry_id,
                identifiers={(DOMAIN, device_id)},
                name=device_name,
                manufacturer="Schellenberg",
                model=f"USB Stick Motor ({device_id}/{device_enum})",
            )
            _LOGGER.debug(
                "Created/updated device %s for paired device %s",
                device.id,
                device_id,
            )

            # Create cover entity linked to this device
            # Create and add the new cover entity attached to the subentry
            _LOGGER.debug("Creating cover entity for device %s", device_id)
            async_add_entities(
                [
                    SchellenbergCover(
                        api=api,
                        device_id=device_id,
                        device_enum=device_enum,
                        device_name=device_name,
                        device_data=subentry.data,
                        config_entry_id=entry.entry_id,
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )
    except Exception:
        _LOGGER.exception("Error setting up cover platform")
        raise


class SchellenbergCover(CoverEntity, RestoreEntity):
    """Representation of a Schellenberg Blind."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    # This entity supports open, close, stop, and setting position.
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        api: SchellenbergUsbApi,
        device_id: str,
        device_enum: str,
        device_name: str,
        device_data: dict | None = None,
        config_entry_id: str | None = None,
    ) -> None:
        """Initialize the Schellenberg cover entity.

        Args:
            api: The API instance for communication
            device_id: The unique device ID (6-character hex)
            device_enum: The device enumerator for commands (2-character hex)
            device_name: Friendly name for the device
            device_data: Device data dict containing calibration times
            config_entry_id: The config entry ID for linking to device

        """
        self._api = api
        self._device_id = device_id
        self._device_enum = device_enum
        self._config_entry_id = config_entry_id

        # Entity attributes
        self._attr_unique_id = f"schellenberg_{device_id}"
        self._attr_name = device_name
        self._attr_is_closed = None
        self._attr_is_opening = False
        self._attr_is_closing = False
        # Position will be restored from last state in async_added_to_hass. Use None until then.
        self._attr_current_cover_position: int | None = None

        # Link this entity to the device using identifiers
        # The device is created separately in async_setup_entry with config_subentry_id
        # So we only set the identifiers here to link the entity to that device
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, device_id)},
        )

        # Position calculation attributes - use calibration times if available
        device_data = device_data or {}
        self._travel_time_open: float = device_data.get(
            CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME
        )
        self._travel_time_close: float = device_data.get(
            CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME
        )
        self._move_start_time: float | None = None
        self._move_start_position: int | None = (
            None  # Starting position when movement began
        )
        self._position_update_task: asyncio.Task[None] | None = (
            None  # Task for real-time position updates
        )
        self._target_position: int | None = (
            None  # Target position for set_cover_position
        )
        # NOTE: Debug/troubleshooting instrumentation removed now that persistence works reliably.

    @property
    def available(self) -> bool:
        """Return if entity is available.

        The entity is available when the USB stick is connected and in listening mode.
        """
        return self._api.is_connected

    @property
    def icon(self) -> str:
        """Return the icon based on cover state."""
        # Show movement direction icons when actively moving
        if self._attr_is_opening:
            return "mdi:arrow-up-box"
        if self._attr_is_closing:
            return "mdi:arrow-down-box"
        # Fallback to open/closed state icons
        if self._attr_is_closed:
            return "mdi:window-shutter"
        return "mdi:window-shutter-open"

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if entity should be enabled by default."""
        return True

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Register this entity with the API so it knows we're listening
        self._api.register_entity(self._device_id, self._device_enum)

        # Restore the last known state
        last_state = await self.async_get_last_state()
        if last_state:
            # HA stores cover position attribute as 'current_position'. Some code historically
            # used 'position'. We try both, then infer from the last state if still missing.
            restored_position: int | None = None
            raw_position = (
                last_state.attributes.get("current_position")
                if "current_position" in last_state.attributes
                else last_state.attributes.get(ATTR_POSITION)
            )
            if isinstance(raw_position, (int, float)):
                restored_position = int(raw_position)
            elif raw_position is not None:
                # Attempt to coerce string digits
                try:
                    restored_position = int(str(raw_position))
                except ValueError:
                    restored_position = None

            # Fallback: infer from last_state.state if attribute absent
            if restored_position is None:
                if last_state.state == "open":
                    restored_position = 100
                elif last_state.state == "closed":
                    restored_position = 0

            if restored_position is not None:
                # Use exact restored value without inferring 100 from 'open' state; allows partial positions.
                self._attr_current_cover_position = max(0, min(100, restored_position))
                self._attr_is_closed = self._attr_current_cover_position == 0
                _LOGGER.debug(
                    "Restored position for %s (%s) to %d%% (raw=%s)",
                    self._attr_name,
                    self._device_id,
                    self._attr_current_cover_position,
                    raw_position,
                )
        # If we still don't have a position, assume fully closed (0) as a conservative default.
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
            self._attr_is_closed = True
            _LOGGER.debug(
                "No previous state for %s (%s); defaulting position to 0%% (closed)",
                self._attr_name,
                self._device_id,
            )

        # IMPORTANT: We must write the restored (or default) position to the state machine now.
        # add_to_platform_finish() already wrote an initial state before restoration ran, so without
        # this call the restored position would not be visible until the first movement/event.
        # Initial write after restoration (debug instrumentation removed).
        self.async_write_ha_state()

        # Register listeners for events and status updates
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DEVICE_EVENT}_{self._device_id}",
                self._handle_event,
            )
        )

        # Subscribe to connection status updates so availability changes are reflected
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STICK_STATUS_UPDATED,
                self._handle_status_update,
            )
        )

        # Subscribe to calibration completion events
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CALIBRATION_COMPLETED,
                self._handle_calibration_completed,
            )
        )

    @callback
    def _handle_status_update(self) -> None:
        """Handle status update from API (connection state changed)."""
        self.async_write_ha_state()

    @callback
    def _handle_calibration_completed(
        self, device_id: str, open_time: float, close_time: float
    ) -> None:
        """Handle calibration completion for this device."""
        # Only update if this is for our device
        if device_id != self._device_id:
            return

        # Update travel times with new calibration values
        self._travel_time_open = open_time
        self._travel_time_close = close_time

        # The device is fully closed after calibration, so set position to 0
        self._attr_current_cover_position = 0
        self._attr_is_closed = True

        _LOGGER.info(
            "Device %s calibration updated: open_time=%.2fs, close_time=%.2fs. "
            "Cover position set to fully closed (0%%)",
            self._attr_name,
            open_time,
            close_time,
        )

        # Update entity state
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        await super().async_will_remove_from_hass()
        # Stop any running position tracking tasks
        self._stop_position_tracking()

    @callback
    def _handle_event(self, event: str) -> None:
        """Handle events from the USB stick for this device."""
        _LOGGER.info(
            "Device %s (%s) received activity event: %s",
            self._attr_name,
            self._device_id,
            event,
        )

        if event == EVENT_STARTED_MOVING_UP:
            _LOGGER.info("Device %s started moving UP", self._attr_name)
            self._attr_is_opening = True
            self._attr_is_closing = False
            self._move_start_time = time.monotonic()
            self._move_start_position = self._attr_current_cover_position
            # Start real-time position tracking
            self._start_position_tracking()
        elif event == EVENT_STARTED_MOVING_DOWN:
            _LOGGER.info("Device %s started moving DOWN", self._attr_name)
            self._attr_is_opening = False
            self._attr_is_closing = True
            self._move_start_time = time.monotonic()
            self._move_start_position = self._attr_current_cover_position
            # Start real-time position tracking
            self._start_position_tracking()
        elif event == EVENT_STOPPED:
            _LOGGER.info(
                "Device %s STOPPED (position: %d%%)",
                self._attr_name,
                self._attr_current_cover_position,
            )
            # Stop real-time position tracking
            self._stop_position_tracking()
            # If we had a target position, keep it exactly; avoid recalculating which could overshoot.
            if self._target_position is not None:
                self._attr_current_cover_position = self._target_position
            else:
                # Final update based on elapsed time only if no explicit target
                self._update_position()
            # Clamp extremes explicitly (defensive)
            if self._attr_current_cover_position is not None:
                if self._attr_current_cover_position <= 0:
                    self._attr_current_cover_position = 0
                elif self._attr_current_cover_position >= 100:
                    self._attr_current_cover_position = 100
            # Update closed flag after clamping
            if self._attr_current_cover_position is not None:
                self._attr_is_closed = self._attr_current_cover_position == 0
            self._attr_is_opening = False
            self._attr_is_closing = False
            # Clear movement tracking variables
            self._move_start_time = None
            self._move_start_position = None
            self._target_position = None  # Clear target position on stop
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._attr_name, event
            )

        self.async_write_ha_state()

    def _start_position_tracking(self) -> None:
        """Start tracking position updates every second."""
        # Cancel any existing tracking task
        self._stop_position_tracking()

        # Create a new task to update position every second
        self._position_update_task = self.hass.async_create_task(
            self._async_position_update_loop()
        )

    def _stop_position_tracking(self) -> None:
        """Stop the position tracking task."""
        if self._position_update_task and not self._position_update_task.done():
            self._position_update_task.cancel()
        self._position_update_task = None

    async def _async_position_update_loop(self) -> None:
        """Update position every 200ms internally, report to HA every 1 second."""
        try:
            ha_update_counter = 0
            while True:
                # Calculate position every 200ms
                await asyncio.sleep(0.2)

                # Update position based on elapsed time
                self._update_position()

                # Increment counter for HA updates (every 1 second = 5 cycles of 200ms)
                ha_update_counter += 1

                # Check if we've reached the target position (for set_cover_position)
                if self._target_position is not None:
                    position_reached = (
                        self._attr_is_opening
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position >= self._target_position
                    ) or (
                        self._attr_is_closing
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position <= self._target_position
                    )
                    if position_reached:
                        # Clamp to exact target position (do not clear _target_position yet)
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._attr_name,
                            self._target_position,
                        )
                        # If target is 0 or 100, let the device stop naturally at its limits.
                        # For intermediate, send STOP and wait for STOP event to finalize & clear target.
                        if self._target_position not in (0, 100):
                            await self._api.control_blind(self._device_enum, CMD_STOP)
                        # Stop tracking loop
                        self._position_update_task = None
                        # Leave opening/closing flags as-is until STOP to aid debugging
                        self._move_start_time = None
                        self._move_start_position = None
                        # Write state immediately (target preserved)
                        self.async_write_ha_state()
                        return

                # Check if we've reached the limits (only if no specific target position)
                # If a target position is set, let the target position check handle it
                if self._target_position is None:
                    if (
                        self._attr_is_closing
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position <= 0
                    ):
                        _LOGGER.info(
                            "Device %s reached fully closed position (0%%)",
                            self._attr_name,
                        )
                        self._attr_current_cover_position = 0
                        self._position_update_task = None
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        self.async_write_ha_state()
                        return
                    if (
                        self._attr_is_opening
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position >= 100
                    ):
                        _LOGGER.info(
                            "Device %s reached fully open position (100%%)",
                            self._attr_name,
                        )
                        self._attr_current_cover_position = 100
                        self._position_update_task = None
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        self.async_write_ha_state()
                        return

                # Update Home Assistant with new position every 1 second (5 cycles)
                if ha_update_counter >= 5:
                    self.async_write_ha_state()
                    ha_update_counter = 0
        except asyncio.CancelledError:
            _LOGGER.debug("Position tracking cancelled for device %s", self._attr_name)
            self._position_update_task = None
            raise

    def _update_position(self) -> None:
        """Calculate and update the position based on travel time."""
        if self._move_start_time is None or self._move_start_position is None:
            return

        elapsed_time = time.monotonic() - self._move_start_time

        # Use the appropriate travel time based on direction
        travel_time = (
            self._travel_time_open if self._attr_is_opening else self._travel_time_close
        )

        # Calculate total percentage moved since movement started
        total_position_change = (elapsed_time / travel_time) * 100

        if self._attr_is_opening:
            # Position = starting position + change since movement began
            new_pos = self._move_start_position + total_position_change
        elif self._attr_is_closing:
            # Position = starting position - change since movement began
            new_pos = self._move_start_position - total_position_change
        else:
            return

        # Clamp position between 0 and 100
        self._attr_current_cover_position = max(0, min(100, int(new_pos)))
        self._attr_is_closed = self._attr_current_cover_position == 0

        _LOGGER.debug(
            "Device %s position updated to %d%% (elapsed: %.2fs, travel_time: %.2fs)",
            self._device_id,
            self._attr_current_cover_position,
            elapsed_time,
            travel_time,
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        _LOGGER.debug("Opening cover %s (enum=%s)", self._attr_name, self._device_enum)
        self._attr_is_opening = True
        self._attr_is_closing = False
        self._move_start_time = time.monotonic()
        # Guard against None (shouldn't happen after added_to_hass, but be safe)
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        self._move_start_position = self._attr_current_cover_position
        self._start_position_tracking()
        self.async_write_ha_state()
        await self._api.control_blind(self._device_enum, CMD_UP)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close cover."""
        _LOGGER.debug("Closing cover %s (enum=%s)", self._attr_name, self._device_enum)
        self._attr_is_opening = False
        self._attr_is_closing = True
        self._move_start_time = time.monotonic()
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        self._move_start_position = self._attr_current_cover_position
        self._start_position_tracking()
        self.async_write_ha_state()
        await self._api.control_blind(self._device_enum, CMD_DOWN)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        _LOGGER.debug("Stopping cover %s (enum=%s)", self._attr_name, self._device_enum)
        self._stop_position_tracking()
        self._update_position()
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        self._target_position = None
        self.async_write_ha_state()
        await self._api.control_blind(self._device_enum, CMD_STOP)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        target_position = kwargs[ATTR_POSITION]
        # If position unknown, treat as 0 (closed) for movement logic
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        current_position = self._attr_current_cover_position

        _LOGGER.info(
            "Setting cover %s position from %d%% to %d%%",
            self._attr_name,
            current_position,
            target_position,
        )

        if target_position == current_position:
            _LOGGER.debug("Target position equals current position, no action needed")
            return

        # Set the target position for the tracking loop to monitor
        self._target_position = target_position

        # Start moving in the correct direction
        if target_position > current_position:
            _LOGGER.info(
                "Moving cover %s UP to reach target %d%%",
                self._attr_name,
                target_position,
            )
            await self.async_open_cover()
        else:
            _LOGGER.info(
                "Moving cover %s DOWN to reach target %d%%",
                self._attr_name,
                target_position,
            )
            await self.async_close_cover()

        # The position tracking loop will automatically send the stop command
        # when the target position is reached
