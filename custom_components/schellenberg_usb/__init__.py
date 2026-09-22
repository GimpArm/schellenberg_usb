"""The Schellenberg USB Stick integration."""

from __future__ import annotations

import logging
from types import MappingProxyType

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .api import SchellenbergUsbApi
from .blind_id import claim_blind_id
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_BLIND_ID,
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENUM,
    CONF_SERIAL_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_TEST_COMMAND,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_BLIND,
    SUBENTRY_TYPE_HUB,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _async_backfill_blind_ids(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> bool:
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
    normalized = cv.string(value).strip().upper()
    if len(normalized) != 6 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise vol.Invalid("device ID must be six hexadecimal characters")
    return normalized


def _validate_device_enum(value: str) -> str:
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


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    _LOGGER.info("Setting up Schellenberg USB integration")

    async def _handle_test_command(call: ServiceCall) -> None:
        requested_entry_id = call.data.get(CONF_CONFIG_ENTRY_ID)
        loaded_entries: list[tuple[SchellenbergConfigEntry, SchellenbergUsbApi]] = []
        for candidate in hass.config_entries.async_entries(DOMAIN):
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
                    f"No loaded Schellenberg USB entry {requested_entry_id}"
                )
        elif len(loaded_entries) == 1:
            api = loaded_entries[0][1]
        else:
            raise ServiceValidationError(
                "Exactly one Schellenberg USB hub must be loaded, or config_entry_id "
                "must be supplied"
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
        if not await api.control_blind(
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
            raise ServiceValidationError("The serial command could not be queued")
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
    if CONF_SERIAL_PORT not in entry.data:
        _LOGGER.warning(
            "Received async_setup_entry for non-hub entry %s, ignoring", entry.entry_id
        )
        return False

    _LOGGER.info("Setting up hub entry: %s", entry.title)

    port = entry.data[CONF_SERIAL_PORT]
    api = SchellenbergUsbApi(hass, port)

    entry.runtime_data = api

    hass.async_create_task(api.connect(), name="schellenberg-initial-connect")

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
    # attempt). Identifiers are also no longer globally unique as of HA 2026.8;
    # they are scoped per config entry, so the lookup must be scoped too.
    hub_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )

    if hub_device is None:
        hub_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=hub_subentry.subentry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model="USB Stick",
        )
    elif hub_device.config_subentry_id != hub_subentry.subentry_id:
        device_registry.async_update_device(
            hub_device.id,
            new_config_subentry_id=hub_subentry.subentry_id,
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

    known_subentries = {
        subentry_id: (
            subentry.subentry_type,
            subentry.title,
            subentry.unique_id,
            dict(subentry.data),
        )
        for subentry_id, subentry in entry.subentries.items()
    }

    async def _on_entry_updated(
        hass_instance: HomeAssistant, updated_entry: SchellenbergConfigEntry
    ) -> None:
        nonlocal known_subentries
        current_subentries = {
            subentry_id: (
                subentry.subentry_type,
                subentry.title,
                subentry.unique_id,
                dict(subentry.data),
            )
            for subentry_id, subentry in updated_entry.subentries.items()
        }
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
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        api: SchellenbergUsbApi = entry.runtime_data
        await api.disconnect()

    return unload_ok
