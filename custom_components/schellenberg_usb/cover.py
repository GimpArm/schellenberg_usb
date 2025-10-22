"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store

from .api import SchellenbergUsbApi
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_OPEN_TIME,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_STICK_STATUS_UPDATED,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_devices"
DEFAULT_TRAVEL_TIME = 60.0  # seconds, a sensible default


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Schellenberg cover entities."""
    api = entry.runtime_data
    device_registry = dr.async_get(hass)

    storage = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await storage.async_load()

    if not stored_data or "devices" not in stored_data:
        _LOGGER.info("No saved Schellenberg devices found")
        return

    devices = stored_data["devices"]
    _LOGGER.info("Loading %d saved Schellenberg devices", len(devices))

    if not devices:
        _LOGGER.warning("Devices list is empty despite 'devices' key existing")
        return

    entities = []
    for device in devices:
        device_id = device["id"]
        device_name = device.get("name", f"Blind {device_id}")
        device_enum = device.get("enum")

        if not device_enum:
            _LOGGER.warning(
                "Device %s is missing enum value, skipping entity creation", device_id
            )
            continue

        # Create or get device in device registry
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Schellenberg",
            model=f"USB Stick Motor ({device_id}/{device_enum})",
        )

        # Create cover entity linked to this device
        # Note: We create entities for ALL devices every time, not checking if they
        # already exist. Home Assistant's entity platform will handle deduplication
        # and updating existing entities with the same unique_id.
        entities.append(
            SchellenbergCover(
                api=api,
                device_id=device_id,
                device_enum=device_enum,
                device_name=device_name,
                device_data=device,
                config_entry_id=entry.entry_id,
            )
        )

    _LOGGER.debug("Setting up %d cover entities", len(entities))
    async_add_entities(entities)


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
        self._attr_current_cover_position = 50  # Assume 50% on startup until restored

        # Link this entity to the device
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Schellenberg",
            model=f"USB Stick Motor ({device_id}/{device_enum})",
        )

        # Position calculation attributes - use calibration times if available
        device_data = device_data or {}
        self._travel_time_open = device_data.get(CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME)
        self._travel_time_close = device_data.get(CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME)
        self._move_start_time = None
        self._move_start_position = None  # Starting position when movement began
        self._position_update_task = None  # Task for real-time position updates
        self._target_position = None  # Target position for set_cover_position

    @property
    def available(self) -> bool:
        """Return if entity is available.

        The entity is available when the USB stick is connected and in listening mode.
        """
        return self._api.is_connected

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Register this entity with the API so it knows we're listening
        self._api.register_entity(self._device_id, self._device_enum)

        # Restore the last known state
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get(ATTR_POSITION) is not None:
            self._attr_current_cover_position = last_state.attributes[ATTR_POSITION]
            self._attr_is_closed = self._attr_current_cover_position == 0

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

        _LOGGER.info(
            "Device %s calibration updated: open_time=%.2fs, close_time=%.2fs",
            self._attr_name,
            open_time,
            close_time,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        await super().async_will_remove_from_hass()
        # Stop any running position tracking tasks
        self._stop_position_tracking()

    @callback
    def _handle_event(self, event: str):
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
            self._update_position()
            self._attr_is_opening = False
            self._attr_is_closing = False
            self._move_start_time = None
            self._move_start_position = None
            self._target_position = None  # Clear target position on stop
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._attr_name, event
            )

        self.async_write_ha_state()

    def _start_position_tracking(self):
        """Start tracking position updates every second."""
        # Cancel any existing tracking task
        self._stop_position_tracking()

        # Create a new task to update position every second
        self._position_update_task = self.hass.async_create_task(
            self._async_position_update_loop()
        )

    def _stop_position_tracking(self):
        """Stop the position tracking task."""
        if self._position_update_task and not self._position_update_task.done():
            self._position_update_task.cancel()
        self._position_update_task = None

    async def _async_position_update_loop(self):
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
                        and self._attr_current_cover_position >= self._target_position
                    ) or (
                        self._attr_is_closing
                        and self._attr_current_cover_position <= self._target_position
                    )

                    if position_reached:
                        # Clamp to exact target position
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._attr_name,
                            self._target_position,
                        )
                        # Send stop command to the device
                        await self._api.control_blind(self._device_enum, CMD_STOP)
                        self._position_update_task = None
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        self._target_position = None
                        self.async_write_ha_state()
                        return

                # Check if we've reached the limits
                if self._attr_current_cover_position <= 0:
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
                    self._target_position = None
                    self.async_write_ha_state()
                    return
                if self._attr_current_cover_position >= 100:
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
                    self._target_position = None
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

    def _update_position(self):
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

    async def async_open_cover(self, **kwargs) -> None:
        """Open the cover."""
        _LOGGER.debug("Opening cover %s (enum=%s)", self._attr_name, self._device_enum)
        await self._api.control_blind(self._device_enum, CMD_UP)

    async def async_close_cover(self, **kwargs) -> None:
        """Close cover."""
        _LOGGER.debug("Closing cover %s (enum=%s)", self._attr_name, self._device_enum)
        await self._api.control_blind(self._device_enum, CMD_DOWN)

    async def async_stop_cover(self, **kwargs) -> None:
        """Stop the cover."""
        _LOGGER.debug("Stopping cover %s (enum=%s)", self._attr_name, self._device_enum)
        await self._api.control_blind(self._device_enum, CMD_STOP)

    async def async_set_cover_position(self, **kwargs) -> None:
        """Move the cover to a specific position."""
        target_position = kwargs[ATTR_POSITION]
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

        if abs(target_position - current_position) < 2:  # Ignore very small changes
            _LOGGER.debug("Position change too small (<%d%%), ignoring", 2)
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
