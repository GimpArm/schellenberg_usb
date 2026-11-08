"""Tests for integration setup/unload and the test_command service handler.

Also covers async_setup_entry()'s own device-registry bookkeeping (hub
subentry/device creation vs. reuse, reassigning a device found on the
wrong subentry, and the firmware-version tracking callback) and the
non-hub-entry guard, config-entry-id-forwarding, and initial-connect-task
scheduling around it - see the section near the end of this file.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.schellenberg_usb.api as api_module
from custom_components.schellenberg_usb import (
    _async_backfill_blind_ids,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.blind_id import normalize_blind_id
from custom_components.schellenberg_usb.const import (
    CMD_STOP,
    CMD_TRANSMIT,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND,
    CONF_CONFIG_ENTRY_ID,
    CONF_DEVICE_ENUM,
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
)
from custom_components.schellenberg_usb.device_registry_compat import (
    async_device_on_subentry_compat,
    async_get_device_by_identifier_compat,
)
from tests.conftest import written

EXPECTED_STOP_PAYLOAD = f"{CMD_TRANSMIT}109{CMD_STOP}0000\r\n".encode("ascii")


async def test_service_rejects_an_entry_that_is_not_loaded(hass: HomeAssistant) -> None:
    """A configured-but-unloaded entry must not crash the service handler.

    Regression: the handler used to read `candidate.runtime_data` directly
    while looping over every configured entry, including ones that were
    never set up - which have no such attribute at all yet, so this raised
    AttributeError instead of the intended ServiceValidationError.
    """
    not_loaded_entry = MockConfigEntry(domain=DOMAIN, data={})
    not_loaded_entry.add_to_hass(hass)

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {
                CONF_DEVICE_ID: "ABCDEF",
                CONF_ENUM: "10",
                CONF_COMMAND: "stop",
                CONF_CONFIG_ENTRY_ID: not_loaded_entry.entry_id,
            },
            blocking=True,
        )


async def test_service_uses_the_single_loaded_entry_when_others_are_unloaded(
    hass: HomeAssistant, connected_api_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must survive an unloaded entry and still find the loaded one."""
    # No stick reply is ever fed, so without this, control_blind_exclusive()'s
    # own ack-wait would block for the real MOVE_ACK_TIMEOUT - see
    # test_config_flow.py's test_add_shutter_from_
    # remote_runs_test_motor_before_calibration_choice for the same caveat.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    not_loaded_entry = MockConfigEntry(domain=DOMAIN, data={})
    not_loaded_entry.add_to_hass(hass)

    loaded_entry = MockConfigEntry(domain=DOMAIN, data={})
    loaded_entry.add_to_hass(hass)
    api = connected_api_factory()
    loaded_entry.runtime_data = api

    await async_setup(hass, {})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_COMMAND,
        {CONF_DEVICE_ID: "ABCDEF", CONF_ENUM: "10", CONF_COMMAND: "stop"},
        blocking=True,
    )

    assert written(api) == [EXPECTED_STOP_PAYLOAD]


async def test_service_requires_config_entry_id_when_multiple_are_loaded(
    hass: HomeAssistant, connected_api_factory: Any
) -> None:
    entry_a = MockConfigEntry(domain=DOMAIN, data={})
    entry_a.add_to_hass(hass)
    entry_a.runtime_data = connected_api_factory("/dev/fake-a")

    entry_b = MockConfigEntry(domain=DOMAIN, data={})
    entry_b.add_to_hass(hass)
    entry_b.runtime_data = connected_api_factory("/dev/fake-b")

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {CONF_DEVICE_ID: "ABCDEF", CONF_ENUM: "10", CONF_COMMAND: "stop"},
            blocking=True,
        )


async def test_service_raises_when_control_blind_fails_to_queue(
    hass: HomeAssistant,
) -> None:
    """The third ServiceValidationError branch: control_blind() refused.

    test_service_rejects_an_entry_that_is_not_loaded and
    test_service_requires_config_entry_id_when_multiple_are_loaded above
    cover the other two branches of _handle_test_command; this covers the
    one where a single loaded entry is found but the stick itself refuses
    to transmit right now. A never-connected API already refuses on its
    own (see _transmit_capability_block_reason in api.py), so no
    FakeTransport is needed to provoke this.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")

    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_COMMAND,
            {CONF_DEVICE_ID: "ABCDEF", CONF_ENUM: "10", CONF_COMMAND: "stop"},
            blocking=True,
        )


async def test_async_unload_entry_disconnects_the_api(
    hass: HomeAssistant, connected_api_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    api = connected_api_factory()
    entry.runtime_data = api

    # async_unload_platforms() itself belongs to Home Assistant's own
    # forwarded-platform bookkeeping, not to this integration's logic; it is
    # stubbed out so this test is only about async_unload_entry's own two
    # lines - unload, then disconnect the right api using entry.runtime_data.
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )

    result = await async_unload_entry(hass, entry)

    assert result is True
    assert api.is_connected is False


async def test_async_setup_entry_disconnects_the_api_on_hass_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: no HA-stop listener at the hub level.

    async_unload_entry() above already disconnects the stick on an
    ordinary reload/remove/disable, but Home Assistant does not call
    that for every loaded entry on a full Core stop - only cover.py's
    own per-entity EVENT_HOMEASSISTANT_STOP listener used to run then,
    leaving this hub's shared serial connection for the process teardown
    to close however it happened to, rather than the same clean,
    intentional close() a reload already gets. This fires the real
    event through the bus - not just calling a handler directly - so it
    also proves async_setup_entry() actually registered the listener,
    not only that disconnecting works once something calls it.
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(SchellenbergUsbApi, "disconnect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry)
    api = entry.runtime_data

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    api.disconnect.assert_awaited_once_with()


async def test_calibration_only_subentry_update_does_not_trigger_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recalibrating an existing blind must not force a full hub reload.

    Regression: _on_entry_updated used to compare every subentry data field,
    including open_time/close_time/last_calibration - exactly the fields a
    recalibration persists via async_update_and_abort in
    options_flow_calibration.py. _notify_calibration_completed already
    updates the live cover entity for those three fields with no reload
    needed (see SchellenbergCover._handle_calibration_completed), so the
    reload this used to trigger right afterwards undid that live update and
    also disconnected/rebuilt every other blind on the same hub. Changing
    any other subentry field (e.g. a re-paired device_enum) must still
    reload, since cover.py only reads those once at entity construction.
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    mock_reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", mock_reload)

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    blind = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: "ABCDEF",
                CONF_DEVICE_ENUM: "10",
                CONF_OPEN_TIME: 20.0,
                CONF_CLOSE_TIME: 18.0,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Living Room Blind",
        unique_id="ABCDEF",
    )
    hass.config_entries.async_add_subentry(entry, blind)
    subentry_id = blind.subentry_id

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    mock_reload.reset_mock()  # ignore anything triggered by setup itself

    # ConfigSubentry is immutable - async_update_subentry() swaps in a new
    # instance under the same subentry_id rather than mutating `blind` in
    # place, so every read/write below goes through entry.subentries[...]
    # again instead of reusing the original `blind`/dict reference.
    calibration_only_update = dict(entry.subentries[subentry_id].data)
    calibration_only_update[CONF_OPEN_TIME] = 21.5
    calibration_only_update[CONF_CLOSE_TIME] = 19.0
    calibration_only_update[CONF_LAST_CALIBRATION] = "2026-09-27T10:00:00+00:00"
    hass.config_entries.async_update_subentry(
        entry, entry.subentries[subentry_id], data=calibration_only_update
    )
    await hass.async_block_till_done()

    mock_reload.assert_not_called()

    rebind_update = dict(entry.subentries[subentry_id].data)
    rebind_update[CONF_DEVICE_ENUM] = "11"
    hass.config_entries.async_update_subentry(
        entry, entry.subentries[subentry_id], data=rebind_update
    )
    await hass.async_block_till_done()

    mock_reload.assert_awaited_once_with(entry.entry_id)


def test_backfill_blind_ids_assigns_missing_ids_to_blind_subentries(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Blind 1",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, subentry)

    changed = _async_backfill_blind_ids(hass, entry)

    assert changed is True
    updated_data = entry.subentries[subentry.subentry_id].data
    assert normalize_blind_id(updated_data.get(CONF_BLIND_ID)) is not None


def test_backfill_blind_ids_is_a_noop_once_ids_are_already_valid(
    hass: HomeAssistant,
) -> None:
    valid_id = "12345678-1234-5678-1234-567812345678"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_BLIND_ID: valid_id}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Blind 1",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, subentry)

    changed = _async_backfill_blind_ids(hass, entry)

    assert changed is False
    assert entry.subentries[subentry.subentry_id].data[CONF_BLIND_ID] == valid_id


# ---------------------------------------------------------------------------
# async_setup_entry(): the non-hub-entry guard, hub subentry/device
# creation vs. reuse, reassigning a device found on the wrong subentry, the
# firmware-version tracking callback, forwarding setup to all platforms,
# and scheduling the initial connect as its own task.
# ---------------------------------------------------------------------------


async def test_async_setup_entry_ignores_a_non_hub_entry(hass: HomeAssistant) -> None:
    """A config entry with no CONF_SERIAL_PORT is not this integration's hub
    entry and must be skipped rather than crashing on the missing port.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry) is False


async def test_async_setup_entry_creates_a_hub_subentry_and_device_when_none_exist(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    hub_subentries = [
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_HUB
    ]
    assert len(hub_subentries) == 1
    device_registry = dr.async_get(hass)
    hub_device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert hub_device is not None
    assert async_device_on_subentry_compat(
        hub_device, entry.entry_id, hub_subentries[0].subentry_id
    )


async def test_async_setup_entry_reuses_an_existing_hub_subentry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub entry that already has its hub subentry (e.g. on reload) must
    not grow a second one.
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    existing_hub = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="Hub",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, existing_hub)

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    hub_subentries = [
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_HUB
    ]
    assert len(hub_subentries) == 1
    assert hub_subentries[0].subentry_id == existing_hub.subentry_id


async def test_async_setup_entry_reassigns_a_device_found_on_the_wrong_subentry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub device that already exists but sits on the wrong subentry (no
    subentry at all, here - e.g. left over from a recreated hub subentry)
    must be moved onto the current hub subentry, not duplicated or left
    behind. This is the branch a real past bug used to crash on (see the
    NOTE comment above the lookup in __init__.py).
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    hub_subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="Hub",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, hub_subentry)
    device_registry = dr.async_get(hass)
    stale_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Schellenberg USB Stick",
    )
    assert not async_device_on_subentry_compat(
        stale_device, entry.entry_id, hub_subentry.subentry_id
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    updated_device = device_registry.async_get(stale_device.id)
    assert updated_device is not None
    assert async_device_on_subentry_compat(
        updated_device,  # type: ignore[arg-type]
        entry.entry_id,
        hub_subentry.subentry_id,
    )


async def test_firmware_tracking_updates_the_device_once_a_version_is_known(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    api: SchellenbergUsbApi = entry.runtime_data
    # device_version is a read-only property; _handle_message() is what
    # normally sets the backing attribute once the stick answers an RFTU_
    # version query, so the private attribute is set directly here too.
    api._device_version = "1.2.3"
    async_dispatcher_send(hass, SIGNAL_STICK_STATUS_UPDATED)

    device_registry = dr.async_get(hass)
    hub_device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert hub_device is not None
    assert hub_device.sw_version == "1.2.3"


async def test_firmware_tracking_does_nothing_without_a_known_version(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the stick has answered its version query, device_version is
    still None - the callback must not touch the device registry at all,
    rather than overwrite sw_version with that absence.
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    device_registry = dr.async_get(hass)
    update_spy = MagicMock(wraps=device_registry.async_update_device)
    monkeypatch.setattr(device_registry, "async_update_device", update_spy)

    async_dispatcher_send(hass, SIGNAL_STICK_STATUS_UPDATED)

    update_spy.assert_not_called()


async def test_async_setup_entry_forwards_setup_to_all_platforms(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    forward_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward_spy)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)

    assert await async_setup_entry(hass, entry)

    forward_spy.assert_awaited_once_with(entry, PLATFORMS)


async def test_async_setup_entry_schedules_the_initial_connect_as_its_own_task(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The initial connect attempt must be fired off as its own task rather
    than awaited inline - a slow or unresponsive stick must not block
    async_setup_entry (and therefore Home Assistant startup) on it.

    The task itself is now created inside api.start_connecting() (shared
    with the scheduled-reconnect path, so disconnect() can always find and
    cancel whichever connect attempt is currently running) rather than
    directly in async_setup_entry, so the tracked task's name is
    start_connecting()'s own "schellenberg-connect", not a setup-specific
    one.
    """
    monkeypatch.setattr(SchellenbergUsbApi, "connect", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    task_spy = MagicMock(wraps=hass.async_create_task)
    monkeypatch.setattr(hass, "async_create_task", task_spy)

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    task_spy.assert_called_once()
    _args, kwargs = task_spy.call_args
    assert kwargs.get("name") == "schellenberg-connect"
