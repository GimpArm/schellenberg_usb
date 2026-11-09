"""The Schellenberg USB Stick integration."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .api import SchellenbergUsbApi
from .blind_id import claim_blind_id
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENUM,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_TEST_COMMAND,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_BLIND,
    SUBENTRY_TYPE_HUB,
    SchellenbergConfigEntry,
)
from .device_registry_compat import (
    async_device_on_subentry_compat,
    async_get_device_by_identifier_compat,
    async_reassign_device_subentry_compat,
)
from .runtime_translation_text import (
    runtime_translation_text as _runtime_translation_text,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _async_backfill_blind_ids(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
    """Assign every blind subentry a stable, collision-free blind ID.

    Needed for subentries created before the stable blind-ID migration
    (or restored from an export that predates it), which have no
    `CONF_BLIND_ID` of their own yet. Persists the assigned ID onto
    the subentry so it survives this call. Returns whether any
    subentry was actually changed.
    """
    used_ids: set[str] = set()
    changed = False
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type != SUBENTRY_TYPE_BLIND:
            continue
        blind_id, needs_update = claim_blind_id(
            subentry.data.get(CONF_BLIND_ID), used_ids
        )
        if not needs_update:
            continue
        data = dict(subentry.data)
        data[CONF_BLIND_ID] = blind_id
        hass.config_entries.async_update_subentry(entry, subentry, data=data)
        changed = True
        _LOGGER.info("Assigned stable blind ID %s to %s", blind_id, subentry.title)
    return changed


CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: cv.config_entry_only_config_schema(DOMAIN)},
    extra=vol.ALLOW_EXTRA,
)


def _validate_device_id(value: str) -> str:
    """Normalize `value` to uppercase hex and reject anything else.

    Used as a voluptuous validator for the `test_command` service's
    device-ID field; raises `vol.Invalid` for anything that is not
    exactly six hexadecimal characters.
    """
    normalized = cv.string(value).strip().upper()
    if len(normalized) != 6 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise vol.Invalid("device ID must be six hexadecimal characters")
    return normalized


def _validate_device_enum(value: str) -> str:
    """Normalize `value` to uppercase hex and reject anything else.

    Used as a voluptuous validator for the `test_command` service's
    enum field; raises `vol.Invalid` for anything that is not exactly
    two hexadecimal characters.
    """
    normalized = cv.string(value).strip().upper()
    if len(normalized) != 2 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise vol.Invalid("enum must be two hexadecimal characters")
    return normalized


TEST_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): _validate_device_id,
        vol.Required(CONF_ENUM): _validate_device_enum,
        vol.Required(CONF_COMMAND): vol.In({"open", "close", "stop"}),
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide `test_command` service.

    This runs once regardless of how many hub config entries exist;
    per-hub setup (connecting to the stick, creating entities) happens
    separately in `async_setup_entry` for each one.
    """
    _LOGGER.info("Setting up Schellenberg USB integration")

    async def _handle_test_command(call: ServiceCall) -> None:
        """Run the `test_command` service call against the right hub.

        Resolves which loaded hub's API to use: the explicitly
        requested `CONF_CONFIG_ENTRY_ID` if given, otherwise the
        single loaded hub if there is exactly one - raising
        `ServiceValidationError` to ask the caller to disambiguate
        when neither applies. Also raises `ServiceValidationError` if
        the stick rejects the write outright; a busy/offline stick
        that still accepts the write and answers later is not treated
        as a failure here.
        """
        requested_entry_id = call.data.get(CONF_CONFIG_ENTRY_ID)
        loaded_entries: list[tuple[SchellenbergConfigEntry, SchellenbergUsbApi]] = []
        for candidate in hass.config_entries.async_entries(DOMAIN):
            # candidate.runtime_data is only set by async_setup_entry, so a
            # configured-but-not-loaded entry (never set up, failed setup,
            # or unloaded) has no such attribute at all yet - accessing it
            # directly raises AttributeError instead of returning None, and
            # this loop must survive that for every *other* entry to still
            # be considered. This is unrelated to the identifier-lookup
            # compatibility handled by device_registry_compat.py: it is not
            # about older Home Assistant releases, it is about a config
            # entry that simply is not loaded right now.
            api = getattr(candidate, "runtime_data", None)
            if isinstance(api, SchellenbergUsbApi):
                loaded_entries.append((candidate, api))

        if requested_entry_id:
            api = next(
                (
                    candidate_api
                    for candidate, candidate_api in loaded_entries
                    if candidate.entry_id == requested_entry_id
                ),
                None,
            )
            if api is None:
                raise ServiceValidationError(
                    _runtime_translation_text(
                        hass,
                        "service_entry_not_loaded",
                        entry_id=requested_entry_id,
                    )
                )
        elif len(loaded_entries) == 1:
            api = loaded_entries[0][1]
        else:
            raise ServiceValidationError(
                _runtime_translation_text(hass, "service_need_single_entry")
            )

        requested_command = call.data[CONF_COMMAND]
        action = {
            "open": CMD_UP,
            "close": CMD_DOWN,
            "stop": CMD_STOP,
        }[requested_command]
        _LOGGER.warning(
            "test_command service called command_requested=%s device_id=%s enum=%s "
            "config_entry_id=%s connected=%s mode=%s ready=%s pairing=%s "
            "transmitter_active=%s busy_latched=%s",
            requested_command,
            call.data[CONF_DEVICE_ID],
            call.data[CONF_ENUM],
            requested_entry_id or "auto",
            api.is_connected,
            api.device_mode or "unknown",
            api.transmit_ready,
            api.pairing_active,
            api.transmitter_active,
            api.busy_latched,
        )
        if not await api.control_blind_exclusive(
            call.data[CONF_ENUM],
            action,
            device_id=call.data[CONF_DEVICE_ID],
            source="service",
        ):
            _LOGGER.error(
                "test_command service failed command_requested=%s device_id=%s "
                "enum=%s reason=%s",
                requested_command,
                call.data[CONF_DEVICE_ID],
                call.data[CONF_ENUM],
                api.transmit_block_reason or "serial write failed",
            )
            raise ServiceValidationError(
                _runtime_translation_text(hass, "service_command_not_queued")
            )
        _LOGGER.warning(
            "test_command service result command_requested=%s device_id=%s enum=%s "
            "result=written_awaiting_ack",
            requested_command,
            call.data[CONF_DEVICE_ID],
            call.data[CONF_ENUM],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_COMMAND,
        _handle_test_command,
        schema=TEST_COMMAND_SCHEMA,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
    """Set up one Schellenberg USB hub config entry.

    Ignored for a non-hub entry (no `CONF_SERIAL_PORT`). Creates the
    `SchellenbergUsbApi` and starts connecting to the stick as its own
    background task rather than awaiting it here, so a slow or
    failing serial port does not block setup. Ensures the hub's own
    config subentry and device-registry device exist, creating or
    reassigning them as needed, backfills stable blind IDs onto any
    subentry that predates that migration, then forwards setup to the
    cover/sensor/switch platforms. Finally wires an update listener
    that reloads the whole entry - disconnecting and reconnecting the
    stick - only when a subentry was actually added, removed, or had
    a non-recalibration field change; see
    `reload_ignored_subentry_data_keys` below for why pure calibration
    updates are deliberately excluded from that comparison.
    """
    if CONF_SERIAL_PORT not in entry.data:
        _LOGGER.warning(
            "Received async_setup_entry for non-hub entry %s, ignoring", entry.entry_id
        )
        return False

    _LOGGER.info("Setting up hub entry: %s", entry.title)

    port = entry.data[CONF_SERIAL_PORT]
    api = SchellenbergUsbApi(hass, port)

    entry.runtime_data = api

    api.start_connecting()

    async def _async_handle_hass_stop(_event: Event) -> None:
        """Disconnect the USB stick when Home Assistant itself is stopping.

        async_unload_entry() below already disconnects the stick on an
        ordinary reload/remove/disable, but Home Assistant does not call
        that for every loaded entry on a full Core stop/restart - until
        now, only cover.py's own per-entity EVENT_HOMEASSISTANT_STOP
        listener ran at that point, freezing each blind's own position
        estimate but leaving this hub's shared serial connection itself
        open for the process teardown to close however it happened to,
        rather than the same clean, intentional close() a reload already
        gets.
        """
        await api.disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_handle_hass_stop)
    )

    hub_subentry = next(
        (s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_HUB),
        None,
    )
    if hub_subentry is None:
        _LOGGER.debug("Creating hub subentry for entry %s", entry.entry_id)
        hub_subentry = ConfigSubentry(
            data=MappingProxyType({}),
            subentry_type=SUBENTRY_TYPE_HUB,
            title="Hub",
            unique_id=None,
        )
        hass.config_entries.async_add_subentry(entry, hub_subentry)

    device_registry = dr.async_get(hass)

    # NOTE: `dr.async_get_device_id_by_identifier` never existed in Home Assistant
    # (calling it raised AttributeError, which was NOT caught by the ValueError
    # handler that used to be here, so this crashed async_setup_entry on every
    # attempt). Identifiers are also no longer globally unique as of HA 2026.9;
    # they are scoped per config entry there. See device_registry_compat.py for
    # why the lookup below still works on Home Assistant releases older than
    # 2026.9 too, rather than trading one AttributeError for another.
    hub_device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, entry.entry_id), entry.entry_id
    )

    if hub_device is None:
        hub_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=hub_subentry.subentry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model=_runtime_translation_text(hass, "device_model"),
        )
    elif not async_device_on_subentry_compat(
        hub_device, entry.entry_id, hub_subentry.subentry_id
    ):
        async_reassign_device_subentry_compat(
            device_registry, hub_device, entry.entry_id, hub_subentry.subentry_id
        )
    hub_device_id = hub_device.id

    @callback
    def _handle_stick_status_for_device_registry() -> None:
        """Keep the hub device's reported firmware version current.

        device_version is None until the stick has been verified, which
        normally happens after entity platforms are already set up, so the
        DeviceInfo captured once at entity-construction time (switch/sensor)
        never gets refreshed on its own.
        """
        if api.device_version:
            device_registry.async_update_device(
                hub_device_id, sw_version=api.device_version
            )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_STICK_STATUS_UPDATED, _handle_stick_status_for_device_registry
        )
    )

    _async_backfill_blind_ids(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Fields the running entities already pick up live via a dispatcher
    # signal (_notify_calibration_completed in options_flow_calibration.py
    # -> SchellenbergCover._handle_calibration_completed in cover.py)
    # instead of needing a reload. Recalibrating an *existing* blind persists
    # exactly these three fields onto its subentry, so counting them here
    # would trigger a full hub reload - disconnecting the serial port and
    # rebuilding every cover/sensor/switch on this hub, not just the
    # recalibrated one - immediately after that live update, defeating the
    # whole point of it. Everything else (a subentry being added/removed, or
    # any other data field changing, including the status-identity fields
    # cover.py only reads once at entity construction) still needs the
    # reload below to take effect.
    reload_ignored_subentry_data_keys = (
        CONF_OPEN_TIME,
        CONF_CLOSE_TIME,
        CONF_LAST_CALIBRATION,
    )

    def _subentry_reload_snapshot(
        current_entry: SchellenbergConfigEntry,
    ) -> dict[str, tuple[str, str, str | None, dict[str, Any]]]:
        """Build a comparable snapshot of the entry's current subentries.

        Used to detect whether a subentry was added, removed, or
        meaningfully changed since the last comparison, deliberately
        excluding `reload_ignored_subentry_data_keys` (see the comment
        above this function) since those fields already propagate to
        running entities on their own.
        """
        return {
            subentry_id: (
                subentry.subentry_type,
                subentry.title,
                subentry.unique_id,
                {
                    key: value
                    for key, value in subentry.data.items()
                    if key not in reload_ignored_subentry_data_keys
                },
            )
            for subentry_id, subentry in current_entry.subentries.items()
        }

    known_subentries = _subentry_reload_snapshot(entry)

    async def _on_entry_updated(
        hass_instance: HomeAssistant, updated_entry: SchellenbergConfigEntry
    ) -> None:
        """Reload the entry if its subentries changed since last compared.

        Registered as the config entry's update listener. Compares a
        fresh `_subentry_reload_snapshot` against the one taken at the
        last reload (or at setup) and, only on a real difference,
        performs a full `async_reload` - disconnecting the serial port
        and rebuilding every entity on this hub.
        """
        nonlocal known_subentries
        current_subentries = _subentry_reload_snapshot(updated_entry)
        if current_subentries != known_subentries:
            _LOGGER.info(
                "Subentry configuration changed; reloading entry %s", entry.entry_id
            )
            known_subentries = current_subentries
            await hass_instance.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_on_entry_updated))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
    """Unload this hub's platforms and disconnect the USB stick.

    Only disconnects the API if every platform unloaded successfully,
    matching Home Assistant's convention that a failed unload leaves
    the entry's runtime state untouched for a possible retry.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        api: SchellenbergUsbApi = entry.runtime_data
        await api.disconnect()

    return unload_ok
