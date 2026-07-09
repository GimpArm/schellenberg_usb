"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import SchellenbergUsbApi
from .blind_id import normalize_blind_id
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INVERT_DIRECTION,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_MANUAL_POSITION_SYNC,
    SIGNAL_STICK_STATUS_UPDATED,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
    SchellenbergConfigEntry,
)
from .identities import normalize_status_identities, normalize_status_identity

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
        subentries = [
            subentry
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_BLIND
        ]
        _LOGGER.info("Hub has %d saved blind subentries", len(subentries))

        if not subentries:
            _LOGGER.info("No saved blind subentries found for hub")
            return

        _LOGGER.info("Loading %d saved Schellenberg blinds", len(subentries))

        for subentry in subentries:
            legacy_device_id = subentry.data.get(CONF_DEVICE_ID)
            legacy_device_enum = subentry.data.get(CONF_DEVICE_ENUM)
            command_device_id = (
                subentry.data.get(CONF_COMMAND_DEVICE_ID) or legacy_device_id
            )
            command_enum = subentry.data.get(CONF_COMMAND_ENUM) or legacy_device_enum
            status_identity_source = subentry.data.get(CONF_STATUS_IDENTITY_SOURCE)
            if status_identity_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
                status_device_id = subentry.data.get(CONF_STATUS_DEVICE_ID)
                status_enum = subentry.data.get(CONF_STATUS_ENUM)
            else:
                # Preserve historical behavior only for entries that predate the
                # explicit unknown/automatic/manual provenance field.
                status_device_id = (
                    subentry.data.get(CONF_STATUS_DEVICE_ID)
                    or legacy_device_id
                    or command_device_id
                )
                status_enum = (
                    subentry.data.get(CONF_STATUS_ENUM)
                    or legacy_device_enum
                    or command_enum
                )
            secondary_status_identities = normalize_status_identities(
                subentry.data.get(CONF_SECONDARY_STATUS_IDENTITIES)
            )
            subentry_unique_id = getattr(subentry, "unique_id", None)
            stable_device_id = (
                subentry_unique_id
                if isinstance(subentry_unique_id, str) and subentry_unique_id
                else legacy_device_id or command_device_id
            )
            device_name = subentry.title

            if not all(
                (
                    stable_device_id,
                    command_device_id,
                    command_enum,
                )
            ):
                # This subentry lacks motor identification info; it's likely a non-motor type
                # or pairing is incomplete. Downgrade to debug to avoid user confusion.
                _LOGGER.debug(
                    "Skipping subentry %s (type=%s) with incomplete command identity",
                    subentry.subentry_id,
                    getattr(subentry, "subentry_type", "unknown"),
                )
                continue

            stable_device_id = str(stable_device_id)
            command_device_id = str(command_device_id).strip().upper()
            command_enum = str(command_enum).strip().upper().zfill(2)
            if status_device_id is not None and status_enum is not None:
                status_device_id = str(status_device_id).strip().upper()
                status_enum = str(status_enum).strip().upper().zfill(2)
            else:
                status_device_id = None
                status_enum = None
            blind_id = normalize_blind_id(subentry.data.get(CONF_BLIND_ID)) or str(
                subentry.subentry_id or stable_device_id
            )

            # A UUID remains stable when the blind name or radio identity changes.
            # Existing protocol-derived registry entries are migrated below.
            entity_unique_id = f"{DOMAIN}_blind_{blind_id}"
            existing_entity_id = entity_registry.async_get_entity_id(
                "cover", DOMAIN, entity_unique_id
            )
            if existing_entity_id is None:
                for legacy_unique_id in dict.fromkeys(
                    (
                        f"schellenberg_{stable_device_id}",
                        f"schellenberg_{command_device_id}",
                    )
                ):
                    legacy_entity_id = entity_registry.async_get_entity_id(
                        "cover", DOMAIN, legacy_unique_id
                    )
                    if legacy_entity_id is None:
                        continue
                    entity_registry.async_update_entity(
                        legacy_entity_id,
                        new_unique_id=entity_unique_id,
                        config_subentry_id=subentry.subentry_id,
                    )
                    existing_entity_id = legacy_entity_id
                    _LOGGER.info(
                        "Migrated cover entity %s from %s to stable blind ID %s",
                        legacy_entity_id,
                        legacy_unique_id,
                        blind_id,
                    )
                    break

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
                identifiers={(DOMAIN, stable_device_id)},
                name=device_name,
                manufacturer="Schellenberg",
                model=(
                    f"USB Stick Motor (command {command_device_id}/{command_enum}, "
                    f"primary status "
                    f"{f'{status_device_id}/{status_enum}' if status_device_id else 'unknown'}, "
                    f"secondary statuses {len(secondary_status_identities)})"
                ),
            )
            _LOGGER.debug(
                "Created/updated device %s for paired device %s",
                device.id,
                stable_device_id,
            )

            # Register persisted status identities immediately. Incoming frames can
            # arrive before Home Assistant calls async_added_to_hass on the entity.
            api.register_entity(
                status_device_id,
                status_enum,
                device_name,
                command_device_id=command_device_id,
                command_enum=command_enum,
                secondary_status_identities=secondary_status_identities,
            )

            # Create cover entity linked to this device
            # Create and add the new cover entity attached to the subentry
            _LOGGER.debug("Creating cover entity for device %s", stable_device_id)
            async_add_entities(
                [
                    SchellenbergCover(
                        api=api,
                        device_id=stable_device_id,
                        device_enum=command_enum,
                        device_name=device_name,
                        blind_id=blind_id,
                        device_data=subentry.data,
                        config_entry_id=entry.entry_id,
                        command_device_id=command_device_id,
                        status_device_id=status_device_id,
                        status_enum=status_enum,
                        status_identity_source=str(status_identity_source or "legacy"),
                        secondary_status_identities=secondary_status_identities,
                        invert_direction=bool(
                            subentry.data.get(CONF_INVERT_DIRECTION, False)
                        ),
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
        blind_id: str | None = None,
        device_data: Mapping[str, Any] | None = None,
        config_entry_id: str | None = None,
        command_device_id: str | None = None,
        status_device_id: str | None = None,
        status_enum: str | None = None,
        status_identity_source: str | None = None,
        secondary_status_identities: object = None,
        invert_direction: bool = False,
    ) -> None:
        """Initialize the Schellenberg cover entity.

        Args:
            api: The API instance for communication
            device_id: The unique device ID (6-character hex)
            device_enum: The device enumerator for commands (2-character hex)
            device_name: Friendly name for the device
            blind_id: Stable per-blind UUID used for the entity registry
            device_data: Device data dict containing calibration times
            config_entry_id: The config entry ID for linking to device
            command_device_id: Protocol ID associated with outgoing commands
            status_device_id: Protocol ID expected in incoming status messages
            status_enum: Enum expected in primary incoming status messages
            status_identity_source: How the primary status identity was obtained
            secondary_status_identities: Additional identities matched diagnostically
            invert_direction: Swap physical up/down commands for logical open/close

        """
        self._api = api
        self._device_id = device_id
        self._command_device_id = command_device_id or device_id
        self._command_enum = device_enum
        self._status_identity_source = status_identity_source or "legacy"
        if self._status_identity_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
            primary_identity = None
        else:
            primary_identity = normalize_status_identity(
                status_device_id or self._command_device_id,
                status_enum or device_enum,
            )
        self._status_device_id: str | None
        self._status_enum: str | None
        if primary_identity is None:
            self._status_device_id = None
            self._status_enum = None
        else:
            self._status_device_id, self._status_enum = primary_identity
        secondary_source = secondary_status_identities
        if secondary_source is None and device_data is not None:
            secondary_source = device_data.get(CONF_SECONDARY_STATUS_IDENTITIES)
        primary_identity = normalize_status_identity(
            self._status_device_id, self._status_enum
        )
        self._secondary_status_identities = tuple(
            identity
            for identity in normalize_status_identities(secondary_source)
            if identity != primary_identity
        )
        self._invert_direction = invert_direction
        # Backward-compatible alias retained for diagnostics.
        self._device_enum = self._command_enum
        self._config_entry_id = config_entry_id
        blind_id_source = blind_id
        if blind_id_source is None and device_data is not None:
            blind_id_source = device_data.get(CONF_BLIND_ID)
        self._blind_id = normalize_blind_id(blind_id_source) or str(
            blind_id_source or device_id
        )

        # Entity attributes
        self._attr_unique_id = f"{DOMAIN}_blind_{self._blind_id}"
        self._device_name = device_name
        self._attr_name = None
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
        device_data_dict = dict(device_data) if device_data is not None else {}
        self._travel_time_open: float = device_data_dict.get(
            CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME
        )
        self._travel_time_close: float = device_data_dict.get(
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
        self._position_update_source = "not recorded"
        self._position_source_kind = "startup default"
        self._position_confirmed_since_restart = False
        self._full_travel_resync_direction: str | None = None
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
        self._api.register_entity(
            self._status_device_id,
            self._status_enum,
            self._device_name,
            command_device_id=self._command_device_id,
            command_enum=self._command_enum,
            secondary_status_identities=self._secondary_status_identities,
        )

        # Restore the last known state
        restored_from_ha = False
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
                restored_from_ha = True
                _LOGGER.debug(
                    "Restored position for %s (%s) to %d%% (raw=%s)",
                    self._device_name,
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
                self._device_name,
                self._device_id,
            )

        if restored_from_ha:
            self._position_source_kind = "restored HA state"
            self._position_update_source = "restored HA state"
            startup_status = "restored / estimated / not confirmed since restart"
        else:
            self._position_source_kind = "startup default"
            self._position_update_source = "startup default (no restored HA state)"
            startup_status = "estimated / not confirmed since restart"
        self._position_confirmed_since_restart = False
        self._full_travel_resync_direction = None

        # IMPORTANT: We must write the restored (or default) position to the state machine now.
        # add_to_platform_finish() already wrote an initial state before restoration ran, so without
        # this call the restored position would not be visible until the first movement/event.
        # Initial write after restoration (debug instrumentation removed).
        self.async_write_ha_state()
        self._record_position_update(
            source=self._position_update_source,
            direction="idle",
            previous_position=None,
            new_position=self._attr_current_cover_position,
            status=startup_status,
        )

        # Only an observed or manually supplied primary status identity may drive
        # received-frame position tracking. Unknown status never aliases command ID.
        if self._status_device_id is not None and self._status_enum is not None:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_EVENT}_{self._status_device_id}_{self._status_enum}",
                    self._handle_event,
                )
            )

        # Developer Tools position corrections target the command identity because it
        # is unique per configured cover and does not depend on a received RF frame.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_MANUAL_POSITION_SYNC}_{self._command_device_id.upper()}",
                self._handle_manual_position_sync,
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

        # Persist the latest estimate and stop the non-critical loop before HA's
        # final-write shutdown stage.
        self.async_on_remove(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_handle_hass_stop
            )
        )

    def _record_position_update(
        self,
        *,
        source: str,
        direction: str,
        previous_position: int | None,
        new_position: int | None,
        status: str,
    ) -> None:
        """Publish one position model update for Developer Tools diagnostics."""
        self._api.record_position_update(
            self._command_device_id,
            source=source,
            direction=direction,
            previous_position=previous_position,
            new_position=new_position,
            position_source=self._position_source_kind,
            confirmed_since_restart=self._position_confirmed_since_restart,
            status=status,
        )

    @callback
    def _handle_status_update(self) -> None:
        """Handle status update from API (connection state changed)."""
        self.async_write_ha_state()

    @callback
    def _handle_manual_position_sync(self, position: int) -> None:
        """Apply an exact user-provided position without transmitting to the motor."""
        normalized_position = max(0, min(100, int(position)))
        previous_position = self._attr_current_cover_position
        self._stop_position_tracking()
        self._attr_current_cover_position = normalized_position
        self._attr_is_closed = normalized_position == 0
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        self._target_position = None
        self._position_update_source = "Developer Tools manual position sync"
        self._position_source_kind = "manual sync"
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._record_position_update(
            source=self._position_update_source,
            direction="manual",
            previous_position=previous_position,
            new_position=normalized_position,
            status="confirmed/manual",
        )
        _LOGGER.warning(
            "Manual position sync applied cover=%s command_device_id=%s "
            "previous_position=%s new_position=%d status=confirmed/manual",
            self._device_name,
            self._command_device_id,
            previous_position,
            normalized_position,
        )
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
        previous_position = self._attr_current_cover_position
        self._travel_time_open = open_time
        self._travel_time_close = close_time

        # The device is fully closed after calibration, so set position to 0
        self._attr_current_cover_position = 0
        self._attr_is_closed = True
        self._position_update_source = "completed calibration"
        self._position_source_kind = "calibration"
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._record_position_update(
            source=self._position_update_source,
            direction="stop",
            previous_position=previous_position,
            new_position=0,
            status="confirmed",
        )

        _LOGGER.info(
            "Device %s calibration updated: open_time=%.2fs, close_time=%.2fs. "
            "Cover position set to fully closed (0%%)",
            self._device_name,
            open_time,
            close_time,
        )

        # Update entity state
        self.async_write_ha_state()

    async def _async_handle_hass_stop(self, _event: Event) -> None:
        """Persist and stop position tracking before final-write shutdown."""
        await self._async_shutdown_position_tracking("Home Assistant stopping")

    async def _async_shutdown_position_tracking(self, reason: str) -> None:
        """Persist the latest estimate and await position-loop cancellation."""
        task = self._position_update_task
        if task is None:
            return

        previous_position = self._attr_current_cover_position
        self._update_position()
        if self.entity_id is not None:
            self.async_write_ha_state()
        _LOGGER.debug(
            "Stopping position tracking cover=%s reason=%s position=%s previous=%s",
            self._device_name,
            reason,
            self._attr_current_cover_position,
            previous_position,
        )
        await self._async_cancel_position_tracking(reason)

    async def _async_cancel_position_tracking(self, reason: str) -> None:
        """Cancel and await the currently tracked position loop, if any."""
        task = self._position_update_task
        if task is None:
            return
        if task is asyncio.current_task():
            if self._position_update_task is task:
                self._position_update_task = None
            return

        if self._position_update_task is task:
            self._position_update_task = None
        if not task.done():
            task.cancel(reason)
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception(
                "Position tracking task failed while stopping cover=%s reason=%s",
                self._device_name,
                reason,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed or its config entry is unloaded."""
        await self._async_shutdown_position_tracking("entity removal or entry unload")
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: str) -> None:
        """Handle events from the USB stick for this device."""
        _LOGGER.info(
            "Device %s (%s) received activity event: %s",
            self._device_name,
            self._device_id,
            event,
        )

        if event in (EVENT_STARTED_MOVING_UP, EVENT_STARTED_MOVING_DOWN):
            previous_position = self._attr_current_cover_position
            physical_up = event == EVENT_STARTED_MOVING_UP
            logical_opening = physical_up != self._invert_direction
            _LOGGER.info(
                "Device %s physical_direction=%s logical_direction=%s",
                self._device_name,
                "up" if physical_up else "down",
                "opening" if logical_opening else "closing",
            )
            self._attr_is_opening = logical_opening
            self._attr_is_closing = not logical_opening
            self._move_start_time = time.monotonic()
            if self._attr_current_cover_position is None:
                self._attr_current_cover_position = 0
            self._move_start_position = self._attr_current_cover_position
            self._position_source_kind = "primary status"
            self._position_confirmed_since_restart = True
            self._full_travel_resync_direction = None
            self._position_update_source = (
                f"primary status {self._status_device_id}/{self._status_enum} "
                f"command {event}"
            )
            self._record_position_update(
                source=self._position_update_source,
                direction="opening" if logical_opening else "closing",
                previous_position=previous_position,
                new_position=self._attr_current_cover_position,
                status="confirmed",
            )
            self._start_position_tracking()
        elif event == EVENT_STOPPED:
            previous_position = self._attr_current_cover_position
            self._position_source_kind = "primary status"
            self._position_confirmed_since_restart = True
            self._full_travel_resync_direction = None
            self._position_update_source = (
                f"primary status {self._status_device_id}/{self._status_enum} "
                f"command {event}"
            )
            _LOGGER.info(
                "Device %s STOPPED (position: %d%%)",
                self._device_name,
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
            self._record_position_update(
                source=self._position_update_source,
                direction="stop",
                previous_position=previous_position,
                new_position=self._attr_current_cover_position,
                status="confirmed",
            )
            # Clear movement tracking variables
            self._move_start_time = None
            self._move_start_position = None
            self._target_position = None  # Clear target position on stop
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._device_name, event
            )

        self.async_write_ha_state()

    def _start_position_tracking(self) -> None:
        """Ensure exactly one lifecycle-managed position loop is running."""
        existing_task = self._position_update_task
        if (
            existing_task is not None
            and not existing_task.done()
            and not existing_task.cancelling()
        ):
            return
        if self._position_update_task is existing_task:
            self._position_update_task = None

        task_name = f"{DOMAIN} position update {self._blind_id}"
        config_entry = (
            self.hass.config_entries.async_get_entry(self._config_entry_id)
            if self._config_entry_id is not None
            else None
        )
        if config_entry is not None:
            task = config_entry.async_create_background_task(
                self.hass,
                self._async_position_update_loop(),
                task_name,
            )
        else:
            task = self.hass.async_create_background_task(
                self._async_position_update_loop(), task_name
            )
        self._position_update_task = task

    def _stop_position_tracking(
        self, reason: str = "position tracking stopped"
    ) -> None:
        """Request immediate cancellation from synchronous event handlers."""
        task = self._position_update_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._position_update_task is task:
            self._position_update_task = None

    def _waiting_for_full_travel_resync(self, direction: str) -> bool:
        """Return whether an unconfirmed startup estimate needs more travel time."""
        if (
            self._full_travel_resync_direction != direction
            or self._move_start_time is None
        ):
            return False
        travel_time = (
            self._travel_time_open
            if direction == "opening"
            else self._travel_time_close
        )
        return time.monotonic() - self._move_start_time < travel_time

    def _confirm_full_travel_resync(self, direction: str, position: int) -> None:
        """Anchor an endpoint after one complete configured travel interval."""
        if self._full_travel_resync_direction != direction:
            return
        previous_position = self._move_start_position
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._record_position_update(
            source=self._position_update_source,
            direction=direction,
            previous_position=previous_position,
            new_position=position,
            status="estimated from full travel",
        )

    async def _async_position_update_loop(self) -> None:
        """Update position every 200ms internally, report to HA every 1 second."""
        position_task = asyncio.current_task()
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
                            self._device_name,
                            self._target_position,
                        )
                        # If target is 0 or 100, let the device stop naturally at its limits.
                        # For intermediate, send STOP and wait for STOP event to finalize & clear target.
                        if self._target_position not in (0, 100):
                            await self._api.control_blind(
                                self._command_enum,
                                CMD_STOP,
                                device_id=self._command_device_id,
                            )
                        # Stop tracking loop
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
                        and not self._waiting_for_full_travel_resync("closing")
                    ):
                        _LOGGER.info(
                            "Device %s reached fully closed position (0%%)",
                            self._device_name,
                        )
                        self._attr_current_cover_position = 0
                        self._confirm_full_travel_resync("closing", 0)
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
                        and not self._waiting_for_full_travel_resync("opening")
                    ):
                        _LOGGER.info(
                            "Device %s reached fully open position (100%%)",
                            self._device_name,
                        )
                        self._attr_current_cover_position = 100
                        self._confirm_full_travel_resync("opening", 100)
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
            _LOGGER.debug(
                "Position tracking cancelled for device %s", self._device_name
            )
            raise
        finally:
            # A cancelled older loop must never clear its newer replacement.
            if self._position_update_task is position_task:
                self._position_update_task = None

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
        previous_position = self._attr_current_cover_position
        self._attr_current_cover_position = max(0, min(100, int(new_pos)))
        self._attr_is_closed = self._attr_current_cover_position == 0
        if self._attr_current_cover_position != previous_position:
            self._record_position_update(
                source=self._position_update_source,
                direction="opening" if self._attr_is_opening else "closing",
                previous_position=previous_position,
                new_position=self._attr_current_cover_position,
                status="estimated",
            )

        _LOGGER.debug(
            "Device %s position updated to %d%% (elapsed: %.2fs, travel_time: %.2fs)",
            self._device_id,
            self._attr_current_cover_position,
            elapsed_time,
            travel_time,
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        action = CMD_DOWN if self._invert_direction else CMD_UP
        _LOGGER.debug(
            "Opening cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        self._attr_is_opening = True
        self._attr_is_closing = False
        self._move_start_time = time.monotonic()
        # Guard against None (shouldn't happen after added_to_hass, but be safe)
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        self._move_start_position = self._attr_current_cover_position
        self._position_update_source = "Home Assistant open command"
        self._position_source_kind = "HA command"
        self._full_travel_resync_direction = (
            "opening"
            if not self._position_confirmed_since_restart
            and self._target_position is None
            else None
        )
        self._record_position_update(
            source=self._position_update_source,
            direction="opening",
            previous_position=self._attr_current_cover_position,
            new_position=self._attr_current_cover_position,
            status="estimated",
        )
        await self._async_cancel_position_tracking("new open command")
        self._start_position_tracking()
        self.async_write_ha_state()
        await self._api.control_blind(
            self._command_enum, action, device_id=self._command_device_id
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close cover."""
        action = CMD_UP if self._invert_direction else CMD_DOWN
        _LOGGER.debug(
            "Closing cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        self._attr_is_opening = False
        self._attr_is_closing = True
        self._move_start_time = time.monotonic()
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        self._move_start_position = self._attr_current_cover_position
        self._position_update_source = "Home Assistant close command"
        self._position_source_kind = "HA command"
        self._full_travel_resync_direction = (
            "closing"
            if not self._position_confirmed_since_restart
            and self._target_position is None
            else None
        )
        self._record_position_update(
            source=self._position_update_source,
            direction="closing",
            previous_position=self._attr_current_cover_position,
            new_position=self._attr_current_cover_position,
            status="estimated",
        )
        await self._async_cancel_position_tracking("new close command")
        self._start_position_tracking()
        self.async_write_ha_state()
        await self._api.control_blind(
            self._command_enum, action, device_id=self._command_device_id
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        _LOGGER.debug(
            "Stopping cover %s (command_id=%s enum=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
        )
        previous_position = self._attr_current_cover_position
        self._position_source_kind = "HA command"
        self._full_travel_resync_direction = None
        self._position_update_source = "Home Assistant stop command"
        await self._async_cancel_position_tracking("Home Assistant stop command")
        self._update_position()
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        self._target_position = None
        self._record_position_update(
            source=self._position_update_source,
            direction="stop",
            previous_position=previous_position,
            new_position=self._attr_current_cover_position,
            status="estimated",
        )
        self.async_write_ha_state()
        await self._api.control_blind(
            self._command_enum, CMD_STOP, device_id=self._command_device_id
        )

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        target_position = kwargs[ATTR_POSITION]
        # If position unknown, treat as 0 (closed) for movement logic
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
        current_position = self._attr_current_cover_position

        _LOGGER.info(
            "Setting cover %s position from %d%% to %d%%",
            self._device_name,
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
                self._device_name,
                target_position,
            )
            await self.async_open_cover()
        else:
            _LOGGER.info(
                "Moving cover %s DOWN to reach target %d%%",
                self._device_name,
                target_position,
            )
            await self.async_close_cover()

        # The position tracking loop will automatically send the stop command
        # when the target position is reached
