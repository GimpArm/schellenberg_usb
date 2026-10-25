"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import functools
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

from .api import SchellenbergUsbApi
from .blind_id import normalize_blind_id
from .const import (
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INITIAL_POSITION,
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
    MAX_STOP_LEAD_PERCENT,
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

    Does nothing for a non-hub config entry (no `CONF_SERIAL_PORT`) or a
    hub with no blind subentries yet. For each saved blind subentry,
    resolves the stable device ID - falling back to the legacy
    device/enum fields for blinds configured before the stable-ID
    migration - and migrates a pre-existing entity registry entry onto
    that stable unique ID if one is found under an older identifier.
    Creates the device registry device on first load, or reassigns it
    if it was somehow left on the wrong subentry, then registers the
    device with the API and adds the `SchellenbergCover` entity itself.
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
                # Matches identities.py's resolve_blind_identities(): an
                # UNKNOWN source means "no confirmed primary status identity
                # yet", regardless of whatever CONF_STATUS_DEVICE_ID/
                # CONF_STATUS_ENUM values are still sitting in the
                # subentry's data from before it became unconfirmed. This
                # used to read those stale fields back out instead of
                # nulling them, so api.register_entity() below briefly
                # registered this blind's old, no-longer-trusted status
                # identity - immediately corrected a moment later by
                # SchellenbergCover's own re-derivation in
                # async_added_to_hass() (see its own
                # STATUS_IDENTITY_SOURCE_UNKNOWN check), but never retracted
                # in between. Not reachable through the current UI (every
                # write path already clears these fields together), only
                # with hand-edited or legacy config data - found during the
                # lifecycle audit round.
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
            # Captured *before* the legacy-unique_id fallback below can set
            # existing_entity_id: True for a blind_id whose stable unique ID
            # has never been registered before - whether that is a genuinely
            # brand-new pairing, or one that is about to inherit an old
            # entity_id through that fallback (a reused remote: paired,
            # removed, paired again). False for every later reload of an
            # already-established blind (e.g. triggered by some *other*
            # blind being paired/removed), where the stable unique ID is
            # already registered on the first lookup above. See
            # async_added_to_hass()'s use of this via
            # SchellenbergCover's own is_newly_registered_entity.
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

            # NOTE: `dr.async_get_device_id_by_identifier` never existed in Home
            # Assistant (calling it raised AttributeError, not caught by the
            # ValueError handler that used to sit here). Identifiers are also no
            # longer globally unique as of HA 2026.9 (they are scoped per config
            # entry there), so the lookup itself must be scoped. See
            # device_registry_compat.py for why the call below still works on
            # Home Assistant releases older than 2026.9 too, rather than
            # trading one AttributeError for another.
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
                # The device registry entry above is only ever written at
                # first creation by HA itself - unlike device_name and
                # current_model, both freshly recomputed from the
                # subentry's *current* data on every setup/reload. Without
                # this sync, editing a blind's name or status-identity
                # fields through async_step_edit's "edit" form
                # (config_flow.py) silently reported success while the
                # device page, dashboards and entity list kept showing
                # whatever was registered at the very first pairing,
                # forever - found during the same audit as the entries
                # above. Guarded by the equality check so a reload with no
                # real identity/name change (the overwhelmingly common case
                # - most reloads are plain HA restarts, not edits) never
                # writes to the device registry or fires its update event.
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
        is_newly_registered_entity: bool = False,
    ) -> None:
        """Initialize the cover entity from its config subentry data.

        is_newly_registered_entity is True only the first time this blind's
        stable unique ID is ever set up (see async_setup_entry) - whether
        that is a genuinely new pairing or one that just inherited an old
        entity_id via the legacy-unique_id migration. It stays False on
        every later reload of an already-established blind, caused by some
        *other* blind being paired, removed, or recalibrated. Defaults to
        False so a SchellenbergCover built directly (as the tests do,
        bypassing async_setup_entry) gets the safe "trust restored state"
        behavior unless a test opts in explicitly - see
        async_added_to_hass()'s use of this flag for why that direction
        matters.
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
        # Set only on a subentry created fresh from a pair_device calibration
        # that measured which end stop the blind actually ended at (see
        # options_flow_calibration.py's calibration_close step). None for
        # every blind paired before this field existed and for the hybrid
        # (add_shutter_from_remote) workflow, which never calibrates - the
        # "nothing to restore" branch of async_added_to_hass() below falls
        # back to 0 (closed) in both of those cases, as it always did.
        self._initial_position_from_calibration: int | None = device_data_dict.get(
            CONF_INITIAL_POSITION
        )
        self._is_newly_registered_entity = is_newly_registered_entity
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._position_update_task: asyncio.Task[None] | None = None
        # Set only while a *secondary* status identity (a paired remote or
        # wall switch - see _handle_secondary_event's own docstring) has
        # reported a close command and nothing else is yet tracking a move
        # for this blind. None whenever no such timer is pending.
        self._secondary_close_timer_task: asyncio.Task[None] | None = None
        # Same idea, mirrored for the opposite direction: set only while a
        # secondary status identity has reported an open command and
        # nothing else is yet tracking a move. None whenever no such timer
        # is pending.
        self._secondary_open_timer_task: asyncio.Task[None] | None = None
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

        # A calibration that finished moments ago, in the same "Pair and
        # calibrate" run that is creating this entity for the first time,
        # is more trustworthy than *any* restored state - including one
        # that looks valid, since a reused blind_id/entity_id (e.g. this
        # same remote was paired, removed, and is now being paired again,
        # inheriting an old entity_id through async_setup_entry's legacy-
        # unique_id migration) would restore a leftover position from that
        # earlier, unrelated pairing episode.
        #
        # The in-memory fresh-calibration cache this originally relied on
        # (self._api.pop_fresh_calibration_position) is reliably empty
        # here, not just occasionally: adding this very subentry is
        # exactly the kind of subentry-set change __init__.py's
        # _on_entry_updated reloads the whole hub entry for, and that
        # reload replaces entry.runtime_data with a brand-new
        # SchellenbergUsbApi - wiping the cache - before this entity is
        # ever constructed to read it back.
        #
        # CONF_INITIAL_POSITION (-> _initial_position_from_calibration) is
        # the same value, persisted to this subentry's own data in that
        # same calibration-complete step (see options_flow_calibration.py),
        # so unlike the in-memory cache it survives the reload intact - but
        # only treat it as that same trusted, restored-state-overriding
        # signal for a blind that is itself newly registered
        # (is_newly_registered_entity, set in async_setup_entry). Every
        # other, already-established blind on the same hub goes through
        # this exact same reload whenever *any* blind is paired, removed,
        # or recalibrated, and each one also has its own, possibly
        # years-old CONF_INITIAL_POSITION sitting in its subentry data from
        # whenever *it* was first paired - for those, restored_from_ha is
        # the blind's real current position, and letting a stale
        # calibration-time snapshot override it every time some other
        # blind changes would be its own regression.
        fresh_calibration = False
        fresh_position = self._api.pop_fresh_calibration_position(self._blind_id)
        if fresh_position is None and self._is_newly_registered_entity:
            fresh_position = self._initial_position_from_calibration

        if fresh_position is not None:
            if restored_from_ha:
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
            fresh_calibration = True

        if self._attr_current_cover_position is None:
            if self._initial_position_from_calibration is not None:
                self._attr_current_cover_position = (
                    self._initial_position_from_calibration
                )
                _LOGGER.debug(
                    "No previous state for %s (%s); using the position "
                    "calibration actually ended at: %d%%",
                    self._device_name,
                    self._device_id,
                    self._attr_current_cover_position,
                )
            else:
                self._attr_current_cover_position = 0
                _LOGGER.debug(
                    "No previous state for %s (%s); defaulting position to "
                    "0%% (closed)",
                    self._device_name,
                    self._device_id,
                )
            self._attr_is_closed = self._attr_current_cover_position == 0

        if fresh_calibration:
            self._position_source_kind = "calibration"
            self._position_update_source = "fresh calibration result"
            startup_status = "measured during pairing / not confirmed since restart"
        elif restored_from_ha:
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
                    f"{SIGNAL_DEVICE_EVENT}_{self._status_device_id}_"
                    f"{self._status_enum}",
                    self._handle_event,
                )
            )

        # One subscription per paired secondary transmitter - a remote or a
        # wall switch, the config flow doesn't distinguish between them
        # (CONF_SECONDARY_STATUS_IDENTITIES is plural - a blind can have
        # more than one). api.py only ever dispatches on this exact signal
        # name for a frame matching this specific, registered secondary
        # identity, so an unmatched/unknown RF frame - one with no
        # recognised transmitter behind it - can never reach
        # _handle_secondary_event. functools.partial binds each
        # identity's device_id/enum immediately, rather than by reference,
        # so the loop variables below can't be mixed up between signals the
        # way a plain closure/lambda defined in a loop notoriously can.
        for secondary_device_id, secondary_enum in self._secondary_status_identities:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_EVENT}_{secondary_device_id}_{secondary_enum}",
                    functools.partial(
                        self._handle_secondary_event,
                        secondary_device_id,
                        secondary_enum,
                    ),
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
        self._cancel_secondary_close_timer("manual position sync")
        self._cancel_secondary_open_timer("manual position sync")
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
        self,
        device_id: str,
        open_time: float,
        close_time: float,
        ending_position: int,
    ) -> None:
        """Apply newly measured travel times and calibration's end position.

        ending_position is whichever end stop the last-driven leg of the
        calibration (open or close - the person's own choice, see
        options_flow_calibration.py's calibration_close step) reached.
        """
        if device_id != self._device_id:
            return

        self._cancel_secondary_close_timer("calibration completed")
        self._cancel_secondary_open_timer("calibration completed")
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
        # Unconditional, and first: a pending secondary-close/open timer can
        # be the *only* background task this blind has running (exactly the
        # no-primary-identity case the feature targets), so neither must be
        # skipped by the early return just below, which only looks at
        # _position_update_task.
        self._cancel_secondary_close_timer(reason)
        self._cancel_secondary_open_timer(reason)
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
        # Audit finding 16: cancel_pending_move() is the same per-device
        # mechanism async_stop_cover()/the auto-stop-at-target path already
        # use to end an in-flight control_blind_with_retry() loop for
        # *this* device_id (see its own docstring in api.py) - calling it
        # here too, first, means removing this blind's own subentry/
        # device ends its own in-flight move/retry loop right away,
        # instead of leaving it running - still transmitting real RF, or
        # processing a stray ack, for a blind Home Assistant no longer has
        # an entity for - until the subsequent full hub reload's unload
        # phase eventually reaches disconnect() (which, unlike this, wakes
        # every blind's own retry hub-wide, not just this one's). A no-op
        # if nothing is in flight for this device_id, the common case for
        # most removals - safe to call unconditionally, exactly like every
        # other cancel_pending_move() call site already does.
        self._api.cancel_pending_move(self._command_device_id)
        await self._async_shutdown_position_tracking("entity removal or entry unload")
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: str) -> None:
        """Handle a device activity event (started moving, or stopped)."""
        # This handler only ever runs for the *primary* status identity
        # (see async_added_to_hass()'s single subscription on
        # self._status_device_id/self._status_enum), so any call here is
        # fresh, authoritative information straight from the motor itself -
        # it must always supersede a merely-speculative secondary-identity
        # "assume closed"/"assume open" timer (see _handle_secondary_event),
        # whatever event this turns out to be.
        self._cancel_secondary_close_timer(f"primary status command {event}")
        self._cancel_secondary_open_timer(f"primary status command {event}")
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
                self._attr_current_cover_position = max(
                    0, min(100, self._attr_current_cover_position)
                )
                self._attr_is_closed = self._attr_current_cover_position == 0
            self._attr_is_opening = False
            self._attr_is_closing = False
            # Audit finding 17: this log used to run *before* the
            # _update_position() recalculation (and clamp) above, right
            # after previous_position was captured - so it always printed
            # the position as of the last 0.2s polling tick (or the move's
            # starting position, if STOPPED arrived before the tracking
            # loop ever ticked), never the freshly recalculated value the
            # rest of this handler goes on to use. A purely cosmetic field
            # for this one human-readable log line - every real field
            # (self._attr_current_cover_position itself, and
            # _record_position_update()'s new_position just below, which
            # already reads it after the recalculation) was always
            # correct - so this never had any functional impact, only a
            # log line that could lag the true stopped position by up to
            # one polling tick's worth of travel.
            _LOGGER.info(
                "Device %s STOPPED (position: %d%%)",
                self._device_name,
                self._attr_current_cover_position,
            )
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

    def _cancel_secondary_close_timer(self, reason: str = "superseded") -> None:
        """Request cancellation of a pending secondary-close timer, if any.

        Mirrors _stop_position_tracking()'s own fire-and-forget shape
        (request cancellation, clear the slot, don't await it) - every
        call site here is a @callback, synchronous context, exactly like
        every existing call site of that method.
        """
        task = self._secondary_close_timer_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._secondary_close_timer_task is task:
            self._secondary_close_timer_task = None

    def _start_secondary_close_timer(
        self, secondary_device_id: str, secondary_enum: str, command: str
    ) -> None:
        """(Re)start the timer that assumes "closed" after a close press.

        Always cancels/replaces any timer already pending first, so a
        second close press via the same or another paired secondary
        transmitter (a remote or a wall switch) re-bases the wait on the
        travel time remaining from *this* press, rather than risking the
        entity being marked closed early against an earlier press's
        clock. command is the raw frame value that made
        _handle_secondary_event treat this as a close (EVENT_STARTED_
        MOVING_DOWN normally, but EVENT_STARTED_MOVING_UP when
        invert_direction flips which physical button is the logical
        close) - kept only so the diagnostic text below names the command
        that was actually received, not an assumed one.
        """
        self._cancel_secondary_close_timer("restarted by a new secondary close command")
        task_name = f"{DOMAIN} secondary close timer {self._blind_id}"
        coro = self._async_secondary_close_timer(
            secondary_device_id, secondary_enum, command
        )
        config_entry = (
            self.hass.config_entries.async_get_entry(self._config_entry_id)
            if self._config_entry_id is not None
            else None
        )
        if config_entry is not None:
            task = config_entry.async_create_background_task(self.hass, coro, task_name)
        else:
            task = self.hass.async_create_background_task(coro, task_name)
        self._secondary_close_timer_task = task

    async def _async_secondary_close_timer(
        self, secondary_device_id: str, secondary_enum: str, command: str
    ) -> None:
        """Wait out the close travel time, then assume the blind is fully closed.

        Only ever started for a close command seen via a *secondary*
        status identity while nothing else was yet tracking a move (see
        _handle_secondary_event) - the gap this feature exists to cover is
        exactly the one described in the request that asked for it: a
        blind with no working primary status identity, driven fully shut
        from a paired remote or wall switch, otherwise leaves Home
        Assistant's dashboard showing whatever position it last had,
        forever, since nothing else will ever report that the move
        happened or finished.

        Cancelled well before completion by _cancel_secondary_close_timer()
        whenever anything more authoritative supersedes it - another
        secondary-identity command (open, stop, or a repeated close
        restarting the wait), any primary-status event, a manual position
        sync, a fresh calibration result, or the entity/hub shutting down.
        """
        # Mirrors _async_position_update_loop()'s own try/except/finally
        # shape for the same reason: the finally clears this task's slot
        # from inside itself on every exit (cancelled or not), the same
        # safety net that loop relies on instead of leaving that solely to
        # whichever caller cancelled it.
        timer_task = asyncio.current_task()
        still_current = False
        try:
            await asyncio.sleep(self._travel_time_close)
        except asyncio.CancelledError:
            _LOGGER.debug(
                "Secondary close timer cancelled for device %s", self._device_name
            )
            raise
        finally:
            still_current = self._secondary_close_timer_task is timer_task
            if still_current:
                self._secondary_close_timer_task = None

        if not still_current:
            # Replaced (a newer close press re-based the wait) between the
            # sleep finishing and this task actually resuming; let that
            # newer timer own the outcome instead of double-applying one.
            _LOGGER.debug(
                "Device %s secondary close timer superseded before it "
                "could apply; not forcing closed",
                self._device_name,
            )
            return

        if self._attr_is_opening or self._attr_is_closing:
            # Something else - almost always the same move, since seen via
            # the primary status identity too - started tracking in the
            # meantime without going through a path that cancels this
            # timer. Deferring to it rather than overwriting it follows the
            # same reasoning as _has_live_tracking_in_direction()'s own
            # docstring.
            _LOGGER.debug(
                "Device %s secondary close timer elapsed but a move is now "
                "tracked some other way; not forcing closed",
                self._device_name,
            )
            return

        previous_position = self._attr_current_cover_position
        self._attr_current_cover_position = 0
        self._attr_is_closed = True
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        self._target_position = None
        self._position_source_kind = "secondary status"
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._position_update_source = (
            f"secondary status {secondary_device_id}/{secondary_enum} command {command}"
        )
        self._record_position_update(
            source=self._position_update_source,
            direction="closing",
            previous_position=previous_position,
            new_position=0,
            status="assumed from secondary status + elapsed close time",
        )
        _LOGGER.info(
            "Device %s assumed fully closed: close command via a paired "
            "secondary transmitter (status %s/%s), %.1fs close travel "
            "time elapsed with no contradicting signal",
            self._device_name,
            secondary_device_id,
            secondary_enum,
            self._travel_time_close,
        )
        self.async_write_ha_state()

    def _cancel_secondary_open_timer(self, reason: str = "superseded") -> None:
        """Request cancellation of a pending secondary-open timer, if any.

        Mirrors _cancel_secondary_close_timer() exactly, for the opposite
        direction - see that method's own docstring for why this is a
        fire-and-forget cancellation (request cancellation, clear the slot,
        don't await it) rather than an awaited one.
        """
        task = self._secondary_open_timer_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._secondary_open_timer_task is task:
            self._secondary_open_timer_task = None

    def _start_secondary_open_timer(
        self, secondary_device_id: str, secondary_enum: str, command: str
    ) -> None:
        """(Re)start the timer that assumes "open" after an open press.

        Mirrors _start_secondary_close_timer() exactly, for the opposite
        direction: always cancels/replaces any open timer already pending
        first, so a second open press via the same or another paired
        secondary transmitter (a remote or a wall switch) re-bases the wait
        on the travel time remaining from *this* press, rather than risking
        the entity being marked open early against an earlier press's
        clock. command is the raw frame value that made
        _handle_secondary_event treat this as an open (EVENT_STARTED_
        MOVING_UP normally, but EVENT_STARTED_MOVING_DOWN when
        invert_direction flips which physical button is the logical open)
        - kept only so the diagnostic text below names the command that was
        actually received, not an assumed one.
        """
        self._cancel_secondary_open_timer("restarted by a new secondary open command")
        task_name = f"{DOMAIN} secondary open timer {self._blind_id}"
        coro = self._async_secondary_open_timer(
            secondary_device_id, secondary_enum, command
        )
        config_entry = (
            self.hass.config_entries.async_get_entry(self._config_entry_id)
            if self._config_entry_id is not None
            else None
        )
        if config_entry is not None:
            task = config_entry.async_create_background_task(self.hass, coro, task_name)
        else:
            task = self.hass.async_create_background_task(coro, task_name)
        self._secondary_open_timer_task = task

    async def _async_secondary_open_timer(
        self, secondary_device_id: str, secondary_enum: str, command: str
    ) -> None:
        """Wait out the open travel time, then assume the blind is fully open.

        Mirrors _async_secondary_close_timer() exactly, for the opposite
        direction: only ever started for an open command seen via a
        *secondary* status identity while nothing else was yet tracking a
        move (see _handle_secondary_event) - the same gap this feature
        exists to cover, now for a blind driven fully open from a paired
        remote or wall switch with no working primary status identity,
        which would otherwise leave Home Assistant's dashboard showing
        whatever position it last had, forever.

        Cancelled well before completion by _cancel_secondary_open_timer()
        whenever anything more authoritative supersedes it - another
        secondary-identity command (close, stop, or a repeated open
        restarting the wait), any primary-status event, a manual position
        sync, a fresh calibration result, or the entity/hub shutting down.
        """
        # Mirrors _async_secondary_close_timer()'s own try/except/finally
        # shape for the same reason: the finally clears this task's slot
        # from inside itself on every exit (cancelled or not), the same
        # safety net that method relies on instead of leaving that solely
        # to whichever caller cancelled it.
        timer_task = asyncio.current_task()
        still_current = False
        try:
            await asyncio.sleep(self._travel_time_open)
        except asyncio.CancelledError:
            _LOGGER.debug(
                "Secondary open timer cancelled for device %s", self._device_name
            )
            raise
        finally:
            still_current = self._secondary_open_timer_task is timer_task
            if still_current:
                self._secondary_open_timer_task = None

        if not still_current:
            # Replaced (a newer open press re-based the wait) between the
            # sleep finishing and this task actually resuming; let that
            # newer timer own the outcome instead of double-applying one.
            _LOGGER.debug(
                "Device %s secondary open timer superseded before it "
                "could apply; not forcing open",
                self._device_name,
            )
            return

        if self._attr_is_opening or self._attr_is_closing:
            # Something else - almost always the same move, since seen via
            # the primary status identity too - started tracking in the
            # meantime without going through a path that cancels this
            # timer. Deferring to it rather than overwriting it follows the
            # same reasoning as _has_live_tracking_in_direction()'s own
            # docstring.
            _LOGGER.debug(
                "Device %s secondary open timer elapsed but a move is now "
                "tracked some other way; not forcing open",
                self._device_name,
            )
            return

        previous_position = self._attr_current_cover_position
        self._attr_current_cover_position = 100
        self._attr_is_closed = False
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        self._target_position = None
        self._position_source_kind = "secondary status"
        self._position_confirmed_since_restart = True
        self._full_travel_resync_direction = None
        self._position_update_source = (
            f"secondary status {secondary_device_id}/{secondary_enum} command {command}"
        )
        self._record_position_update(
            source=self._position_update_source,
            direction="opening",
            previous_position=previous_position,
            new_position=100,
            status="assumed from secondary status + elapsed open time",
        )
        _LOGGER.info(
            "Device %s assumed fully open: open command via a paired "
            "secondary transmitter (status %s/%s), %.1fs open travel "
            "time elapsed with no contradicting signal",
            self._device_name,
            secondary_device_id,
            secondary_enum,
            self._travel_time_open,
        )
        self.async_write_ha_state()

    @callback
    def _handle_secondary_event(
        self, secondary_device_id: str, secondary_enum: str, command: str
    ) -> None:
        """Handle a status frame from one of this blind's paired secondary
        transmitters (a remote control or a wall switch - the config flow
        doesn't distinguish between them, see CONF_SECONDARY_STATUS_
        IDENTITIES's own "Additional status identities" label).

        Reached only for a frame matching a *secondary* status identity -
        one explicitly paired to this blind (CONF_SECONDARY_STATUS_
        IDENTITIES is plural: there can be more than one) - never for an
        unmatched/unknown RF frame, since api.py only dispatches on this
        exact signal name for a registered identity match; see
        async_added_to_hass()'s subscription loop. That is what keeps this
        feature from ever triggering for a bare RF command with no
        recognised transmitter behind it, exactly as asked for.

        Unlike the primary status identity (the motor's own echo, handled
        by _handle_event and already the only source position_tracking is
        ever computed from - see api.py's _handle_message()), a secondary
        identity's frames never drive position tracking directly. This
        adds exactly two narrow, symmetric behaviours on top: a close (or
        open) press, seen only via a paired remote or wall switch, while
        nothing is already tracking a move, starts a timer that - unless
        something else intervenes first - assumes after the known close
        (or open) travel time that the blind ran all the way down (or up),
        since otherwise nothing will ever tell Home Assistant that it did.
        A stop cancels whichever of the two, if either, is still pending,
        without starting anything new - a mid-travel position reached via
        a secondary identity's stop is exactly as unknowable as one reached
        via the primary identity (see _handle_event and this project's own
        CHANGELOG for why that's true there too), so this deliberately
        never guesses one.
        """
        if command in (EVENT_STARTED_MOVING_UP, EVENT_STARTED_MOVING_DOWN):
            # Same physical-to-logical mapping _handle_event() itself uses,
            # applied symmetrically: with invert_direction set, the
            # *physical* "up" button is this blind's logical close, not
            # its "down" button.
            physical_up = command == EVENT_STARTED_MOVING_UP
            logical_opening: bool | None = physical_up != self._invert_direction
        elif command == EVENT_STOPPED:
            logical_opening = None
        else:
            # Unrecognized command byte. Not acted on, and not treated as
            # a reason to cancel an already-pending timer either - an
            # undecodable frame shouldn't be able to discard a close/open
            # press that was itself perfectly valid.
            return

        reason = (
            f"secondary status {secondary_device_id}/{secondary_enum} command {command}"
        )

        if logical_opening is None:
            # Stop: neither direction's assumption is still warranted: a
            # stop halfway through either a close or an open means the
            # blind did not, after all, reach the end it was timing toward.
            self._cancel_secondary_close_timer(reason)
            self._cancel_secondary_open_timer(reason)
            return

        # Whichever direction this press is NOT going is the one that can
        # no longer still be in progress - supersede its timer the same
        # way a stop would, before possibly starting the matching one for
        # the direction this press IS going.
        if logical_opening:
            self._cancel_secondary_close_timer(reason)
        else:
            self._cancel_secondary_open_timer(reason)

        if self._attr_is_opening or self._attr_is_closing:
            _LOGGER.debug(
                "Device %s secondary status %s/%s %s command ignored: "
                "a move is already being tracked",
                self._device_name,
                secondary_device_id,
                secondary_enum,
                "open" if logical_opening else "close",
            )
            return

        if logical_opening:
            _LOGGER.info(
                "Device %s secondary status %s/%s open command: starting a "
                "%.1fs assume-open timer (no move currently tracked)",
                self._device_name,
                secondary_device_id,
                secondary_enum,
                self._travel_time_open,
            )
            self._start_secondary_open_timer(
                secondary_device_id, secondary_enum, command
            )
        else:
            _LOGGER.info(
                "Device %s secondary status %s/%s close command: starting a "
                "%.1fs assume-closed timer (no move currently tracked)",
                self._device_name,
                secondary_device_id,
                secondary_enum,
                self._travel_time_close,
            )
            self._start_secondary_close_timer(
                secondary_device_id, secondary_enum, command
            )

    def _has_live_tracking_in_direction(self, *, opening: bool) -> bool:
        """Return whether a move already in progress is tracked in this direction.

        True only when the cover is already reported moving the same way
        (opening/closing) AND the background tracking loop is actually
        still running with a valid baseline. async_open_cover()/
        async_close_cover() use this to recognise a redundant re-send of
        an already-in-progress move - for instance a script's own
        intentional double-send of the same open/close command roughly
        0.3s later, sent purely for RF reliability - as distinct from a
        genuinely new command.

        Without this distinction, every such re-send used to cancel
        tracking immediately (at call entry) and only re-establish it
        once that re-send's own control_blind_with_retry() succeeded,
        which can take well over a second under contention from another
        blind sharing the hub-wide transmit lock. The blind keeps moving
        the whole time under the first, already-accepted command; only
        the HA-side estimate froze, then resumed from that stale, frozen
        value - silently losing however far the blind actually travelled
        during the gap for the rest of that move. A redundant re-send
        detected this way skips re-freezing/re-basing the estimate
        entirely (it still reaches the stick again below, unchanged) so
        the running estimate stays continuous across it instead.
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
                    # An intermediate target (unlike 0/100, see
                    # needs_stop_command below) is only ever reached because
                    # a stop command sent from here makes it so - and that
                    # command still has to be transmitted and acted on after
                    # this check fires. Measured on real hardware (see
                    # STOP_COMMAND_LATENCY_SECONDS's own comment in
                    # const.py), that takes roughly 1.1-1.2s, during which
                    # the blind keeps moving under the already-issued open/
                    # close command. Triggering this many percentage points
                    # of this move's own travel early compensates for that
                    # otherwise-fixed overshoot, scaled to this blind's own
                    # travel time so a fast blind and a slow blind are each
                    # compensated correctly. Uncompensated, this is what
                    # made any target within roughly this fraction of a fast
                    # blind's travel time of either end stop unreachable -
                    # the earliest a stop could physically land was already
                    # past it.
                    needs_stop_command = self._target_position not in (0, 100)
                    stop_lead_percent = 0.0
                    if needs_stop_command:
                        travel_time_for_lead = max(
                            self._travel_time_open
                            if self._attr_is_opening
                            else self._travel_time_close,
                            0.1,
                        )
                        # travel_time_for_lead's own floor just above only
                        # keeps this division from raising or exploding -
                        # on its own it leaves the result with no matching
                        # ceiling. See MAX_STOP_LEAD_PERCENT's own comment
                        # in const.py (audit finding 18) for why an
                        # unbounded lead here is not just a cosmetically
                        # large number: past 100 it makes the position_
                        # reached check below satisfied by any position at
                        # all, so a short/corrupted travel time used to
                        # stop the blind the instant this loop was
                        # entered, however far it actually was from
                        # target_position.
                        stop_lead_percent = min(
                            (STOP_COMMAND_LATENCY_SECONDS / travel_time_for_lead) * 100,
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
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._device_name,
                            self._target_position,
                        )
                        if needs_stop_command:
                            # This is functionally a stop, same as
                            # async_stop_cover()'s own manual one - see its
                            # docstring for why cancel_pending_move() comes
                            # first: the move that is ending here is the
                            # same one control_blind_with_retry() may still
                            # be retrying if the stick answered "tE" to it
                            # earlier, and that retry must not resend the
                            # open/close command after this stop goes out.
                            self._api.cancel_pending_move(self._command_device_id)
                            # control_blind_stop() waits for the open/close
                            # command issued when this move started to
                            # actually finish transmitting, and is briefly
                            # serialized against any other blind's own stop
                            # or move too (see its own docstring) - closes
                            # the same gap async_stop_cover() closes for a
                            # manual stop press, for this automatic one.
                            # Returns immediately once nothing is in flight,
                            # which for any move longer than roughly one
                            # transmit cycle - the overwhelming majority -
                            # it already has by the time this point is
                            # reached, so this adds no delay in the normal
                            # case.
                            stop_sent = await self._api.control_blind_stop(
                                self._command_enum,
                                device_id=self._command_device_id,
                            )
                            if not stop_sent:
                                # control_blind_stop() already retries
                                # internally within its own STOP_RETRY_
                                # BUDGET and only returns False once even
                                # its final fallback write failed to reach
                                # the wire - not merely "no ack". Treating
                                # that as a successful stop would clear
                                # is_opening/is_closing/the target while
                                # the motor is still moving (toward this
                                # target, or now its real end stop with
                                # nothing having told it otherwise), and
                                # would discard this same loop's own
                                # ability to act once a stop - this retry,
                                # or the motor's own end-stop - actually
                                # does land, since the tracking state
                                # would already be torn down. Leave it
                                # untouched and retry next tick instead -
                                # found during the concurrency/lifecycle
                                # audit round.
                                _LOGGER.warning(
                                    "Device %s: auto-stop at target "
                                    "position (%d%%) failed to send - "
                                    "retrying",
                                    self._device_name,
                                    self._target_position,
                                )
                                continue
                        else:
                            # A plain open_cover()/close_cover() reaching 0/100
                            # confirms this via _confirm_full_travel_resync()
                            # (see the target_position-is-None branch below) -
                            # set_cover_position(0)/set_cover_position(100)
                            # reaching the exact same physical endstop through
                            # this branch instead never did, leaving
                            # _position_confirmed_since_restart stuck and the
                            # diagnostics history missing its "estimated from
                            # full travel" record for this transit, even
                            # though the live position reported above is
                            # identical either way - found during the same
                            # audit as the entries above.
                            self._confirm_full_travel_resync(
                                "closing" if self._attr_is_closing else "opening",
                                self._target_position,
                            )
                        # Reaching a target here is a stop in every sense
                        # that matters to the entity's own reported state,
                        # whether a real stop command went out above or the
                        # motor's own end-stop did the job instead via the
                        # else branch - and must clear is_opening/is_closing
                        # exactly like async_stop_cover() and the plain-
                        # open/close branch below (target_position is None)
                        # already do. Missing here before this fix, which
                        # left the entity reporting "closing"/"opening"
                        # forever after any set_cover_position() call
                        # finished - confirmed from Home Assistant still
                        # showing a cover as "Wird geschlossen" several
                        # minutes after it had actually reached 80% and
                        # stopped.
                        self._attr_is_opening = False
                        self._attr_is_closing = False
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
        # Fresh, authoritative information straight from Home Assistant
        # itself (or set_cover_position(), chaining into this via
        # _preserve_target) - must always supersede a merely-speculative
        # secondary-identity "assume closed"/"assume open" timer (see
        # _handle_secondary_event), the same reasoning _handle_event()
        # already applies for a primary status event. Without this, a
        # stale pending timer from an earlier secondary-identity press
        # could still fire later and overwrite whatever this call's own
        # open actually settles the entity at - its own guard against that
        # (checking is_opening/is_closing when it fires) is only a
        # snapshot, not a substitute for cancelling it outright.
        self._cancel_secondary_close_timer("direct Home Assistant open command")
        self._cancel_secondary_open_timer("direct Home Assistant open command")
        if not _preserve_target:
            # A plain open request (not a set_cover_position leg) always
            # means "go all the way to 100" - clear any stale target left
            # over from an earlier partial move so this one isn't cut short.
            self._target_position = None
        action = resolve_direction_command("open", self._invert_direction)
        _LOGGER.debug(
            "Opening cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        # A redundant re-send of an already-in-progress open (for instance
        # a script's own intentional double-send, ~0.3s after the first,
        # purely for RF reliability) must still reach the stick again
        # below, but must not re-freeze/re-base position tracking the way
        # a genuinely new open does just below it - the blind never
        # stopped moving between the two sends, so there is nothing to
        # re-base from. See _has_live_tracking_in_direction()'s own
        # docstring for the exact mechanism this avoids.
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
        # Wait for any other transmission already in flight - another
        # blind's own open/close, a teach/pairing/raw-transmit step, or
        # this same blind's own prior stop - to finish, then write and keep
        # retrying until the stick actually accepts it rather than merely
        # attempting once. control_blind_with_retry() (api.py) serializes
        # this hub-wide through its own lock, held for the whole cycle: a
        # bare wait-then-write here, each blind checking the same shared
        # "idle" event independently, let several blinds commanded together
        # all observe idle at once and all write within the same
        # millisecond window - confirmed from a real log - of which the
        # stick then accepted exactly one and silently dropped the rest,
        # since control_blind() itself never waits for or retries a "tE"
        # busy reply. Deliberately not applied to async_stop_cover() - see
        # its own docstring for why a manual stop must stay instant and
        # unretried instead.
        #
        # Deliberately not (re)started above either when this is a
        # genuinely new open: _move_start_time, _move_start_position and
        # the tracking loop itself only get set up once this call below
        # actually succeeds (see there) - not here, before this blind's own
        # command has even reached the stick.
        if not await self._api.control_blind_with_retry(
            self._command_enum, action, device_id=self._command_device_id
        ):
            # Re-check rather than trust the snapshot taken before the
            # await above: a real status frame (for instance the
            # auto-stop-at-target path, or an external stop) can land
            # while this call's own write was in flight and already
            # stop-and-clear the original move's own tracking. When that
            # happened there is no longer a live original move to leave
            # alone, so fall through to ordinary failure handling below
            # instead of silently leaving a stale "opening" state behind.
            if is_redundant_resend and self._has_live_tracking_in_direction(
                opening=True
            ):
                # The original move (from the first send) is still
                # genuinely under way and its own tracking is untouched -
                # only this resend's own extra transmission didn't get
                # through. Leave the entity and the running move alone
                # rather than reporting the whole open as failed and
                # potentially aborting a caller's script mid-sequence over
                # a redundant resend that was never required for
                # correctness.
                _LOGGER.debug(
                    "Redundant open resend for device %s did not reach "
                    "the stick; the original move is unaffected",
                    self._device_name,
                )
                self.async_write_ha_state()
                return
            # The command never reached the stick - undo the optimistic
            # "opening" state set above so the entity doesn't report a
            # movement that never physically happened. Also drop any
            # target this call was asked to preserve (a set_cover_
            # position() leg, _preserve_target=True): with no move
            # actually under way, there is nothing left for that target to
            # describe, and leaving it stale let a later, completely
            # unrelated move - typically the original remote control -
            # read it back via _async_position_update_loop() and get
            # auto-stopped at that long-invalid position. Safe to clear
            # unconditionally here specifically because the redundant-
            # resend branch above (where the original move's own target is
            # still genuinely live) already returned before reaching this
            # point.
            self._attr_is_opening = False
            self._move_start_time = None
            self._move_start_position = None
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

        # Only now that the stick has actually accepted the command do we
        # know the motor is genuinely about to start turning - start the
        # elapsed-time clock and the background tracking loop from here,
        # not earlier. Starting them optimistically above (right when this
        # method was first called, before the wait/retry above) meant a
        # set_cover_position() leg queued behind other blinds in
        # control_blind_with_retry()'s hub-wide lock already had its target
        # evaluated against time spent only waiting in that queue, not
        # moving: for a short enough leg queued behind enough other blinds
        # in the same burst, the elapsed-time estimate could reach the
        # target before this blind's own command had been transmitted even
        # once - so the real command would go out afterwards with no
        # tracking left running to stop it at the target. Skipped entirely
        # for a redundant resend: the original move's own tracking has
        # been running the whole time and already reflects however long
        # this resend's own wait took.
        # Re-check here too: this call's own command just reached the
        # stick, but if the original move it was meant to piggyback on
        # already got stopped-and-cleared for real while this call's own
        # write was in flight (see the matching re-check in the failure
        # branch above), the motor is now moving under this call's own
        # command alone, with nothing left tracking it. (Re)start tracking
        # exactly as a fresh move would rather than skip it because of a
        # now-stale snapshot, and re-assert the optimistic state the real
        # stop event in between overwrote.
        if not is_redundant_resend or not self._has_live_tracking_in_direction(
            opening=True
        ):
            self._attr_is_opening = True
            self._attr_is_closing = False
            self._move_start_position = self._attr_current_cover_position
            self._move_start_time = time.monotonic()
            self._start_position_tracking()
        self.async_write_ha_state()

    async def async_close_cover(
        self, *, _preserve_target: bool = False, **kwargs: Any
    ) -> None:
        """Close the cover, or drive it downward as a leg of set_cover_position."""
        # Same reasoning as async_open_cover() - see its own comment.
        self._cancel_secondary_close_timer("direct Home Assistant close command")
        self._cancel_secondary_open_timer("direct Home Assistant close command")
        if not _preserve_target:
            # Same reasoning as async_open_cover: a plain close always
            # targets 0, so drop any stale partial-move target first.
            self._target_position = None
        action = resolve_direction_command("close", self._invert_direction)
        _LOGGER.debug(
            "Closing cover %s (command_id=%s enum=%s action=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
            action,
        )
        # A redundant re-send of an already-in-progress close (for
        # instance a script's own intentional double-send, ~0.3s after the
        # first, purely for RF reliability) must still reach the stick
        # again below, but must not re-freeze/re-base position tracking
        # the way a genuinely new close does just below it - the blind
        # never stopped moving between the two sends, so there is nothing
        # to re-base from. See _has_live_tracking_in_direction()'s own
        # docstring for the exact mechanism this avoids.
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
        # async_open_cover() above - see its comment for why. Also, same as
        # there: _move_start_time/_move_start_position/the tracking loop
        # are deliberately not (re)started above either when this is a
        # genuinely new close - see the comment right after this call
        # succeeds below for why.
        if not await self._api.control_blind_with_retry(
            self._command_enum, action, device_id=self._command_device_id
        ):
            # Re-check rather than trust the snapshot taken before the
            # await above: a real status frame (for instance the
            # auto-stop-at-target path, or an external stop) can land
            # while this call's own write was in flight and already
            # stop-and-clear the original move's own tracking. When that
            # happened there is no longer a live original move to leave
            # alone, so fall through to ordinary failure handling below
            # instead of silently leaving a stale "closing" state behind.
            if is_redundant_resend and self._has_live_tracking_in_direction(
                opening=False
            ):
                # The original move (from the first send) is still
                # genuinely under way and its own tracking is untouched -
                # only this resend's own extra transmission didn't get
                # through. Leave the entity and the running move alone
                # rather than reporting the whole close as failed and
                # potentially aborting a caller's script mid-sequence over
                # a redundant resend that was never required for
                # correctness.
                _LOGGER.debug(
                    "Redundant close resend for device %s did not reach "
                    "the stick; the original move is unaffected",
                    self._device_name,
                )
                self.async_write_ha_state()
                return
            # The command never reached the stick - undo the optimistic
            # "closing" state set above so the entity doesn't report a
            # movement that never physically happened. Also drop any
            # target this call was asked to preserve - see
            # async_open_cover()'s mirror of this point for why.
            self._attr_is_closing = False
            self._move_start_time = None
            self._move_start_position = None
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

        # Same reasoning as async_open_cover()'s own comment at its mirror
        # of this point: only start the clock and the tracking loop once
        # the stick has actually accepted the command, not before the
        # wait/retry above - otherwise a short set_cover_position() leg
        # queued behind other blinds in control_blind_with_retry()'s
        # hub-wide lock could have its target evaluated as "reached" purely
        # from time spent waiting in that queue. Skipped entirely for a
        # redundant resend: the original move's own tracking has been
        # running the whole time and already reflects however long this
        # resend's own wait took.
        # Re-check here too: this call's own command just reached the
        # stick, but if the original move it was meant to piggyback on
        # already got stopped-and-cleared for real while this call's own
        # write was in flight (see the matching re-check in the failure
        # branch above), the motor is now moving under this call's own
        # command alone, with nothing left tracking it. (Re)start tracking
        # exactly as a fresh move would rather than skip it because of a
        # now-stale snapshot, and re-assert the optimistic state the real
        # stop event in between overwrote.
        if not is_redundant_resend or not self._has_live_tracking_in_direction(
            opening=False
        ):
            self._attr_is_opening = False
            self._attr_is_closing = True
            self._move_start_position = self._attr_current_cover_position
            self._move_start_time = time.monotonic()
            self._start_position_tracking()
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover, freezing the estimated position once it lands.

        Sent via control_blind_stop(), which is to a manual stop what
        control_blind_with_retry() is to open/close: briefly serialized
        (see its own docstring for why that is a short, independent budget
        rather than open/close's own lock and retry), falling back to a
        plain control_blind() write - one direct write, no lock, no retry -
        if that serialization cannot be had quickly. That plain write is
        also exactly what this used to call directly, before control_
        blind_stop() existed; this method's very first action below,
        cancel_pending_move(), predates it too and is still needed just the
        same: control_blind_stop()'s own serialization only keeps this
        stop's write from colliding with another write already in flight,
        it does not stop a move's retry loop for *this* blind from firing
        again afterwards on its own schedule, which only cancel_pending_
        move() ends. Now that async_open_cover()/async_close_cover() retry
        on "tE" again (via control_blind_with_retry(), see its own
        docstring), a stop must actively end any retry loop still running
        for this same blind instead of merely not starting one of its own.

        Position tracking is deliberately left running - and the position
        is not frozen - until *after* control_blind_stop() actually
        returns, not up front the way this used to work. Freezing up front
        made the estimate stop advancing at the instant this method was
        called, while the real write could still be queued for over a
        second behind another blind's own transmission under contention
        (see control_blind_stop()'s and _move_lock's own docstrings in
        api.py) - confirmed from a real two-blind test where that gap put
        the frozen estimate several percentage points away from where the
        blind had actually gotten to by the time the stop really went out,
        on a blind whose own preceding close/open had gone out instantly
        with no queueing at all, so the two delays did not cancel out.
        Letting the existing elapsed-time tracking keep running for
        however long that wait actually takes, then reading it only once
        the write has happened, keeps the estimate accurate regardless of
        how long the stop was queued - exactly why async_open_cover()'s and
        async_close_cover()'s own _move_start_time is only ever set after
        their own write succeeds, not before it either.

        _target_position is cleared up front regardless: the background
        tracking loop's own auto-stop-at-target branch (see
        _async_position_update_loop) triggers only while a target is set,
        and must not fire its own, second control_blind_stop() for this
        same blind while this one is still in flight just because tracking
        itself was deliberately left running above.
        """
        # First, and before any other statement: if this device has a move
        # retry loop still running (control_blind_with_retry(), in flight
        # because the stick is busy with something else), end it now so it
        # cannot resend the open/close command after the stop below has
        # already gone out. Called this early, with no await beforehand, so
        # there is no window in which this task could yield to that retry
        # loop before the cancellation is actually requested.
        self._api.cancel_pending_move(self._command_device_id)
        # Same reasoning as async_open_cover()/async_close_cover() - a
        # direct stop is fresh, authoritative information that must
        # supersede a merely-speculative secondary-identity timer too.
        # Placed here, right alongside cancel_pending_move() above and
        # still with no await before it, for the same no-window reason
        # that call is already placed first.
        self._cancel_secondary_close_timer("direct Home Assistant stop command")
        self._cancel_secondary_open_timer("direct Home Assistant stop command")
        _LOGGER.debug(
            "Stopping cover %s (command_id=%s enum=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
        )
        previous_target_position = self._target_position
        self._target_position = None
        if not await self._api.control_blind_stop(
            self._command_enum, device_id=self._command_device_id
        ):
            # The stop never reached the stick. Nothing else has been
            # touched yet - position tracking, if it was running, still is,
            # and is_opening/is_closing/_move_start_time/_move_start_position
            # are exactly as they were, so there is nothing to restore and
            # nothing to restart - only the target this method itself
            # cleared above needs putting back.
            self._target_position = previous_target_position
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

        # Only now, with the write actually done, is it safe to stop the
        # background tracking loop and read its estimate - see this
        # method's own docstring for why reading it any earlier would risk
        # a stale, prematurely frozen position under contention.
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
        self._record_position_update(
            source=self._position_update_source,
            direction="stop",
            previous_position=previous_position,
            new_position=self._attr_current_cover_position,
            status="estimated",
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
