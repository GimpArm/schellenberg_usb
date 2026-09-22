"""Tests for cover.py's SchellenbergCover entity, against the real Home
Assistant test harness (pytest-homeassistant-custom-component's `hass`
fixture) rather than hand-written fakes.

Focus: the most significant fix made to this integration - control_blind()
(the RF send) is now called *before* any entity state is mutated, and a
failed send raises HomeAssistantError instead of leaving the entity
reporting a movement that never physically happened. Only self._api is
mocked (there is no real serial stick here); hass, entity state writes and
background-task scheduling are all the genuine Home Assistant runtime.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.schellenberg_usb.const import CMD_DOWN, CMD_STOP, CMD_UP
from custom_components.schellenberg_usb.cover import SchellenbergCover


def make_cover(hass: HomeAssistant, *, invert_direction: bool = False) -> SchellenbergCover:
    """Build a SchellenbergCover wired to a real hass and a fake API."""
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    api.transmit_block_reason = None
    api.is_connected = True
    api.register_entity = MagicMock()

    cover = SchellenbergCover(
        api=api,
        device_id="5D3E7C",
        device_enum="01",
        device_name="Test Blind",
        blind_id="test-blind",
        invert_direction=invert_direction,
    )
    cover.hass = hass
    cover.entity_id = "cover.test_blind"
    cover._attr_current_cover_position = 50
    cover._attr_is_closed = False
    return cover


async def cleanup(hass: HomeAssistant, cover: SchellenbergCover) -> None:
    """Stop any background position-tracking task a test may have started."""
    await cover._async_cancel_position_tracking("test cleanup")
    await hass.async_block_till_done()


async def test_open_cover_sends_command_and_updates_state(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    try:
        await cover.async_open_cover()
        cover._api.control_blind.assert_awaited_once_with(
            "01", CMD_UP, device_id="5D3E7C"
        )
        assert cover._attr_is_opening is True
        assert cover._attr_is_closing is False
    finally:
        await cleanup(hass, cover)


async def test_open_cover_inverted_direction_sends_down_command(
    hass: HomeAssistant,
) -> None:
    cover = make_cover(hass, invert_direction=True)
    try:
        await cover.async_open_cover()
        cover._api.control_blind.assert_awaited_once_with(
            "01", CMD_DOWN, device_id="5D3E7C"
        )
    finally:
        await cleanup(hass, cover)


async def test_open_cover_raises_and_leaves_state_untouched_when_command_fails(
    hass: HomeAssistant,
) -> None:
    cover = make_cover(hass)
    cover._api.control_blind = AsyncMock(return_value=False)
    cover._api.transmit_block_reason = "stick disconnected"

    with pytest.raises(HomeAssistantError, match="stick disconnected"):
        await cover.async_open_cover()

    # The regression this guards against: a failed send must NOT leave the
    # entity reporting a movement that never physically happened.
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._position_update_task is None


async def test_close_cover_sends_command_and_updates_state(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    try:
        await cover.async_close_cover()
        cover._api.control_blind.assert_awaited_once_with(
            "01", CMD_DOWN, device_id="5D3E7C"
        )
        assert cover._attr_is_closing is True
        assert cover._attr_is_opening is False
    finally:
        await cleanup(hass, cover)


async def test_close_cover_raises_and_leaves_state_untouched_when_command_fails(
    hass: HomeAssistant,
) -> None:
    cover = make_cover(hass)
    cover._api.control_blind = AsyncMock(return_value=False)
    cover._api.transmit_block_reason = "wrong mode"

    with pytest.raises(HomeAssistantError, match="wrong mode"):
        await cover.async_close_cover()

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False


async def test_stop_cover_sends_command(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    cover._attr_is_closing = True

    await cover.async_stop_cover()

    cover._api.control_blind.assert_awaited_once_with(
        "01", CMD_STOP, device_id="5D3E7C"
    )
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False


async def test_stop_cover_raises_when_command_fails(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    cover._attr_is_closing = True
    cover._api.control_blind = AsyncMock(return_value=False)
    cover._api.transmit_block_reason = None

    with pytest.raises(HomeAssistantError, match="command was not sent"):
        await cover.async_stop_cover()

    # On a failed stop, the previous moving state is deliberately left
    # alone (see the comment in async_stop_cover) rather than frozen, since
    # the blind - if still physically moving - is closer to the truth via
    # the ongoing elapsed-time estimate than a prematurely frozen one.
    assert cover._attr_is_closing is True


async def test_set_position_equal_to_current_while_idle_is_a_noop(
    hass: HomeAssistant,
) -> None:
    cover = make_cover(hass)
    cover._attr_current_cover_position = 40

    await cover.async_set_cover_position(position=40)

    cover._api.control_blind.assert_not_awaited()


async def test_set_position_equal_to_current_while_moving_stops_instead_of_noop(
    hass: HomeAssistant,
) -> None:
    """Regression test: reaching the requested position mid-move must stop
    the blind, not silently do nothing and let it sail past toward
    wherever it was originally headed."""
    cover = make_cover(hass)
    cover._attr_current_cover_position = 40
    cover._attr_is_opening = True

    await cover.async_set_cover_position(position=40)

    cover._api.control_blind.assert_awaited_once_with(
        "01", CMD_STOP, device_id="5D3E7C"
    )


async def test_set_position_above_current_opens(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    cover._attr_current_cover_position = 20
    try:
        await cover.async_set_cover_position(position=80)
        cover._api.control_blind.assert_awaited_once_with(
            "01", CMD_UP, device_id="5D3E7C"
        )
        assert cover._target_position == 80
    finally:
        await cleanup(hass, cover)


async def test_set_position_below_current_closes(hass: HomeAssistant) -> None:
    cover = make_cover(hass)
    cover._attr_current_cover_position = 80
    try:
        await cover.async_set_cover_position(position=20)
        cover._api.control_blind.assert_awaited_once_with(
            "01", CMD_DOWN, device_id="5D3E7C"
        )
        assert cover._target_position == 20
    finally:
        await cleanup(hass, cover)
