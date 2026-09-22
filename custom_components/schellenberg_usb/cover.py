"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
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
    DEFAULT_TRAVEL_TIME_SECONDS,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    try:
        _LOGGER.info("Cover platform async_setup_entry called for: %s", entry.entry_id)
        _LOGGER.debug("Entry data: %s", entry.data)

        if CONF_SERIAL_PORT not in entry.data:
            _LOGGER.warning(
                "Cover platform called for non-hub entry %s, ignoring", entry.entry_id
            )
            return

        _LOGGER.info("Setting up cover for hub entry: %s", entry.title)
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        api = entry.runtime_data

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
                    "Re-instantiating cover entity object for existing "
                    "registry entry %s",
                    existing_entity_id,
                )

            # NOTE: `dr.async_get_device_id_by_identifier` never existed in Home
            # Assistant (calling it raised AttributeError, not caught by the
            # ValueError handler that used to sit here). Identifiers are also no
            # longer globally unique as of HA 2026.8 (they are scoped per config
            # entry), so the lookup itself must be scoped: use the registry's
            # async_get_device_by_identifier(identifier, entry_id), which returns
            # the DeviceEntry directly (or None), instead of the removed
            # async_get_device(identifiers=...) helper.
            device = device_registry.async_get_device_by_identifier(
                (DOMAIN, stable_device_id), entry.entry_id
            )

            if device is None:
                primary_status_text = (
                    f"{status_device_id}/{status_enum}"
                    if status_device_id
                    else "unknown"
                )
                device = device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    config_subentry_id=subentry.subentry_id,
                    identifiers={(DOMAIN, stable_device_id)},
                    name=device_name,
                    manufacturer="Schellenberg",
                    model=(
                        f"USB Stick Motor (command {command_device_id}/{command_enum}, "
                        f"primary status {primary_status_text}, "
                        f"secondary statuses {len(secondary_status_identities)})"
                    ),
                )
            elif device.config_subentry_id != subentry.subentry_id:
                device_registry.async_update_device(
                    device.id,
                    new_config_subentry_id=subentry.subentry_id,
                )
            stable_device_registry_id = device.id
            _LOGGER.debug(
                "Created/updated device %s for paired device %s",
                stable_device_registry_id,
                stable_device_id,
            )

            api.register_entity(
                status_device_id,
                status_enum,
                device_name,
                command_device_id=command_device_id,
                command_enum=command_enum,
                secondary_status_identities=secondary_status_identities,
            )

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
    """Representation of a single Schellenberg-controlled roller shutter/blind.

    Position is not reported by the hardware in real time: it is estimated
    from elapsed time against a calibrated (or default) travel time, and
    resynchronized whenever a status broadcast, manual sync, or calibration
    event provides a more authoritative data point.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

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
        """Initialize the cover entity from its config subentry data."""
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
        self._device_enum = self._command_enum
        self._config_entry_id = config_entry_id
        blind_id_source = blind_id
        if blind_id_source is None and device_data is not None:
            blind_id_source = device_data.get(CONF_BLIND_ID)
        self._blind_id = normalize_blind_id(blind_id_source) or str(
            blind_id_source or device_id
        )

        self._attr_unique_id = f"{DOMAIN}_blind_{self._blind_id}"
        self._device_name = device_name
        self._attr_name = None
        self._attr_is_closed = None
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_current_cover_position: int | None = None

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, device_id)},
        )

        device_data_dict = dict(device_data) if device_data is not None else {}
        self._travel_time_open: float = device_data_dict.get(
            CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME_SECONDS
        )
        self._travel_time_close: float = device_data_dict.get(
            CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME_SECONDS
        )
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._position_update_task: asyncio.Task[None] | None = None
        self._target_position: int | None = None
        self._position_update_source = "not recorded"
        self._position_source_kind = "startup default"
        self._position_confirmed_since_restart = False
        self._full_travel_resync_direction: str | None = None

    @property
    def available(self) -> bool:
        """Return whether the hub's serial connection is up."""
        return self._api.is_connected

    @property
    def icon(self) -> str:
        """Return an icon reflecting current movement direction or position."""
        if self._attr_is_opening:
            return "mdi:arrow-up-box"
        if self._attr_is_closing:
            return "mdi:arrow-down-box"
        if self._attr_is_closed:
            return "mdi:window-shutter"
        return "mdi:window-shutter-open"

    async def async_added_to_hass(self) -> None:
        """Restore the last known position and subscribe to update signals."""
        await super().async_added_to_hass()

        self._api.register_entity(
            self._status_device_id,
            self._status_enum,
            self._device_name,
            command_device_id=self._command_device_id,
            command_enum=self._command_enum,
            secondary_status_identities=self._secondary_status_identities,
        )

        restored_from_ha = False
        last_state = await self.async_get_last_state()
        if last_state:
            restored_position: int | None = None
            # ATTR_CURRENT_POSITION ("current_position") is the state attribute
            # cover entities report. ATTR_POSITION ("position") is only the
            # set_cover_position *service* field (used below in
            # async_set_cover_position) and is never present on a restored
            # state, so it must not be used as a fallback here.
            raw_position = last_state.attributes.get(ATTR_CURRENT_POSITION)
            if isinstance(raw_position, (int, float)):
                restored_position = int(raw_position)
            elif raw_position is not None:
                try:
                    restored_position = int(str(raw_position))
                except ValueError:
                    restored_position = None

            if restored_position is None:
                if last_state.state == "open":
                    restored_position = 100
                elif last_state.state == "closed":
                    restored_position = 0

            if restored_position is not None:
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

        self.async_write_ha_state()
        self._record_position_update(
            source=self._position_update_source,
            direction="idle",
            previous_position=None,
            new_position=self._attr_current_cover_position,
            status=startup_status,
        )

        if self._status_device_id is not None and self._status_enum is not None:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_EVENT}_{self._status_device_id}_{self._status_enum}",
                    self._handle_event,
                )
            )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_MANUAL_POSITION_SYNC}_{self._command_device_id.upper()}",
                self._handle_manual_position_sync,
            )
        )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STICK_STATUS_UPDATED,
                self._handle_status_update,
            )
        )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CALIBRATION_COMPLETED,
                self._handle_calibration_completed,
            )
        )

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
        """Forward a position change to the API's diagnostic history log."""
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
        """Refresh entity state when the hub's overall status changes."""
        self.async_write_ha_state()

    @callback
    def _handle_manual_position_sync(self, position: int) -> None:
        """Apply a position manually confirmed via Developer Tools."""
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
        """Apply newly measured travel times and reset to fully closed."""
        if device_id != self._device_id:
            return

        previous_position = self._attr_current_cover_position
        self._travel_time_open = open_time
        self._travel_time_close = close_time

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

        self.async_write_ha_state()

    async def _async_handle_hass_stop(self, _event: Event) -> None:
        """Freeze the estimated position before Home Assistant shuts down."""
        await self._async_shutdown_position_tracking("Home Assistant stopping")

    async def _async_shutdown_position_tracking(self, reason: str) -> None:
        """Take a final position snapshot and cancel background tracking."""
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
        """Cancel the background position-tracking task and await its exit."""
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
        """Clean up background tracking when the entity is removed."""
        await self._async_shutdown_position_tracking("entity removal or entry unload")
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: str) -> None:
        """Handle a device activity event (started moving, or stopped)."""
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
            self._stop_position_tracking()
            # Always re-estimate from elapsed time rather than snapping to
            # _target_position. By the time a STOPPED echo can arrive for a
            # stop this entity itself requested, the target has already been
            # reached and cleared (see _async_position_update_loop and
            # async_stop_cover, and note control_blind()/send_command() have
            # no internal await-suspension point, so nothing can interleave
            # while either is in flight). A STOPPED event that still finds a
            # target set therefore means the blind was interrupted externally
            # (e.g. the physical remote), in which case the commanded target
            # was never reached and snapping to it would report a false
            # position instead of where the blind actually stopped.
            self._update_position()
            if self._attr_current_cover_position is not None:
                if self._attr_current_cover_position <= 0:
                    self._attr_current_cover_position = 0
                elif self._attr_current_cover_position >= 100:
                    self._attr_current_cover_position = 100
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
            self._move_start_time = None
            self._move_start_position = None
            self._target_position = None
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._device_name, event
            )

        self.async_write_ha_state()

    def _start_position_tracking(self) -> None:
        """Start the background loop that estimates position while moving."""
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
        """Request cancellation of the position-tracking task without awaiting it."""
        task = self._position_update_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._position_update_task is task:
            self._position_update_task = None

    def _waiting_for_full_travel_resync(self, direction: str) -> bool:
        """Return whether a fresh full-travel confirmation is still pending."""
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
        """Record that reaching a travel limit has been confirmed by full transit."""
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
        """Poll and estimate position every 0.2s while the cover is moving."""
        position_task = asyncio.current_task()
        try:
            ha_update_counter = 0
            while True:
                await asyncio.sleep(0.2)

                self._update_position()

                ha_update_counter += 1

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
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._device_name,
                            self._target_position,
                        )
                        if self._target_position not in (0, 100):
                            await self._api.control_blind(
                                self._command_enum,
                                CMD_STOP,
                                device_id=self._command_device_id,
                            )
                        self._move_start_time = None
                        self._move_start_position = None
                        self._target_position = None
                        self.async_write_ha_state()
                        return

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

                if ha_update_counter >= 5:
                    self.async_write_ha_state()
                    ha_update_counter = 0
        except asyncio.CancelledError:
            _LOGGER.debug(
                "Position tracking cancelled for device %s", self._device_name
            )
            raise
        finally:
            if self._position_update_task is position_task:
                self._position_update_task = None

    def _update_position(self) -> None:
        """Recompute the estimated current position from elapsed travel time."""
        if self._move_start_time is None or self._move_start_position is None:
            return

        elapsed_time = time.monotonic() - self._move_start_time

        # Guard against a zero/negative travel time (corrupt or hand-edited
        # config data): dividing by it would raise ZeroDivisionError inside
        # this background task and silently freeze position tracking.
        travel_time = max(
            self._travel_time_open
            if self._attr_is_opening
            else self._travel_time_close,
            0.1,
        )

        total_position_change = (elapsed_time / travel_time) * 100

        if self._attr_is_opening:
            new_pos = self._move_start_position + total_position_change
        elif self._attr_is_closing:
            new_pos = self._move_start_position - total_position_change
        else:
            return

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

    async def async_open_cover(
        self, *, _preserve_target: bool = False, **kwargs: Any
    ) -> None:
        """Open the cover, or drive it upward as a leg of set_cover_position."""
        if not _preserve_target:
            # A plain open request (not a set_cover_position leg) always
            # means "go all the way to 100" - clear any stale target left
            # over from an earlier partial move so this one isn't cut short.
            self._target_position = None
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

    async def async_close_cover(
        self, *, _preserve_target: bool = False, **kwargs: Any
    ) -> None:
        """Close the cover, or drive it downward as a leg of set_cover_position."""
        if not _preserve_target:
            # Same reasoning as async_open_cover: a plain close always
            # targets 0, so drop any stale partial-move target first.
            self._target_position = None
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
        """Stop the cover immediately and freeze the estimated position."""
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
        """Move the cover to a specific position by chaining open/close."""
        target_position = kwargs[ATTR_POSITION]
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

        self._target_position = target_position

        if target_position > current_position:
            _LOGGER.info(
                "Moving cover %s UP to reach target %d%%",
                self._device_name,
                target_position,
            )
            await self.async_open_cover(_preserve_target=True)
        else:
            _LOGGER.info(
                "Moving cover %s DOWN to reach target %d%%",
                self._device_name,
                target_position,
            )
            await self.async_close_cover(_preserve_target=True)
