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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import SchellenbergUsbApi, StopOutcome
from .blind_id import normalize_blind_id
from .const import (
    AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INITIAL_POSITION,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CONFIRMED_POSITION,
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
    MAX_STOP_LEAD_PERCENT,
    NEAR_END_STOP_SNAP_TOLERANCE_PERCENT,
    POSITION_UPDATE_LOOP_TIMEOUT_SECONDS,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_MANUAL_POSITION_SYNC,
    SIGNAL_STICK_STATUS_UPDATED,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    STOP_COMMAND_LATENCY_SECONDS,
    SUBENTRY_TYPE_BLIND,
    SchellenbergConfigEntry,
    resolve_direction_command,
)
from .device_registry_compat import (
    async_device_on_subentry_compat,
    async_get_device_by_identifier_compat,
    async_reassign_device_subentry_compat,
)
from .identities import normalize_status_identities, normalize_status_identity
from .runtime_translation_text import (
    runtime_translation_text as _runtime_translation_text,
)
from .runtime_translation_text import translate_block_reason as _translate_block_reason

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchellenbergConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cover entities from the hub's saved blind subentries.

    Resolves each blind's stable device ID (falling back to legacy
    device/enum fields for pre-migration blinds), creates or reassigns
    the device registry device, backfills stable blind IDs, then
    forwards setup to the cover/sensor/switch platforms.
    """
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
                # UNKNOWN status-identity source means no confirmed primary
                # identity yet; null the stale CONF_STATUS_DEVICE_ID/ENUM fields
                # too rather than re-registering an identity that is no longer
                # trusted.
                status_device_id = None
                status_enum = None
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
            # Captured before the legacy-unique_id fallback below can mark
            # existing_entity_id True - False only means "this stable unique ID
            # was never registered before", not "this is a brand-new pairing".
            is_newly_registered_entity = existing_entity_id is None
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

            # Device identifiers are scoped per config entry since HA 2026.9;
            # see device_registry_compat.py for the lookup that works on older
            # releases too.
            device = async_get_device_by_identifier_compat(
                device_registry, (DOMAIN, stable_device_id), entry.entry_id
            )

            primary_status_text = (
                f"{status_device_id}/{status_enum}"
                if status_device_id
                else _runtime_translation_text(hass, "unknown_label")
            )
            current_model = _runtime_translation_text(
                hass,
                "device_model_motor",
                command_device_id=command_device_id,
                command_enum=command_enum,
                primary_status_text=primary_status_text,
                secondary_count=len(secondary_status_identities),
            )
            if device is None:
                device = device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    config_subentry_id=subentry.subentry_id,
                    identifiers={(DOMAIN, stable_device_id)},
                    name=device_name,
                    manufacturer="Schellenberg",
                    model=current_model,
                )
            else:
                if not async_device_on_subentry_compat(
                    device, entry.entry_id, subentry.subentry_id
                ):
                    async_reassign_device_subentry_compat(
                        device_registry, device, entry.entry_id, subentry.subentry_id
                    )
                # device_name/current_model are recomputed from the subentry's
                # current data on every setup/reload, but the device registry entry
                # itself is only written here - without this sync, editing a
                # blind's name or status identity never reached the device page.
                # Guarded by the equality check so an ordinary reload with no real
                # change never writes to the registry.
                if device.name != device_name or device.model != current_model:
                    device_registry.async_update_device(
                        device.id,
                        name=device_name,
                        model=current_model,
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
                        is_newly_registered_entity=is_newly_registered_entity,
                        config_subentry_id=subentry.subentry_id,
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
        config_subentry_id: str | None = None,
        command_device_id: str | None = None,
        status_device_id: str | None = None,
        status_enum: str | None = None,
        status_identity_source: str | None = None,
        secondary_status_identities: object = None,
        invert_direction: bool = False,
        is_newly_registered_entity: bool = False,
    ) -> None:
        """Initialize the cover entity from its config subentry data.

        is_newly_registered_entity is True only the first time this blind's
        stable unique ID is set up (see async_setup_entry); it defaults to
        False so a SchellenbergCover built directly (as tests do) gets the
        safe "trust restored state" behavior unless a test opts in.
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
        self._device_enum = self._command_enum
        self._config_entry_id = config_entry_id
        self._config_subentry_id = config_subentry_id
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
        # This blind's own persisted CONF_INITIAL_POSITION startup
        # fallback, set by pairing, calibration, manual entry, or a
        # Developer Tools position sync. None only for a blind paired
        # before this field existed - see the "nothing to restore" branch
        # of async_added_to_hass() below for what happens then.
        self._initial_position_from_calibration: int | None = device_data_dict.get(
            CONF_INITIAL_POSITION
        )
        # This blind's own last-confirmed-position backup (see that
        # constant's own comment in const.py) - consulted in
        # async_added_to_hass() only when Home Assistant's own restore_state
        # has nothing for this entity, before falling all the way back to
        # the calibration anchor above. Kept current by _record_position_
        # update()'s own durable=True call sites; never by a Developer
        # Tools manual sync.
        self._last_confirmed_position_from_config: int | None = device_data_dict.get(
            CONF_LAST_CONFIRMED_POSITION
        )
        self._is_newly_registered_entity = is_newly_registered_entity
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._position_update_task: asyncio.Task[None] | None = None
        self._target_position: int | None = None
        # True for the whole async_open_cover()/async_close_cover() body,
        # including any suspension inside _async_cancel_position_tracking()
        # or control_blind_with_retry(). _handle_event() checks this
        # alongside _has_live_position_tracking() because tracking itself
        # only starts once control_blind_with_retry() actually succeeds, so
        # a set_cover_position() leg's own live target can otherwise look
        # like stale leftover state while its write is still queued.
        self._command_in_flight = False
        self._position_update_source = "not recorded"
        self._position_source_kind = "startup default"
        self._position_confirmed_since_restart = False
        self._full_travel_resync_direction: str | None = None
        # monotonic() timestamp of the first stop attempt for the
        # *current* auto-stop-at-target episode, or None when none is in
        # flight. Reset to None on a CONFIRMED outcome; left untouched (and
        # retried next tick) on FAILED/UNCONFIRMED, up to
        # AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS - see that constant's own
        # comment in const.py. Also cleared whenever tracking itself is torn
        # down, and re-stamped fresh whenever self._pending_auto_stop_target
        # no longer matches self._target_position, so a timestamp from one
        # episode can never be silently inherited by a later, unrelated one.
        self._pending_auto_stop_since: float | None = None
        # The target self._pending_auto_stop_since above was stamped for.
        # None exactly when self._pending_auto_stop_since is.
        self._pending_auto_stop_target: int | None = None

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
            # ATTR_CURRENT_POSITION is the reported state attribute;
            # ATTR_POSITION is only the set_cover_position service field and is
            # never present on a restored state.
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

        # Home Assistant's own restore_state had nothing for this entity -
        # fall back to this blind's own, independently-persisted backup
        # (see CONF_LAST_CONFIRMED_POSITION's own comment in const.py)
        # before falling all the way back to the calibration anchor below.
        # Exists because restore_state itself is not always reliable (a
        # known Home Assistant Core regression on some versions; see
        # CHANGELOG.md's v1.6.0 entry) - this backup does not depend on it.
        restored_from_own_backup = False
        if (
            not restored_from_ha
            and self._last_confirmed_position_from_config is not None
        ):
            self._attr_current_cover_position = (
                self._last_confirmed_position_from_config
            )
            self._attr_is_closed = self._attr_current_cover_position == 0
            restored_from_own_backup = True
            _LOGGER.debug(
                "No previous Home Assistant state for %s (%s); using this "
                "integration's own last confirmed position instead: %d%%",
                self._device_name,
                self._device_id,
                self._attr_current_cover_position,
            )

        # A calibration that just finished in this same "Pair and
        # calibrate" run is more trustworthy than any restored state,
        # including a seemingly-valid one (a reused blind_id could restore
        # a leftover position from an earlier, unrelated pairing episode).
        # CONF_INITIAL_POSITION (persisted by that same calibration step)
        # carries this across the hub reload that adding a subentry
        # triggers, but only counts as this override signal for a newly
        # registered blind - every other, already-established blind has its
        # own, possibly years-old CONF_INITIAL_POSITION that must not
        # override its real current restored position.
        fresh_calibration = False
        fresh_position = self._api.pop_fresh_calibration_position(self._blind_id)
        if fresh_position is None and self._is_newly_registered_entity:
            fresh_position = self._initial_position_from_calibration

        if fresh_position is not None:
            if restored_from_ha or restored_from_own_backup:
                _LOGGER.debug(
                    "Restored position for %s (%s) was %d%%, but this blind "
                    "was just calibrated in this same pairing - trusting "
                    "the freshly measured %d%% instead",
                    self._device_name,
                    self._device_id,
                    self._attr_current_cover_position,
                    fresh_position,
                )
            self._attr_current_cover_position = fresh_position
            self._attr_is_closed = fresh_position == 0
            restored_from_ha = False
            restored_from_own_backup = False
            fresh_calibration = True

        if self._attr_current_cover_position is None:
            if self._initial_position_from_calibration is not None:
                self._attr_current_cover_position = (
                    self._initial_position_from_calibration
                )
                self._attr_is_closed = self._attr_current_cover_position == 0
                _LOGGER.debug(
                    "No previous state for %s (%s); using the position "
                    "calibration actually ended at: %d%%",
                    self._device_name,
                    self._device_id,
                    self._attr_current_cover_position,
                )
            else:
                # Nothing to restore and not a freshly (re)calibrated blind
                # either: leave position/is_closed as the unknown None from
                # __init__ rather than asserting a specific but unverified 0%
                # (closed). Not a regression once the blind actually moves - the
                # same 0% anchor is still used then, just by async_open_cover()/
                # async_close_cover()/_handle_event() at the moment a move starts,
                # when a numeric baseline is actually needed.
                _LOGGER.debug(
                    "No previous state for %s (%s); leaving position "
                    "unknown until the next confirmed move",
                    self._device_name,
                    self._device_id,
                )

        if fresh_calibration:
            self._position_source_kind = "calibration"
            self._position_update_source = "fresh calibration result"
            startup_status = "measured during pairing / not confirmed since restart"
        elif restored_from_ha:
            self._position_source_kind = "restored HA state"
            self._position_update_source = "restored HA state"
            startup_status = "restored / estimated / not confirmed since restart"
        elif restored_from_own_backup:
            self._position_source_kind = "restored own backup"
            self._position_update_source = (
                "restored own backup (no Home Assistant restore_state)"
            )
            startup_status = "restored from own backup / not confirmed since restart"
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
                    f"{SIGNAL_DEVICE_EVENT}_{self._status_device_id}_"
                    f"{self._status_enum}",
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
        durable: bool = False,
    ) -> None:
        """Forward a position change to the API's diagnostic history log.

        Read back via api.get_last_position_update() by the Developer Tools
        page (config_flow.py) and diagnostics.py - a view separate from, and
        not automatically kept in sync with, the live
        self._attr_current_cover_position reported to Home Assistant
        itself. Every code path that changes the reported position calls
        this before returning; see each call site's own comment.

        durable=True additionally persists new_position as this blind's own
        CONF_LAST_CONFIRMED_POSITION backup (see that constant's own
        comment in const.py) - reserved for a genuinely RF/calibration-
        confirmed completion, never a plain elapsed-time estimate or a
        Developer Tools manual sync; see each call site's own comment.
        """
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
        if durable and new_position is not None:
            self._persist_last_confirmed_position(new_position)

    def _persist_last_confirmed_position(self, position: int) -> None:
        """Durably back up a genuinely confirmed position to this subentry.

        Called only from _record_position_update()'s own durable=True call
        sites. A no-op when the value has not actually changed (the common
        case - called on every confirmed stop) or when this entity was
        built without a config_entry_id/config_subentry_id, same as
        _async_position_update_loop()'s own background-task setup guards
        the missing config_entry_id case above.
        """
        if position == self._last_confirmed_position_from_config:
            return
        if self._config_entry_id is None or self._config_subentry_id is None:
            return
        config_entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        if config_entry is None:
            return
        subentry = config_entry.subentries.get(self._config_subentry_id)
        if subentry is None:
            return
        self._last_confirmed_position_from_config = position
        data = dict(subentry.data)
        data[CONF_LAST_CONFIRMED_POSITION] = position
        self.hass.config_entries.async_update_subentry(
            config_entry, subentry, data=data
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
            # Deliberately not durable - see CONF_LAST_CONFIRMED_POSITION's
            # own comment in const.py.
            durable=False,
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
        self,
        device_id: str,
        open_time: float,
        close_time: float,
        ending_position: int,
    ) -> None:
        """Apply newly measured travel times and calibration's end position.

        ending_position is whichever end stop the last-driven leg of the
        calibration (open or close - the person's own choice, see
        options_flow_calibration.py's calibration_initial_state step) reached.
        """
        if device_id != self._device_id:
            return

        previous_position = self._attr_current_cover_position
        self._travel_time_open = open_time
        self._travel_time_close = close_time

        self._attr_current_cover_position = ending_position
        self._attr_is_closed = ending_position == 0
        self._position_update_source = "completed calibration"
        self._position_source_kind = "calibration"
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._record_position_update(
            source=self._position_update_source,
            direction="stop",
            previous_position=previous_position,
            new_position=ending_position,
            status="confirmed",
            durable=True,
        )

        _LOGGER.info(
            "Device %s calibration updated: open_time=%.2fs, close_time=%.2fs. "
            "Cover position set to %d%% (%s)",
            self._device_name,
            open_time,
            close_time,
            ending_position,
            "closed" if ending_position == 0 else "open",
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
        # Ends whatever auto-stop-at-target episode the task being torn
        # down here was mid-retry on, unconditionally and before any early
        # return, so every caller gets this for free.
        self._pending_auto_stop_since = None
        self._pending_auto_stop_target = None
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
        # Ends this device's own in-flight move/retry loop immediately on
        # removal (the same mechanism async_stop_cover() uses), rather than
        # leaving it transmitting RF for a blind Home Assistant no longer
        # has an entity for. A no-op if nothing is in flight.
        self._api.cancel_pending_move(self._command_device_id)
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
            genuinely_idle = (
                not self._has_live_position_tracking() and not self._command_in_flight
            )
            if genuinely_idle:
                # Neither a tracking task nor a command of ours is in flight, so
                # this can only be reporting a move started externally (remote,
                # wall switch, Gurtwickler winder) rather than confirming one of
                # ours. Checked alongside self._command_in_flight, not
                # _has_live_position_tracking() alone, because a set_cover_position()
                # leg's own tracking does not start synchronously (see
                # self._command_in_flight's own comment in __init__) - using only
                # the tracking check would null out a genuinely in-flight target.
                # Once both are false, any leftover self._target_position is stale
                # and would otherwise silently cap this externally-started move at
                # an old percentage instead of the real full-travel endstop, exactly
                # like async_open_cover()/async_close_cover() already guard against
                # for a Home-Assistant-issued command starting the same way.
                self._target_position = None
            if self._attr_current_cover_position is None:
                # Mirrors async_open_cover()'s/async_close_cover()'s own guard for
                # a command starting from this same unknown state: a move is
                # actually starting now, so _update_position() needs a concrete
                # numeric baseline, unlike while idle where "unknown" can stand.
                self._attr_current_cover_position = 0
            # Explicitly int | None, matching the attribute's own declared
            # type - the EVENT_STOPPED branch below assigns this same local
            # without the guard above, so a narrower inferred type here would
            # wrongly reject that.
            previous_position: int | None = self._attr_current_cover_position
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
            # First, before any other statement - same reasoning as
            # async_stop_cover()'s own identical first line. Ends this device's
            # own retry loop (if any) the instant a STOPPED echo lands, from
            # whatever source (HA-issued stop, auto-stop, or a physical
            # remote/Gurtwickler press that never goes through this
            # integration's own command path), so a move's best-effort resend
            # cannot restart the motor right after a legitimate stop. A no-op
            # when nothing is in flight for this device_id.
            self._api.cancel_pending_move(self._command_device_id)
            previous_position = self._attr_current_cover_position
            self._position_source_kind = "primary status"
            self._position_confirmed_since_restart = True
            self._full_travel_resync_direction = None
            self._position_update_source = (
                f"primary status {self._status_device_id}/{self._status_enum} "
                f"command {event}"
            )
            self._stop_position_tracking()
            # Always re-estimate from elapsed time rather than snapping to
            # _target_position: by the time a self-requested stop's STOPPED echo
            # arrives, the target has already been reached and cleared, so a
            # STOPPED event that still finds a target set means the blind was
            # interrupted externally and the commanded target was never reached.
            self._update_position()
            # Tracks whether the branch below actually snapped to an exact
            # endstop (0/100), as opposed to leaving _update_position()'s raw
            # elapsed-time estimate in place - see its use on
            # _record_position_update()'s durable= argument below.
            position_snapped_to_endstop = False
            if self._attr_current_cover_position is not None:
                self._attr_current_cover_position = max(
                    0, min(100, self._attr_current_cover_position)
                )
                if (
                    self._target_position is None
                    and self._move_start_position is not None
                    and (
                        (
                            self._attr_is_closing
                            and self._attr_current_cover_position
                            <= NEAR_END_STOP_SNAP_TOLERANCE_PERCENT
                            and self._move_start_position
                            >= 100 - NEAR_END_STOP_SNAP_TOLERANCE_PERCENT
                        )
                        or (
                            self._attr_is_opening
                            and self._attr_current_cover_position
                            >= 100 - NEAR_END_STOP_SNAP_TOLERANCE_PERCENT
                            and self._move_start_position
                            <= NEAR_END_STOP_SNAP_TOLERANCE_PERCENT
                        )
                    )
                ):
                    # An untargeted move (remote/wall-switch/Gurtwickler) landing near
                    # an edge only snaps to it as calibration drift when it also started
                    # near the opposite edge - a plausible full-range traversal, not a
                    # deliberately chosen near-edge stop (see
                    # NEAR_END_STOP_SNAP_TOLERANCE_PERCENT's own comment in const.py).
                    # A move that did not start near the opposite edge keeps its raw
                    # elapsed-time estimate instead, like any other mid-travel stop.
                    self._attr_current_cover_position = (
                        0 if self._attr_is_closing else 100
                    )
                    position_snapped_to_endstop = True
                self._attr_is_closed = self._attr_current_cover_position == 0
            self._attr_is_opening = False
            self._attr_is_closing = False
            # The log field is purely cosmetic (every real field - position
            # itself, and _record_position_update()'s new_position - was always
            # correct); it now reads the already-recalculated position instead
            # of a stale polling-tick snapshot.
            # self._attr_current_cover_position can legitimately be None here -
            # pre-calibration, or a late STOPPED confirming a device whose
            # auto-stop-at-target episode already gave up and reported unknown
            # (see AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS above). Guarded so
            # "%d%%" on None logs cleanly instead of a formatting error.
            _LOGGER.info(
                "Device %s STOPPED (position: %s)",
                self._device_name,
                "unknown"
                if self._attr_current_cover_position is None
                else f"{self._attr_current_cover_position}%",
            )
            self._record_position_update(
                source=self._position_update_source,
                direction="stop",
                previous_position=previous_position,
                new_position=self._attr_current_cover_position,
                status="confirmed",
                # Only durable when the branch above actually snapped to an
                # exact endstop - the far more common case (a mid-travel
                # remote/wall-switch/Gurtwickler stop) leaves
                # self._attr_current_cover_position as _update_position()'s raw
                # elapsed-time estimate, which must never become this blind's
                # CONF_LAST_CONFIRMED_POSITION backup (see that constant's own
                # comment in const.py) - caught in review before this shipped.
                durable=position_snapped_to_endstop,
            )
            self._move_start_time = None
            self._move_start_position = None
            if not self._has_live_position_tracking() and not self._command_in_flight:
                # Gap in the "usually external interruption" reasoning above: a
                # set_cover_position() leg that reverses direction while a
                # different, superseded move is still live awaits
                # _async_cancel_position_tracking() - a real suspension point -
                # after already setting its own new self._target_position, so
                # _has_live_position_tracking() alone cannot tell this window apart
                # from genuine idle. If the superseded move's own STOPPED echo lands
                # in that window, it is only confirming the move being superseded,
                # not interrupting the new leg, so it must not null out the new
                # leg's own target. self._command_in_flight (set for the whole
                # async_open_cover()/async_close_cover() duration) tells the two
                # apart, the same as it does for STARTED_MOVING.
                self._target_position = None
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._device_name, event
            )

        self.async_write_ha_state()

    def _has_live_position_tracking(self) -> bool:
        """Return whether the position-tracking task is already live.

        "Live" means actually still running, not merely sitting in the slot
        uncleared. Used by _start_position_tracking() to decide whether a
        (re)start is needed, and by _handle_event() alongside
        self._command_in_flight - not alone - since a Home Assistant
        command's own tracking only starts once its control_blind_with_retry()
        call succeeds, not from the moment it is called, so this can read
        False for a stretch while a set_cover_position() leg's target is
        already genuinely current.
        """
        task = self._position_update_task
        return task is not None and not task.done() and not task.cancelling()

    def _start_position_tracking(self) -> None:
        """Start the background loop that estimates position while moving."""
        existing_task = self._position_update_task
        if self._has_live_position_tracking():
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
        # Same reasoning and fix as _async_cancel_position_tracking() just
        # above, for this method's own fire-and-forget cancellation path.
        self._pending_auto_stop_since = None
        self._pending_auto_stop_target = None
        task = self._position_update_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._position_update_task is task:
            self._position_update_task = None

    def _has_live_tracking_in_direction(self, *, opening: bool) -> bool:
        """Return whether a move already in progress is tracked in this direction.

        True only when the cover already reports moving the same way AND
        the background tracking loop is actually still running with a valid
        baseline. Used by async_open_cover()/async_close_cover() to
        recognise a redundant re-send of an already-in-progress move (e.g.
        a script's own intentional double-send for RF reliability) and skip
        re-freezing/re-basing the position estimate for it, so the running
        estimate stays continuous across the re-send instead of losing
        whatever distance the blind travelled while re-establishing it.
        """
        already_same_direction = (
            self._attr_is_opening if opening else self._attr_is_closing
        )
        task = self._position_update_task
        return bool(
            already_same_direction
            and task is not None
            and not task.done()
            and self._move_start_time is not None
            and self._move_start_position is not None
        )

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
        """Record that reaching a travel limit makes the position trustworthy.

        Called from _async_position_update_loop()'s two plain-open/close
        branches once they reach 0/100, and from its explicit-target branch
        when a set_cover_position(0)/(100) call reaches that same endstop.
        self._position_confirmed_since_restart is set unconditionally: a
        real, elapsed-time-tracked full transit is equally trustworthy
        either way. The resync-specific cleanup below (clearing
        self._full_travel_resync_direction and recording the "estimated
        from full travel" diagnostic) only fires for a genuinely matching,
        actively-pending resync - the set_cover_position(0)/(100) call site
        never arms that attribute in the first place (see
        async_open_cover()'s/async_close_cover()'s own ternary, gated on
        self._target_position is None), so it only ever gets the
        unconditional confirmed-flag behavior above.
        """
        self._position_confirmed_since_restart = True
        if self._full_travel_resync_direction != direction:
            return
        previous_position = self._move_start_position
        self._full_travel_resync_direction = None
        self._record_position_update(
            source=self._position_update_source,
            direction=direction,
            previous_position=previous_position,
            new_position=position,
            status="estimated from full travel",
            # Despite the "estimated" label, a full-travel resync is a real
            # endstop confirmation (elapsed time matched a full open/close),
            # so it counts as confirmed for CONF_LAST_CONFIRMED_POSITION.
            durable=True,
        )

    async def _async_position_update_loop(self) -> None:
        """Poll and estimate position every 0.2s while the cover is moving.

        The auto-stop-at-target branch below sends its own control_blind_
        stop() once the elapsed-time estimate reaches an intermediate
        target, then re-checks self._target_position against the value
        captured just before that await once the write returns - without
        that re-check, a same-direction retarget landing during the await
        would have its brand new target wiped out by whichever stale
        cleanup runs next.
        """
        position_task = asyncio.current_task()
        # Captured once, here, rather than re-based on any later retarget
        # - see POSITION_UPDATE_LOOP_TIMEOUT_SECONDS's own comment in
        # const.py for why this loop's total running time is what gets
        # bounded, not the time since whichever target is current right now.
        loop_start_time = time.monotonic()
        try:
            ha_update_counter = 0
            while True:
                await asyncio.sleep(0.2)

                self._update_position()

                if (
                    time.monotonic() - loop_start_time
                    >= POSITION_UPDATE_LOOP_TIMEOUT_SECONDS
                ):
                    # Outer safety net - see POSITION_UPDATE_LOOP_TIMEOUT_SECONDS's own
                    # comment in const.py. Mirrors the AUTO_STOP_CONFIRMATION_BACKSTOP_
                    # SECONDS-exhausted cleanup further down, plus clearing
                    # self._full_travel_resync_direction too, since this one can fire
                    # from any state this loop was in.
                    _LOGGER.error(
                        "Device %s: position tracking still running after "
                        "%.0fs with no target ever reached - reporting "
                        "position as unknown rather than keep polling "
                        "indefinitely",
                        self._device_name,
                        POSITION_UPDATE_LOOP_TIMEOUT_SECONDS,
                    )
                    previous_position = self._attr_current_cover_position
                    self._attr_current_cover_position = None
                    self._attr_is_closed = None
                    self._attr_is_opening = False
                    self._attr_is_closing = False
                    self._move_start_time = None
                    self._move_start_position = None
                    self._target_position = None
                    self._pending_auto_stop_since = None
                    self._pending_auto_stop_target = None
                    self._full_travel_resync_direction = None
                    self._position_source_kind = (
                        "unconfirmed - position update loop timed out"
                    )
                    self._record_position_update(
                        source=self._position_update_source,
                        direction="stop",
                        previous_position=previous_position,
                        new_position=None,
                        status="unconfirmed - position update loop timed out",
                    )
                    self.async_write_ha_state()
                    return

                ha_update_counter += 1

                if self._target_position is not None:
                    # An intermediate target (unlike 0/100, see needs_stop_command
                    # below) is only reached because a stop command sent from here makes
                    # it so, and two real delays sit between "software decides to stop"
                    # and "motor actually stops at target": D_start (the motor doesn't
                    # start the instant the stick accepts the move's own write) and
                    # D_stop (the motor doesn't stop the instant the stop write is
                    # issued) - roughly STOP_COMMAND_LATENCY_SECONDS each (see its own
                    # comment in const.py). Worked through the actual arithmetic, these
                    # enter the needed lead with opposite sign: when uncontended
                    # (D_start == D_stop) they cancel completely, leaving only
                    # T_stamp - _move_start_time's own now-near-zero stamping delay - to
                    # compensate for, which is itself ~0. An uncontended intermediate
                    # stop therefore needs no lead at all.
                    needs_stop_command = self._target_position not in (0, 100)
                    stop_lead_percent = 0.0
                    if needs_stop_command:
                        travel_time_for_lead = max(
                            self._travel_time_open
                            if self._attr_is_opening
                            else self._travel_time_close,
                            0.1,
                        )
                        # Baseline is T_stamp alone (see the derivation above), now ~0:
                        # STOP_COMMAND_LATENCY_SECONDS alone cancels out of this
                        # baseline exactly when uncontended, since both its appearances
                        # are equal.
                        stop_latency_seconds = 0.0
                        # When the shared lock is held by another blind's own move or
                        # stop right now, this stop still has to wait out whatever that
                        # cycle has left - up to one more STOP_COMMAND_LATENCY_SECONDS
                        # added to D_stop alone, so the uncontended cancellation above
                        # no longer holds and this adds it back. Checked fresh every
                        # tick, so this only widens the lead on ticks where contention
                        # is actually observed.
                        if self._api.move_lock_busy:
                            stop_latency_seconds += STOP_COMMAND_LATENCY_SECONDS
                        # travel_time_for_lead's own floor above only keeps this
                        # division from raising; it has no matching ceiling. See
                        # MAX_STOP_LEAD_PERCENT's own comment in const.py for why an
                        # unbounded lead is not just a cosmetically large number: past
                        # 100 it makes position_reached satisfied immediately regardless
                        # of actual position.
                        stop_lead_percent = min(
                            (stop_latency_seconds / travel_time_for_lead) * 100,
                            MAX_STOP_LEAD_PERCENT,
                        )
                    position_reached = (
                        self._attr_is_opening
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position
                        >= self._target_position - stop_lead_percent
                    ) or (
                        self._attr_is_closing
                        and self._attr_current_cover_position is not None
                        and self._attr_current_cover_position
                        <= self._target_position + stop_lead_percent
                    )
                    if position_reached:
                        previous_position_at_target = self._attr_current_cover_position
                        # Default: no resync was pending. Only the
                        # needs_stop_command=False sub-branch below can flip this True,
                        # right before calling _confirm_full_travel_resync() - see its
                        # own comment there.
                        resync_was_pending = False
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._device_name,
                            self._target_position,
                        )
                        if needs_stop_command:
                            # Snapshot the target this stop is actually for, taken
                            # before the await below - see the re-check just after that
                            # same await for why.
                            if (
                                self._pending_auto_stop_since is None
                                or self._pending_auto_stop_target
                                != self._target_position
                            ):
                                # Either the first attempt for this episode, or a stale
                                # timestamp left over from a different target's episode
                                # that ended without going through the usual clearing
                                # paths. Either way, start this episode's own clock now
                                # rather than inheriting another episode's already-spent
                                # budget.
                                self._pending_auto_stop_since = time.monotonic()
                                self._pending_auto_stop_target = self._target_position
                            target_when_stop_was_sent = self._target_position
                            # Functionally a stop, same as async_stop_cover()'s own
                            # manual one - cancel_pending_move() comes first because the
                            # move ending here may still have control_blind_with_retry()
                            # retrying it.
                            self._api.cancel_pending_move(self._command_device_id)
                            # control_blind_stop() waits for this move's own open/close
                            # write to finish transmitting and is briefly serialized
                            # against other writes too - closes the same gap
                            # async_stop_cover() closes for a manual stop, for this
                            # automatic one. Adds no delay in the normal case (nothing
                            # left in flight by the time this is reached).
                            stop_outcome = await self._api.control_blind_stop(
                                self._command_enum,
                                device_id=self._command_device_id,
                            )
                            if stop_outcome is not StopOutcome.CONFIRMED:
                                # Neither FAILED nor UNCONFIRMED is safe to treat as a
                                # successful stop: the motor may still be moving and
                                # this loop's own ability to act once a real stop lands
                                # would already be torn down. Leave state untouched and
                                # retry next tick, up to
                                # AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS total (see
                                # that constant's own comment in const.py).
                                if (
                                    time.monotonic() - self._pending_auto_stop_since
                                    >= AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS
                                ):
                                    # Re-check rather than trust the pre-await snapshot:
                                    # a same-direction retarget landing during this
                                    # attempt's own await already pointed
                                    # self._target_position at its new value, and
                                    # falling through to the unconditional clear below
                                    # would wipe that still-live target out from under
                                    # it.
                                    if (
                                        self._target_position
                                        != target_when_stop_was_sent
                                    ):
                                        self._pending_auto_stop_since = None
                                        self._pending_auto_stop_target = None
                                        _LOGGER.debug(
                                            "Device %s: auto-stop backstop "
                                            "exhausted for target (%d%%), "
                                            "but a newer retarget already "
                                            "claimed this device in the "
                                            "meantime - leaving its state "
                                            "alone instead of overwriting "
                                            "it with this now-stale "
                                            "target's own backstop "
                                            "cleanup",
                                            self._device_name,
                                            target_when_stop_was_sent,
                                        )
                                        continue
                                    _LOGGER.error(
                                        "Device %s: auto-stop at target "
                                        "position (%d%%) still unconfirmed "
                                        "after %.0fs - reporting position "
                                        "as unknown rather than keep "
                                        "trusting a stale estimate",
                                        self._device_name,
                                        self._target_position,
                                        AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS,
                                    )
                                    previous_position = (
                                        self._attr_current_cover_position
                                    )
                                    self._attr_current_cover_position = None
                                    self._attr_is_closed = None
                                    self._attr_is_opening = False
                                    self._attr_is_closing = False
                                    self._move_start_time = None
                                    self._move_start_position = None
                                    self._target_position = None
                                    self._pending_auto_stop_since = None
                                    self._pending_auto_stop_target = None
                                    self._position_source_kind = (
                                        "unconfirmed - backstop exhausted"
                                    )
                                    self._record_position_update(
                                        source=self._position_update_source,
                                        direction="stop",
                                        previous_position=previous_position,
                                        new_position=None,
                                        status="unconfirmed - backstop exhausted",
                                    )
                                    self.async_write_ha_state()
                                    return
                                _LOGGER.warning(
                                    "Device %s: auto-stop at target "
                                    "position (%d%%) %s - retrying",
                                    self._device_name,
                                    self._target_position,
                                    "failed to send"
                                    if stop_outcome is StopOutcome.FAILED
                                    else "sent but not yet confirmed",
                                )
                                continue
                            self._pending_auto_stop_since = None
                            self._pending_auto_stop_target = None
                            if self._target_position != target_when_stop_was_sent:
                                # A same-direction retarget landed while the await above
                                # was in flight and already pointed
                                # self._target_position at its own new value
                                # (is_redundant_resend's own piggyback path leaves this
                                # same task running to keep tracking it). Falling
                                # through to the unconditional clear below would wipe
                                # that still-live target out from under it and end this
                                # task with nothing left tracking or auto-stopping the
                                # new leg. The stop just sent above is harmless either
                                # way - the motor only ever sees a bare pulse, never a
                                # position - so simply continue instead.
                                continue
                        else:
                            # A plain open_cover()/close_cover() reaching 0/100 confirms
                            # this via _confirm_full_travel_resync() (see the
                            # target_position-is-None branch below); a
                            # set_cover_position(0)/(100) call reaching the same endstop
                            # through this branch needs the identical call since
                            # self._full_travel_resync_direction is never armed for this
                            # branch in the first place (see
                            # async_open_cover()'s/async_close_cover()'s own ternary,
                            # gated on self._target_position is None).
                            #
                            # resync_was_pending is captured before this call, which
                            # clears self._full_travel_resync_direction the instant it
                            # matches, so the record_position_update() call in this
                            # branch's own common tail below can tell whether
                            # _confirm_full_travel_resync() already recorded this
                            # transition itself and skip a duplicate record.
                            resync_was_pending = self._full_travel_resync_direction == (
                                "closing" if self._attr_is_closing else "opening"
                            )
                            self._confirm_full_travel_resync(
                                "closing" if self._attr_is_closing else "opening",
                                self._target_position,
                            )
                        # Reaching a target here is a stop in every sense that matters
                        # to the entity's own reported state, whether a real stop
                        # command went out above or the motor's own end-stop did the job
                        # via the else branch - must clear is_opening/is_closing exactly
                        # like async_stop_cover() and the plain-open/close branch below
                        # do.
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        self._target_position = None
                        if not resync_was_pending:
                            # Refreshes the diagnostics/"Entwicklerwerkzeuge" page's own
                            # last-position-update record
                            # (api.get_last_position_update()), separate from the live
                            # position reported above, for a set_cover_position() call
                            # reaching its intermediate target. Skipped on a
                            # freshly-armed resync (see resync_was_pending above):
                            # _confirm_full_travel_resync() already recorded that same
                            # transition itself, just above.
                            self._record_position_update(
                                source=self._position_update_source,
                                direction="stop",
                                previous_position=previous_position_at_target,
                                new_position=self._attr_current_cover_position,
                                status="confirmed",
                                durable=True,
                            )
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
                        previous_position_at_endstop = self._attr_current_cover_position
                        self._attr_current_cover_position = 0
                        # Captured before the call below, which clears
                        # self._full_travel_resync_direction the instant it matches
                        # "closing" - lets the record_position_update() call further
                        # down know whether _confirm_full_travel_resync() already
                        # recorded this transition itself, to avoid a duplicate record.
                        resync_was_pending = (
                            self._full_travel_resync_direction == "closing"
                        )
                        self._confirm_full_travel_resync("closing", 0)
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        if not resync_was_pending:
                            # Same gap as the intermediate-target branch above, reached
                            # via the plain full-travel-to-endstop path instead. Skipped
                            # on a freshly-armed resync: _confirm_full_travel_resync()
                            # already recorded that same transition itself, just above.
                            self._record_position_update(
                                source=self._position_update_source,
                                direction="closing",
                                previous_position=previous_position_at_endstop,
                                new_position=self._attr_current_cover_position,
                                status="confirmed",
                                durable=True,
                            )
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
                        previous_position_at_endstop = self._attr_current_cover_position
                        self._attr_current_cover_position = 100
                        # Captured before the call below, which clears
                        # self._full_travel_resync_direction the instant it matches
                        # "opening" - lets the record_position_update() call further
                        # down know whether _confirm_full_travel_resync() already
                        # recorded this transition itself, to avoid a duplicate record.
                        resync_was_pending = (
                            self._full_travel_resync_direction == "opening"
                        )
                        self._confirm_full_travel_resync("opening", 100)
                        self._attr_is_opening = False
                        self._attr_is_closing = False
                        self._move_start_time = None
                        self._move_start_position = None
                        if not resync_was_pending:
                            # Same gap as the intermediate-target branch above, reached
                            # via the plain full-travel-to-endstop path instead. Skipped
                            # on a freshly-armed resync: _confirm_full_travel_resync()
                            # already recorded that same transition itself, just above.
                            self._record_position_update(
                                source=self._position_update_source,
                                direction="opening",
                                previous_position=previous_position_at_endstop,
                                new_position=self._attr_current_cover_position,
                                status="confirmed",
                                durable=True,
                            )
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

        # Never negative: nothing in production pushes self._move_start_time
        # into the future (see STOP_COMMAND_LATENCY_SECONDS's own comment in
        # const.py for where that compensation lives instead). Stays as a
        # defensive guard against clock weirdness regardless.
        elapsed_time = max(0.0, time.monotonic() - self._move_start_time)

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
            # An intermediate set_cover_position() target has no real
            # endstop to confirm against - just this estimate and the
            # loop's own explicit stop command. Capping here keeps the
            # estimate (and the "estimated" record it triggers below)
            # from counting past the target while that stop is still
            # being confirmed/resent.
            if self._target_position is not None:
                new_pos = min(new_pos, self._target_position)
        elif self._attr_is_closing:
            new_pos = self._move_start_position - total_position_change
            # Mirrors the opening branch's own cap above.
            if self._target_position is not None:
                new_pos = max(new_pos, self._target_position)
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
        self._command_in_flight = True
        try:
            await self._async_open_cover_body(_preserve_target=_preserve_target)
        finally:
            self._command_in_flight = False

    async def _async_open_cover_body(self, *, _preserve_target: bool = False) -> None:
        """The actual work of async_open_cover(), wrapped by its own
        self._command_in_flight bookkeeping - see that method and
        self._command_in_flight's own comment in __init__ for why this
        needs to cover the method's *entire* body, every exit path
        included, not just its first await.
        """
        if not _preserve_target:
            # A plain open request always means "go all the way to 100" -
            # clear any stale target left over from an earlier partial move.
            self._target_position = None
        action = resolve_direction_command("open", self._invert_direction)
        _LOGGER.debug(
            "Opening cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        # A redundant re-send of an already-in-progress open must still
        # reach the stick again below, but must not re-freeze/re-base
        # position tracking - the blind never stopped moving between the two
        # sends. See _has_live_tracking_in_direction()'s own docstring.
        is_redundant_resend = self._has_live_tracking_in_direction(opening=True)
        self._attr_is_opening = True
        self._attr_is_closing = False
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
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
        if not is_redundant_resend:
            await self._async_cancel_position_tracking("new open command")
        self.async_write_ha_state()
        # Waits for any other transmission in flight, then retries until
        # the stick actually accepts the write rather than attempting once -
        # control_blind_with_retry() (api.py) serializes this hub-wide so
        # several blinds commanded together cannot all write in the same
        # window and have the stick silently drop all but one. Not applied
        # to async_stop_cover() - see its own docstring for why a manual
        # stop must stay instant and unretried instead.
        #
        # _move_start_time/tracking are deliberately not (re)started above
        # either for a genuinely new open - only once this call below
        # actually succeeds.
        if not await self._api.control_blind_with_retry(
            self._command_enum, action, device_id=self._command_device_id
        ):
            # Re-check rather than trust the pre-await snapshot: a real status
            # frame can land while this write was in flight and already
            # stop-and-clear the original move's own tracking.
            if is_redundant_resend and self._has_live_tracking_in_direction(
                opening=True
            ):
                # The original move is still genuinely under way and its own
                # tracking is untouched - only this resend's own extra transmission
                # didn't get through. Leave the entity and the running move alone.
                _LOGGER.debug(
                    "Redundant open resend for device %s did not reach "
                    "the stick; the original move is unaffected",
                    self._device_name,
                )
                self.async_write_ha_state()
                return
            # The command never reached the stick - undo the optimistic
            # "opening" state so the entity doesn't report a movement that never
            # happened. Also drop any target this call was asked to preserve
            # (_preserve_target=True): with no move under way there is nothing
            # left for that target to describe, and leaving it stale let a
            # later, unrelated move read it back and get auto-stopped at a
            # long-invalid position.
            #
            # Gated on _preserve_target specifically, not unconditional: a plain
            # open never owns a target concept, but a different, still-live
            # set_cover_position() leg's own target can have landed in
            # self._target_position during this call's own await, and clearing
            # it unconditionally would clobber that other leg's legitimate
            # target for no reason of this call's own.
            self._attr_is_opening = False
            self._move_start_time = None
            self._move_start_position = None
            if _preserve_target:
                self._target_position = None
            await self._async_cancel_position_tracking("open command failed")
            self.async_write_ha_state()
            reason = _translate_block_reason(
                self.hass, self._api.transmit_block_reason
            ) or _runtime_translation_text(self.hass, "command_not_sent")
            raise HomeAssistantError(
                _runtime_translation_text(
                    self.hass,
                    "cover_open_failed",
                    name=self._device_name,
                    reason=reason,
                )
            )

        # Only now that the stick has accepted the command do we know the
        # motor is genuinely about to start turning - starting the clock and
        # tracking loop earlier meant a set_cover_position() leg queued
        # behind other blinds could have its target evaluated against time
        # spent only waiting, not moving. Skipped for a redundant resend,
        # whose original tracking already reflects the wait.
        # Re-checked here too: if the original move was stopped-and-cleared
        # for real while this write was in flight, (re)start tracking as a
        # fresh move would rather than leave nothing tracking it.
        if not is_redundant_resend or not self._has_live_tracking_in_direction(
            opening=True
        ):
            self._attr_is_opening = True
            self._attr_is_closing = False
            self._move_start_position = self._attr_current_cover_position
            # Plain time.monotonic(), not pushed into the future - see
            # stop_lead_percent's own comment in _async_position_update_loop()
            # below for where that compensation lives instead and why: pushing
            # it here fixed an intermediate-target overshoot but froze the
            # reported position for just over a second at the start of every
            # move. Compensating on the stop side instead costs accuracy only on
            # an intermediate stop's own trigger instant, already compensated
            # for on purpose, rather than responsiveness on every move.
            self._move_start_time = time.monotonic()
            self._start_position_tracking()
        self.async_write_ha_state()

    async def async_close_cover(
        self, *, _preserve_target: bool = False, **kwargs: Any
    ) -> None:
        """Close the cover, or drive it downward as a leg of set_cover_position."""
        self._command_in_flight = True
        try:
            await self._async_close_cover_body(_preserve_target=_preserve_target)
        finally:
            self._command_in_flight = False

    async def _async_close_cover_body(self, *, _preserve_target: bool = False) -> None:
        """The actual work of async_close_cover() - see async_open_cover()'s
        own mirror of this split for why it exists.
        """
        if not _preserve_target:
            # Same reasoning as async_open_cover: a plain close always targets
            # 0, so drop any stale partial-move target first.
            self._target_position = None
        action = resolve_direction_command("close", self._invert_direction)
        _LOGGER.debug(
            "Closing cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        # A redundant re-send of an already-in-progress close must still
        # reach the stick again below, but must not re-freeze/re-base
        # position tracking - the blind never stopped moving between the two
        # sends. See _has_live_tracking_in_direction()'s own docstring.
        is_redundant_resend = self._has_live_tracking_in_direction(opening=False)
        self._attr_is_opening = False
        self._attr_is_closing = True
        if self._attr_current_cover_position is None:
            self._attr_current_cover_position = 0
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
        if not is_redundant_resend:
            await self._async_cancel_position_tracking("new close command")
        self.async_write_ha_state()
        # Same retry-until-accepted write, same reasoning, as
        # async_open_cover() above. Also the same: tracking is deliberately
        # not (re)started above for a genuinely new close either.
        if not await self._api.control_blind_with_retry(
            self._command_enum, action, device_id=self._command_device_id
        ):
            # Re-check rather than trust the pre-await snapshot: a real status
            # frame can land while this write was in flight and already
            # stop-and-clear the original move's own tracking.
            if is_redundant_resend and self._has_live_tracking_in_direction(
                opening=False
            ):
                # The original move is still genuinely under way and its own
                # tracking is untouched - only this resend's own extra transmission
                # didn't get through. Leave the entity and the running move alone.
                _LOGGER.debug(
                    "Redundant close resend for device %s did not reach "
                    "the stick; the original move is unaffected",
                    self._device_name,
                )
                self.async_write_ha_state()
                return
            # The command never reached the stick - undo the optimistic
            # "closing" state. Also drop any preserved target, gated on
            # _preserve_target - see async_open_cover()'s mirror of this point
            # for the full reasoning.
            self._attr_is_closing = False
            self._move_start_time = None
            self._move_start_position = None
            if _preserve_target:
                self._target_position = None
            await self._async_cancel_position_tracking("close command failed")
            self.async_write_ha_state()
            reason = _translate_block_reason(
                self.hass, self._api.transmit_block_reason
            ) or _runtime_translation_text(self.hass, "command_not_sent")
            raise HomeAssistantError(
                _runtime_translation_text(
                    self.hass,
                    "cover_close_failed",
                    name=self._device_name,
                    reason=reason,
                )
            )

        # Same reasoning as async_open_cover()'s own mirror of this point:
        # only start the clock and tracking loop once the stick has actually
        # accepted the command. Skipped for a redundant resend, whose
        # original tracking already reflects the wait.
        # Re-checked here too: if the original move was stopped-and-cleared
        # for real while this write was in flight, (re)start tracking rather
        # than leave nothing tracking it.
        if not is_redundant_resend or not self._has_live_tracking_in_direction(
            opening=False
        ):
            self._attr_is_opening = False
            self._attr_is_closing = True
            self._move_start_position = self._attr_current_cover_position
            # Plain time.monotonic() - see async_open_cover()'s own mirror of
            # this point for why.
            self._move_start_time = time.monotonic()
            self._start_position_tracking()
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover, freezing the estimated position once it lands.

        Sent via control_blind_stop(), which falls back to a plain
        control_blind() write if its own brief serialization cannot be had
        quickly (see that method's own docstring in api.py).
        cancel_pending_move() runs first, unconditionally, so a stop
        actively ends any retry loop still running for this blind rather
        than merely not starting a new one.

        Position tracking is deliberately left running - and the position
        is not frozen - until control_blind_stop() actually returns: freezing
        up front made the estimate stop advancing while the real write could
        still be queued behind another blind's transmission under
        contention, several points away from where the blind had actually
        gotten to.

        self._target_position is cleared up front regardless: the
        background tracking loop's own auto-stop-at-target branch triggers
        only while a target is set, and must not fire its own, second
        control_blind_stop() for this blind while this one is still in
        flight.

        control_blind_stop() returns a StopOutcome (see its own docstring in
        api.py). FAILED is handled exactly as a bare False always was.
        UNCONFIRMED means a write went out but nothing confirms the stick -
        let alone the motor - ever received it; this method does not retry
        further on it (unlike the auto-stop-at-target loop) since a manual
        stop press must stay responsive rather than wait on a confirmation
        that may never come - only the confidence behind the reported
        estimate changes, reflected in _position_source_kind below.

        was_opening_before_stop/was_closing_before_stop, and the fallback
        branch's own re-check against them further down, exist because
        control_blind_stop() above is a genuine suspension point during
        which a different, newer async_open_cover()/async_close_cover() call
        can start for this same device and claim it before this call
        resumes - the matching re-checks below (for self._target_position,
        for StopOutcome.FAILED's own restore, and in the general fallback)
        all guard against clobbering that newer call's own live state.
        """
        # First, before any other statement: ends this device's own move
        # retry loop now so it cannot resend the open/close command after
        # the stop below has already gone out.
        self._api.cancel_pending_move(self._command_device_id)
        _LOGGER.debug(
            "Stopping cover %s (command_id=%s enum=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
        )
        previous_target_position = self._target_position
        self._target_position = None
        # Snapshot of which direction (if any) this device was already
        # reporting, taken at this same pre-await point. The fallback branch
        # near the end of this method compares against this to recognise
        # when a different, newer async_open_cover()/async_close_cover() call
        # has already taken over this device while this call was suspended.
        was_opening_before_stop = self._attr_is_opening
        was_closing_before_stop = self._attr_is_closing
        stop_outcome = await self._api.control_blind_stop(
            self._command_enum, device_id=self._command_device_id
        )
        if stop_outcome is StopOutcome.FAILED:
            # The stop never reached the stick. Nothing else has been touched
            # yet, so tracking/is_opening/is_closing/_move_start_* need no
            # restore. The target is different: it was actively cleared above,
            # so it is re-checked (not unconditionally restored) against a
            # different, newer call that may have already claimed the target
            # slot for its own live move while this await was in flight.
            if self._target_position is None:
                self._target_position = previous_target_position
            else:
                _LOGGER.debug(
                    "Device %s: stop command failed to send, but a newer "
                    "move already claimed its own target in the meantime "
                    "- leaving it alone instead of restoring this now-"
                    "stale stop's own target",
                    self._device_name,
                )
            reason = _translate_block_reason(
                self.hass, self._api.transmit_block_reason
            ) or _runtime_translation_text(self.hass, "command_not_sent")
            raise HomeAssistantError(
                _runtime_translation_text(
                    self.hass,
                    "cover_stop_failed",
                    name=self._device_name,
                    reason=reason,
                )
            )

        if (
            self._attr_is_opening != was_opening_before_stop
            or self._attr_is_closing != was_closing_before_stop
        ):
            # A different, newer async_open_cover()/async_close_cover() call
            # already started for this same device while this call was
            # suspended inside control_blind_stop() above, and has already set
            # up (or torn down) its own state. Falling through to the cleanup
            # below regardless would cancel tracking and publish a position for
            # whichever move is current now, not the one this call was actually
            # trying to stop - read as the position "jumping" on a quick
            # reversal. Nothing left for this call to do.
            _LOGGER.debug(
                "Device %s: a newer move already started for this device "
                "while this stop command was still in flight - leaving "
                "its state alone instead of overwriting it with this "
                "now-stale stop's own fallback cleanup",
                self._device_name,
            )
            return

        # Only now, with a write actually done, is it safe to stop the
        # background tracking loop and read its estimate.
        previous_position = self._attr_current_cover_position
        confirmed = stop_outcome is StopOutcome.CONFIRMED
        self._position_source_kind = "HA command" if confirmed else "unconfirmed stop"
        self._full_travel_resync_direction = None
        self._position_update_source = "Home Assistant stop command"
        # Deliberately not touched here either way: self._target_position
        # was already cleared to None at the top of this method. If it is
        # not None here, a different, newer async_set_cover_position() call's
        # own write already claimed this slot for its own live target -
        # clearing it here would be actively wrong, since that racing call's
        # own post-await re-check already re-arms tracking for it and needs
        # this same target intact to auto-stop at.
        if self._target_position is not None:
            _LOGGER.debug(
                "Device %s: stop fallback running with a newer target "
                "already claimed (%d%%) - leaving it for that call's own "
                "logic to resolve",
                self._device_name,
                self._target_position,
            )
        await self._async_cancel_position_tracking("Home Assistant stop command")
        self._update_position()
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        if not confirmed:
            # UNCONFIRMED: logged, not retried - staying responsive matters
            # more here than for the background auto-stop-at-target loop. The
            # estimate is reported exactly as it always was; only the confidence
            # behind it changes, reflected in _position_source_kind above.
            _LOGGER.warning(
                "Device %s: stop command sent but not confirmed accepted "
                "by the stick - position now estimated, not confirmed",
                self._device_name,
            )
        self._record_position_update(
            source=self._position_update_source,
            direction="stop",
            previous_position=previous_position,
            new_position=self._attr_current_cover_position,
            status="estimated" if confirmed else "estimated/unconfirmed",
        )
        self.async_write_ha_state()

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
            if self._attr_is_opening or self._attr_is_closing:
                _LOGGER.debug(
                    "Target position equals current position while moving, "
                    "stopping instead of letting it continue past the target"
                )
                await self.async_stop_cover()
            else:
                _LOGGER.debug(
                    "Target position equals current position, no action needed"
                )
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
