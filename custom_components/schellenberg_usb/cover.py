"""Cover platform for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
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
    MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_SECONDS,
    MAX_STOP_LEAD_PERCENT,
    NEAR_END_STOP_SNAP_TOLERANCE_PERCENT,
    OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS,
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
                # with hand-edited or legacy config data.
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
                # forever. Guarded by the equality check so a reload with no
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
        # This blind's own persisted CONF_INITIAL_POSITION startup fallback -
        # set whenever any setup or confirmation path has recorded a known
        # position: a fresh pairing (async_step_user) regardless of which
        # calibration_method_choice option was picked - a live calibration
        # run (options_flow_calibration.py's calibration_close step) sets
        # it to the real ending position, manual entry sets it directly
        # from the person's own stated current position - recalibrating an
        # already-paired blind, or confirming a position via Developer
        # Tools' "Set position" sync (config_flow.py). None only for a
        # blind paired before this field existed, or under an older code
        # revision that predated one of the paths above gaining it - see
        # the "nothing to restore" branch of async_added_to_hass() below
        # for what happens in that case (not a 0%/closed default).
        self._initial_position_from_calibration: int | None = device_data_dict.get(
            CONF_INITIAL_POSITION
        )
        self._is_newly_registered_entity = is_newly_registered_entity
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._position_update_task: asyncio.Task[None] | None = None
        self._target_position: int | None = None
        # True from the moment async_open_cover()/async_close_cover() are
        # entered until they return or raise - including the whole stretch
        # where they are suspended awaiting _async_cancel_position_tracking()
        # or control_blind_with_retry() (see each method's own try/finally).
        # _handle_event()'s STARTED_MOVING branch reads this alongside
        # _has_live_position_tracking() specifically because tracking itself
        # only starts once control_blind_with_retry() actually succeeds (see
        # test_async_open_cover_does_not_start_tracking_before_retry_succeeds)
        # - so a set_cover_position() leg's own, genuinely current
        # self._target_position would otherwise look exactly like stale
        # leftover garbage to that check for as long as the leg's own write
        # is still queued behind another blind's transmission.
        self._command_in_flight = False
        # Set only while an optimistic, direction-guessed dashboard state
        # from a secondary identity's frame is pending its own withdrawal
        # (see _handle_secondary_activity()/_clear_optimistic_secondary_
        # move() below) - also doubles as the debounce that keeps repeated
        # frames from the same held-down button from scheduling another.
        self._optimistic_secondary_move_unsub: CALLBACK_TYPE | None = None
        self._position_update_source = "not recorded"
        self._position_source_kind = "startup default"
        self._position_confirmed_since_restart = False
        self._full_travel_resync_direction: str | None = None
        # monotonic() timestamp of the first attempt to send the stop for
        # the *current* auto-stop-at-target episode (see the position_
        # reached branch of _async_position_update_loop below), or None
        # when no such episode is in flight. A StopOutcome of CONFIRMED
        # resets this to None - the next episode (if any) starts its own
        # fresh clock. A StopOutcome of FAILED or UNCONFIRMED instead
        # leaves it untouched and retries next tick, for as long as
        # time.monotonic() - self._pending_auto_stop_since stays under
        # AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS - see that constant's
        # own comment in const.py for why retrying this long, here, is
        # safe even though STOP_RETRY_BUDGET itself is only 2.0s.
        #
        # Also cleared whenever the tracking task itself is torn down for
        # any other reason (_async_cancel_position_tracking()/_stop_
        # position_tracking(), e.g. a direction reversal landing mid-
        # retry) - and, as a second, independent safeguard against the
        # same class of leak, re-stamped fresh (never trusted stale)
        # whenever self._pending_auto_stop_target no longer matches
        # self._target_position at the point this is read. Without both
        # of these, a timestamp stamped for one episode could survive
        # that episode's end - via a path that forgot to clear it, or
        # simply because self._target_position changed to None while
        # this same task kept running - and get silently inherited by a
        # later, unrelated episode, eating into (or outright exhausting)
        # its own backstop budget before that episode's very first
        # attempt. Found during an independent review of this
        # mechanism's first version (2026-11-01), which cleared this
        # only on CONFIRMED and on backstop exhaustion itself.
        self._pending_auto_stop_since: float | None = None
        # The target self._pending_auto_stop_since above was stamped
        # for - see its own comment immediately above for why this
        # exists. None exactly when self._pending_auto_stop_since is.
        self._pending_auto_stop_target: int | None = None
        # Set to a fresh asyncio.Event() by async_stop_cover() right
        # before it calls control_blind_stop(), and back to None once
        # that method is done waiting on it (either way: motor-confirmed
        # or timed out) - see MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_
        # SECONDS's own comment in const.py for what this closes and
        # why it is scoped to the stop side only. _handle_event()'s own
        # EVENT_STOPPED branch sets it, unconditionally, whenever this
        # entity's primary status identity reports a stop - from *any*
        # cause, not only a manual one - which is fine: by the time
        # async_stop_cover() is the one awaiting it, a stop frame for
        # this same device arriving for some other reason still means
        # the motor is now genuinely stopped, which is exactly the fact
        # being confirmed. None the rest of the time, so that branch has
        # nothing to do with the ordinary auto-stop/remote-press cases
        # where nothing is waiting on it.
        self._pending_manual_stop_confirmation: asyncio.Event | None = None

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
                self._attr_is_closed = self._attr_current_cover_position == 0
                _LOGGER.debug(
                    "No previous state for %s (%s); using the position "
                    "calibration actually ended at: %d%%",
                    self._device_name,
                    self._device_id,
                    self._attr_current_cover_position,
                )
            else:
                # Truly nothing to go on: no restored Home Assistant state,
                # and this is not a freshly (re)calibrated blind either -
                # used to default to 0% (closed) here, the same anchor
                # async_open_cover()/async_close_cover() fall back to when
                # a command starts moving a blind from this same unknown
                # state (see their own, identical guard, and _handle_
                # event()'s newly added mirror of it for the remote/
                # Gurtwickler-driven path below). Unlike those, nothing is
                # actually moving yet at this point - the blind is simply
                # sitting idle - so asserting 0% asserted one specific,
                # confident position (closed) that was not actually known
                # to be true. Real-world report (2026-10-31): every blind
                # on a hub showing closed immediately after a Home
                # Assistant restart, for blinds that were not actually
                # closed - traced to exactly this branch, on a restart
                # where Home Assistant's own restored state was not
                # available for any of them (self._attr_current_cover_
                # position and self._attr_is_closed are both already None
                # from __init__, so leaving them untouched here reports
                # the entity's state as unknown, which it genuinely is,
                # instead of a specific wrong answer). This is not a
                # regression in tracking once the blind actually moves -
                # the same 0% anchor is still used, just deferred to the
                # moment a move actually starts and the elapsed-time math
                # genuinely needs a numeric baseline to count from, by
                # whichever path starts it first.
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

        # One subscription per secondary identity (an additional physical
        # remote/wall-switch paired directly to the same motor, distinct
        # from whichever identity is trusted as primary) - each routed to
        # _handle_secondary_activity(), never to _handle_event(). This
        # broadcast was never removed when the close+open elapsed-timer
        # feature built on top of it was (see #116's own revert commits) -
        # only cover.py's subscriber side was, which is why nothing has
        # been listening for it since.
        for secondary_device_id, secondary_enum in self._secondary_status_identities:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_EVENT}_{secondary_device_id}_{secondary_enum}",
                    self._handle_secondary_activity,
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
        # Whatever auto-stop-at-target episode the task being torn down
        # here was mid-retry on (if any) ends with it - see self.
        # _pending_auto_stop_since's own comment in __init__ for why
        # leaving this set would let a later, unrelated episode silently
        # inherit it. Cleared unconditionally, before any of the early
        # returns below, so every caller of this method - not just the
        # ones that happen to still have a live task - gets this for
        # free.
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
        # cancel_pending_move() is the same per-device mechanism
        # async_stop_cover()/the auto-stop-at-target path already
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
        if self._optimistic_secondary_move_unsub is not None:
            self._optimistic_secondary_move_unsub()
            self._optimistic_secondary_move_unsub = None
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
            if not self._has_live_position_tracking() and not self._command_in_flight:
                # Neither a tracking task nor a command of ours is in
                # flight, so this echo can only be *reporting* a move this
                # entity did not itself start - a paired remote control, a
                # wall switch, or a Gurtwickler winder's own local buttons
                # drove it directly - never one merely *confirming* a move
                # already requested through async_open_cover()/async_
                # close_cover()/async_set_cover_position().
                #
                # The second half of that check matters just as much as
                # the first: those three methods do NOT start tracking
                # synchronously - self._command_in_flight's own comment in
                # __init__, and test_async_open_cover_does_not_start_
                # tracking_before_retry_succeeds, explain why tracking only
                # starts once control_blind_with_retry() actually succeeds,
                # which can suspend for a real stretch (hub-wide lock
                # contention, the busy-retry wait) with self._target_position
                # already set - for a set_cover_position() leg specifically
                # - but no tracking task live yet. Checking only
                # _has_live_position_tracking() here would wrongly treat
                # that genuinely in-flight target as stale and null it out
                # from underneath a command this same entity is still in
                # the middle of sending. self._command_in_flight is True
                # for exactly that whole stretch (see the two methods' own
                # try/finally), so the combined check only treats a target
                # as stale once truly nothing of this entity's own is
                # using it any more.
                #
                # Whatever _target_position still holds at that point is
                # therefore stale, left over from an earlier set_cover_
                # position() leg that this entity never saw cleanly
                # finish - plausibly because this same primary status
                # identity's unreliability is also why that move's own
                # STOPPED echo never arrived. Left in place, it silently
                # caps this fresh, independently-triggered move at that
                # old percentage instead of the full travel the remote/
                # switch actually commanded: _async_position_update_loop()
                # auto-stops once the live estimate crosses whatever
                # _target_position still holds, not 100/0, regardless of
                # who or what actually started the move it is now
                # tracking. A remote press driving the blind all the way
                # to its real end stop then reads back from Home Assistant
                # as stuck at the old target's percentage forever -
                # physically fully open/closed, but never shown as such.
                # async_open_cover()/async_close_cover() already guard
                # against this same staleness for a plain open/close
                # reached through Home Assistant instead (see their own,
                # identical comment) - a request that likewise never means
                # anything other than 100/0. This is the same guard,
                # applied at the one remaining path that reaches a fresh
                # move without going through either of them.
                self._target_position = None
            if self._attr_current_cover_position is None:
                # Mirrors async_open_cover()'s/async_close_cover()'s own
                # identical guard for a Home Assistant-issued command
                # starting from this same unknown state (see their own
                # comments) - needed here too now that async_added_to_
                # hass() can leave position genuinely unknown instead of
                # asserting 0% while idle (see its own comment). A move
                # is actually starting now, and _update_position() needs
                # a concrete numeric baseline to count elapsed time from;
                # unlike while idle, there is no option here to simply
                # report "unknown" and leave it there, since nothing else
                # would ever set a real value afterwards either.
                self._attr_current_cover_position = 0
            # Explicitly int | None, matching the attribute's own
            # declared type (see __init__) rather than letting mypy
            # infer a narrower `int` from this branch alone just because
            # the guard above happens to rule out None here - the EVENT_
            # STOPPED branch below assigns this same local from the same
            # attribute without any such guard, which a narrower
            # inferred type here would then wrongly reject there.
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
            # async_stop_cover()'s own identical first line. This status
            # echo is this entity's own ground truth that the motor has
            # genuinely stopped, from *whatever* source: a Home Assistant-
            # issued stop, the auto-stop-at-target loop, or - the one case
            # neither of those cover - a press directly on the physical
            # remote or a Gurtwickler winder's own local buttons, which
            # never goes through this integration's own command path at
            # all and so never calls cancel_pending_move() on its own.
            # Without this, a move's own best-effort resend (control_
            # blind_with_retry(), see MOVE_COMMAND_EXTRA_RESENDS's own
            # comment in const.py) had no way to learn that an externally-
            # originated stop had just landed for this same device and
            # would fire its resend anyway on its own fixed schedule,
            # silently restarting the motor right after a legitimate stop.
            # A no-op whenever no move/resend is actually in progress for
            # this device_id (see cancel_pending_move()'s own docstring),
            # which is the ordinary case for most STOPPED events - in
            # particular, calibration's own status-frame capture never
            # goes through control_blind_with_retry() at all (it uses
            # control_blind_exclusive() instead), so this adds nothing to
            # that flow beyond a guaranteed no-op check.
            self._api.cancel_pending_move(self._command_device_id)
            # Also unconditional, same reasoning as cancel_pending_move()
            # immediately above: whoever (if anyone) is awaiting this
            # entity's own stop confirmation - only ever async_stop_
            # cover(), see self._pending_manual_stop_confirmation's own
            # comment in __init__ - learns about it from *whatever*
            # caused this STOPPED echo, not only a manual stop. A no-op
            # the rest of the time, when nothing is waiting (None).
            if self._pending_manual_stop_confirmation is not None:
                self._pending_manual_stop_confirmation.set()
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
            # target set therefore *usually* means the blind was interrupted
            # externally (e.g. the physical remote), in which case the
            # commanded target was never reached and snapping to it would
            # report a false position instead of where the blind actually
            # stopped.
            self._update_position()
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
                    # An untargeted move (always a remote/wall-switch/
                    # Gurtwickler-winder press - see NEAR_END_STOP_SNAP_
                    # TOLERANCE_PERCENT's own comment in const.py) landing
                    # this close to the edge it was already heading toward
                    # is only treated as calibration drift against a real
                    # physical end stop - and snapped the rest of the way -
                    # when it *also* started this same move from near
                    # enough the opposite edge to plausibly be a genuine
                    # full-range traversal (self._move_start_position,
                    # still set at this point - see its own clearing a few
                    # lines below). A first version of this check used
                    # position alone, which a direct user report (2026-
                    # 10-31) caught also snapping a deliberately chosen
                    # near-edge stop - e.g. nudging a blind open to a
                    # chosen 98% from some other, non-edge position -
                    # straight to 100%, silently overriding the exact
                    # position the remote's own stop button was pressed
                    # to reach. A move that did not start near the
                    # opposite edge is left exactly at its raw elapsed-
                    # time estimate instead, same as any other mid-travel
                    # stop.
                    self._attr_current_cover_position = (
                        0 if self._attr_is_closing else 100
                    )
                self._attr_is_closed = self._attr_current_cover_position == 0
            self._attr_is_opening = False
            self._attr_is_closing = False
            # This log used to run *before* the _update_position()
            # recalculation (and clamp) above, right
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
            # self._attr_current_cover_position can legitimately be None
            # here - not just pre-calibration/at startup, but also a late,
            # real STOPPED confirmation arriving for a device whose auto-
            # stop-at-target episode already gave up and reported the
            # position unknown (see AUTO_STOP_CONFIRMATION_BACKSTOP_
            # SECONDS's own exhaustion branch above). "%d%%" on None
            # raises inside the logging module's own lazy formatting -
            # caught and reported as a logging error rather than
            # propagating, so this never crashed anything, but it did
            # spam a confusing traceback into the log on exactly the late
            # STOPPED this handler most needs to report cleanly. Found
            # during an independent review (2026-11-01) of the backstop
            # mechanism that first made this reachable through a normal
            # operating path rather than only at startup.
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
            )
            self._move_start_time = None
            self._move_start_position = None
            if not self._has_live_position_tracking() and not self._command_in_flight:
                # The "usually external interruption" reasoning above has one
                # gap: a set_cover_position() leg that reverses direction
                # while a *different*, superseded move is still live awaits
                # _async_cancel_position_tracking() to tear that move's own
                # tracking down - a real suspension point - after already
                # setting self._target_position to its own, brand new target
                # (see async_set_cover_position()). That method clears self.
                # _position_update_task synchronously, before its own await,
                # so _has_live_position_tracking() alone cannot tell this
                # window apart from genuine idle - exactly the same gap
                # test_handle_event_during_a_pending_command_does_not_clear_
                # its_target already proved for the STARTED_MOVING branch.
                # If the superseded move's own genuine STOPPED echo lands in
                # that window, it is not reporting an external interruption
                # of the new leg at all - it is only confirming the move the
                # new leg is itself in the middle of superseding - so it
                # must not null out a target that belongs to that different,
                # still in-flight command. self._command_in_flight (set for
                # async_open_cover()'s/async_close_cover()'s entire duration)
                # is what tells the two apart here, the same as it already
                # does for STARTED_MOVING.
                self._target_position = None
        else:
            _LOGGER.debug(
                "Device %s received unknown event: %s", self._device_name, event
            )

        self.async_write_ha_state()

    @callback
    def _handle_secondary_activity(self, _event: str) -> None:
        """React to a secondary identity's frame with an optimistic guess.

        Deliberately never inspects _event (the decoded command, if any):
        a secondary frame's own command byte has been tried twice as a way
        to learn its real direction/stop meaning, on real Gurtwickler-
        remote hardware, and failed both times in sustained real-world use
        after initially looking promising in a smaller sample (commits
        e911236 and 1db9e69) - so that byte is no longer trusted for
        anything here. All this reacts to is the fact that *something*
        arrived at all, which needs no decoding and is never wrong.

        With nothing decoded, the direction shown is a guess: toward the
        farther end stop from the current known position, on the
        assumption that a remote press most often means "finish the job"
        (already mostly open -> guess closing; already mostly closed ->
        guess opening) rather than a fine intermediate adjustment. This is
        wrong often enough that it must never be allowed to stand
        unconfirmed - which is exactly why it only ever touches
        is_opening/is_closing, never _move_start_time/_move_start_position/
        _target_position or the tracking task itself: _has_live_position_
        tracking() and _command_in_flight therefore both still read exactly
        as if nothing had happened, so when the primary identity's own
        echo of this same physical move arrives - reliably decoded, as
        always - _handle_event()'s STARTED_MOVING branch takes it over
        with zero changes of its own, overwriting this guess with the
        real direction and starting real tracking. If that echo never
        arrives at all (the same real-world RF loss this project's own
        debug logs have shown happens occasionally), _clear_optimistic_
        secondary_move() below withdraws the guess on its own after
        OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS instead of leaving it
        standing forever.
        """
        if self._has_live_position_tracking() or self._command_in_flight:
            # A real move (HA-issued or already picked up from the
            # primary identity's own echo) already owns is_opening/
            # is_closing - never guess over it.
            return
        if self._optimistic_secondary_move_unsub is not None:
            # Already showing a guess for this same burst (a remote
            # commonly repeats its frame every ~400ms while held) -
            # the existing guess and its timeout stand as they are.
            return
        if self._attr_current_cover_position is None:
            # No position baseline yet to guess a direction from.
            return
        guessed_opening = self._attr_current_cover_position < 50
        self._attr_is_opening = guessed_opening
        self._attr_is_closing = not guessed_opening
        _LOGGER.info(
            "Device %s secondary identity activity: optimistic guess "
            "%s (position %d%%, to be confirmed or corrected by the "
            "primary identity's own echo, or withdrawn after %.0fs if "
            "none arrives)",
            self._device_name,
            "opening" if guessed_opening else "closing",
            self._attr_current_cover_position,
            OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS,
        )
        self._optimistic_secondary_move_unsub = async_call_later(
            self.hass,
            OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS,
            self._clear_optimistic_secondary_move,
        )
        self.async_write_ha_state()

    @callback
    def _clear_optimistic_secondary_move(self, _now: datetime) -> None:
        """Withdraw an optimistic guess that nothing ever confirmed."""
        self._optimistic_secondary_move_unsub = None
        if self._has_live_position_tracking() or self._command_in_flight:
            # Either the primary identity's own echo already landed and
            # _handle_event() replaced this guess with the real, decoded
            # direction and started real tracking, or a genuine Home
            # Assistant open/close for this same entity is itself still in
            # flight and has already set its own optimistic is_opening/
            # is_closing before this timeout fired. The second half
            # matters just as much as the first, and for exactly the same
            # reason _handle_event() itself checks both: async_open_
            # cover()/async_close_cover() set that state synchronously,
            # but their own tracking task only starts once control_blind_
            # with_retry() actually succeeds (self._command_in_flight's
            # own comment in __init__), so _has_live_position_tracking()
            # alone can read False for a real stretch - hub-wide lock
            # contention, the busy-retry wait - while that command's own
            # optimistic state is still the genuinely current one.
            # _handle_secondary_activity() already guards its own, initial
            # guess against exactly this same window; checking only
            # tracking liveness here, on the withdrawal side, would let
            # this timeout fire mid-window and wrongly pull a real,
            # in-flight command's state back down to idle a few seconds
            # into it. Nothing of this guess's own is left to withdraw
            # either way, so leave the real state alone and return.
            return
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.async_write_ha_state()

    def _has_live_position_tracking(self) -> bool:
        """Return whether the position-tracking task is already live.

        "Live" means actually still running, not merely sitting in the
        slot uncleared (done, or in the process of being cancelled) -
        the same distinction _has_live_tracking_in_direction() draws for
        its own, narrower purpose. Used here by _start_position_tracking()
        itself to decide whether a (re)start is needed, and by
        _handle_event(), *alongside* self._command_in_flight - not
        alone - to tell an echo that merely *confirms* a move this entity
        already requested (a Home Assistant open/close/set_position
        command) apart from one *reporting* a move it did not. This alone
        is not sufficient for that: a Home Assistant command's own
        tracking only starts once its own control_blind_with_retry() call
        actually succeeds, not from the moment it is called (see
        self._command_in_flight's own comment in __init__, and
        test_async_open_cover_does_not_start_tracking_before_retry_
        succeeds) - so this can read False for a stretch while a
        set_cover_position() leg's own, still genuinely current target is
        already set. See _handle_event()'s own comment for the full
        reasoning on why both checks together are what decides whether a
        lingering _target_position is still legitimate or merely stale.
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
        # See the identical comment in _async_cancel_position_tracking()
        # just above - same reasoning, same fix, for this method's own,
        # separate, fire-and-forget cancellation path.
        self._pending_auto_stop_since = None
        self._pending_auto_stop_target = None
        task = self._position_update_task
        if task is not None and not task.done():
            task.cancel(reason)
        if self._position_update_task is task:
            self._position_update_task = None

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
        """Record that reaching a travel limit makes the position trustworthy.

        Called from three places in _async_position_update_loop(): its two
        plain-open/close branches (target_position is None) once they
        reach 0/100 with that direction's own full-travel resync actually
        pending, and its third, explicit-target branch when a
        set_cover_position(0)/set_cover_position(100) call reaches that
        exact same physical endstop instead - see that branch's own
        comment for why it is treated identically.

        self._position_confirmed_since_restart is set unconditionally now,
        not only when self._full_travel_resync_direction == direction:
        reaching a real endstop through a full, elapsed-time-tracked
        transit is exactly as trustworthy a confirmation regardless of
        whether *this* call's own direction happens to be the one
        self._full_travel_resync_direction currently names. The set_
        cover_position(0)/(100) call site never arms that attribute in
        the first place - async_open_cover()'s/async_close_cover()'s own
        ternary only ever does when self._target_position is None, which
        a set_cover_position() leg's own _preserve_target=True never
        leaves true - so gating the flag itself on a match here left it
        permanently False for a device whose first interaction after a
        restart was a set_cover_position(0)/(100) call (a dashboard's own
        position slider dragged to an end, say) rather than a plain
        open/close. Confirmed reaching that exact endstop never actually
        set it, so the very next plain open_cover()/close_cover() - on
        either side, both share this same ternary - treated position as
        still unconfirmed all over again and sat out the *entire* nominal
        travel time before admitting what this call had already shown to
        be true, reporting "opening"/"closing" for that whole stretch
        despite already sitting at the target the whole time. A plain
        open/close reached after self._position_confirmed_since_restart
        is already True is unaffected either way - the flag was already
        True, so setting it again here is a no-op.

        The resync-specific cleanup below - clearing self._full_travel_
        resync_direction and recording the "estimated from full travel"
        diagnostic - stays scoped to a genuinely matching, actively-
        pending resync, same as before this fix: a mismatch is otherwise
        never actually reachable for the two plain-open/close call sites
        (self._full_travel_resync_direction and self._attr_is_opening/
        self._attr_is_closing are always assigned together, by the same
        ternary, in the same call, in whichever body method is running),
        and is reachable for the set_cover_position(0)/(100) call site
        only in the sense just explained above - nothing pending there to
        clear or report in that case either.
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
        )

    async def _async_position_update_loop(self) -> None:
        """Poll and estimate position every 0.2s while the cover is moving."""
        position_task = asyncio.current_task()
        # Captured once, here, rather than re-based on any later retarget -
        # see POSITION_UPDATE_LOOP_TIMEOUT_SECONDS's own comment in const.py
        # for why this loop's own total running time, across however many
        # targets it ends up tracking in a row (a same-direction retarget
        # keeps this same task running - see its own "continue" below),
        # is what gets bounded, rather than the time since whichever target
        # happens to be current right now.
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
                    # Outer safety net - see POSITION_UPDATE_LOOP_TIMEOUT_
                    # SECONDS's own comment in const.py. Everything below
                    # mirrors the AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS-
                    # exhausted cleanup further down (same "stop trusting a
                    # stale estimate, report unknown instead" outcome), plus
                    # clearing self._full_travel_resync_direction too, since
                    # unlike that narrower branch this one can fire from any
                    # state this loop was in, including a plain open/close
                    # still waiting on its own full-travel resync.
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
                    # An intermediate target (unlike 0/100, see
                    # needs_stop_command below) is only ever reached
                    # because a stop command sent from here makes it so -
                    # and three distinct real delays sit between "software
                    # decides to stop" and "motor actually stops at
                    # target":
                    #
                    # - D_start: the real motor doesn't start moving the
                    #   instant the stick accepts this move's primary
                    #   open/close write - that takes roughly STOP_COMMAND_
                    #   LATENCY_SECONDS (see its own comment in const.py).
                    # - T_stamp: _move_start_time, though, is stamped only
                    #   once control_blind_with_retry() (api.py) returns -
                    #   now effectively 0, immediately on the primary
                    #   write's own ack. Used to be MOVE_COMMAND_EXTRA_
                    #   RESENDS * MOVE_COMMAND_RESEND_DELAY_SECONDS (~0.5s)
                    #   after that same write instead, back when that
                    #   method's own resend loop still ran inline, before
                    #   its own return - see MOVE_COMMAND_EXTRA_RESENDS's
                    #   own comment in const.py for why and since when that
                    #   changed.
                    # - D_stop: once the stop command below is actually
                    #   issued, the real motor doesn't stop instantly
                    #   either - transmitting and acting on it takes
                    #   roughly that same STOP_COMMAND_LATENCY_SECONDS,
                    #   during which the blind keeps moving under the
                    #   already-issued open/close command.
                    #
                    # D_start > T_stamp, so software's elapsed-time
                    # estimate runs ahead of the real motor by a constant
                    # (D_start - T_stamp) for the rest of the move - by
                    # itself, that makes the real landing undershoot
                    # target. D_stop, separately, keeps the real motor
                    # moving on after the stop decision - by itself, that
                    # makes it overshoot. Working the two through the
                    # actual arithmetic (not just comparing their
                    # magnitudes) shows they enter the needed lead with
                    # opposite sign, so when D_start and D_stop are equal -
                    # true uncontended, since both are the same measured
                    # STOP_COMMAND_LATENCY_SECONDS - they cancel
                    # completely, leaving only T_stamp itself (software's
                    # own resend-loop bookkeeping delay, nothing to do
                    # with hardware transmit latency) for this lead to
                    # compensate for - which, T_stamp now being ~0, is
                    # itself ~0: an uncontended intermediate stop needs no
                    # lead at all any more.
                    #
                    # An earlier version of this lead (Issue #78,
                    # 2026-11-07) used 2x STOP_COMMAND_LATENCY_SECONDS as
                    # its baseline instead, on the reasoning that
                    # D_start's and D_stop's contributions should be
                    # *added*. They don't add, they cancel - that version
                    # undershot intermediate targets by 7 to 20+ points
                    # depending on travel time, confirmed both by an
                    # independent review and by a from-scratch, tick-
                    # accurate simulation mirroring this loop's own 0.2s
                    # polling and _update_position()'s int()-floored
                    # estimate.
                    #
                    # Uncompensated altogether, this is what made any
                    # target within roughly this fraction of a fast
                    # blind's travel time of either end stop unreachable -
                    # the earliest a stop could physically land was
                    # already past it.
                    #
                    # T_stamp's own ~0.5s baseline above (2026-11-07
                    # through 2026-11-08) was later found to be the same
                    # root cause behind a separate, unrelated-looking real-
                    # world report: a full open/close (needs_stop_command
                    # False below, so this whole branch never even runs
                    # for it) landing on 92%/4% instead of 100%/0%, plus a
                    # separately-reported sluggish dashboard - both from
                    # control_blind_with_retry()'s own resend loop delaying
                    # _move_start_time's own stamp on *that* path too,
                    # eating into a different margin entirely (see
                    # MOVE_COMMAND_EXTRA_RESENDS's own comment in
                    # const.py). Fixing that by detaching the resends into
                    # a background task zeroes T_stamp here as well, which
                    # is why this baseline is gone outright below rather
                    # than merely recomputed to some other, still-nonzero
                    # value.
                    needs_stop_command = self._target_position not in (0, 100)
                    stop_lead_percent = 0.0
                    if needs_stop_command:
                        travel_time_for_lead = max(
                            self._travel_time_open
                            if self._attr_is_opening
                            else self._travel_time_close,
                            0.1,
                        )
                        # Baseline is T_stamp alone (see the derivation
                        # above), and T_stamp is ~0 now - so there is
                        # deliberately no uncontended term here at all any
                        # more. STOP_COMMAND_LATENCY_SECONDS alone still
                        # isn't it either: uncontended, that constant's two
                        # appearances (D_start, D_stop) still cancel out of
                        # this baseline exactly, since they're still equal.
                        stop_latency_seconds = 0.0
                        # When the shared lock is held by some *other*
                        # blind's own move or stop right now, this blind's
                        # own stop - though served ahead of anyone merely
                        # *waiting* for the lock (see move_lock_busy's own
                        # comment in api.py) - still has to wait out
                        # whatever that other cycle has left, which can
                        # itself take up to about one more full cycle:
                        # D_stop grows by one more STOP_COMMAND_LATENCY_
                        # SECONDS while D_start doesn't, so unlike the
                        # uncontended baseline above this no longer
                        # cancels and has to be added back on top of it.
                        # Real log evidence (2026-10-31): an uncontended
                        # stop reached the wire in 0.39s; a contended one,
                        # queued behind another blind's own stop, took
                        # 1.37s - almost exactly one extra STOP_COMMAND_
                        # LATENCY_SECONDS - and that second blind overshot
                        # its target by close to the matching percentage.
                        # Checked fresh on every tick, so this only widens
                        # the lead for ticks where contention is actually
                        # observed, not unconditionally for every stop.
                        if self._api.move_lock_busy:
                            stop_latency_seconds += STOP_COMMAND_LATENCY_SECONDS
                        # travel_time_for_lead's own floor just above only
                        # keeps this division from raising or exploding -
                        # on its own it leaves the result with no matching
                        # ceiling. See MAX_STOP_LEAD_PERCENT's own comment
                        # in const.py for why an unbounded lead here is not
                        # just a cosmetically large number: past 100 it
                        # makes the position_reached check below satisfied
                        # by any position at all, so a short/corrupted
                        # travel time used to stop the blind the instant
                        # this loop was entered, however far it actually
                        # was from target_position.
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
                        self._attr_current_cover_position = self._target_position
                        _LOGGER.info(
                            "Device %s reached target position (%d%%)",
                            self._device_name,
                            self._target_position,
                        )
                        if needs_stop_command:
                            # Snapshot the target this stop is actually
                            # for, taken before the await below - see the
                            # re-check just after that same await for why.
                            if (
                                self._pending_auto_stop_since is None
                                or self._pending_auto_stop_target
                                != self._target_position
                            ):
                                # Either genuinely the first attempt for
                                # this episode, or - belt and suspenders
                                # alongside _async_cancel_position_
                                # tracking()/_stop_position_tracking()'s
                                # own clearing - a stale timestamp left
                                # over from a *different* target whose
                                # own episode ended without going through
                                # either of those (e.g. this same task
                                # continuing after self._target_position
                                # changed straight to None). Either way,
                                # this target has never had its own clock
                                # running yet, so start one now rather
                                # than silently inheriting however much
                                # of another episode's budget is already
                                # spent. See self._pending_auto_stop_
                                # since's own comment in __init__ for the
                                # full reasoning.
                                self._pending_auto_stop_since = time.monotonic()
                                self._pending_auto_stop_target = self._target_position
                            target_when_stop_was_sent = self._target_position
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
                            stop_outcome = await self._api.control_blind_stop(
                                self._command_enum,
                                device_id=self._command_device_id,
                            )
                            if stop_outcome is not StopOutcome.CONFIRMED:
                                # Neither FAILED (not even the fallback
                                # write reached the wire) nor UNCONFIRMED
                                # (it did, but nothing confirms the stick -
                                # let alone the motor - ever saw it) is
                                # safe to treat as a successful stop here.
                                # Doing so would clear is_opening/is_closing/
                                # the target while the motor may still be
                                # moving (toward this target, or now its
                                # real end stop with nothing having told it
                                # otherwise), and would discard this same
                                # loop's own ability to act once a stop -
                                # this retry, or the motor's own end-stop -
                                # actually does land, since the tracking
                                # state would already be torn down. Leave
                                # it untouched and retry next tick instead,
                                # up to AUTO_STOP_CONFIRMATION_BACKSTOP_
                                # SECONDS total (see that constant's own
                                # comment in const.py) - extended from
                                # FAILED-only to also cover
                                # UNCONFIRMED after a real six-blind Alexa
                                # "25%" command (2026-11-01) left one blind
                                # fully closed in reality while this entity
                                # kept reporting the stale percentage it
                                # had at the moment an UNCONFIRMED fallback
                                # write was wrongly trusted as a done stop.
                                if (
                                    time.monotonic() - self._pending_auto_stop_since
                                    >= AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS
                                ):
                                    # Re-check rather than trust the
                                    # snapshot taken before *this attempt's*
                                    # own await, same idiom as the
                                    # CONFIRMED sibling check a few lines
                                    # below - see its own comment. A same-
                                    # direction retarget landing during
                                    # this specific attempt's await already
                                    # pointed self._target_position at its
                                    # own new value; falling through to the
                                    # unconditional clear below would wipe
                                    # that still-live target out from under
                                    # it and then end this task via the
                                    # return below - the #226/#227 bug
                                    # class again, reached this time via
                                    # 30s of sustained backstop retries
                                    # landing on the same tick as a
                                    # retarget, rather than a direction
                                    # reversal racing a manual stop.
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
                                # A same-direction retarget (a fresh
                                # set_cover_position() call in the same
                                # direction, typically via
                                # async_open_cover()/async_close_cover()'s
                                # own is_redundant_resend piggyback path)
                                # landed while the await above was in
                                # flight, and already pointed
                                # self._target_position at its own new
                                # value - is_redundant_resend there means
                                # that call deliberately left this same
                                # loop/task running rather than starting
                                # its own, on the assumption that this
                                # task would keep tracking it. Falling
                                # through to the unconditional clear below
                                # would wipe that still-live target/
                                # is_opening/is_closing/_move_start_* right
                                # out from under it and then end this task
                                # via the return below - leaving the new
                                # leg's own move physically continuing
                                # with nothing left tracking or auto-
                                # stopping it at all. The stop just sent
                                # above was for the old target and is
                                # harmless regardless of which target is
                                # now current - the motor only ever sees
                                # a bare stop pulse, never a position - so
                                # simply continue instead: _move_start_time/
                                # _move_start_position are still exactly
                                # right for the same direction, and the
                                # next tick re-evaluates position_reached
                                # against whatever self._target_position
                                # has become, including None for a plain
                                # open()/close() that superseded this leg.
                                continue
                        else:
                            # A plain open_cover()/close_cover() reaching 0/100
                            # confirms this via _confirm_full_travel_resync()
                            # (see the target_position-is-None branch below) -
                            # set_cover_position(0)/set_cover_position(100)
                            # reaching the exact same physical endstop through
                            # this branch instead used to never do so in
                            # practice, even once this call was added here:
                            # self._full_travel_resync_direction is never
                            # armed for this branch in the first place (see
                            # async_open_cover()'s/async_close_cover()'s own
                            # ternary, gated on self._target_position is
                            # None), so this call's own direction could never
                            # match it and _confirm_full_travel_resync()'s own
                            # guard quietly no-opped every time - leaving
                            # _position_confirmed_since_restart stuck False
                            # after a restart whenever the first interaction
                            # happened to be a set_cover_position(0)/(100)
                            # call (e.g. a dashboard's position slider dragged
                            # to an end) instead of a plain open/close, even
                            # though the live position reported above was
                            # identical either way. _confirm_full_travel_
                            # resync() now sets that flag unconditionally -
                            # see its own docstring - so this call finally
                            # does what this comment always said it did.
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

        # Never negative. Nothing in production currently pushes self.
        # _move_start_time into the future - async_open_cover()/async_
        # close_cover() used to, to model the same transmit-to-motor-
        # action dead time STOP_COMMAND_LATENCY_SECONDS already measures,
        # but that push was reverted (Issue #78, 2026-11-07): it fixed the
        # intermediate-target overshoot it was added for, but froze the
        # reported position for just over a second at the start of every
        # single move in exchange, a regression of its own. See both call
        # sites' own comments, and STOP_COMMAND_LATENCY_SECONDS's own
        # comment in const.py for where that compensation moved instead.
        # This clamp stays as a defensive guard against a negative
        # elapsed_time regardless - clock weirdness, or some future
        # caller setting _move_start_time ahead of now for a different
        # reason - rather than letting total_position_change run
        # backwards past _move_start_position.
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
            # target *this* call was asked to preserve (a set_cover_
            # position() leg, _preserve_target=True): with no move
            # actually under way, there is nothing left for that target to
            # describe, and leaving it stale let a later, completely
            # unrelated move - typically the original remote control -
            # read it back via _async_position_update_loop() and get
            # auto-stopped at that long-invalid position.
            #
            # Gated on _preserve_target specifically - not unconditional -
            # because self._target_position is shared, per-entity state: a
            # plain open (_preserve_target=False) never owns a target
            # concept to begin with (any stale one of its own was already
            # dropped at the top of this method, _preserve_target already
            # False there too), so it has no legitimate claim on whatever
            # is sitting in self._target_position by the time it reaches
            # this point. If a *different*, still-genuinely-live set_
            # cover_position() leg's own target landed there in the
            # meantime - this call's own await above is a real suspension
            # point such a leg's first synchronous write (self._target_
            # position = target_position in async_set_cover_position())
            # can land inside - clearing it unconditionally here clobbered
            # that other leg's legitimate target for no reason of this
            # call's own. The redundant-resend branch above (where the
            # original move's own target is still genuinely live) already
            # returned before reaching this point, so that case needs no
            # separate guard here.
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
            # Plain time.monotonic(), not pushed into the future - see
            # stop_lead_percent's own comment in _async_position_update_
            # loop() below for where that compensation lives instead now
            # and why (Issue #78, 2026-11-07). This WAS pushed
            # STOP_COMMAND_LATENCY_SECONDS ahead for a while (see git
            # history on this line for that version's own reasoning, still
            # correct as far as it went): the stick accepting this write is
            # not the same instant the motor physically starts moving, and
            # leaving that gap uncompensated made the auto-stop-at-target
            # branch below fire measurably early for an intermediate
            # target (real log evidence, 2026-10-31: two blinds targeting
            # an intermediate position stopped at ~82% after only
            # ~2.2-2.6s). That push fixed the overshoot but introduced a
            # new, user-visible problem of its own (Issue #78): for up to
            # STOP_COMMAND_LATENCY_SECONDS after every single open/close
            # command, _update_position()'s clamp (max(0.0, ...)) holds the
            # reported position frozen at the start position even though
            # the motor may already be genuinely moving - every move
            # appearing to "hang" for just over a second before the
            # dashboard shows any progress at all. Reverting this side and
            # compensating on the stop side instead moves the same fix to
            # the one place it only costs accuracy on an intermediate
            # stop's own exact trigger instant - already compensated for
            # on purpose - rather than costing responsiveness on every
            # single move, intermediate target or not. The STARTED_MOVING
            # branch in _handle_event() still (re)sets this to plain
            # time.monotonic() of its own accord when a real echo confirms
            # the move, same as always - that was already a no-op against
            # this plain assignment even before, so nothing changes there.
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
            # target *this* call was asked to preserve - gated on
            # _preserve_target, not unconditional - see async_open_
            # cover()'s mirror of this point for the full reasoning on why
            # a plain close has no legitimate claim on whatever is
            # sitting in the shared self._target_position by this point.
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
            # Plain time.monotonic() - see async_open_cover()'s own mirror
            # of this exact point for why (Issue #78, 2026-11-07).
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

        control_blind_stop() now returns a StopOutcome rather than a bare
        bool (see that type's own docstring in api.py). FAILED is handled
        exactly as the old bare False always was, below. UNCONFIRMED is
        new: a write went out, but nothing confirms the stick - let alone
        the motor - ever received it. This method does not retry further
        on UNCONFIRMED the way the auto-stop-at-target loop now does for
        the same outcome (see AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS in
        const.py) - a manual stop press must stay responsive, not wait up
        to 30s for a confirmation that may never come. What it does do is
        stop pretending the resulting estimate is as trustworthy as a
        confirmed stop's: see the StopOutcome branch after the position
        read below.

        CONFIRMED only ever meant the stick accepted the write - motor_
        result stays unknown either way, same gap STOP_COMMAND_EXTRA_
        RESENDS's own comment in const.py already describes. On top of
        that, this now additionally waits - briefly, see MANUAL_STOP_
        MOTOR_CONFIRMATION_GRACE_SECONDS's own comment in const.py - for
        this same blind's own primary-identity SS status-frame to echo
        the stop back through _handle_event()'s EVENT_STOPPED branch,
        which sets self._pending_manual_stop_confirmation the instant it
        runs. When that frame arrives in time, _handle_event() has by
        then already done the complete, more-authoritative update itself
        (position_source_kind, _update_position(), tracking torn down,
        its own _record_position_update(status="confirmed")) - so this
        method does nothing further for that case beyond publishing state
        and logging which path was taken. On timeout - no frame, most
        plausibly real RF interference rather than any genuine failure -
        this falls through to the stick-ack-only handling below that
        existed before this wait did, so a dropped motor echo can never
        hang or skip a command, only cost this one extra piece of
        confidence - except that it first re-checks, the same way
        async_open_cover()/async_close_cover() already re-check their own
        snapshots after their own awaits (see _has_live_tracking_in_
        direction()'s docstring), whether a newer open/close call has
        already taken this device over in the meantime (see was_opening_
        before_stop/was_closing_before_stop below and the fallback
        branch's own comment) - real RF interference is not the only way
        this wait can time out, and the two are indistinguishable from
        inside it. #226/#227: without that re-check, a stop whose own
        echo lost the shared radio to a newer command's retransmissions -
        exactly what reversing direction quickly enough to still be
        inside this grace period causes - clobbered that newer command's
        state once this wait finally gave up on it.
        """
        # First, and before any other statement: if this device has a move
        # retry loop still running (control_blind_with_retry(), in flight
        # because the stick is busy with something else), end it now so it
        # cannot resend the open/close command after the stop below has
        # already gone out. Called this early, with no await beforehand, so
        # there is no window in which this task could yield to that retry
        # loop before the cancellation is actually requested.
        self._api.cancel_pending_move(self._command_device_id)
        _LOGGER.debug(
            "Stopping cover %s (command_id=%s enum=%s)",
            self._device_name,
            self._command_device_id,
            self._command_enum,
        )
        previous_target_position = self._target_position
        self._target_position = None
        # Created before control_blind_stop() is even called, not after -
        # that call's own internal waits (lock, transmitter-idle, ack) are
        # genuine suspension points this task can yield across, during
        # which the real serial read loop is free to receive and dispatch
        # this same blind's own STOPPED echo. Creating this any later could
        # miss exactly that race and wait out the full grace period below
        # for a frame that, in truth, already arrived.
        self._pending_manual_stop_confirmation = asyncio.Event()
        # Snapshot which direction (if any) this device was already
        # reporting before the write and wait below - taken at this same
        # "before any await" point as self._pending_manual_stop_
        # confirmation just above, and for the same reason. The fallback
        # branch near the end of this method (see its own comment there)
        # compares against this afterwards to recognise when a different,
        # newer async_open_cover()/async_close_cover() call has already
        # taken over this device while this call was suspended below -
        # that newer call sets self._attr_is_opening/self._attr_is_closing
        # for itself at its own very first line, with no await of its own
        # first, so this is observable the instant it happens even though
        # its own tracking task is only created much later.
        was_opening_before_stop = self._attr_is_opening
        was_closing_before_stop = self._attr_is_closing
        stop_outcome = await self._api.control_blind_stop(
            self._command_enum, device_id=self._command_device_id
        )
        if stop_outcome is StopOutcome.FAILED:
            # The stop never reached the stick - not even the uncertain
            # fallback write control_blind_stop() falls back to internally.
            # Nothing else has been touched yet - position tracking, if it
            # was running, still is, and is_opening/is_closing/
            # _move_start_time/_move_start_position are exactly as they
            # were, so there is nothing to restore and nothing to restart
            # for those.
            #
            # The target is different: this method actively mutated it
            # (cleared to None, above) before the await that just ended, so
            # restoring it unconditionally would repeat the exact mistake
            # #226/#227 fixed a few lines below for is_opening/is_closing -
            # re-check rather than trust the snapshot taken before the
            # await, the same idiom was_opening_before_stop/was_closing_
            # before_stop use there. If a different, newer async_set_cover_
            # position()/async_open_cover()/async_close_cover() call
            # already claimed the target slot for its own, genuinely live
            # move while control_blind_stop() above was suspended, self.
            # _target_position is no longer the None this method itself put
            # there - restoring previous_target_position on top of it would
            # clobber that newer call's own legitimate target with this
            # stale, now-irrelevant one instead.
            self._pending_manual_stop_confirmation = None
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

        motor_confirmed = False
        if stop_outcome is StopOutcome.CONFIRMED:
            # UNCONFIRMED deliberately skips this wait entirely - see this
            # method's own docstring above for why staying responsive
            # matters more there than any extra confidence this could add
            # on top of a stick that never even confirmed the write.
            wait_started = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._pending_manual_stop_confirmation.wait(),
                    timeout=MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_SECONDS,
                )
                motor_confirmed = True
                _LOGGER.debug(
                    "Device %s: stop additionally confirmed by its own "
                    "motor status frame after %.3fs",
                    self._device_name,
                    time.monotonic() - wait_started,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "Device %s: no motor status frame confirmed this stop "
                    "within %.1fs (likely RF interference) - falling back "
                    "to the stick's own acceptance of the write",
                    self._device_name,
                    MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_SECONDS,
                )
        self._pending_manual_stop_confirmation = None
        if motor_confirmed:
            # _handle_event()'s own EVENT_STOPPED branch already did the
            # complete, more-authoritative update by the time the wait
            # above resolved - see this method's own docstring. Nothing
            # left to do here except publish whatever state that branch
            # already set.
            self.async_write_ha_state()
            return

        if (
            self._attr_is_opening != was_opening_before_stop
            or self._attr_is_closing != was_closing_before_stop
        ):
            # Regression guard for #226/#227: a different, newer
            # async_open_cover()/async_close_cover() call already started
            # for this same device while this call was suspended above
            # waiting out MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_SECONDS for
            # an echo that never came - confirmed from a real debug log
            # where the user reversed direction (tapped open again)
            # within that same window, which is also exactly what starved
            # this stop's own echo of a clear RF channel in the first
            # place (the newer move's own retransmissions were going out
            # on the same shared radio at that moment). That newer call
            # has already set up its own is_opening/is_closing,
            # _move_start_time/_move_start_position and tracking task for
            # itself, and - if it has itself already completed - may also
            # already have torn all of that back down again correctly on
            # its own. Either way, falling through to the cleanup below
            # regardless of which move it was originally written for
            # would cancel tracking, clear is_opening/is_closing and
            # move_start_*, and publish a position - all for whichever
            # move happens to be current *now*, not the one this call was
            # actually trying to stop. That silently froze the dashboard
            # at a stale estimate until the next command, which is what
            # read as the position "jumping" on every quick reversal.
            # Nothing left for this call to do - the newer move already
            # is, or will be, publishing its own correct state.
            _LOGGER.debug(
                "Device %s: stop confirmation timed out, but a newer "
                "move already started for this device in the meantime - "
                "leaving its state alone instead of overwriting it with "
                "this now-stale stop's own fallback cleanup",
                self._device_name,
            )
            return

        # Only now, with a write actually done, is it safe to stop the
        # background tracking loop and read its estimate - see this
        # method's own docstring for why reading it any earlier would risk
        # a stale, prematurely frozen position under contention.
        previous_position = self._attr_current_cover_position
        confirmed = stop_outcome is StopOutcome.CONFIRMED
        self._position_source_kind = "HA command" if confirmed else "unconfirmed stop"
        self._full_travel_resync_direction = None
        self._position_update_source = "Home Assistant stop command"
        await self._async_cancel_position_tracking("Home Assistant stop command")
        self._update_position()
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._move_start_time = None
        self._move_start_position = None
        if not confirmed:
            # UNCONFIRMED: logged, not retried - see this method's own
            # docstring for why staying responsive matters more here than
            # it does for the background auto-stop-at-target loop. The
            # estimate below is reported exactly as it always was (this
            # never silently invents an "unknown position" state nothing
            # asked for); only the confidence behind it changes, reflected
            # here and in _position_source_kind above rather than logged
            # identically to a confirmed stop the way this did before
            # StopOutcome existed.
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
