"""Diagnostics support for the Schellenberg USB integration.

Supplements, rather than replaces, config_flow.py's own
async_step_copy_diagnostics step (the per-blind "Developer Tools -> Copy
diagnostics" text field reached from that blind's own options flow): that
step gives an immediate, localized (de/en/es/fr), human-readable snapshot
for one blind right where someone is already troubleshooting it, read
directly off the dialog with no download step. This module is instead Home
Assistant's own, more discoverable mechanism - the "Download diagnostics"
action in Settings -> Devices & Services -> this entry's three-dot menu,
and - via async_get_device_diagnostics below - on each blind's own device
page too - producing plain, English-keyed JSON meant for attaching to a
GitHub issue, not for reading in the UI. The two overlap in *what* data
they expose, not in *how* or *where* it reaches a person, which is why
both are worth having rather than picking one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_CLOSE_TIME,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    DEFAULT_TRAVEL_TIME_SECONDS,
    SUBENTRY_TYPE_BLIND,
    SUBENTRY_TYPE_HUB,
)
from .device_registry_compat import async_device_on_subentry_compat
from .identities import resolve_blind_identities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

    from .api import SchellenbergUsbApi
    from .const import SchellenbergConfigEntry

#: Only CONF_SERIAL_PORT currently lives in entry.data (see options_flow.py/
#: __init__.py) - a local device path, not a credential, but still
#: identifying enough (a /dev/serial/by-id/... path can embed the USB
#: adapter's own hardware serial number) that it should not go out verbatim
#: in a diagnostics dump someone may paste into a public GitHub issue.
TO_REDACT = {CONF_SERIAL_PORT}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SchellenbergConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the whole hub (config entry).

    ``hass`` is required by Home Assistant's diagnostics platform calling
    convention but unused here - everything needed comes from
    ``entry.runtime_data``/``entry.subentries`` instead.
    """
    api = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "stick": _stick_diagnostics(api),
        "blinds": [
            _blind_diagnostics(subentry, api)
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_BLIND
        ],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: SchellenbergConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one device - a single blind, or the hub
    (USB stick) device itself, on this entry.

    ``hass`` is unused here, for the same reason as in
    async_get_config_entry_diagnostics() above.

    The matching subentry is found via async_device_on_subentry_compat -
    the same version-compatible check cover.py/__init__.py themselves use
    when reassigning a device, see device_registry_compat.py - rather than
    by parsing the device's own identifiers() tuple directly, so this keeps
    working unchanged if that encoding ever changes. Every device Home
    Assistant registers for this entry belongs to exactly one subentry: a
    blind, or the single SUBENTRY_TYPE_HUB subentry __init__.py's
    async_setup_entry() creates for the USB stick device itself - so both
    are checked here, branching only once a subentry's device actually
    matches.

    This used to only ever check blind subentries (every
    other subentry type, the hub included, was skipped outright), so asking
    for diagnostics from the hub device's own page - not any blind's - could
    never match anything and always fell through to the generic error below,
    instead of the same real stick data async_get_config_entry_diagnostics()
    already exposes at the entry level.
    """
    api = entry.runtime_data
    for subentry in entry.subentries.values():
        if not async_device_on_subentry_compat(
            device, entry.entry_id, subentry.subentry_id
        ):
            continue
        if subentry.subentry_type == SUBENTRY_TYPE_BLIND:
            return _blind_diagnostics(subentry, api)
        if subentry.subentry_type == SUBENTRY_TYPE_HUB:
            return _stick_diagnostics(api)
    # Should not normally happen - Home Assistant only calls this for a
    # device it already knows belongs to this config entry - but a plain,
    # explicit result beats an unhandled None reaching the diagnostics
    # download as a crash.
    return {"error": f"No subentry found for device {device.id}"}


def _stick_diagnostics(api: SchellenbergUsbApi) -> dict[str, Any]:
    """Return the hub-wide USB stick/transmitter state."""
    return {
        "is_connected": api.is_connected,
        "device_mode": api.device_mode,
        "transmit_ready": api.transmit_ready,
        "pairing_active": api.pairing_active,
        "transmitter_active": api.transmitter_active,
        "busy_latched": api.busy_latched,
    }


def _blind_diagnostics(
    subentry: ConfigSubentry, api: SchellenbergUsbApi
) -> dict[str, Any]:
    """Return one blind's configured identities, settings, and live state.

    Shares resolve_blind_identities() (identities.py) with config_flow.py's
    own async_step_copy_diagnostics step, so the command/status identity
    fallback priority is only implemented once between the two.
    """
    data = subentry.data
    resolved = resolve_blind_identities(data)
    command_device_id = resolved["command_device_id"]
    primary_identity = resolved["primary_identity"]
    return {
        "name": subentry.title,
        "command_device_id": command_device_id,
        "command_enum": resolved["command_enum"],
        "primary_status_identity": primary_identity,
        "status_identity_source": resolved["status_identity_source"],
        "secondary_status_identities": list(resolved["secondary_status_identities"]),
        "invert_direction": bool(data.get(CONF_INVERT_DIRECTION, False)),
        "open_time": float(data.get(CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME_SECONDS)),
        "close_time": float(data.get(CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME_SECONDS)),
        "last_calibration": data.get(CONF_LAST_CALIBRATION),
        "last_matched_frame": api.get_last_received_for_identities(
            resolved["status_identities"]
        ),
        "last_primary_frame": (
            api.get_last_primary_tracking_frame(*primary_identity)
            if primary_identity is not None
            else None
        ),
        "last_secondary_frame": api.get_last_secondary_frame(
            resolved["secondary_status_identities"]
        ),
        "last_position_update": api.get_last_position_update(command_device_id),
        "last_manual_position_sync": api.get_last_manual_position_sync(
            command_device_id
        ),
    }
