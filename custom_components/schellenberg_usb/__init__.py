"""The Schellenberg USB Stick integration."""

from __future__ import annotations

import logging
from types import MappingProxyType

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import SchellenbergUsbApi
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENUM,
    CONF_SERIAL_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_TEST_COMMAND,
    SUBENTRY_TYPE_HUB,
    SchellenbergConfigEntry,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: cv.config_entry_only_config_schema(DOMAIN)},
    extra=vol.ALLOW_EXTRA,
)


def _validate_device_id(value: str) -> str:
    """Validate and normalize a six-character protocol device ID."""
    normalized = cv.string(value).strip().upper()
    if len(normalized) != 6 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise vol.Invalid("device ID must be six hexadecimal characters")
    return normalized


def _validate_device_enum(value: str) -> str:
    """Validate and normalize a two-character protocol enum."""
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
    """Set up integration-level diagnostic services."""

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
    """Set up Schellenberg USB from a config entry."""
    _LOGGER.debug("Setup entry called for entry: %s", entry.entry_id)
    _LOGGER.debug("Entry data keys: %s", list(entry.data.keys()))

    # This is a hub entry - it has CONF_SERIAL_PORT
    if CONF_SERIAL_PORT not in entry.data:
        _LOGGER.warning(
            "Received async_setup_entry for non-hub entry %s, ignoring", entry.entry_id
        )
        return False

    _LOGGER.info("Setting up hub entry: %s", entry.title)
    hass.data.setdefault(DOMAIN, {})

    port = entry.data[CONF_SERIAL_PORT]
    api = SchellenbergUsbApi(hass, port)

    # Store API in runtime_data for platforms and services access
    entry.runtime_data = api

    # Start the connection
    hass.async_create_task(api.connect())

    # Ensure we have a dedicated hub subentry so hub-level devices/entities
    # (like the LED) do not appear under "Devices that don't belong to a sub-entry".
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

    # Attach or create hub device under hub subentry to avoid ungrouped duplication
    device_registry = dr.async_get(hass)
    hub_device = device_registry.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    if hub_device is None:
        _LOGGER.debug(
            "Creating hub device and attaching to hub subentry %s",
            hub_subentry.subentry_id,
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=hub_subentry.subentry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name="Schellenberg USB Stick",
            manufacturer="Schellenberg",
            model="USB Stick",
        )
    else:
        _LOGGER.debug(
            "Ensuring existing hub device %s is associated with entry %s and subentry %s",
            hub_device.id,
            entry.entry_id,
            hub_subentry.subentry_id,
        )
        device_registry.async_update_device(
            hub_device.id,
            add_config_entry_id=entry.entry_id,
            add_config_subentry_id=hub_subentry.subentry_id,
        )

    # Forward setup to the hub's platforms (cover, sensor, switch)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when blind subentries are added, removed, renamed, or edited.
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
        """Handle changes to the hub's blind subentries."""
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
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        api: SchellenbergUsbApi = entry.runtime_data
        await api.disconnect()

    return unload_ok
