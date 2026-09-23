"""Compatibility helpers for Home Assistant's evolving device registry API.

Home Assistant 2026.9 changed the device registry so that identifiers are
no longer globally unique (they are scoped per config entry) and a device
has at most one config subentry. That introduced
``DeviceRegistry.async_get_device_by_identifier(identifier, config_entry_id)``
and a ``new_config_subentry_id`` keyword on ``async_update_device``.

Older Home Assistant releases - including the oldest release this
integration's manifest still declares support for - only have the
un-scoped ``async_get_device(identifiers=...)`` lookup and moved a device
between subentries with ``add_config_subentry_id``/``remove_config_subentry_id``
instead of replacing a single value.

Calling the new-only methods unconditionally raises AttributeError/TypeError
on any Home Assistant release that predates them - the exact class of bug
this integration is already working around elsewhere (see the NOTE in
__init__.py and cover.py about the nonexistent
``async_get_device_id_by_identifier``). These helpers feature-detect which
API is actually available at runtime so device lookups work across both
old and new Home Assistant releases instead of assuming the newest one.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry, DeviceRegistry


def async_get_device_by_identifier_compat(
    device_registry: DeviceRegistry,
    identifier: tuple[str, str],
    config_entry_id: str,
) -> DeviceEntry | None:
    """Look up a device by identifier, scoped to a config entry when possible.

    Uses the HA 2026.9+ scoped lookup when it exists; falls back to the
    older, un-scoped lookup (identifiers were globally unique before
    2026.9, so no entry_id is needed there).
    """
    scoped_lookup = getattr(device_registry, "async_get_device_by_identifier", None)
    if scoped_lookup is not None:
        return scoped_lookup(identifier, config_entry_id)
    return device_registry.async_get_device(identifiers={identifier})


def async_reassign_device_subentry_compat(
    device_registry: DeviceRegistry,
    device: DeviceEntry,
    new_subentry_id: str,
) -> None:
    """Move ``device`` onto ``new_subentry_id``, on old or new Home Assistant.

    HA 2026.9+ models "one device -> at most one subentry" and exposes a
    single ``new_config_subentry_id`` keyword for it. Older releases modeled
    a device's subentries as a set, adjusted with
    ``add_config_subentry_id``/``remove_config_subentry_id``.
    """
    update_params = inspect.signature(device_registry.async_update_device).parameters
    if "new_config_subentry_id" in update_params:
        device_registry.async_update_device(
            device.id, new_config_subentry_id=new_subentry_id
        )
    else:
        device_registry.async_update_device(
            device.id,
            remove_config_subentry_id=device.config_subentry_id,
            add_config_subentry_id=new_subentry_id,
)
