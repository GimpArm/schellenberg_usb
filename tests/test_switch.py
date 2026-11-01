"""Tests for the LED switch platform.

Covers async_turn_on/off, the reconnect-triggered hardware-state restore,
and - the two sections at the end - async_setup_entry() (entity creation,
previously untested) and async_added_to_hass()'s own restore-on-startup
logic and dispatcher-signal wiring (also previously untested; only
_handle_status_update's own behavior once called was covered above).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.const import (
    CONF_SERIAL_PORT,
    DOMAIN,
    SIGNAL_STICK_STATUS_UPDATED,
    SUBENTRY_TYPE_HUB,
)
from custom_components.schellenberg_usb.switch import (
    SchellenbergLedSwitch,
    async_setup_entry,
)


def _build_switch(hass: HomeAssistant, api: Any) -> SchellenbergLedSwitch:
    entry: Any = MagicMock()
    entry.entry_id = "test-entry"
    switch = SchellenbergLedSwitch(api, entry)
    switch.hass = hass
    switch.entity_id = "switch.schellenberg_usb_stick_led"
    switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign, misc]
    return switch


async def test_async_turn_on_calls_led_on(hass: HomeAssistant) -> None:
    api = MagicMock()
    api.led_on = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)

    await switch.async_turn_on()

    api.led_on.assert_awaited_once_with()
    assert switch.is_on is True


async def test_async_turn_off_calls_led_off(hass: HomeAssistant) -> None:
    api = MagicMock()
    api.led_off = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch._is_on = True

    await switch.async_turn_off()

    api.led_off.assert_awaited_once_with()
    assert switch.is_on is False


async def test_async_turn_on_raises_when_led_on_fails(hass: HomeAssistant) -> None:
    api = MagicMock()
    api.led_on = AsyncMock(return_value=False)
    switch = _build_switch(hass, api)

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    assert switch.is_on is False


async def test_async_turn_off_raises_when_led_off_fails(hass: HomeAssistant) -> None:
    api = MagicMock()
    api.led_off = AsyncMock(return_value=False)
    switch = _build_switch(hass, api)
    switch._is_on = True

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()

    assert switch.is_on is True


async def test_reconnect_schedules_hardware_restore_as_a_background_task(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the restore must run as a background task, not a tracked one.

    A plain async_create_task() task is awaited by Home Assistant during
    shutdown; this one is meant to run detached (see
    _restore_hardware_state's own docstring), so it must be scheduled with
    async_create_background_task() instead, which Home Assistant cancels
    outright at shutdown rather than waiting on.
    """
    api = MagicMock()
    api.is_connected = True
    api.led_on = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch._is_on = True
    switch._was_available = False

    background_task_spy = MagicMock(wraps=hass.async_create_background_task)
    monkeypatch.setattr(hass, "async_create_background_task", background_task_spy)
    tracked_task_spy = MagicMock(wraps=hass.async_create_task)
    monkeypatch.setattr(hass, "async_create_task", tracked_task_spy)

    switch._handle_status_update()
    await hass.async_block_till_done()

    background_task_spy.assert_called_once()
    tracked_task_spy.assert_not_called()
    api.led_on.assert_awaited_once()


async def test_no_restore_scheduled_when_already_available(hass: HomeAssistant) -> None:
    """No availability transition must not re-trigger a hardware restore."""
    api = MagicMock()
    api.is_connected = True
    api.led_on = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch._was_available = True

    switch._handle_status_update()
    await hass.async_block_till_done()

    api.led_on.assert_not_awaited()


# ---------------------------------------------------------------------------
# async_setup_entry(): creates exactly one SchellenbergLedSwitch, attached to
# the hub subentry when one already exists on the entry. Previously
# untested.
# ---------------------------------------------------------------------------


async def test_async_setup_entry_creates_one_switch_on_the_hub_subentry(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    entry.runtime_data = api
    hub_subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="Hub",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, hub_subentry)
    added_entities: list[Any] = []
    async_add_entities = Mock(
        side_effect=lambda entities, **kwargs: added_entities.extend(entities)
    )

    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    assert isinstance(added_entities[0], SchellenbergLedSwitch)
    async_add_entities.assert_called_once_with(
        [added_entities[0]], config_subentry_id=hub_subentry.subentry_id
    )


async def test_async_setup_entry_without_a_hub_subentry_passes_none(
    hass: HomeAssistant,
) -> None:
    """A hub entry with no hub subentry yet (predates it, or it was somehow
    removed) must still get its LED switch - just not attached to any
    subentry, rather than async_setup_entry failing outright.
    """
    api = MagicMock()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    entry.runtime_data = api
    async_add_entities = Mock()

    await async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    _args, kwargs = async_add_entities.call_args
    assert kwargs["config_subentry_id"] is None


# ---------------------------------------------------------------------------
# async_added_to_hass(): restores the last known on/off state and - only
# when the stick is already connected at that point - immediately pushes it
# to the hardware too, marking _was_available so the next status-update
# signal does not restore it a second time. Also covers the dispatcher
# subscription itself, which _handle_status_update's own tests above take
# for granted. Previously untested.
# ---------------------------------------------------------------------------


async def test_added_to_hass_restores_and_pushes_hardware_state_when_connected(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.is_connected = True
    api.led_on = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(switch.entity_id, "on")
    )

    await switch.async_added_to_hass()

    assert switch.is_on is True
    api.led_on.assert_awaited_once_with()
    assert switch._was_available is True


async def test_added_to_hass_restores_state_but_skips_hardware_push_when_offline(
    hass: HomeAssistant,
) -> None:
    """Restoring the entity's last state must not be confused with pushing
    it to hardware - with the stick not connected yet, there is nothing to
    push to, and the eventual reconnect (covered above) takes care of it.
    """
    api = MagicMock()
    api.is_connected = False
    api.led_on = AsyncMock(return_value=True)
    api.led_off = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(switch.entity_id, "on")
    )

    await switch.async_added_to_hass()

    assert switch.is_on is True
    api.led_on.assert_not_awaited()
    api.led_off.assert_not_awaited()
    assert switch._was_available is False


async def test_added_to_hass_defaults_to_off_with_nothing_restored(
    hass: HomeAssistant,
) -> None:
    """A brand-new entity with no prior Home Assistant state at all."""
    api = MagicMock()
    api.is_connected = True
    switch = _build_switch(hass, api)
    switch.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    await switch.async_added_to_hass()

    assert switch.is_on is False
    assert switch._was_available is False


async def test_added_to_hass_wires_up_the_status_update_signal(
    hass: HomeAssistant,
) -> None:
    """End-to-end check that async_added_to_hass() actually subscribes
    _handle_status_update to SIGNAL_STICK_STATUS_UPDATED - the reconnect
    tests above call _handle_status_update directly and so never exercise
    that subscription itself.
    """
    api = MagicMock()
    api.is_connected = False
    api.led_off = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)
    switch.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    await switch.async_added_to_hass()
    api.is_connected = True
    async_dispatcher_send(hass, SIGNAL_STICK_STATUS_UPDATED)

    assert switch._was_available is True
    switch.async_write_ha_state.assert_called()  # type: ignore[attr-defined]
    await hass.async_block_till_done()


async def test_added_to_hass_does_not_race_the_restore_against_a_concurrent_reconnect(
    hass: HomeAssistant,
) -> None:
    """Regression for audit finding 19: a brief, self-correcting LED flicker.

    async_get_last_state() suspends this coroutine - if the stick's own
    connect() finished while it was suspended, the status-update signal
    it fires used to reach a dispatcher subscription that was already
    registered *before* this await, so _handle_status_update() restored
    the hardware state from self._is_on while that was still __init__'s
    hardcoded False default - sending one wrong command (here, a
    spurious led_off()) immediately before this method's own restore,
    just below the suspension point, sent the correct one (led_on(), the
    real stored preference). Simulates that exact interleaving: the
    signal fires from inside async_get_last_state() itself, before it
    returns the real "on" state.
    """
    api = MagicMock()
    api.is_connected = True
    api.led_on = AsyncMock(return_value=True)
    api.led_off = AsyncMock(return_value=True)
    switch = _build_switch(hass, api)

    async def racing_get_last_state() -> State:
        # The concurrent reconnect: fires the signal this entity will -
        # pre-fix, already does - listen for, before returning the
        # stored state, then lets any resulting background task run.
        async_dispatcher_send(hass, SIGNAL_STICK_STATUS_UPDATED)
        await hass.async_block_till_done()
        return State(switch.entity_id, "on")

    switch.async_get_last_state = racing_get_last_state  # type: ignore[method-assign]

    await switch.async_added_to_hass()
    await hass.async_block_till_done()

    assert switch.is_on is True
    assert switch._was_available is True
    # Before the fix, this was awaited once too, with self._is_on still
    # False - immediately followed by the correct led_on() below once
    # async_added_to_hass() resumed and restored the real preference.
    api.led_off.assert_not_awaited()
    api.led_on.assert_awaited_once_with()
