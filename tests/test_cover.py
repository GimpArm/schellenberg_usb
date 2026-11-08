"""Tests for the cover entity's command dispatch and stop-at-target logic.

SchellenbergCover instances here are built directly (not through
async_setup_entry) with a MagicMock api, since these tests are only about
the entity's own decision logic: which commands it sends and when.
async_write_ha_state() is stubbed out because these tests assert on command
dispatch, not on the entity's reported state, and a bare entity built this
way was never handed a real entity platform to write state through.

The final section (test_async_setup_entry_...) is the one exception: it
drives the real async_setup_entry() against a MockConfigEntry/ConfigSubentry
pair, because that function's own job - turning a saved blind subentry into
a device-registry device plus one SchellenbergCover, and migrating a
pre-stable-ID entity's unique_id when it finds one - cannot be exercised by
building the entity directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from homeassistant.components.cover import ATTR_CURRENT_POSITION, ATTR_POSITION
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.api import StopOutcome
from custom_components.schellenberg_usb.const import (
    AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS,
    CMD_DOWN,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INITIAL_POSITION,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS,
    POSITION_UPDATE_LOOP_TIMEOUT_SECONDS,
    SIGNAL_DEVICE_EVENT,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    STOP_COMMAND_LATENCY_SECONDS,
    SUBENTRY_TYPE_BLIND,
)
from custom_components.schellenberg_usb.cover import (
    SchellenbergCover,
    async_setup_entry,
)
from custom_components.schellenberg_usb.device_registry_compat import (
    async_get_device_by_identifier_compat,
)


class _FakeClock:
    """A controllable stand-in for time.monotonic().

    _update_position() and the position-tracking loop only ever care about
    *elapsed* time, so a fake clock that advances by an explicit amount each
    time the loop's own asyncio.sleep() is awaited reproduces real elapsed-
    time behavior deterministically and instantly, without any real
    waiting.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _build_cover(hass: HomeAssistant, api: Any, **overrides: Any) -> SchellenbergCover:
    kwargs: dict[str, Any] = {
        "api": api,
        "device_id": "ABCDEF",
        "device_enum": "10",
        "device_name": "Living Room Blind",
    }
    kwargs.update(overrides)
    cover = SchellenbergCover(**kwargs)
    cover.hass = hass
    cover.entity_id = "cover.living_room_blind"
    cover.async_write_ha_state = MagicMock()  # type: ignore[method-assign, misc]
    # A bare MagicMock's pop_fresh_calibration_position(...) would otherwise
    # return a truthy MagicMock instead of None, which async_added_to_hass()
    # would wrongly treat as a real fresh-calibration position. Default it
    # to "nothing to pop" - but only if a test hasn't already configured
    # its own return value (before *or* after calling this helper, since
    # api is the caller's own object either way): an untouched Mock's
    # .return_value is itself still an auto-generated MagicMock, which is
    # how this tells "never configured" apart from "deliberately set".
    if isinstance(api, MagicMock) and isinstance(
        api.pop_fresh_calibration_position.return_value, MagicMock
    ):
        api.pop_fresh_calibration_position.return_value = None
    # Same reasoning, same idiom, for the new contention-aware stop-lead
    # check in _async_position_update_loop(): a bare MagicMock's own
    # .move_lock_busy would otherwise auto-vivify as a (truthy) MagicMock
    # attribute, silently doubling the latency-compensation lead for
    # every existing position-loop test that never heard of contention.
    # Defaulted to "not busy" here, but only if a test hasn't already set
    # its own value (before *or* after calling this helper).
    if isinstance(api, MagicMock) and isinstance(api.move_lock_busy, MagicMock):
        api.move_lock_busy = False
    return cover


async def test_async_open_cover_sends_up_via_control_blind_with_retry(
    hass: HomeAssistant,
) -> None:
    """Open goes through control_blind_with_retry(), not a bare control_blind().

    Regression guard for several blinds commanded close together silently
    losing all but one command to the stick's own single-RF-transmitter
    busy state: async_open_cover() used to await wait_for_transmitter_idle()
    and then call control_blind() directly, each blind checking that same
    shared "idle" event independently - see async_open_cover()'s own
    comment for the full story of why that raced. control_blind_with_retry()
    (api.py) now does the waiting *and* the busy-retry itself, serialized
    hub-wide through its own lock - covered end-to-end at the api.py level
    (test_control_blind_with_retry_serializes_two_blinds_instead_of_racing
    and friends in test_api.py); this only has to confirm cover.py actually
    calls it, with the right arguments.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    await cover.async_open_cover()

    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_UP, device_id=cover._command_device_id
    )


async def test_async_close_cover_sends_down_via_control_blind_with_retry(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_open_cover_sends_up_via_control_blind_with_retry."""
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    await cover.async_close_cover()

    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_DOWN, device_id=cover._command_device_id
    )


async def test_async_open_cover_raises_when_control_blind_with_retry_fails(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_stop_cover_raises_when_control_blind_stop_fails for open.

    Regression guard: a failed open used to leave _attr_is_opening stuck
    True, reporting a movement that never physically happened.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_open_cover()

    assert cover._attr_is_opening is False
    assert cover._move_start_time is None


async def test_async_close_cover_raises_when_control_blind_with_retry_fails(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_stop_cover_raises_when_control_blind_stop_fails for close.

    Regression guard: a failed close used to leave _attr_is_closing stuck
    True, reporting a movement that never physically happened.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover()

    assert cover._attr_is_closing is False
    assert cover._move_start_time is None


async def test_async_open_cover_clears_stale_target_when_a_preserved_leg_fails(
    hass: HomeAssistant,
) -> None:
    """Regression guard: a failed, target-preserving leg must not leave
    a stale target behind.

    A failed set_cover_position() leg (_preserve_target=True) used to leave
    _target_position stale, since only a plain, non-preserving open/close
    cleared it on failure. A later, completely unrelated move - typically
    the blind's own physical remote control, picked up via _handle_event() -
    never touches _target_position either, so it could inherit that stale
    value and get auto-stopped by _async_position_update_loop() at a long-
    invalid position that has nothing to do with it.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._target_position = 40  # stale target a set_cover_position() leg set

    with pytest.raises(HomeAssistantError):
        await cover.async_open_cover(_preserve_target=True)

    assert cover._attr_is_opening is False
    assert cover._target_position is None


async def test_async_close_cover_clears_stale_target_when_a_preserved_leg_fails(
    hass: HomeAssistant,
) -> None:
    """Mirrors the open-side regression guard above for close."""
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._target_position = 40

    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover(_preserve_target=True)

    assert cover._attr_is_closing is False
    assert cover._target_position is None


async def test_async_open_cover_failure_does_not_clobber_anothers_live_target(
    hass: HomeAssistant,
) -> None:
    """A failing *plain* open must not wipe a different leg's live target.

    The two regression tests above cover a *preserved* leg's own target
    being cleared on its own failure (_preserve_target=True) - correct,
    since that leg owns the target it is being asked to preserve. The
    failure path used to clear self._target_position unconditionally
    though, regardless of *this* call's own _preserve_target - so a
    plain open (_preserve_target=False, which has no target concept of
    its own; any stale one of its own was already dropped at the top of
    this same method) could still wipe a target that arrived in the
    meantime and belongs to someone else entirely: self._target_position
    is shared, per-entity state, and this call's own two awaits (the
    cancel-tracking call, and this one) are real suspension points a
    different, genuinely live set_cover_position() leg's own first,
    synchronous write can land inside, same as the STARTED_MOVING/STOPPED
    tests elsewhere in this file already establish for other fields.
    Reproduced here without needing any precise interleaving: the
    control_blind_with_retry mock itself sets the other leg's target
    before failing, which is enough to show the failure path's own
    unconditional clear was the only thing that could still destroy it.
    """

    async def fake_retry_then_fail(*_args: object, **_kwargs: object) -> bool:
        # A different, genuinely live set_cover_position() leg's target
        # landing while this plain open's own write is in flight - then
        # this call's own write fails outright.
        cover._target_position = 40
        return False

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(side_effect=fake_retry_then_fail)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_open_cover()  # plain open - _preserve_target defaults False

    assert cover._target_position == 40


async def test_async_close_cover_failure_does_not_clobber_anothers_live_target(
    hass: HomeAssistant,
) -> None:
    """Mirrors the open-side regression guard above for close."""

    async def fake_retry_then_fail(*_args: object, **_kwargs: object) -> bool:
        cover._target_position = 60
        return False

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(side_effect=fake_retry_then_fail)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover()  # plain close - _preserve_target defaults False

    assert cover._target_position == 60


async def test_async_open_cover_does_not_start_tracking_before_retry_succeeds(
    hass: HomeAssistant,
) -> None:
    """Regression guard for a short set_cover_position() leg queued behind
    other blinds racing past its own target before its command is even sent.

    control_blind_with_retry() serializes hub-wide through api.py's own
    _move_lock and does not return until its command is actually accepted
    - so a blind commanded while another one's own transmission is still
    in flight can sit waiting a while before its own command reaches the
    stick even once. If _move_start_time/_move_start_position and the
    tracking loop were already running during that wait (as they used to
    be - started optimistically before this same await, not after it),
    the elapsed-time estimate would count that queued wait as travel time
    too: for a short enough leg behind enough other blinds in the same
    burst, the estimate could reach the target - and the loop would act on
    that by firing its own stop - before this blind's own open/close
    command had been transmitted even once.

    Proven here by a control_blind_with_retry() stand-in that looks at the
    entity's own state *from inside the call*, before returning - exactly
    the moment a real call would instead still be waiting its turn on
    _move_lock.
    """
    observed: dict[str, Any] = {}
    # A plain local variable, not the reassigned cover._start_position_tracking
    # attribute itself: mypy keeps the latter typed as the real method's
    # Callable[[], None] signature inside fake_retry's own nested scope (the
    # ignore-comment below only covers the assignment statement itself, not
    # later reads of it from inside a closure), so reading .called through
    # it there specifically - unlike the plain, same-scope read after this
    # function's own await below - fails "has no attribute called". This
    # mock is what the attribute actually holds at runtime either way.
    mock_start_tracking = MagicMock()

    async def fake_retry(*_args: object, **_kwargs: object) -> bool:
        # Captured while this call is still "in flight" - standing in for
        # the real call still waiting for _move_lock, command not yet sent.
        observed["move_start_time_before"] = cover._move_start_time
        observed["move_start_position_before"] = cover._move_start_position
        observed["tracking_started_before"] = mock_start_tracking.called
        return True

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(side_effect=fake_retry)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = mock_start_tracking  # type: ignore[method-assign]
    cover._attr_current_cover_position = 50

    await cover.async_open_cover()

    assert observed["move_start_time_before"] is None
    assert observed["move_start_position_before"] is None
    assert observed["tracking_started_before"] is False
    assert cover._move_start_time is not None
    assert cover._move_start_position == 50
    mock_start_tracking.assert_called_once_with()


async def test_async_close_cover_does_not_start_tracking_before_retry_succeeds(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_open_cover_does_not_start_tracking_before_retry_succeeds."""
    observed: dict[str, Any] = {}
    mock_start_tracking = MagicMock()

    async def fake_retry(*_args: object, **_kwargs: object) -> bool:
        observed["move_start_time_before"] = cover._move_start_time
        observed["move_start_position_before"] = cover._move_start_position
        observed["tracking_started_before"] = mock_start_tracking.called
        return True

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(side_effect=fake_retry)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = mock_start_tracking  # type: ignore[method-assign]
    cover._attr_current_cover_position = 50

    await cover.async_close_cover()

    assert observed["move_start_time_before"] is None
    assert observed["move_start_position_before"] is None
    assert observed["tracking_started_before"] is False
    assert cover._move_start_time is not None
    assert cover._move_start_position == 50
    mock_start_tracking.assert_called_once_with()


async def test_async_open_cover_sets_move_start_time_to_plain_monotonic(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real _move_start_time must land at plain time.monotonic(), not
    pushed into the future.

    Companion to test_async_open_cover_does_not_start_tracking_before_
    retry_succeeds above, which only proves _move_start_time is set at
    all (not None) once control_blind_with_retry() succeeds. This proves
    *where* it is set to. Used to be pushed STOP_COMMAND_LATENCY_SECONDS
    ahead instead; reverted (Issue #78, 2026-11-07) because that push
    froze the reported position for just over a second at the start of
    every move - see this assignment's own comment in
    _async_open_cover_body() for the full reasoning, and
    stop_lead_percent's own comment in _async_position_update_loop() for
    where that compensation lives now instead.
    """
    clock = _FakeClock(2_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    await cover.async_open_cover()

    assert cover._move_start_time == 2_000.0


async def test_async_close_cover_sets_move_start_time_to_plain_monotonic(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_async_open_cover_sets_move_start_time_to_plain_monotonic."""
    clock = _FakeClock(2_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    await cover.async_close_cover()

    assert cover._move_start_time == 2_000.0


async def test_async_close_cover_skips_reset_for_redundant_resend(
    hass: HomeAssistant,
) -> None:
    """A redundant re-send of an already-in-progress close must not re-base
    the running position estimate.

    Regression guard confirmed from a real log: the README's own
    fine-position-control example scripts intentionally send
    open_cover/close_cover a second time, ~0.3s after the first, purely
    for RF reliability. Before this fix, async_close_cover() cancelled
    position tracking unconditionally at call entry and only
    re-established it once *this* call's own control_blind_with_retry()
    succeeded - which, queued behind another blind sharing the hub-wide
    _move_lock, measured well over a second in that log. The blind kept
    moving under the first, already-accepted send the whole time; only
    the HA-side estimate froze, then resumed from that stale, frozen
    value - permanently losing however far the blind actually travelled
    during the gap. Simulated here by marking the cover as already
    closing with a live tracking task and a valid baseline before calling
    async_close_cover() again, the way a real redundant re-send would
    find it.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._async_cancel_position_tracking = AsyncMock()  # type: ignore[method-assign]
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_closing = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 73

    await cover.async_close_cover()

    cover._async_cancel_position_tracking.assert_not_awaited()
    cover._start_position_tracking.assert_not_called()
    assert cover._move_start_time == 1234.5
    assert cover._move_start_position == 73
    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_DOWN, device_id=cover._command_device_id
    )


async def test_async_open_cover_skips_reset_for_redundant_resend(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_close_cover_skips_reset_for_redundant_resend."""
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._async_cancel_position_tracking = AsyncMock()  # type: ignore[method-assign]
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_opening = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 27

    await cover.async_open_cover()

    cover._async_cancel_position_tracking.assert_not_awaited()
    cover._start_position_tracking.assert_not_called()
    assert cover._move_start_time == 1234.5
    assert cover._move_start_position == 27
    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_UP, device_id=cover._command_device_id
    )


async def test_async_close_cover_redundant_resend_failure_keeps_move_alive(
    hass: HomeAssistant,
) -> None:
    """A redundant re-send's own failed transmission must not abort the
    still-genuinely-in-progress original move or raise to the caller.

    Unlike a genuinely new close's failed control_blind_with_retry() (see
    test_async_close_cover_raises_when_control_blind_with_retry_fails),
    the first send already succeeded here - that is what makes this
    second call a detected "redundant resend" in the first place - so the
    blind is still really closing. Reporting the whole call as failed
    would incorrectly flip is_closing to False and could abort a caller's
    script mid-sequence over a reliability re-send that was never
    required for correctness.
    """
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._async_cancel_position_tracking = AsyncMock()  # type: ignore[method-assign]
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_closing = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 73

    await cover.async_close_cover()  # must not raise

    assert cover._attr_is_closing is True
    assert cover._move_start_time == 1234.5
    assert cover._move_start_position == 73
    cover._async_cancel_position_tracking.assert_not_awaited()


async def test_async_open_cover_redundant_resend_failure_keeps_move_alive(
    hass: HomeAssistant,
) -> None:
    """Mirrors test_async_close_cover_redundant_resend_failure_keeps_move_alive."""
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=False)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._async_cancel_position_tracking = AsyncMock()  # type: ignore[method-assign]
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_opening = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 27

    await cover.async_open_cover()  # must not raise

    assert cover._attr_is_opening is True
    assert cover._move_start_time == 1234.5
    assert cover._move_start_position == 27
    cover._async_cancel_position_tracking.assert_not_awaited()


async def test_has_live_tracking_in_direction_false_when_not_moving(
    hass: HomeAssistant,
) -> None:
    """A fresh/idle cover has no live tracking in either direction."""
    cover = _build_cover(hass, MagicMock())

    assert cover._has_live_tracking_in_direction(opening=True) is False
    assert cover._has_live_tracking_in_direction(opening=False) is False


async def test_has_live_tracking_in_direction_false_when_task_already_done(
    hass: HomeAssistant,
) -> None:
    """A finished tracking task (the move already ended) is not live tracking.

    Guards against treating a stale task reference - e.g. one that reached
    its own 0%/100% end stop and returned, but has not yet been cleared -
    as evidence of an in-progress move.
    """
    cover = _build_cover(hass, MagicMock())
    fake_task = MagicMock()
    fake_task.done.return_value = True
    cover._position_update_task = fake_task
    cover._attr_is_closing = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 73

    assert cover._has_live_tracking_in_direction(opening=False) is False


async def test_has_live_tracking_in_direction_false_for_opposite_direction(
    hass: HomeAssistant,
) -> None:
    """Live tracking while opening must not count as live tracking for closing."""
    cover = _build_cover(hass, MagicMock())
    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_opening = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 27

    assert cover._has_live_tracking_in_direction(opening=False) is False
    assert cover._has_live_tracking_in_direction(opening=True) is True


async def test_async_open_cover_redundant_resend_restarts_tracking_if_cleared(
    hass: HomeAssistant,
) -> None:
    """A redundant resend must re-check live tracking after its own await,
    not trust the snapshot taken before it.

    Regression guard for a real race: a genuine status frame for the
    original move (an auto-stop at target, or an external stop) can land
    and clear that move's own tracking via _handle_event() while *this*
    call's own control_blind_with_retry() is still in flight. By the time
    it returns, the is_redundant_resend snapshot taken at entry is stale -
    there is no longer a live original move for this call to piggyback
    on, so skipping re-tracking here would leave the blind moving under
    this call's own just-accepted command with nothing tracking it at
    all. Simulated by having control_blind_with_retry() itself clear the
    tracking state as a side effect, the way a real event handled mid-write
    would.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    async def clear_tracking_then_succeed(*_args: object, **_kwargs: object) -> bool:
        cover._position_update_task = None
        cover._attr_is_opening = False
        return True

    api.control_blind_with_retry = clear_tracking_then_succeed

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 40
    cover._move_start_time = 1234.5
    cover._move_start_position = 27

    await cover.async_open_cover()

    # The original move is gone; this call's own command must now be
    # (re)tracked as a fresh move rather than silently left untracked.
    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    cover._start_position_tracking.assert_called_once()
    assert cover._move_start_time != 1234.5
    assert cover._move_start_position == 40


async def test_async_close_cover_redundant_resend_raises_if_cleared_during_failed_await(
    hass: HomeAssistant,
) -> None:
    """Mirrors the restart-tracking regression above for the failure branch.

    If the original move's tracking is cleared mid-await (a real event
    handled while this call's own write was in flight) and this call's
    own transmission then fails too, there is no longer a genuinely
    in-progress original move to leave alone - the stale
    is_redundant_resend snapshot must not suppress ordinary failure
    handling, or a real failure would be silently swallowed.
    """
    api = MagicMock()
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    async def clear_tracking_then_fail(*_args: object, **_kwargs: object) -> bool:
        cover._position_update_task = None
        cover._attr_is_closing = False
        return False

    api.control_blind_with_retry = clear_tracking_then_fail

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._attr_is_closing = True
    cover._move_start_time = 1234.5
    cover._move_start_position = 73

    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover()


async def test_async_stop_cover_sends_stop_via_control_blind_stop(
    hass: HomeAssistant,
) -> None:
    """Stop itself is sent via control_blind_stop(), not the plain control_blind().

    async_stop_cover() used to call control_blind() directly for its
    initiating stop, same as open/close - it was only the two automatic
    follow-up stop frames sent after it that used to bypass the transmit
    lock via send_priority_stop(). Confirmed real-hardware testing showed one
    of those follow-ups could still collide with an in-flight open/close
    command's own busy-retry, fighting over the stick's one RF transmitter.
    Those follow-ups (and send_priority_stop() itself) are long gone; the
    direct control_blind() call that replaced them has since been replaced
    in turn by control_blind_stop(), which briefly serializes against
    exactly that kind of collision instead of merely accepting it - see its
    own docstring and test_api.py's tests for control_blind_stop() itself.
    control_blind() must not be called from here any more at all.
    """
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api)

    await cover.async_stop_cover()

    api.control_blind_stop.assert_awaited_once_with(
        cover._command_enum, device_id=cover._command_device_id
    )
    api.control_blind.assert_not_awaited()


async def test_async_stop_cover_cancels_a_pending_move_for_this_device_first(
    hass: HomeAssistant,
) -> None:
    """A stop must end this same blind's own move-retry loop before it sends.

    Regression guard for the exact collision described above and in
    async_stop_cover()'s own docstring: cancel_pending_move() has to run
    before control_blind_stop()'s write, not after, or a retry already
    queued by control_blind_with_retry() for this blind could resend
    open/close once this stop has already gone out.
    """
    call_order: list[str] = []

    def _record_cancel(_device_id: str) -> None:
        call_order.append("cancel")

    async def _record_stop(*_args: object, **_kwargs: object) -> StopOutcome:
        call_order.append("stop")
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.cancel_pending_move = Mock(side_effect=_record_cancel)
    api.control_blind_stop = AsyncMock(side_effect=_record_stop)
    cover = _build_cover(hass, api)

    await cover.async_stop_cover()

    api.cancel_pending_move.assert_called_once_with(cover._command_device_id)
    assert call_order == ["cancel", "stop"]


async def test_async_stop_cover_does_not_itself_wait_for_transmitter_idle(
    hass: HomeAssistant,
) -> None:
    """async_stop_cover() leaves any idle-waiting entirely to control_blind_stop().

    control_blind_stop() does now wait for the transmitter to go idle in
    some cases (see its own docstring and test_api.py) - but that happens
    inside api.py, behind one call. This guards against async_stop_cover()
    itself ever also calling api.wait_for_transmitter_idle() directly, the
    way the old async_open_cover()/async_close_cover() pattern did before
    control_blind_with_retry() absorbed it for them: that would double the
    wait rather than close any gap, since control_blind_stop() already
    does it. api.wait_for_transmitter_idle is still configured (rather
    than left as a bare, non-awaitable MagicMock attribute) so this would
    fail loudly with a TypeError, not silently pass, if it ever were
    awaited from here.
    """
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    api.wait_for_transmitter_idle = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)

    await cover.async_stop_cover()

    api.wait_for_transmitter_idle.assert_not_awaited()


async def test_async_stop_cover_raises_when_control_blind_stop_fails(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.FAILED)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)

    with pytest.raises(HomeAssistantError):
        await cover.async_stop_cover()


async def test_async_stop_cover_leaves_moving_state_untouched_when_stop_fails(
    hass: HomeAssistant,
) -> None:
    """Regression: a failed stop used to leave a moving cover's position frozen.

    async_stop_cover() now defers freezing the position and cancelling
    tracking until *after* control_blind_stop() succeeds (see its own
    docstring for why) - so when it fails instead, nothing has been torn
    down yet: is_closing, _move_start_time, _move_start_position and the
    live tracking task are all exactly as they were, with no explicit
    "restore" step needed and nothing to restart. Only _target_position,
    which this method clears unconditionally up front (see its own
    docstring for why), needs putting back on this path.
    """
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.FAILED)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._attr_is_closing = True
    cover._move_start_time = 123.0
    cover._move_start_position = 80
    cover._target_position = 0
    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_stop_cover()

    assert cover._attr_is_closing is True
    assert cover._attr_is_opening is False
    assert cover._move_start_time == 123.0
    assert cover._move_start_position == 80
    assert cover._target_position == 0
    assert cover._position_update_task is fake_task
    fake_task.cancel.assert_not_called()
    cover._start_position_tracking.assert_not_called()


async def test_async_stop_cover_does_not_touch_idle_state_when_stop_fails(
    hass: HomeAssistant,
) -> None:
    """A failed stop on an already-idle cover must not spin up tracking.

    Companion to the regression test above: with nothing torn down until
    control_blind_stop() succeeds, a failed stop on a cover that was never
    moving in the first place has nothing to restore, and must not call
    _start_position_tracking() either.
    """
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.FAILED)
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError):
        await cover.async_stop_cover()

    cover._start_position_tracking.assert_not_called()
    assert cover._position_update_task is None


async def test_async_stop_cover_clears_target_before_awaiting_control_blind_stop(
    hass: HomeAssistant,
) -> None:
    """_target_position must already be None by the time the write goes out.

    Position tracking is deliberately left running across the await below
    (see async_stop_cover()'s own docstring) - but _async_position_update_
    loop's own auto-stop-at-target branch only triggers while a target is
    still set, and must not fire its own, second control_blind_stop() for
    this same blind while this one is still in flight. Checking inside
    control_blind_stop()'s own side effect, rather than only after
    async_stop_cover() returns, confirms the target is cleared *before*
    the write goes out - not merely by the time the whole call finishes,
    which a clear placed after the await would also satisfy without
    closing the actual race.
    """
    seen_target_during_call: list[int | None] = []

    async def _record_target(*_args: object, **_kwargs: object) -> StopOutcome:
        seen_target_during_call.append(cover._target_position)
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(side_effect=_record_target)
    cover = _build_cover(hass, api)
    cover._target_position = 42

    await cover.async_stop_cover()

    assert seen_target_during_call == [None]


async def test_async_stop_cover_does_not_cancel_tracking_before_stop_succeeds(
    hass: HomeAssistant,
) -> None:
    """Tracking must stay live for as long as control_blind_stop() is in flight.

    Regression guard for the actual bug this fix addresses: cancelling the
    tracking task (or reading its estimate) before the stop write has
    happened reads a stale position under contention - see the test right
    after this one, and async_stop_cover()'s own docstring, for the
    real-hardware numbers this came from.
    """
    task = asyncio.ensure_future(asyncio.sleep(100))
    still_live_when_called = False

    async def _check_still_tracking(*_args: object, **_kwargs: object) -> StopOutcome:
        nonlocal still_live_when_called
        still_live_when_called = cover._position_update_task is task and not task.done()
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(side_effect=_check_still_tracking)
    cover = _build_cover(hass, api)
    cover._attr_is_closing = True
    cover._move_start_time = time.monotonic()
    cover._move_start_position = 80
    cover._position_update_task = task

    await cover.async_stop_cover()

    assert still_live_when_called is True
    assert cover._position_update_task is None
    assert task.cancelled()


async def test_async_stop_cover_freezes_position_from_elapsed_time_after_stop_returns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen position must reflect when the stop actually transmitted.

    Regression test for the real bug: async_stop_cover() used to call
    _update_position() (and so freeze current_cover_position) *before*
    awaiting control_blind_stop() - using elapsed time up to the instant
    the service was called, not up to whenever the write actually reached
    the stick. Under contention from another blind sharing the hub-wide
    _move_lock (see api.py), control_blind_stop() can take over a second
    to actually write - confirmed from a real two-blind test log where
    exactly this gap, on its own, put the frozen estimate about 6
    percentage points away from where the blind had actually gotten to by
    the time the stop really went out. Advancing the fake clock *inside*
    control_blind_stop()'s own await, before it returns, reproduces that
    gap deterministically: the frozen position must reflect the clock
    *after* the advance, not before it.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)

    async def _delayed_stop(*_args: object, **_kwargs: object) -> StopOutcome:
        # Stands in for a real transmit queued behind another blind's own
        # command - see STOP_RETRY_BUDGET's and _move_lock's own comments
        # in api.py for why this is a realistic, not a worst-case, gap.
        clock.advance(1.5)
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(side_effect=_delayed_stop)
    cover = _build_cover(hass, api)
    cover._travel_time_close = 50.0
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._move_start_position = 100
    cover._move_start_time = time.monotonic()

    await cover.async_stop_cover()

    # 1.5s elapsed at 50s full travel = 3 percentage points, so the frozen
    # position must be 97 - not 100, which is what a freeze taken before
    # control_blind_stop()'s own 1.5s advance would have produced instead.
    assert cover._attr_current_cover_position == 97
    assert cover._attr_is_closing is False
    assert cover._move_start_time is None


async def test_async_stop_cover_fallback_leaves_a_racing_retarget_untouched(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A racing same-direction retarget's own target must survive this fallback.

    A third independent review (2026-11-08) of #226/#227 and its own two
    follow-up fixes found that this general fallback - reached here via an
    ordinary CONFIRMED stop - never touches self._target_position, unlike
    the FAILED and backstop-exhaustion branches fixed elsewhere in this
    method. On its face that looks like the same staleness hazard, but is
    deliberately correct: a different, newer async_set_cover_position()
    call's own synchronous write can land during control_blind_stop()'s
    own await (simulated here exactly as test_async_stop_cover_fallback_
    does_not_clobber_a_newer_move does for its own is_opening/is_closing
    case) and leave self._target_position holding its own, live target -
    which must come through this fallback's own cleanup completely
    untouched, not reset to None, since _async_open_cover_body()'s/_async_
    close_cover_body()'s own is_redundant_resend re-arm (or, on its own
    write failing instead, its own _preserve_target handling) is what
    actually resolves it, not this method.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.cover")

    async def _confirmed_while_a_retarget_already_landed(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        # Stands in for a different, newer async_set_cover_position()
        # call's own first, synchronous write (self._target_position =
        # target_position) landing while this stop's own control_blind_
        # stop() is still suspended.
        cover._target_position = 30
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=_confirmed_while_a_retarget_already_landed
    )
    cover = _build_cover(hass, api)
    cover._travel_time_close = 50.0
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._move_start_position = 100
    cover._move_start_time = time.monotonic()

    await cover.async_stop_cover()

    # The ordinary fallback still runs for this, still-same-direction
    # move - is_closing clears exactly as the companion test above
    # asserts...
    assert cover._attr_is_closing is False
    assert cover._position_source_kind == "HA command"
    # ...but the racing retarget's own target must survive completely
    # untouched, for that call's own logic to resolve.
    assert cover._target_position == 30
    assert any(
        "a newer target already claimed" in record.message for record in caplog.records
    )


async def test_async_stop_cover_fallback_does_not_clobber_a_newer_move(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard for #226/#227.

    A stop whose own control_blind_stop() call is still suspended - here
    because, standing in for the real debug log that root-caused this, a
    different, newer async_open_cover() call for this same device already
    set its own is_opening/is_closing (its own very first line, no await
    of its own first) while this call's own await of control_blind_stop()
    was still pending - must not fall through to the ordinary stick-ack-
    only cleanup once that await resolves: doing so clobbered the newer
    move's own is_opening/is_closing, move_start_* and tracking task with
    state meant for the move this stop actually targeted, freezing the
    dashboard until the next command. The newer move's state (simulated
    here exactly as test_async_close_cover_skips_reset_for_redundant_
    resend above does for its own already-in-progress case) must come
    through this call completely untouched.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.cover")

    async def _stop_while_a_newer_open_already_took_over(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        # Stands in for a real dashboard "open" tap racing in while this
        # stop is still in flight: async_open_cover()'s own very first
        # lines (no await before them) flip these two and nothing else,
        # before its own control_blind_with_retry() or tracking loop ever
        # runs - exactly the window this test is closing.
        cover._attr_is_opening = True
        cover._attr_is_closing = False
        return StopOutcome.CONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=_stop_while_a_newer_open_already_took_over
    )
    cover = _build_cover(hass, api)
    cover._async_cancel_position_tracking = AsyncMock()  # type: ignore[method-assign]
    cover._attr_is_closing = True  # the move this stop call was actually sent for
    cover._attr_current_cover_position = 60

    fake_task = MagicMock()
    fake_task.done.return_value = False
    cover._position_update_task = fake_task  # the newer open's own tracking
    cover._move_start_time = 999.0  # the newer open's own baseline
    cover._move_start_position = 54

    await cover.async_stop_cover()

    # The newer move's state must come through exactly as it set it - none
    # of it read back from this call's own fallback, which was written for
    # the close this stop call actually targeted, not the open that
    # superseded it.
    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    assert cover._position_update_task is fake_task
    assert cover._move_start_time == 999.0
    assert cover._move_start_position == 54
    assert cover._attr_current_cover_position == 60
    cover._async_cancel_position_tracking.assert_not_awaited()
    assert any(
        "a newer move already started for this device" in record.message
        for record in caplog.records
    )


async def test_async_stop_cover_failed_outcome_does_not_clobber_a_newer_target(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard for the same bug class, in the FAILED branch.

    A second independent review (2026-11-08) of the fix above found an
    analogous, unconfirmed assumption a few lines away: the FAILED branch
    restores self._target_position from previous_target_position
    unconditionally, with no re-check that nothing else claimed the
    target slot while control_blind_stop() above was suspended - exactly
    the mistake #226/#227 fixed for is_opening/is_closing. Here, a
    different, newer async_set_cover_position() call's own synchronous
    write (self._target_position = target_position) lands during that
    same await, before this stop's own write fails. The newer call's
    target must survive completely untouched, not be overwritten with
    this stale call's own, now-irrelevant previous target.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.cover")

    async def _fail_while_a_newer_target_already_landed(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        # Stands in for a different, newer async_set_cover_position()
        # call's own first, synchronous write landing while this stop's
        # own control_blind_stop() is still suspended - see async_set_
        # cover_position()'s own self._target_position = target_position.
        cover._target_position = 30
        return StopOutcome.FAILED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=_fail_while_a_newer_target_already_landed
    )
    api.transmit_block_reason = "disconnected"  # stable code, see api.py
    cover = _build_cover(hass, api)
    cover._target_position = 50  # the target this stop call itself cleared

    with pytest.raises(HomeAssistantError):
        await cover.async_stop_cover()

    assert cover._target_position == 30
    assert any(
        "a newer move already claimed its own target" in record.message
        for record in caplog.records
    )


async def test_async_stop_cover_reports_unconfirmed_without_raising(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manual stop must stay responsive on UNCONFIRMED, not raise or hang.

    Unlike the auto-stop-at-target loop (which can afford to keep retrying
    for up to AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS - see its own tests
    below), a manual stop press must not wait any longer than control_
    blind_stop() itself already does internally: StopOutcome.UNCONFIRMED
    here still completes the same way StopOutcome.CONFIRMED does (freezes
    the estimate, stops tracking, clears is_opening/is_closing) rather than
    raising HomeAssistantError the way FAILED does - a write genuinely went
    out, it is simply not provably accepted. What changes is honesty about
    that: _position_source_kind must reflect this was not positively
    confirmed, and a warning must say so, rather than this being logged
    identically to a confirmed stop the way it was before StopOutcome
    existed.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.schellenberg_usb.cover")
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.UNCONFIRMED)
    cover = _build_cover(hass, api)
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._move_start_position = 50
    cover._move_start_time = time.monotonic()

    await cover.async_stop_cover()

    assert cover._attr_is_closing is False
    assert cover._move_start_time is None
    assert cover._position_source_kind == "unconfirmed stop"
    assert any("not confirmed accepted" in record.message for record in caplog.records)


async def test_position_loop_stops_at_a_partial_target(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 40),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40

    await cover._async_position_update_loop()

    # This is functionally a stop too, so it must end this same blind's own
    # move-retry loop first - see cancel_pending_move()'s own docstring and
    # test_async_stop_cover_cancels_a_pending_move_for_this_device_first
    # above for the collision this prevents.
    api.cancel_pending_move.assert_called_once_with(cover._command_device_id)
    # control_blind_stop() is what now waits for the move's own open/close
    # transmission to be clear of the transmitter before writing - see its
    # own docstring and test_api.py - so this loop only needs to call it,
    # not wait separately itself.
    api.control_blind_stop.assert_awaited_once_with(
        cover._command_enum, device_id=cover._command_device_id
    )
    assert cover._target_position is None
    # Regression guard: before this fix, the loop never cleared this,
    # leaving the entity reporting "closing" forever after a partial-target
    # move finished - see the matching comment in cover.py at this point.
    assert cover._attr_is_closing is False


async def test_position_loop_retries_auto_stop_when_the_write_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed auto-stop write must not be treated as a successful stop.

    Regression guard: control_blind_stop()'s return
    value used to be discarded, so is_closing/is_opening/the target were
    cleared unconditionally even when the write never reached the wire -
    reporting the move as finished while the motor kept running toward its
    real end stop, and discarding the loop's own ability to act on a later,
    genuine stop. The fix retries on the next 0.2s tick instead of clearing
    state, so it must call control_blind_stop() again rather than return.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=[StopOutcome.FAILED, _LoopKeptRunning]
    )
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 40),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    assert api.control_blind_stop.await_count == 2
    # Must still be a live, untouched move so the retry (or the motor's own
    # eventual end-stop) can still be acted on - not silently discarded.
    assert cover._target_position == 40
    assert cover._attr_is_closing is True


async def test_position_loop_retries_auto_stop_when_the_write_is_unconfirmed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UNCONFIRMED auto-stop write must not be treated as a successful stop either.

    The actual bug this whole fix exists for: control_blind_stop() used to
    return a bare bool, so STOP_RETRY_BUDGET's own exhausted-fallback write
    - sent, but nothing confirms the stick (let alone the motor) ever saw
    it - was indistinguishable from a genuinely stick-accepted stop. A real
    six-blind Alexa "25%" command (2026-11-01) hit exactly this: the
    fallback fired for one blind, this loop finalized on the strength of
    it, and that blind then kept physically closing all the way to its own
    end stop while the dashboard stayed frozen at the stale percentage this
    loop had already committed to. StopOutcome.UNCONFIRMED must retry next
    tick, exactly like FAILED already does (see the test above), not
    finalize - see test_position_loop_backstop_reports_unknown_after_
    prolonged_unconfirmed_stops below for how long it may keep retrying.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=[StopOutcome.UNCONFIRMED, _LoopKeptRunning]
    )
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 40),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    assert api.control_blind_stop.await_count == 2
    # Same assertions as the FAILED case above, for the same reason: must
    # still be a live, untouched move so the retry (or the motor's own
    # eventual end-stop) can still be acted on - not silently discarded.
    assert cover._target_position == 40
    assert cover._attr_is_closing is True


async def test_position_loop_backstop_reports_unknown_after_prolonged_unconfirmed_stops(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retrying an unconfirmed auto-stop is not unbounded.

    Companion to the UNCONFIRMED-retries test above: retrying forever would
    just trade one false-confidence bug (a stale, wrong percentage treated
    as a confirmed stop) for another (an entity that looks like it is still
    confidently tracking a position it can no longer vouch for at all).
    Once AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS has genuinely elapsed
    since the first attempt at this stop, the loop must give up and report
    the position as unknown (None, Home Assistant's own representation for
    this, already used elsewhere in this integration) rather than leave the
    stale, already-snapped target percentage on the dashboard looking as
    trustworthy as a confirmed one.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    caplog.set_level(logging.ERROR, logger="custom_components.schellenberg_usb.cover")
    call_count = 0

    async def _unconfirmed_then_past_the_backstop(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Still well inside the backstop - proves this isn't simply a
            # one-strike give-up disguised as a budget.
            clock.advance(1.0)
        else:
            clock.advance(AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS)
        return StopOutcome.UNCONFIRMED

    api = MagicMock()
    api.control_blind_stop = AsyncMock(side_effect=_unconfirmed_then_past_the_backstop)
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 40),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40
    cover._move_start_time = clock()
    cover._move_start_position = 50

    await cover._async_position_update_loop()

    assert api.control_blind_stop.await_count == 2
    # Gives up for real this time: no more retrying, and the dashboard must
    # not keep showing the stale, already-snapped 40% with no real backing
    # any more - nor "closing", which is just as much a confidence claim
    # this loop can no longer make.
    assert cover._attr_current_cover_position is None
    assert cover._attr_is_closed is None
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._move_start_time is None
    assert cover._move_start_position is None
    assert cover._target_position is None
    assert cover._pending_auto_stop_since is None
    assert any("still unconfirmed" in record.message for record in caplog.records)


async def test_position_loop_backstop_resets_after_a_retarget_mid_unconfirmed_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retarget landing while UNCONFIRMED must not inherit the old clock.

    Regression guard for a gap an independent review (2026-11-01) found in
    the backstop mechanism's first version: self._pending_auto_stop_since
    was only ever cleared on StopOutcome.CONFIRMED or on the backstop's own
    exhaustion - never when a retarget (or a direction-reversal
    cancellation; see _async_cancel_position_tracking()'s own comment)
    lands while still UNCONFIRMED/FAILED and retrying. A stale timestamp
    left over from the *old* target could then survive into a completely
    different, later episode and silently eat into (or exhaust outright)
    its own AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS budget before that
    episode's very first attempt. Exercised here via a same-task retarget
    (the same gap test_position_loop_keeps_tracking_a_same_direction_
    retarget_mid_stop covers for the CONFIRMED case) rather than an actual
    cancel+restart, to isolate this loop's own re-stamping logic from
    _async_cancel_position_tracking()'s separate, already-straightforward
    clearing.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    call_count = 0

    async def _unconfirmed_retarget_then_unconfirmed_again(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Burns almost this episode's whole budget against the
            # *original* target (40) before a retarget lands.
            clock.advance(AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS - 1.0)
            cover._target_position = 25
            return StopOutcome.UNCONFIRMED
        if call_count == 2:
            # ... then only a couple more seconds against the *new*
            # target (25). If the old timestamp leaked through, the
            # combined elapsed time (almost the full budget, plus this)
            # would already exceed AUTO_STOP_CONFIRMATION_BACKSTOP_
            # SECONDS and the loop would wrongly give up right here, on
            # this target's very first attempt.
            clock.advance(2.0)
            return StopOutcome.UNCONFIRMED
        raise _LoopKeptRunning

    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=_unconfirmed_retarget_then_unconfirmed_again
    )
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", cover._target_position),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40
    cover._move_start_time = clock()
    cover._move_start_position = 50

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    # Three calls: proves the loop neither gave up prematurely right after
    # the retarget (call 2, the old-bug outcome) nor got stuck retrying the
    # stale 40 target forever instead of noticing 25.
    assert api.control_blind_stop.await_count == 3
    assert cover._target_position == 25
    assert cover._attr_is_closing is True
    # The clock was actually re-stamped for the new target, not merely
    # "happened to not exceed the budget" by coincidence.
    assert cover._pending_auto_stop_target == 25
    assert cover._pending_auto_stop_since == pytest.approx(
        1_000.0 + AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS - 1.0
    )


async def test_position_loop_backstop_exhaustion_does_not_clobber_a_retarget(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard for the same bug class, in the backstop-exhaustion path.

    A second independent review (2026-11-08) of #226/#227 found an
    analogous gap here: unlike the sibling test above - a retarget landing
    on some *earlier*, still-retrying attempt, which self-corrects on the
    very next tick's own fresh target_when_stop_was_sent - nothing
    protected the one attempt whose own clock.advance() happens to cross
    AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS itself. Before this fix, a
    retarget landing in that exact attempt's own await fell straight into
    the backstop-exhaustion branch's unconditional clear, wiping out the
    brand new target (and is_closing/tracking) a retarget call had already
    claimed moments earlier in that same await, and ending this task via
    return with nothing left to track or auto-stop the new leg at all -
    the #226/#227 bug class again, reached via 30s of sustained backstop
    retries landing on the same tick as a retarget rather than a direction
    reversal racing a manual stop.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.cover")
    call_count = 0

    async def _retarget_then_cross_the_backstop_on_the_same_attempt(
        *_args: object, **_kwargs: object
    ) -> StopOutcome:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Stands in for a same-direction retarget (a fresh
            # set_cover_position() call) landing during *this* attempt's
            # own control_blind_stop() await - its own first synchronous
            # write already pointed self._target_position at its own new
            # value before this same attempt's own clock.advance() below
            # crosses the backstop.
            cover._target_position = 25
            clock.advance(AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS)
            return StopOutcome.UNCONFIRMED
        raise _LoopKeptRunning

    api = MagicMock()
    api.control_blind_stop = AsyncMock(
        side_effect=_retarget_then_cross_the_backstop_on_the_same_attempt
    )
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", cover._target_position),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40  # the stale target this attempt was sent for
    cover._move_start_time = clock()
    cover._move_start_position = 50

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    # Reached a second attempt - against the new target - rather than
    # returning from inside the first attempt's own backstop-exhaustion
    # cleanup.
    assert api.control_blind_stop.await_count == 2
    # The retarget's own state must come through exactly as it set it -
    # none of it read back from this attempt's own backstop-exhaustion
    # cleanup, which was written for the stale 40 target this attempt
    # actually sent a stop for, not the 25 that superseded it.
    assert cover._target_position == 25
    assert cover._attr_is_closing is True
    assert cover._attr_current_cover_position == 25
    assert cover._move_start_time == 1_000.0
    assert cover._move_start_position == 50
    assert any(
        "a newer retarget already claimed this device" in record.message
        for record in caplog.records
    )


async def test_position_loop_times_out_after_the_outer_budget_is_exhausted(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole loop, not just the auto-stop retry sub-step, is bounded.

    Issue #78's own outer safety net (2026-11-07, confirmed directly with
    the project owner): POSITION_UPDATE_LOOP_TIMEOUT_SECONDS bounds
    _async_position_update_loop()'s own total running time, regardless of
    which branch it is stuck in - unlike AUTO_STOP_CONFIRMATION_BACKSTOP_
    SECONDS above, which only times the auto-stop-at-target retry sub-loop
    and never engages for a plain open()/close() at all (target_position
    is None there). Exercised here via exactly that plain-close scenario,
    with its own position estimate pinned well short of the 0% end stop no
    matter how much simulated time passes, so neither the ordinary
    "reached fully closed" branch nor the auto-stop backstop can be what
    ends this loop - only this outer timeout can.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    caplog.set_level(logging.ERROR, logger="custom_components.schellenberg_usb.cover")
    call_count = 0

    def _pinned_position_update() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Still well inside the budget - proves this isn't simply a
            # one-tick give-up disguised as a timeout.
            clock.advance(1.0)
        else:
            clock.advance(POSITION_UPDATE_LOOP_TIMEOUT_SECONDS)
        # Pinned short of the end stop no matter how much simulated time
        # passes - the real _update_position() is a monotonic, clamped
        # function of elapsed time and would otherwise eventually reach 0
        # on its own, which would let the ordinary end-stop branch end
        # this loop first and defeat the point of this test.
        cover._attr_current_cover_position = 50

    api = MagicMock()
    cover = _build_cover(hass, api)
    monkeypatch.setattr(cover, "_update_position", _pinned_position_update)
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = None
    cover._move_start_time = clock()
    cover._move_start_position = 50
    # Set to prove this outer timeout's own cleanup clears it too - unlike
    # the narrower auto-stop backstop above, this can fire mid-full-travel-
    # resync as well as mid-auto-stop, so it has to clean up after either.
    cover._full_travel_resync_direction = "closing"

    await cover._async_position_update_loop()

    assert call_count == 2
    # Same "stop trusting a stale estimate" outcome as the auto-stop
    # backstop above, reached via a completely different, outer path.
    assert cover._attr_current_cover_position is None
    assert cover._attr_is_closed is None
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._move_start_time is None
    assert cover._move_start_position is None
    assert cover._target_position is None
    assert cover._pending_auto_stop_since is None
    assert cover._pending_auto_stop_target is None
    assert cover._full_travel_resync_direction is None
    assert cover._position_source_kind == "unconfirmed - position update loop timed out"
    assert any("still running after" in record.message for record in caplog.records)


async def test_position_loop_keeps_tracking_a_same_direction_retarget_mid_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-direction retarget landing mid-stop must not be wiped out.

    Regression guard for the race the independent review of aa33670+#116
    flagged (task #117): _has_live_tracking_in_direction() only checks
    direction, not the target value, so async_close_cover()'s own
    is_redundant_resend piggyback path can see this exact loop/task as
    still live - same direction, not done, _move_start_* still set - and
    deliberately leave it running rather than starting a tracking task of
    its own, on the assumption that this loop will keep tracking its new
    target. Before this fix, this loop had no way to notice the target
    had moved out from under it while its own control_blind_stop() for
    the *old* target was in flight: once that await returned, it
    unconditionally cleared is_closing/_move_start_*/_target_position and
    returned, ending itself - leaving the new leg's own, still-physically-
    continuing move with nothing left tracking or auto-stopping it at
    all. A plain _command_in_flight check could not close this gap either
    (noted by the same reviewer): the new leg has already returned by the
    time this loop's stop completes, so nothing is "in flight" to check.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    call_count = 0

    async def _stop_side_effect(*_args: object, **_kwargs: object) -> StopOutcome:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Stands in for async_close_cover()'s own is_redundant_resend
            # branch landing while this call is in flight: by the time it
            # returns, self._target_position already points at its own,
            # newer value - set directly here rather than by actually
            # calling async_close_cover(), to isolate this loop's own
            # reaction to the race from that method's side of it (already
            # covered by the is_redundant_resend tests above).
            cover._target_position = 25
            return StopOutcome.CONFIRMED
        # Proves the loop re-evaluated against the new target on the very
        # next tick instead of having already ended itself.
        raise _LoopKeptRunning

    api.control_blind_stop = AsyncMock(side_effect=_stop_side_effect)
    cover = _build_cover(hass, api)
    # Deliberately tracks self._target_position, not a fixed 40: the first
    # tick only needs current==40 to satisfy position_reached against the
    # original target (40), but the second tick, after the retarget to 25,
    # needs current==25 to satisfy it against the *new* one - a fixed 40
    # would make the second tick's own position_reached check (40 <= 25 +
    # ~2-point lead) false, so control_blind_stop() would never be called
    # a second time and this test would hang until pytest's own timeout
    # instead of ever reaching _LoopKeptRunning. Found while independently
    # re-verifying this test's own arithmetic rather than trusting it.
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", cover._target_position),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 40
    cover._move_start_time = time.monotonic()
    cover._move_start_position = 50

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    # Called once for the original target (40) and once for the retarget
    # (25) - proof the loop kept running and re-evaluated, rather than
    # clearing everything and returning after the first, now-stale stop.
    assert api.control_blind_stop.await_count == 2
    assert cover._target_position == 25
    assert cover._attr_is_closing is True
    assert cover._move_start_time is not None
    assert cover._move_start_position is not None


class _LoopKeptRunning(Exception):
    """Marks that the position loop survived a pass without stopping.

    Used by test_position_loop_does_not_stop_early_on_a_short_uncontended_leg
    and test_position_loop_does_not_stop_before_the_compensated_lead below
    to end the loop once it has proven the point, instead of letting it
    spin forever. Deliberately not asyncio.CancelledError and deliberately
    not delivered through a real task.cancel(): both of those were tried
    here first and both crashed the pytest-xdist worker running this test
    in real CI - first as an "ERROR at teardown ... CancelledError", and
    after switching to a genuine task.cancel() on a real
    asyncio.ensure_future() task, as an unrecoverable "[gw3] node down:
    Not properly terminated" with no traceback at all. Either way,
    something about asyncio's own cancellation machinery interacting
    with a real, long asyncio.sleep() and this suite's thread-based
    pytest-timeout does not survive cleanly in that environment. An
    ordinary exception type sidesteps all of that: the loop's own
    `except asyncio.CancelledError` does not catch it, there is no Task
    object to cancel or reap, and no real sleep ever happens, so nothing
    is left for asyncio, Home Assistant's task bookkeeping, or the test
    timeout watchdog to mishandle.
    """


async def test_position_loop_does_not_stop_early_on_a_short_uncontended_leg(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short leg's own travel time must no longer inflate the lead.

    Replaces this test's own former self (named ..._stops_early_to_
    compensate_stop_latency), which proved the opposite: that a short,
    5s close travel time turned the old ~0.5s uncontended baseline
    (MOVE_COMMAND_EXTRA_RESENDS * MOVE_COMMAND_RESEND_DELAY_SECONDS) into
    a disproportionately large ~10-point lead, large enough that a
    position estimate of 25% - still 5 points short of the literal 20%
    target - satisfied the compensated threshold and fired the stop
    early. That was the same mechanism as the separately-reported
    Küchenrolladen 92%/4%-undershoot/dashboard-sluggishness bug, just on
    the intermediate-target side of it instead of the full-travel side -
    see stop_lead_percent's own comment in _async_position_update_loop()
    for the shared root cause and why the uncontended baseline is now
    zeroed outright rather than merely recomputed. Uncontended, the lead
    is now exactly 0 regardless of how short the travel time is, so this
    same 25% estimate must no longer satisfy "<=20": the loop has to
    keep waiting for the estimate to actually reach the literal target
    instead, proving short legs no longer get an inflated lead at all.
    """

    sleep_calls = 0

    async def counting_sleep(*_args: object, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            # See _LoopKeptRunning's own docstring above for why this is
            # how this suite ends a loop proven not to stop, rather than
            # a real cancellation.
            raise _LoopKeptRunning

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 5.0})
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 25),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 30
    cover._target_position = 20

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    api.control_blind_stop.assert_not_awaited()
    api.cancel_pending_move.assert_not_called()
    assert cover._target_position == 20


async def test_position_loop_does_not_stop_before_the_compensated_lead(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The early trigger must not fire any earlier than the compensated lead.

    10s close travel time, uncontended: the lead (stop_latency_seconds /
    travel_time * 100 - see stop_lead_percent's own comment in
    _async_position_update_loop() for the derivation) is 0.0/10*100 = 0,
    putting the threshold at the literal target(20) alone. The estimate
    (30%) is still outside that - above it, since this is a close - so
    the loop must keep waiting rather than stopping prematurely. Same
    fixture as test_position_loop_widens_the_stop_lead_when_the_move_
    lock_is_busy below, uncontended, to show the contrast: there, the
    same 30% estimate *does* satisfy that test's own widened, contended
    threshold.
    """

    sleep_calls = 0

    async def counting_sleep(*_args: object, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            # Looping back for a second pass proves the first one did not
            # stop early - end the test right here instead of actually
            # waiting (see _LoopKeptRunning for why this isn't a real
            # cancellation). Exactly "== 2", not "and beyond": this patches
            # asyncio.sleep globally for as long as monkeypatch keeps it
            # installed, which outlives the `with pytest.raises(...)` block
            # below and reaches into this test's own fixture teardown - the
            # `hass` fixture's cleanup does its own unrelated real sleep(s)
            # there. ">= 2" raised again on that later, unguarded call with
            # nothing left to catch it, surfacing as a separate "ERROR at
            # teardown" even though the test itself had already passed.
            # Beyond the second call, behaving like a plain instant no-op
            # sleep (the same thing every other mocked-sleep test in this
            # file already does) is harmless.
            raise _LoopKeptRunning

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 10.0})
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 30),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 20

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    api.control_blind_stop.assert_not_awaited()
    api.cancel_pending_move.assert_not_called()
    assert cover._target_position == 20


async def test_position_loop_widens_the_stop_lead_when_the_move_lock_is_busy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contention for the shared transmit lock must widen the lead further.

    Exact same fixture as test_position_loop_does_not_stop_before_the_
    compensated_lead above - 10s close travel, estimate stuck at 30%,
    target 20 - which that test used to prove the plain, uncontended
    lead (now 0, threshold the literal target(20) alone) does *not* yet
    cover 30. The only difference here is api.move_lock_busy=True: the
    lead widens to include one STOP_COMMAND_LATENCY_SECONDS on top of
    that same, now-zeroed uncontended baseline (0.0 + 1.2 = 1.2s), to
    1.2/10*100 = 12 points (threshold target(20) + 12 = 32), which 30
    *does* satisfy - proving the loop actually reads move_lock_busy and
    widens the lead for it, rather than ignoring it or always capping at
    MAX_STOP_LEAD_PERCENT regardless. Real-log motivation: see
    move_lock_busy's own comment in api.py.
    """
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    api.move_lock_busy = True
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 10.0})
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 30),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 50
    cover._target_position = 20

    await cover._async_position_update_loop()

    api.cancel_pending_move.assert_called_once_with(cover._command_device_id)
    api.control_blind_stop.assert_awaited_once_with(
        cover._command_enum, device_id=cover._command_device_id
    )
    assert cover._attr_current_cover_position == 20
    assert cover._target_position is None
    assert cover._attr_is_closing is False


async def test_position_loop_caps_the_stop_lead_for_a_very_short_travel_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latency-compensation lead must not swallow the whole travel range.

    Regression guard: stop_lead_percent's own divisor
    (travel_time_for_lead) is floored at 0.1s, same as _update_position()'s
    - but unlike that calculation, whose final position *is* clamped to
    [0, 100], stop_lead_percent itself used to carry no matching ceiling.
    Exercised here with api.move_lock_busy=True: the uncontended baseline
    is now zeroed outright (see stop_lead_percent's own comment in
    _async_position_update_loop()), so it can no longer produce a lead
    needing capping however short travel_time_for_lead is - only the
    contended term (STOP_COMMAND_LATENCY_SECONDS) can still do that now.
    A 0.2s close time - clearly outside anything a real shutter reports,
    but exactly the kind of corrupt/legacy config value that floor exists
    for - makes the raw lead (STOP_COMMAND_LATENCY_SECONDS / 0.2 * 100)
    = 600 points: far more than the entire 0-100 range. Uncapped,
    target(30) + 600 = 630 is satisfied by any position at all, so the
    loop would fire its stop the instant it was entered, however far
    from 30 the blind actually was. Capped at MAX_STOP_LEAD_PERCENT (50),
    the threshold is a sane 80: a position of 85 - the blind has barely
    started closing from 100 - must not satisfy "85 <= 80" and must not
    stop yet.
    """

    sleep_calls = 0

    async def counting_sleep(*_args: object, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            # See _LoopKeptRunning's own docstring above for why this is
            # how this suite ends a loop proven not to stop, rather than a
            # real cancellation.
            raise _LoopKeptRunning

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    api.move_lock_busy = True
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 0.2})
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 85),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._target_position = 30

    with pytest.raises(_LoopKeptRunning):
        await cover._async_position_update_loop()

    api.control_blind_stop.assert_not_awaited()
    api.cancel_pending_move.assert_not_called()
    assert cover._target_position == 30


async def test_position_loop_endstop_target_skips_the_transmitter_wait_too(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit 0/100 target needs neither a wait nor a stop command.

    Extends test_position_loop_reaching_an_endstop_target_sends_no_active_stop
    (which only checks control_blind_stop) to also cover
    api.wait_for_transmitter_idle, since both are gated by the same
    needs_stop_command condition and a future change to one must not
    silently un-gate the other. api.wait_for_transmitter_idle is not
    called from this loop directly any more either way - control_blind_
    stop() does its own waiting internally - but is kept configured here
    so this would fail loudly, not silently pass, if that ever changed.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    api.wait_for_transmitter_idle = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 0),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 5
    cover._target_position = 0

    await cover._async_position_update_loop()

    api.wait_for_transmitter_idle.assert_not_awaited()
    api.control_blind_stop.assert_not_awaited()
    api.cancel_pending_move.assert_not_called()
    # Regression guard: the needs_stop_command=False branch (an explicit
    # 0/100 target) is gated by the same missing reset as the partial-
    # target branch above - see the matching comment in cover.py.
    assert cover._attr_is_closing is False


async def test_position_loop_reaching_an_endstop_target_sends_no_active_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching an explicit target of exactly 0 percent (full close) sends no stop.

    The motor's own physical end-stop handles full travel, unlike stopping
    partway which needs an actual stop command.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 0),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 5
    cover._target_position = 0

    await cover._async_position_update_loop()

    api.control_blind_stop.assert_not_awaited()
    # No stop command here means no move-retry loop to end either - see
    # cancel_pending_move()'s own docstring on why calling it when nothing
    # for this device is in progress would be harmless but is still never
    # called here, since needs_stop_command gates both.
    api.cancel_pending_move.assert_not_called()
    assert cover._attr_is_closing is False


async def test_position_loop_set_position_endstop_also_confirms_full_travel(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching 0/100 via an *explicit* target (set_cover_position) must
    confirm full-travel resync exactly like the plain open_cover()/
    close_cover() path (the target_position-is-None branch further below)
    already does. Before this fix, this branch set the
    live position correctly but never called _confirm_full_travel_resync(),
    so _position_confirmed_since_restart stayed False and the diagnostics
    history never recorded this transit, even though the blind had, in
    fact, just completed a full, verified close.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api)
    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 0),
    )
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 5
    cover._target_position = 0
    cover._move_start_position = 100
    # Set right after a Home Assistant restart, exactly like
    # async_close_cover's own logic - see _waiting_for_full_travel_resync's
    # own tests above for what this gates.
    cover._full_travel_resync_direction = "closing"

    await cover._async_position_update_loop()

    assert cover._position_confirmed_since_restart is True
    assert cover._full_travel_resync_direction is None
    api.record_position_update.assert_called_once_with(
        cover._command_device_id,
        source=cover._position_update_source,
        direction="closing",
        previous_position=100,
        new_position=0,
        position_source=cover._position_source_kind,
        confirmed_since_restart=True,
        status="estimated from full travel",
    )
    assert cover._attr_is_closing is False


async def test_confirm_full_travel_resync_marks_confirmed_even_unarmed(
    hass: HomeAssistant,
) -> None:
    """The real bug the test above's own hand-set _full_travel_resync_
    direction masked: a set_cover_position(0)/(100) call never arms that
    attribute in the first place (see async_close_cover's/async_open_
    cover's own ternary, gated on self._target_position is None, which a
    set_cover_position() leg's own _preserve_target=True never leaves
    true) - so in real production code, reaching the endstop this way
    always calls _confirm_full_travel_resync() with that attribute still
    None, never "closing"/"opening". Before this fix, the mismatch made
    the whole call a no-op, so self._position_confirmed_since_restart
    stayed False forever if the first interaction after a restart
    happened to be a set_cover_position() call instead of a plain
    open/close - found and fixed at the project owner's own request
    (2026-11-08), confirmed to be the same problem on both open and
    close since async_open_cover()'s own ternary is the identical
    mirror of async_close_cover()'s.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    assert cover._position_confirmed_since_restart is False
    assert cover._full_travel_resync_direction is None
    cover._move_start_position = 30

    cover._confirm_full_travel_resync("closing", 0)

    assert cover._position_confirmed_since_restart is True
    # Nothing was actually pending, so there is nothing to clear and no
    # special "estimated from full travel" diagnostic to record either -
    # only the flag itself changes.
    assert cover._full_travel_resync_direction is None
    api.record_position_update.assert_not_called()


async def test_confirm_full_travel_resync_leaves_a_different_pending_direction_alone(
    hass: HomeAssistant,
) -> None:
    """Confirming one direction must still not cancel a genuinely
    different, still-pending resync for the other direction - mirrors
    test_waiting_for_full_travel_resync_is_false_for_the_other_direction's
    own point, for this method instead of that one, now that this
    method's own early return no longer gates the confirmed flag too.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._full_travel_resync_direction = "opening"
    cover._move_start_position = 30

    cover._confirm_full_travel_resync("closing", 0)

    assert cover._position_confirmed_since_restart is True
    assert cover._full_travel_resync_direction == "opening"
    api.record_position_update.assert_not_called()


async def test_set_cover_position_to_an_endstop_confirms_position_after_a_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end version of the two tests above, through the real
    async_set_cover_position()/_async_position_update_loop() wiring
    rather than _confirm_full_travel_resync() called directly - proves
    the fix against the actual bug report, not just the helper method:
    a device whose first interaction after a restart is a
    set_cover_position(0) call (e.g. a dashboard's own position slider
    dragged all the way down, rather than the dedicated close button)
    must come out of it with self._position_confirmed_since_restart set,
    exactly as a plain close_cover() reaching the same endstop already
    does.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    assert cover._position_confirmed_since_restart is False
    cover._attr_current_cover_position = 30

    await cover.async_set_cover_position(**{ATTR_POSITION: 0})

    assert cover._target_position == 0
    assert cover._attr_is_closing is True
    # Never armed in the first place - see _confirm_full_travel_resync()'s
    # own docstring for why that is exactly the gap this test closes.
    assert cover._full_travel_resync_direction is None

    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 0),
    )
    # _start_position_tracking() was mocked out above, exactly like the
    # other async_set_cover_position() tests above - this call stands in
    # for the real background task it would otherwise have spawned.
    await cover._async_position_update_loop()

    assert cover._attr_current_cover_position == 0
    assert cover._position_confirmed_since_restart is True
    assert cover._attr_is_closing is False


async def test_set_cover_position_to_an_endstop_also_confirms_when_opening(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the test above, for the opening side.

    Added on the project owner's own direct follow-up question of whether
    both directions were truly verified, not just argued to be identical
    by reading async_open_cover()'s ternary alongside async_close_cover()'s
    (see _confirm_full_travel_resync()'s own docstring) - that argument is
    correct (the two ternaries differ only in the "opening"/"closing"
    string literal, see cover.py directly), but is a code-reading argument,
    not a test. This gives the opening side the same direct, end-to-end
    proof the test above already gives the closing side, rather than
    leaving it resting on the symmetry argument alone.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    assert cover._position_confirmed_since_restart is False
    cover._attr_current_cover_position = 70

    await cover.async_set_cover_position(**{ATTR_POSITION: 100})

    assert cover._target_position == 100
    assert cover._attr_is_opening is True
    # Never armed in the first place - same reason as the closing-side
    # test above, mirrored: async_open_cover()'s own ternary is gated on
    # self._target_position is None exactly like async_close_cover()'s,
    # and async_set_cover_position() already set it to 100 beforehand.
    assert cover._full_travel_resync_direction is None

    monkeypatch.setattr(
        cover,
        "_update_position",
        lambda: setattr(cover, "_attr_current_cover_position", 100),
    )
    await cover._async_position_update_loop()

    assert cover._attr_current_cover_position == 100
    assert cover._position_confirmed_since_restart is True
    assert cover._attr_is_opening is False


# ---------------------------------------------------------------------------
# _update_position(): the elapsed-time position estimate itself.
#
# The stick has no return channel from the motor - it never learns the
# shutter's real position while moving. Every position shown in Home
# Assistant while a move is in progress is this calculation: elapsed time
# since the move started, divided by the configured (or default) travel
# time for that direction, applied to the position the move started from.
# This is the actual core logic the UI's "live" position during a move is
# built on, so it gets its own direct tests with controlled elapsed time,
# not just indirectly through the loop tests above (which replace this
# method with a stub).
# ---------------------------------------------------------------------------


async def test_update_position_estimates_opening_progress_from_elapsed_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._attr_is_opening = True
    cover._move_start_time = clock()
    cover._move_start_position = 0

    clock.advance(5.0)  # a quarter of the 20s open travel time
    cover._update_position()

    assert cover._attr_current_cover_position == 25
    assert cover._attr_is_closed is False


async def test_update_position_estimates_closing_progress_from_elapsed_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 10.0})
    cover._attr_is_closing = True
    cover._move_start_time = clock()
    cover._move_start_position = 100

    clock.advance(5.0)  # half of the 10s close travel time
    cover._update_position()

    assert cover._attr_current_cover_position == 50


async def test_update_position_holds_at_start_during_the_transmit_dead_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A _move_start_time pushed into the future must not run the position
    estimate backwards past the start position.

    async_open_cover()/async_close_cover() used to push _move_start_time
    this far into the future themselves, modeling the real ~1.1-1.2s it
    takes the stick to accept a write before the motor actually starts
    turning - reverted (Issue #78, 2026-11-07; see both call sites' own
    comments) because it froze the reported position for just over a
    second at the start of every single move, a real, user-visible
    regression of its own. Neither call site constructs this scenario any
    more, but _update_position()'s own max(0.0, ...) clamp - the actual
    thing under test here - is left in place as a defensive guard rather
    than removed along with the one caller that used to exercise it, so
    this test now builds the scenario directly rather than through
    async_open_cover()/async_close_cover(): while time.monotonic() has
    not yet reached a (here, manually set) pushed-forward _move_start_
    time, elapsed_time would otherwise go negative and run total_
    position_change backwards past _move_start_position - the clamp
    holds the estimate exactly at the start position instead, and the
    position resumes its ordinary ramp, from that same start position,
    once real time catches up to it.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._attr_is_opening = True
    cover._move_start_position = 0
    cover._move_start_time = clock() + STOP_COMMAND_LATENCY_SECONDS

    clock.advance(STOP_COMMAND_LATENCY_SECONDS - 0.2)  # still inside the dead time
    cover._update_position()

    assert cover._attr_current_cover_position == 0

    clock.advance(0.2 + 5.0)  # past the dead time, then a quarter of the 20s travel
    cover._update_position()

    assert cover._attr_current_cover_position == 25


async def test_update_position_clamps_overshoot_to_the_travel_limits(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Elapsed time past the travel time must clamp to 0/100, never overshoot.

    This is what makes it safe for the tracking loop to rely on this method
    alone to detect "fully closed"/"fully open" (see the loop tests below) -
    an estimate of -8% or 107% would break that comparison.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 10.0})
    cover._attr_is_closing = True
    cover._move_start_time = clock()
    cover._move_start_position = 20

    clock.advance(60.0)  # far past the 10s close travel time
    cover._update_position()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


async def test_update_position_is_a_noop_without_a_move_in_progress(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No _move_start_time means no move is tracked - nothing to estimate."""
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 42
    cover._move_start_time = None
    cover._move_start_position = None

    cover._update_position()

    assert cover._attr_current_cover_position == 42


# ---------------------------------------------------------------------------
# _async_position_update_loop(): the two branches reached purely by elapsed
# time with no explicit _target_position - a plain async_open_cover() or
# async_close_cover() all the way to an end stop, as opposed to a partial
# set_cover_position() target (already covered above) or an explicit 0/100
# target (also already covered above). These use the loop's own real
# _update_position() (not stubbed), advancing a fake clock each time the
# loop's asyncio.sleep() is awaited, so the loop actually runs its own
# elapsed-time math to termination exactly as it would in production.
# ---------------------------------------------------------------------------


async def test_position_loop_detects_fully_closed_by_elapsed_time_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)

    async def advancing_sleep(seconds: float, *_a: object, **_kw: object) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(asyncio, "sleep", advancing_sleep)
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 1.0})
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._move_start_time = clock()
    cover._move_start_position = 100
    cover._target_position = None

    await cover._async_position_update_loop()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True
    assert cover._attr_is_closing is False
    # Reaching 0/100 by elapsed time alone relies on the motor's own
    # end-stop, same as an explicit target of exactly 0/100 - no active
    # stop command is sent for either.
    api.control_blind.assert_not_awaited()


async def test_position_loop_detects_fully_open_by_elapsed_time_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)

    async def advancing_sleep(seconds: float, *_a: object, **_kw: object) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(asyncio, "sleep", advancing_sleep)
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 1.0})
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 0
    cover._move_start_time = clock()
    cover._move_start_position = 0
    cover._target_position = None

    await cover._async_position_update_loop()

    assert cover._attr_current_cover_position == 100
    assert cover._attr_is_closed is False
    assert cover._attr_is_opening is False
    api.control_blind.assert_not_awaited()


async def test_waiting_for_full_travel_resync_holds_until_the_full_travel_time_elapses(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_waiting_for_full_travel_resync() is the loop's gate against declaring
    "fully closed/open" too early after a restart.

    Set right after Home Assistant restarts (see async_open_cover's/
    async_close_cover's own _full_travel_resync_direction logic), it means
    this move's elapsed time must cover the *entire* configured travel time
    before an end stop reached by elapsed time alone is trusted - guarding
    against a stale/wrong _move_start_position restored from a previous run
    making the loop declare "fully closed" after only a fraction of the
    real travel. Tested directly against this pure gate rather than by
    running the full loop, which would make the same point far less
    directly and at the mercy of the mocked loop's own scheduling.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 2.0})
    cover._move_start_time = clock()
    cover._full_travel_resync_direction = "closing"

    clock.advance(1.0)  # half of the 2s close travel time
    assert cover._waiting_for_full_travel_resync("closing") is True

    clock.advance(1.5)  # 2.5s total, past the 2s close travel time
    assert cover._waiting_for_full_travel_resync("closing") is False


async def test_waiting_for_full_travel_resync_is_false_for_the_other_direction(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resync pending for one direction must not gate the other.

    E.g. a close that was interrupted and immediately reopened must not
    have its still-pending close-resync flag block the open side's own
    "fully open" detection.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 2.0})
    cover._move_start_time = clock()
    cover._full_travel_resync_direction = "closing"

    assert cover._waiting_for_full_travel_resync("opening") is False


# ---------------------------------------------------------------------------
# _handle_event(): reacting to a device-activity signal picked up from the
# original remote's own RF identity (status discovery/calibration), which
# is how this integration learns a move is happening at all - the stick
# cannot ask the motor directly. This is the other half of the "no
# bidirectional feedback" story: _update_position() estimates *while*
# moving, _handle_event() is what starts and stops that estimate in the
# first place in response to that overheard activity.
# ---------------------------------------------------------------------------


async def test_handle_event_started_moving_up_sets_opening_and_starts_tracking(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_current_cover_position = 30

    cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    assert cover._move_start_position == 30
    cover._start_position_tracking.assert_called_once_with()


async def test_handle_event_started_moving_down_sets_closing_and_starts_tracking(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_current_cover_position = 70

    cover._handle_event(EVENT_STARTED_MOVING_DOWN)

    assert cover._attr_is_closing is True
    assert cover._attr_is_opening is False
    cover._start_position_tracking.assert_called_once_with()


async def test_handle_event_started_moving_from_unknown_position_anchors_to_zero(
    hass: HomeAssistant,
) -> None:
    """A move-start echo arriving while position is still None (e.g. right
    after a restart with nothing restored - see async_added_to_hass()'s own
    comment on why that can now happen) must not leave _move_start_position
    None too: _update_position() refuses to track from a None baseline (its
    own first guard), which would silently freeze tracking at "unknown"
    forever, even once the blind is plainly moving. Mirrors async_open_
    cover()'s/async_close_cover()'s identical guard for the same unknown
    starting state on a Home Assistant-issued command.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_current_cover_position = None

    cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._attr_current_cover_position == 0
    assert cover._move_start_position == 0
    assert cover._attr_is_opening is True
    cover._start_position_tracking.assert_called_once_with()


async def test_handle_event_respects_invert_direction_for_the_physical_signal(
    hass: HomeAssistant,
) -> None:
    """A physical "moving up" signal on an inverted device is a logical close.

    Some Schellenberg motors are wired/oriented so the stick's "up" command
    physically closes rather than opens - invert_direction flips the
    logical meaning of commands sent (see async_open_cover/
    async_close_cover) and must equally flip the logical meaning of
    activity overheard from the original remote, or the UI would show the
    cover opening while it is actually closing.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, invert_direction=True)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._attr_is_closing is True
    assert cover._attr_is_opening is False


async def test_handle_event_stopped_freezes_the_estimated_position(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 20
    cover._move_start_time = clock()
    cover._move_start_position = 20
    cover._target_position = 55  # an externally-interrupted set_position

    clock.advance(5.0)  # a quarter of the 20s open travel time: +25
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 45
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False


async def test_handle_event_stopped_after_backstop_exhaustion_does_not_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A late STOPPED after the backstop gave up must not break log formatting.

    Regression guard for a second bug the same independent review
    (2026-11-01) found alongside the retarget-leak covered above: once the
    backstop branch in _async_position_update_loop() sets self._attr_
    current_cover_position to None (see AUTO_STOP_CONFIRMATION_BACKSTOP_
    SECONDS's own exhaustion branch), nothing stops a real, late STOPPED
    confirmation from the stick itself arriving afterwards - the backstop
    giving up does not mean the motor stopped trying to answer. This
    handler's own log line used to format that None straight into "%d%%",
    which raises inside the logging module's own lazy formatting
    (record.getMessage()) - normally caught by a handler's own
    handleError() rather than propagating, so this never crashed Home
    Assistant, but it did turn exactly the event this handler most needs
    to report cleanly into a confusing traceback instead. Checked here by
    calling getMessage() directly, the same way a handler's own formatting
    would, rather than relying on whichever error-swallowing behaviour
    caplog's own handler happens to have.
    """
    caplog.set_level(logging.INFO, logger="custom_components.schellenberg_usb.cover")
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    # Mirrors exactly what the backstop-exhaustion branch itself leaves
    # behind (see _async_position_update_loop()'s own code) - position
    # genuinely unknown, no move tracked any more.
    cover._attr_current_cover_position = None
    cover._attr_is_closed = None
    cover._attr_is_opening = False
    cover._attr_is_closing = False
    cover._move_start_time = None
    cover._move_start_position = None
    cover._target_position = None

    cover._handle_event(EVENT_STOPPED)

    stopped_messages = [
        record.getMessage() for record in caplog.records if "STOPPED" in record.msg
    ]
    assert stopped_messages
    assert "unknown" in stopped_messages[0]
    # A STOPPED echo must not snap to the stale target - see _handle_event's
    # own comment: the blind was interrupted externally (e.g. the physical
    # remote), so the commanded target was never actually reached.
    assert cover._target_position is None
    cover._stop_position_tracking.assert_called_once_with()


async def test_handle_event_stopped_cancels_any_pending_move_for_this_device(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every STOPPED echo cancels a pending move/resend for this device_id,
    not only one this entity's own async_stop_cover() or the auto-stop-
    at-target loop issued.

    Closes the one source control_blind_with_retry()'s own cancellation
    check (see MOVE_COMMAND_EXTRA_RESENDS's own comment in const.py)
    cannot see on its own: a stop pressed directly on the physical remote
    or a Gurtwickler winder's own local buttons, which never calls
    cancel_pending_move() itself since it never goes through this
    integration's own command path. cancel_pending_move() is a no-op
    without a move actually in progress for this device_id (see its own
    docstring) - this only proves _handle_event() calls it unconditionally,
    the same way async_stop_cover()'s own identical first line does.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 20
    cover._move_start_time = clock()
    cover._move_start_position = 20

    cover._handle_event(EVENT_STOPPED)

    api.cancel_pending_move.assert_called_once_with(cover._command_device_id)


async def test_handle_event_stopped_snaps_a_near_zero_targetless_close_to_zero(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEAR_END_STOP_SNAP_TOLERANCE_PERCENT: calibration drift vs a real stop.

    Real-world report (2026-10-31): closing a blind all the way via its
    remote consistently left the estimate at 2% instead of 0%, every
    time, regardless of how directly/quickly it was driven down - the
    motor's own physical end stop does not line up exactly with elapsed
    time crossing 0% once calibration's own travel_time is an
    approximation of the real motor's, which it necessarily always is.
    An untargeted move (_target_position already None, as every remote/
    wall-switch/Gurtwickler-driven move always is - see EVENT_STARTED_
    MOVING_DOWN's own handling) landing this close to the edge it was
    already heading toward is this drift, not a deliberate mid-travel
    stop - contrast test_handle_event_stopped_freezes_the_estimated_
    position above, which proves a genuine mid-travel stop (far from
    either edge, with a target still set) is correctly left exactly at
    the raw estimate, un-snapped.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._move_start_time = clock()
    cover._move_start_position = 100
    cover._target_position = None  # a remote/Gurtwickler-driven move

    clock.advance(19.6)  # 98% of the 20s close travel time: estimate lands
    # at ~2% (1% once _update_position()'s int() truncates the
    # binary-float result of 19.6/20.0*100 - either way, within tolerance)
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


async def test_handle_event_stopped_snaps_a_near_full_targetless_open_to_100(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_handle_event_stopped_snaps_a_near_zero_targetless_close_to_
    zero above, for the opening/98% side of the same real-world report.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 0
    cover._move_start_time = clock()
    cover._move_start_position = 0
    cover._target_position = None  # a remote/Gurtwickler-driven move

    clock.advance(19.6)  # 98% of the 20s open travel time: estimate lands at 98%
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 100
    assert cover._attr_is_closed is False


async def test_handle_event_stopped_does_not_snap_a_near_full_open_started_mid_travel(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEAR_END_STOP_SNAP_TOLERANCE_PERCENT also requires the move to have
    *started* near the opposite edge - see its own comment in const.py for
    the direct user report that caught a first, position-only version of
    this check. Starting at 20% (nowhere near the opposite edge, 0%) and
    landing at a deliberately chosen 98% must be left exactly there, not
    forced to 100% the way test_handle_event_stopped_snaps_a_near_full_
    targetless_open_to_100 above - which starts at 0% - correctly is.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 20
    cover._move_start_time = clock()
    cover._move_start_position = 20  # not near the opposite edge (0%)
    cover._target_position = None  # a remote/Gurtwickler-driven move

    clock.advance(15.6)  # (98 - 20) / 100 * 20s: estimate lands at 98%
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 98
    assert cover._attr_is_closed is False


async def test_handle_event_stopped_does_not_snap_a_near_zero_close_started_mid_travel(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_handle_event_stopped_does_not_snap_a_near_full_open_
    started_mid_travel above for the closing/near-zero side. Starting at
    80% (nowhere near the opposite edge, 100%) and landing at a
    deliberately chosen ~2% must be left there, not forced to 0%.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_CLOSE_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 80
    cover._move_start_time = clock()
    cover._move_start_position = 80  # not near the opposite edge (100%)
    cover._target_position = None  # a remote/Gurtwickler-driven move

    clock.advance(15.6)  # (80 - 2) / 100 * 20s: estimate lands at ~2%
    # (1%, not 2%, once int() truncates the actual binary-float result -
    # see the sibling snap-to-zero test's own comment above; either way,
    # within tolerance, and the point here is that it stays un-snapped)
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 1
    assert cover._attr_is_closed is False


async def test_handle_event_stopped_logs_the_recalculated_position(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: the STOPPED info log must print the recalculated
    position, not a stale polling-tick snapshot.

    The "Device %s STOPPED (position: %d%%)" info log used to be emitted
    *before* _update_position() recalculated self._attr_current_cover_
    position for the elapsed time, so it always printed the position as
    of the last 0.2s polling tick (here, the move's unchanged starting
    position of 20%) instead of where _handle_event's own recalculation -
    the same one test_handle_event_stopped_freezes_the_estimated_position
    already proves lands on 45% - says the blind actually stopped. Purely
    a logging-field staleness: self._attr_current_cover_position itself,
    and the diagnostics history record_position_update() writes right
    after this log call, were never affected.
    """
    caplog.set_level(logging.INFO, logger="custom_components.schellenberg_usb.cover")
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_OPEN_TIME: 20.0})
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 20
    cover._move_start_time = clock()
    cover._move_start_position = 20

    clock.advance(5.0)  # a quarter of the 20s open travel time: +25
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == 45
    stopped_records = [
        record for record in caplog.records if "STOPPED" in record.message
    ]
    assert len(stopped_records) == 1
    # Before the fix this asserted "(position: 20%)" - the stale,
    # pre-recalculation value - and failed.
    assert "(position: 45%)" in stopped_records[0].message
    assert "(position: 20%)" not in stopped_records[0].message


@pytest.mark.parametrize(
    ("start_position", "elapsed", "expected"),
    [
        (2, 10.0, 0),  # within rounding distance of the bottom end stop
        (97, 10.0, 100),  # within rounding distance of the top end stop
    ],
)
async def test_handle_event_stopped_clamps_a_near_endstop_position(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    start_position: int,
    elapsed: float,
    expected: int,
) -> None:
    """A STOPPED right at an end stop must clamp to a clean 0/100.

    Without this, floating-point elapsed-time math could leave the cover
    reporting 99% as "open" or 1% as "closed" indefinitely after physically
    reaching an end stop, since nothing else would ever nudge it the rest
    of the way without a further command.
    """
    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)
    api = MagicMock()
    opening = expected == 100
    device_data = {CONF_OPEN_TIME: 1.0} if opening else {CONF_CLOSE_TIME: 1.0}
    cover = _build_cover(hass, api, device_data=device_data)
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = opening
    cover._attr_is_closing = not opening
    cover._attr_current_cover_position = start_position
    cover._move_start_time = clock()
    cover._move_start_position = start_position

    clock.advance(elapsed)  # far past the 1s travel time either way
    cover._handle_event(EVENT_STOPPED)

    assert cover._attr_current_cover_position == expected
    assert cover._attr_is_closed == (expected == 0)


async def test_handle_event_ignores_an_unrecognized_event(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 33
    cover._attr_is_opening = False
    cover._attr_is_closing = False

    cover._handle_event("some_future_event_type")

    assert cover._attr_current_cover_position == 33
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False


# ---------------------------------------------------------------------------
# _has_live_position_tracking(): the predicate _handle_event() and
# _start_position_tracking() both now rely on to tell a position-tracking
# task that is genuinely still running apart from one merely sitting in
# the slot unmanaged (done, or mid-cancellation).
# ---------------------------------------------------------------------------


async def test_has_live_position_tracking_false_when_idle(
    hass: HomeAssistant,
) -> None:
    """A fresh/idle cover has no live position tracking."""
    cover = _build_cover(hass, MagicMock())

    assert cover._has_live_position_tracking() is False


async def test_has_live_position_tracking_false_when_task_already_done(
    hass: HomeAssistant,
) -> None:
    """A finished tracking task - the move already ended - is not live."""
    cover = _build_cover(hass, MagicMock())
    fake_task = MagicMock()
    fake_task.done.return_value = True
    cover._position_update_task = fake_task

    assert cover._has_live_position_tracking() is False


async def test_has_live_position_tracking_false_when_task_is_cancelling(
    hass: HomeAssistant,
) -> None:
    """A task mid-cancellation must not count as live either.

    Mirrors _start_position_tracking()'s own pre-existing guard, which this
    helper now backs - see its own docstring.
    """
    cover = _build_cover(hass, MagicMock())
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = True
    cover._position_update_task = fake_task

    assert cover._has_live_position_tracking() is False


async def test_has_live_position_tracking_true_when_task_is_running(
    hass: HomeAssistant,
) -> None:
    """A task that is neither done nor cancelling counts as live."""
    cover = _build_cover(hass, MagicMock())
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = False
    cover._position_update_task = fake_task

    assert cover._has_live_position_tracking() is True


# ---------------------------------------------------------------------------
# _handle_event() STARTED_MOVING vs. a stale _target_position: a remote
# control or wall switch (or an electric Gurtwickler's own local buttons)
# drives a move this entity never initiated, so _handle_event() is the
# *only* signal it ever gets - unlike async_open_cover()/async_close_
# cover(), it never otherwise gets a chance to clear a target left over
# from an earlier, not-cleanly-finished set_cover_position() leg.
# ---------------------------------------------------------------------------


async def test_handle_event_started_moving_clears_stale_target_if_untracked(
    hass: HomeAssistant,
) -> None:
    """Regression guard: an independently-triggered move must not inherit a
    stale target.

    _target_position can be left behind by an earlier set_cover_position()
    leg that never cleanly finished (test_async_open_cover_clears_stale_
    target_when_a_preserved_leg_fails's own docstring documents one way
    that happens - a failed leg is only one of several; a STOPPED echo
    that never arrives at all, the exact reliability problem this project
    keeps running into, is another). With no position-tracking task yet
    live, a STARTED_MOVING echo can only be *reporting* a move driven
    independently - a paired remote control, a wall switch, or an electric
    Gurtwickler's own local buttons - never one this entity itself started
    (see _has_live_position_tracking()'s own docstring for why). Any
    leftover target is therefore stale and must not cut that move short:
    before this fix, _handle_event() never touched _target_position at
    all, so _async_position_update_loop() would auto-stop - and report -
    the blind at the old, unrelated percentage forever, even though it
    physically kept going to its real 100/0 end stop.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._target_position = 55  # stale, left over from an earlier leg
    assert cover._position_update_task is None  # nothing tracking yet

    cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._target_position is None
    assert cover._attr_is_opening is True


async def test_handle_event_started_moving_preserves_a_live_targets_position(
    hass: HomeAssistant,
) -> None:
    """A STARTED_MOVING echo confirming our own command must keep its target.

    Mirrors the regression guard above for the opposite case: when a
    position-tracking task is already live - which happens once async_
    open_cover()/async_close_cover()'s own control_blind_with_retry() call
    has actually succeeded - this echo is only confirming a move this
    entity already knows about, so its _target_position (a set_cover_
    position() leg's own, genuinely current target) is not stale and must
    survive untouched. See test_handle_event_during_a_pending_command_
    does_not_clear_its_target below for the other half of this same
    guard: a command still in flight, before its own tracking task exists
    yet.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = False
    cover._position_update_task = fake_task
    cover._target_position = 55  # a genuinely in-progress set_cover_position()

    cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._target_position == 55


async def test_handle_event_during_a_pending_command_does_not_clear_its_target(
    hass: HomeAssistant,
) -> None:
    """A concurrent echo arriving mid-command must not clear its target either.

    The two tests above only cover the two states that existed before
    self._command_in_flight was introduced: nothing of ours in flight at
    all, or a command whose own tracking task is already live. Between
    those two moments sits a real, non-theoretical window: async_open_
    cover()/async_close_cover() only start their own tracking task *after*
    control_blind_with_retry() actually succeeds (see test_async_open_
    cover_does_not_start_tracking_before_retry_succeeds), and that call
    can itself stay suspended for a real stretch - hub-wide lock
    contention, the busy-retry ack-wait. self._target_position is already
    set for the whole of that stretch whenever this is a set_cover_
    position() leg (_preserve_target=True), but self._position_update_
    task is still None.

    An independent, concurrently-triggered echo (a different remote
    press, a wall switch, a Gurtwickler's own local buttons) can land at
    exactly that moment. Checking only _has_live_position_tracking() - as
    the first version of this fix did - would misread that window as
    "nothing of ours is happening" and wrongly null out a target this
    same entity's own in-flight command still needs, capping the leg at
    whatever the live estimate happens to be once tracking finally starts
    instead of the position it was actually asked to reach.
    self._command_in_flight exists specifically to keep that window from
    being mistaken for idle.
    """

    async def fake_retry(*_args: object, **_kwargs: object) -> bool:
        # Simulates a concurrent, independently-triggered echo landing
        # while this call is still suspended here - exactly the moment a
        # real hub-wide lock wait or busy-retry ack-wait can occupy.
        cover._handle_event(EVENT_STARTED_MOVING_UP)
        return True

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(side_effect=fake_retry)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._target_position = 70  # a genuinely in-progress set_cover_position()
    assert cover._position_update_task is None  # tracking hasn't started yet

    await cover.async_open_cover(_preserve_target=True)

    assert cover._target_position == 70


async def test_handle_event_stopped_does_not_clear_a_concurrent_commands_target(
    hass: HomeAssistant,
) -> None:
    """A STOPPED echo for a just-superseded move must not clear a new leg's target.

    Same self._command_in_flight window as the two tests above, but for
    the STOPPED branch instead of STARTED_MOVING - nothing there ever
    checked it, even though the branch's own comment already acknowledges
    a target can still be set when STOPPED arrives (it reads that as
    proof of an *external* interruption and clears it unconditionally on
    that assumption).

    Concrete scenario this misses: a set_cover_position() leg reverses
    direction while an older, different move is still live - say that
    older move is closing, and _async_position_update_loop()'s own
    auto-stop-at-target branch has already sent it a real stop. The new
    leg sets self._target_position to its own, brand new target
    synchronously (see async_set_cover_position()), then its own async_
    open_cover()/async_close_cover() call awaits _async_cancel_position_
    tracking() to tear down the superseded move's tracking task - a real
    suspension point, during which self._position_update_task is already
    cleared (the real method clears it synchronously before its own
    await), so _has_live_position_tracking() alone cannot tell this
    window apart from genuine idle, exactly as test_handle_event_during_
    a_pending_command_does_not_clear_its_target already established for
    STARTED_MOVING. If the older move's own genuine STOPPED echo lands
    during exactly that window, it is not an external interruption of the
    new leg at all - it is just confirming the move the new leg itself is
    in the middle of superseding - so it must not null out a target that
    belongs to a different, still in-flight command.
    """

    async def fake_cancel_tracking(*_args: object, **_kwargs: object) -> None:
        # Mirrors _async_cancel_position_tracking()'s own real order: it
        # clears self._position_update_task synchronously, before its own
        # suspension point - so by the time a concurrent echo could land
        # here, nothing is "live" there any more, and self._command_in_
        # flight is the only thing left that can tell this apart from
        # genuine idle.
        cover._position_update_task = None
        cover._handle_event(EVENT_STOPPED)

    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._async_cancel_position_tracking = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_cancel_tracking
    )
    # The superseded move: closing, so this new open() is not itself
    # mistaken for a redundant resend of the same direction (which would
    # skip the cancel-tracking await entirely via a different path).
    cover._attr_is_closing = True
    cover._attr_is_opening = False
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = False
    cover._position_update_task = fake_task
    cover._target_position = 70  # the new leg's own, brand new target

    await cover.async_open_cover(_preserve_target=True)

    assert cover._target_position == 70


# ---------------------------------------------------------------------------
# _handle_secondary_activity()/_clear_optimistic_secondary_move(): an
# optimistic, direction-guessed dashboard state triggered by a secondary
# identity's own frame (a second remote/wall-switch paired to the same
# motor). Deliberately never decodes that frame's own command byte - see
# the two methods' own docstrings/comments for why - so these tests never
# pass one in either; async_call_later is stubbed so no test schedules a
# real timer, and _clear_optimistic_secondary_move is always exercised by
# calling it directly rather than by waiting one out.
# ---------------------------------------------------------------------------


async def test_handle_secondary_activity_guesses_opening_when_mostly_closed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later", schedule
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 20

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    schedule.assert_called_once_with(
        hass,
        OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS,
        cover._clear_optimistic_secondary_move,
    )
    assert cover._optimistic_secondary_move_unsub is schedule.return_value


async def test_handle_secondary_activity_guesses_closing_when_mostly_open(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later",
        MagicMock(return_value=MagicMock()),
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 80

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_closing is True
    assert cover._attr_is_opening is False


async def test_handle_secondary_activity_does_nothing_while_already_tracking(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = MagicMock()
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later", schedule
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 20
    cover._attr_is_opening = False
    cover._attr_is_closing = True  # a real move already tracked, in progress
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = False
    cover._position_update_task = fake_task

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_closing is True
    assert cover._attr_is_opening is False
    schedule.assert_not_called()


async def test_handle_secondary_activity_does_nothing_while_command_in_flight(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = MagicMock()
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later", schedule
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 20
    cover._command_in_flight = True

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    schedule.assert_not_called()


async def test_handle_secondary_activity_does_nothing_without_a_known_position(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = MagicMock()
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later", schedule
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = None

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    schedule.assert_not_called()


async def test_handle_secondary_activity_debounces_a_repeated_frame(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote commonly repeats its frame every ~400ms while held down.

    The first frame's guess and its withdrawal timeout must stand as they
    are - not be re-guessed or re-scheduled on every repeat.
    """
    schedule = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later", schedule
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 20

    cover._handle_secondary_activity("irrelevant - never decoded")
    cover._attr_current_cover_position = 80  # would guess the other way
    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_opening is True  # unchanged from the first guess
    assert cover._attr_is_closing is False
    schedule.assert_called_once()


async def test_clear_optimistic_secondary_move_withdraws_unconfirmed_guess(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_is_opening = True
    cover._optimistic_secondary_move_unsub = MagicMock()
    cover._position_update_task = None  # no real echo ever took over

    cover._clear_optimistic_secondary_move(datetime(2024, 1, 1))

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._optimistic_secondary_move_unsub is None


async def test_clear_optimistic_secondary_move_leaves_real_tracking_alone(
    hass: HomeAssistant,
) -> None:
    """If the primary identity's own echo already took over, don't stomp it."""
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_is_opening = True  # the real, decoded direction by now
    cover._attr_is_closing = False
    cover._optimistic_secondary_move_unsub = MagicMock()
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancelling.return_value = False
    cover._position_update_task = fake_task  # _handle_event() already took over

    cover._clear_optimistic_secondary_move(datetime(2024, 1, 1))

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    assert cover._optimistic_secondary_move_unsub is None


async def test_clear_optimistic_secondary_move_leaves_a_pending_command_alone(
    hass: HomeAssistant,
) -> None:
    """A command already in flight must not be pulled back to idle either.

    Mirrors the test above, but for the other half of _handle_event()'s own
    two-part check (_has_live_position_tracking() or self._command_in_
    flight), which this method's guard had not been mirroring: async_open_
    cover()/async_close_cover() set their own optimistic is_opening/
    is_closing synchronously, before their own tracking task exists - it
    only starts once control_blind_with_retry() actually succeeds, which
    can stay suspended for a real stretch (hub-wide lock contention, the
    busy-retry wait), same window test_handle_event_during_a_pending_
    command_does_not_clear_its_target already proved for _handle_event()
    itself. Before this fix, this method checked only _has_live_position_
    tracking() - unlike _handle_secondary_activity()'s own, already-correct
    mirror of this same check - so a withdrawal timeout firing inside that
    window pulled a real, in-flight command's own optimistic state straight
    back down to idle a few seconds into it.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_is_opening = True  # async_open_cover()'s own optimistic state
    cover._attr_is_closing = False
    cover._command_in_flight = True  # control_blind_with_retry() still pending
    cover._optimistic_secondary_move_unsub = MagicMock()
    cover._position_update_task = None  # tracking hasn't started yet

    cover._clear_optimistic_secondary_move(datetime(2024, 1, 1))

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    assert cover._optimistic_secondary_move_unsub is None


async def test_secondary_activity_from_a_stale_post_restart_frame_self_corrects(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closes out the open "shows closed right after restart" regression note.

    That regression (see CHANGELOG.md / commit d21735d) was caused by a
    mechanism that no longer exists at all: the old close+open elapsed-
    timer detection feature (reverted) plus a command-byte decode
    fallback (also reverted) together let a stale, pre-restart secondary
    frame - delivered as a burst the instant the integration starts
    reading the serial port again - commit a blind straight to a wrong,
    permanent state, since that old timer had no withdrawal of its own
    to correct a guess nothing ever confirmed.

    _handle_secondary_activity()/_clear_optimistic_secondary_move() is
    the mechanism that replaced it, and until now each had only ever
    been tested in isolation - never against this exact scenario end to
    end. Simulates it directly: a position already restored from before
    the restart (restore_state's own job, unrelated to either mechanism
    and not re-tested here), one stale secondary frame with nothing ever
    confirming it, then the withdrawal timeout firing with no primary
    echo and no command of this entity's own having started either.
    Confirms the worst case under the current design is a several-
    second transient guess that clears itself - never a wrong state
    left standing indefinitely, the way the old, withdrawn mechanism
    did. No production code changed for this: the redesign that
    replaced the old mechanism already closes this gap as a side
    effect of never trusting an unconfirmed guess for longer than
    OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS; this test only makes
    that confirmed rather than assumed.
    """
    monkeypatch.setattr(
        "custom_components.schellenberg_usb.cover.async_call_later",
        MagicMock(return_value=MagicMock()),
    )
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 70  # restored: mostly open

    cover._handle_secondary_activity("irrelevant - never decoded")

    assert cover._attr_is_closing is True  # transient guess only
    assert cover._attr_is_opening is False

    # The primary identity's own echo never arrives for this stale frame
    # - nothing physically moved - so only the withdrawal timeout fires.
    cover._clear_optimistic_secondary_move(datetime(2024, 1, 1))

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._optimistic_secondary_move_unsub is None


# ---------------------------------------------------------------------------
# async_set_cover_position(): the position slider in the UI. Chains into
# async_open_cover()/async_close_cover() with _preserve_target=True so the
# tracking loop's existing partial-target logic (already covered above)
# does the actual driving - this only covers the routing/no-op decision
# itself.
# ---------------------------------------------------------------------------


async def test_set_cover_position_above_current_opens_toward_the_target(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_current_cover_position = 30

    await cover.async_set_cover_position(**{ATTR_POSITION: 80})

    assert cover._target_position == 80
    assert cover._attr_is_opening is True
    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_UP, device_id=cover._command_device_id
    )


async def test_set_cover_position_below_current_closes_toward_the_target(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.control_blind_with_retry = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_current_cover_position = 80

    await cover.async_set_cover_position(**{ATTR_POSITION: 30})

    assert cover._target_position == 30
    assert cover._attr_is_closing is True
    api.control_blind_with_retry.assert_awaited_once_with(
        cover._command_enum, CMD_DOWN, device_id=cover._command_device_id
    )


async def test_set_cover_position_equal_to_current_while_moving_stops_instead(
    hass: HomeAssistant,
) -> None:
    """Reaching exactly the already-in-flight target by a new command of the
    same value must stop, not let the loop drive past it."""
    api = MagicMock()
    api.control_blind_stop = AsyncMock(return_value=StopOutcome.CONFIRMED)
    cover = _build_cover(hass, api)
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 50

    await cover.async_set_cover_position(**{ATTR_POSITION: 50})

    api.control_blind_stop.assert_awaited_once_with(
        cover._command_enum, device_id=cover._command_device_id
    )


async def test_set_cover_position_equal_to_current_while_idle_is_a_noop(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    cover = _build_cover(hass, api)
    cover._attr_current_cover_position = 50

    await cover.async_set_cover_position(**{ATTR_POSITION: 50})

    api.control_blind.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_manual_position_sync() / _handle_calibration_completed(): the two
# other ways a position becomes authoritative without a live command -
# confirmed by a person via Developer Tools, or by finishing a calibration
# run.
# ---------------------------------------------------------------------------


async def test_handle_manual_position_sync_applies_the_confirmed_position(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._stop_position_tracking = MagicMock()  # type: ignore[method-assign]
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 61
    cover._target_position = 90

    cover._handle_manual_position_sync(73)

    assert cover._attr_current_cover_position == 73
    assert cover._attr_is_closed is False
    assert cover._attr_is_opening is False
    assert cover._target_position is None
    assert cover._position_confirmed_since_restart is True
    cover._stop_position_tracking.assert_called_once_with()


async def test_handle_calibration_completed_ignores_a_different_device(
    hass: HomeAssistant,
) -> None:
    """Every SchellenbergCover instance listens on the shared
    SIGNAL_CALIBRATION_COMPLETED signal (see async_added_to_hass), so this
    guard is what keeps one blind's calibration from resetting every other
    configured blind to 0% as well.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, device_id="ABCDEF")
    cover._attr_current_cover_position = 55

    cover._handle_calibration_completed("A_DIFFERENT_DEVICE", 12.0, 9.0, 0)

    assert cover._attr_current_cover_position == 55


async def test_handle_calibration_completed_updates_travel_times_and_closes(
    hass: HomeAssistant,
) -> None:
    """ending_position=0: the close leg ran last, see
    options_flow_calibration.py's calibration_close step.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, device_id="ABCDEF")
    cover._attr_current_cover_position = 55

    cover._handle_calibration_completed("ABCDEF", 18.5, 16.5, 0)

    assert cover._travel_time_open == 18.5
    assert cover._travel_time_close == 16.5
    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True
    assert cover._position_confirmed_since_restart is True


async def test_handle_calibration_completed_can_end_open_instead_of_closed(
    hass: HomeAssistant,
) -> None:
    """Mirror of the test above with ending_position=100: when the person
    chose to measure the close leg first, the open leg runs last and the
    blind ends up fully open, not closed - calibration's result is no
    longer hardcoded to 0.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, device_id="ABCDEF")
    cover._attr_current_cover_position = 55

    cover._handle_calibration_completed("ABCDEF", 18.5, 16.5, 100)

    assert cover._travel_time_open == 18.5
    assert cover._travel_time_close == 16.5
    assert cover._attr_current_cover_position == 100
    assert cover._attr_is_closed is False
    assert cover._position_confirmed_since_restart is True


# ---------------------------------------------------------------------------
# async_added_to_hass(): restoring the last known position after a restart
# (there is no hardware to ask), and wiring up the signal that lets
# _handle_event() react to activity overheard on this blind's status
# identity.
# ---------------------------------------------------------------------------


async def test_added_to_hass_restores_position_from_the_current_position_attribute(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(
            "cover.living_room_blind", "open", {ATTR_CURRENT_POSITION: 73}
        )
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 73
    assert cover._attr_is_closed is False


async def test_added_to_hass_restores_position_from_a_plain_closed_state(
    hass: HomeAssistant,
) -> None:
    """A restored state with no current_position attribute (e.g. from an
    older version that never recorded one) falls back to the plain
    open/closed state string.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(cover.entity_id, "closed")
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


async def test_added_to_hass_leaves_position_unknown_when_nothing_was_restored(
    hass: HomeAssistant,
) -> None:
    """A brand-new entity with no prior Home Assistant state at all, and no
    CONF_INITIAL_POSITION in its device_data either (e.g. a blind paired
    before that key existed, or - before the Issue #78 pairing-flow
    unification (2026-11-07) - via what was then a separate manual-entry
    path that never calibrated) - see the sibling test right below for
    the case where that key *is* present.

    Used to default to 0%/closed here - see this branch's own comment in
    cover.py for the real-world report that traced "every blind on a hub
    shows closed right after a Home Assistant restart, even ones that
    were not actually closed" to exactly this fallback. Position/
    is_closed are both already None from __init__, so leaving them
    untouched is what actually reports "unknown" rather than a specific,
    wrong answer.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position is None
    assert cover._attr_is_closed is None


async def test_added_to_hass_uses_calibration_ending_position_when_nothing_was_restored(
    hass: HomeAssistant,
) -> None:
    """No restored HA state, but the subentry carries the position
    calibration actually ended at (see options_flow_calibration.py's
    calibration_close step and cover.py's own
    _initial_position_from_calibration) - that real measured value should
    be used instead of unconditionally defaulting to closed.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_INITIAL_POSITION: 100})
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 100
    assert cover._attr_is_closed is False


async def test_added_to_hass_prefers_restored_state_over_calibration_ending_position(
    hass: HomeAssistant,
) -> None:
    """Restored Home Assistant state - the blind's actual last known
    position - outranks CONF_INITIAL_POSITION, which only ever reflects
    where calibration itself ended, possibly a while ago: the fallback
    above must stay a fallback, used only when nothing was restored.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, device_data={CONF_INITIAL_POSITION: 100})
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(
            "cover.living_room_blind", "closed", {ATTR_CURRENT_POSITION: 0}
        )
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


async def test_added_to_hass_prefers_initial_position_for_newly_registered_entity(
    hass: HomeAssistant,
) -> None:
    """A newly registered blind (is_newly_registered_entity=True - set by
    async_setup_entry only the first time a blind_id's stable unique ID is
    set up, see the test_async_setup_entry_marks_*_as_newly_registered*
    tests below) trusts CONF_INITIAL_POSITION over a restored HA state,
    the same way a live fresh-calibration cache hit would.

    This reproduces the reported bug directly: pairing/calibrating a
    blind that ends up fully closed, where the legacy-unique_id migration
    in async_setup_entry hands the new entity a stale restored state of
    "open" left over from an earlier pairing of the same remote. Without
    this override, the cover would incorrectly show open/100% -
    disabling the "open" button until the person manually closes it once
    - even though calibration just confirmed it is physically closed.

    Contrast with
    test_added_to_hass_prefers_restored_state_over_calibration_ending_position
    above, which covers the *default* (is_newly_registered_entity=False)
    case: an already-established blind reloading for some unrelated
    reason (e.g. a different blind being paired), where the restored
    state is the trustworthy, current one and must NOT be overridden by
    that blind's own, possibly years-old CONF_INITIAL_POSITION.
    """
    api = MagicMock()
    cover = _build_cover(
        hass,
        api,
        device_data={CONF_INITIAL_POSITION: 0},
        is_newly_registered_entity=True,
    )
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(
            "cover.living_room_blind", "open", {ATTR_CURRENT_POSITION: 100}
        )
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


async def test_added_to_hass_prefers_fresh_calibration_over_restored_state(
    hass: HomeAssistant,
) -> None:
    """A calibration that just finished, in the same "Pair and calibrate"
    run that is creating this entity for the first time, outranks even a
    restored Home Assistant state - unlike CONF_INITIAL_POSITION above,
    which a restored state always beats. A reused blind_id (the same
    remote paired, removed, and paired again) can restore a leftover
    position from that earlier, unrelated device; what calibration just
    measured, seconds ago, is the one value guaranteed to be current.
    """
    api = MagicMock()
    api.pop_fresh_calibration_position.return_value = 100
    cover = _build_cover(hass, api, device_data={CONF_INITIAL_POSITION: 0})
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=State(
            "cover.living_room_blind", "closed", {ATTR_CURRENT_POSITION: 0}
        )
    )

    await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 100
    assert cover._attr_is_closed is False
    api.pop_fresh_calibration_position.assert_called_once_with(cover._blind_id)


async def test_added_to_hass_wires_up_the_status_identity_signal(
    hass: HomeAssistant,
) -> None:
    """The dispatcher signal subscribed to must exactly match the one
    _handle_event's own status identity would be broadcast on - this is an
    end-to-end check of that string assembly, not just that *some* callback
    got registered.
    """
    api = MagicMock()
    cover = _build_cover(hass, api, status_device_id="FEDCBA", status_enum="05")
    cover.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    cover._start_position_tracking = MagicMock()  # type: ignore[method-assign]

    await cover.async_added_to_hass()
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_FEDCBA_05", EVENT_STARTED_MOVING_UP
    )
    await hass.async_block_till_done()

    assert cover._attr_is_opening is True


# ---------------------------------------------------------------------------
# async_setup_entry(): turns each saved blind subentry into a device-registry
# device plus one SchellenbergCover, keyed on the subentry's own unique_id -
# a stable per-blind ID that survives a re-pair onto a different RF channel
# - rather than the raw RF command identity that is only ever used to
# transmit. It also migrates a pre-stable-ID entity's unique_id onto the
# current format when it finds one. See the module docstring for why this
# is the one place here that drives the real function instead of building
# the entity by hand.
# ---------------------------------------------------------------------------


def _build_hub_entry(hass: HomeAssistant, api: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    entry.runtime_data = api
    return entry


def _add_blind_subentry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    unique_id: str | None = "ABCDEF",
    title: str = "Living Room Blind",
    data: dict[str, Any] | None = None,
) -> ConfigSubentry:
    if data is None:
        data = {
            CONF_DEVICE_ID: "ABCDEF",
            CONF_DEVICE_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "ABCDEF",
            CONF_STATUS_ENUM: "10",
            CONF_OPEN_TIME: 20.0,
            CONF_CLOSE_TIME: 18.0,
        }
    subentry = ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title=title,
        unique_id=unique_id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry


async def test_async_setup_entry_creates_device_and_cover_with_stable_id(
    hass: HomeAssistant,
) -> None:
    """The device registry's identity - and the entity's own device_id,
    which its DeviceInfo is keyed on - must come from the subentry's own
    unique_id, never from the raw command device_id that is only used to
    actually transmit. A second, incomplete subentry (no command identity
    at all) must be silently skipped rather than crashing setup for every
    other blind.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    _add_blind_subentry(hass, entry, unique_id="blind-uuid-1")
    _add_blind_subentry(
        hass,
        entry,
        unique_id=None,
        title="Incomplete Blind",
        data={CONF_OPEN_TIME: 20.0, CONF_CLOSE_TIME: 18.0},
    )
    added_entities: list[Any] = []
    async_add_entities = Mock(
        side_effect=lambda entities, **kwargs: added_entities.extend(entities)
    )

    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    cover = added_entities[0]
    assert isinstance(cover, SchellenbergCover)
    # The stable, device-registry-facing ID is the subentry's own unique_id...
    assert cover._device_id == "blind-uuid-1"
    # ...never the raw RF identity, which is only ever used to transmit.
    assert cover._command_device_id == "ABCDEF"
    assert cover._command_enum == "10"
    api.register_entity.assert_called_once_with(
        "ABCDEF",
        "10",
        "Living Room Blind",
        command_device_id="ABCDEF",
        command_enum="10",
        secondary_status_identities=(),
    )
    device_registry = dr.async_get(hass)
    device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, "blind-uuid-1"), entry.entry_id
    )
    assert device is not None
    assert device.name == "Living Room Blind"
    assert (
        async_get_device_by_identifier_compat(
            device_registry, (DOMAIN, "ABCDEF"), entry.entry_id
        )
        is None
    )


async def test_async_setup_entry_nulls_a_stale_status_identity_when_source_is_unknown(
    hass: HomeAssistant,
) -> None:
    """Regression guard: a stale status identity left over from before
    the source went unconfirmed must not be read back as live.

    identities.py's resolve_blind_identities() - the canonical logic for
    this exact resolution, shared by config_flow.py's developer-tools
    diagnostics step and diagnostics.py's own dump - treats
    CONF_STATUS_IDENTITY_SOURCE == STATUS_IDENTITY_SOURCE_UNKNOWN as "no
    confirmed primary status identity", regardless of whatever
    CONF_STATUS_DEVICE_ID/CONF_STATUS_ENUM values are still sitting in the
    subentry's data from before it became unconfirmed. async_setup_entry()
    used to read those stale fields back out instead, so api.
    register_entity() below would have briefly (re-)registered this
    blind's old, no-longer-trusted status identity on every reload - not
    reachable through the current UI (every write path already clears
    these fields together), only with hand-edited or legacy config data,
    which is exactly what this test constructs.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    _add_blind_subentry(
        hass,
        entry,
        unique_id="blind-uuid-1",
        data={
            CONF_DEVICE_ID: "ABCDEF",
            CONF_DEVICE_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "FEDCBA",
            CONF_STATUS_ENUM: "20",
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
            CONF_OPEN_TIME: 20.0,
            CONF_CLOSE_TIME: 18.0,
        },
    )
    async_add_entities = Mock()

    await async_setup_entry(hass, entry, async_add_entities)

    api.register_entity.assert_called_once_with(
        None,
        None,
        "Living Room Blind",
        command_device_id="ABCDEF",
        command_enum="10",
        secondary_status_identities=(),
    )


async def test_async_setup_entry_resyncs_device_registry_name_and_model_on_edit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a blind's name or status identity (config_flow.py's "edit"
    form, via async_update_and_abort() -> the entry-update listener's
    async_reload() -> this same async_setup_entry() running again) must
    update the already-existing device registry entry's name/model to
    match. Before this fix, device_registry.async_get_or_
    create()'s name=/model= kwargs were only ever honored on the device's
    very first creation; every later call found the device already
    existing and left its name/model exactly as first registered, forever,
    regardless of how many times the blind was "renamed" through that
    form. The reload trigger itself is already covered by test_init.py;
    this test is only about what async_setup_entry() does once it runs.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    subentry = _add_blind_subentry(
        hass, entry, unique_id="blind-uuid-1", title="Old Name"
    )

    await async_setup_entry(hass, entry, Mock())

    device_registry = dr.async_get(hass)
    device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, "blind-uuid-1"), entry.entry_id
    )
    assert device is not None
    assert device.name == "Old Name"
    original_model = device.model

    # Simulate async_step_edit's commit: a new title, plus a changed status
    # identity (which feeds into the device's `model` string).
    updated_data = dict(entry.subentries[subentry.subentry_id].data)
    updated_data[CONF_STATUS_DEVICE_ID] = "112233"
    updated_data[CONF_STATUS_ENUM] = "05"
    hass.config_entries.async_update_subentry(
        entry,
        entry.subentries[subentry.subentry_id],
        title="New Name",
        data=updated_data,
    )

    await async_setup_entry(hass, entry, Mock())

    device = async_get_device_by_identifier_compat(
        device_registry, (DOMAIN, "blind-uuid-1"), entry.entry_id
    )
    assert device is not None
    assert device.name == "New Name"
    assert device.model != original_model

    # A third run with nothing changed since must leave the registry
    # alone - most reloads are plain HA restarts, not edits, and firing a
    # device-registry update event every time would be needless churn.
    update_spy = Mock(wraps=device_registry.async_update_device)
    monkeypatch.setattr(device_registry, "async_update_device", update_spy)

    await async_setup_entry(hass, entry, Mock())

    update_spy.assert_not_called()


async def test_async_setup_entry_migrates_a_legacy_entity_unique_id(
    hass: HomeAssistant,
) -> None:
    """Before blinds had their own stable per-blind unique_id, a cover's
    entity-registry unique_id was "schellenberg_<raw device_id>". An
    installation upgrading from that era must not end up with a duplicate,
    disconnected entity - the existing registry entry is migrated onto the
    new f"{DOMAIN}_blind_<blind_id>" unique_id in place instead.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    blind = _add_blind_subentry(hass, entry, unique_id="ABCDEF")
    entity_registry = er.async_get(hass)
    legacy_entry = entity_registry.async_get_or_create(
        "cover", DOMAIN, "schellenberg_ABCDEF", config_entry=entry
    )
    expected_unique_id = f"{DOMAIN}_blind_{blind.subentry_id}"

    await async_setup_entry(hass, entry, Mock())

    assert (
        entity_registry.async_get_entity_id("cover", DOMAIN, "schellenberg_ABCDEF")
        is None
    )
    migrated_entity_id = entity_registry.async_get_entity_id(
        "cover", DOMAIN, expected_unique_id
    )
    assert migrated_entity_id == legacy_entry.entity_id
    assert (
        entity_registry.entities[legacy_entry.entity_id].config_subentry_id
        == blind.subentry_id
    )


async def test_async_setup_entry_marks_first_time_setup_as_newly_registered_entity(
    hass: HomeAssistant,
) -> None:
    """A blind whose stable unique_id has never been registered before -
    the ordinary case the very first time it is set up, right after
    pairing - is marked is_newly_registered_entity=True, so
    async_added_to_hass() knows a restored HA state found under this
    (freshly assigned) entity_id can only be stale leftover data, never
    this blind's own history. See
    test_added_to_hass_prefers_initial_position_for_newly_registered_entity
    above for why that distinction matters.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    _add_blind_subentry(hass, entry, unique_id="ABCDEF")
    added_entities: list[Any] = []
    async_add_entities = Mock(
        side_effect=lambda entities, **kwargs: added_entities.extend(entities)
    )

    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    assert added_entities[0]._is_newly_registered_entity is True


async def test_async_setup_entry_marks_a_legacy_migrated_blind_as_newly_registered(
    hass: HomeAssistant,
) -> None:
    """The legacy-unique_id migration case (see
    test_async_setup_entry_migrates_a_legacy_entity_unique_id above) is
    itself a form of first-time setup for this blind_id's *stable*
    unique ID - it must also be marked is_newly_registered_entity=True,
    not just the "nothing existed at all" case covered by the sibling
    test above. The entity_id it inherits here carries whatever state
    was last restored under the old unique_id, which belongs to that
    earlier pairing episode, not to this blind.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    _add_blind_subentry(hass, entry, unique_id="ABCDEF")
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "cover", DOMAIN, "schellenberg_ABCDEF", config_entry=entry
    )
    added_entities: list[Any] = []
    async_add_entities = Mock(
        side_effect=lambda entities, **kwargs: added_entities.extend(entities)
    )

    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    assert added_entities[0]._is_newly_registered_entity is True


async def test_async_setup_entry_marks_existing_blind_as_not_newly_registered(
    hass: HomeAssistant,
) -> None:
    """A blind whose stable unique_id is already in the entity registry -
    the ordinary case on every reload after its first setup, triggered by
    some *other* blind being paired, removed, or recalibrated - must be
    marked is_newly_registered_entity=False, so async_added_to_hass()
    trusts this blind's own restored HA state over its own (possibly
    years-old) CONF_INITIAL_POSITION instead of snapping back to it on
    every unrelated reload.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    blind = _add_blind_subentry(hass, entry, unique_id="ABCDEF")
    expected_unique_id = f"{DOMAIN}_blind_{blind.subentry_id}"
    entity_registry = er.async_get(hass)
    existing_entry = entity_registry.async_get_or_create(
        "cover", DOMAIN, expected_unique_id, config_entry=entry
    )
    added_entities: list[Any] = []
    async_add_entities = Mock(
        side_effect=lambda entities, **kwargs: added_entities.extend(entities)
    )

    await async_setup_entry(hass, entry, async_add_entities)

    assert len(added_entities) == 1
    assert added_entities[0]._is_newly_registered_entity is False
    # Unchanged - this already-registered entry is reused, not migrated.
    assert (
        entity_registry.async_get_entity_id("cover", DOMAIN, expected_unique_id)
        == existing_entry.entity_id
    )


async def test_async_setup_entry_ignores_a_non_hub_entry(hass: HomeAssistant) -> None:
    """A config entry with no serial port belongs to some other platform -
    cover setup must leave the registries and the api alone for it.
    """
    api = MagicMock()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = api
    async_add_entities = Mock()

    await async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_not_called()
    api.register_entity.assert_not_called()


async def test_async_setup_entry_does_nothing_with_no_saved_blinds(
    hass: HomeAssistant,
) -> None:
    """A freshly added hub with no blinds taught yet must not crash or
    create anything - there is simply nothing to set up.
    """
    api = MagicMock()
    entry = _build_hub_entry(hass, api)
    async_add_entities = Mock()

    await async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_not_called()
    api.register_entity.assert_not_called()


# ---------------------------------------------------------------------------
# Position-tracking shutdown/cleanup: _async_handle_hass_stop,
# async_will_remove_from_hass, _async_shutdown_position_tracking, and
# _async_cancel_position_tracking. These run whenever Home Assistant stops
# or the entity is removed/reloaded while a blind is mid-move, and must
# never leave the background tracking task running, or let a failure in
# it escape and break shutdown for every other entity.
# ---------------------------------------------------------------------------


async def test_shutdown_position_tracking_is_a_no_op_with_nothing_running(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)

    await cover._async_shutdown_position_tracking("no tracking active")

    cover.async_write_ha_state.assert_not_called()  # type: ignore[attr-defined]
    assert cover._position_update_task is None


async def test_shutdown_position_tracking_snapshots_and_cancels_the_running_task(
    hass: HomeAssistant,
) -> None:
    """A live tracking task must be snapshotted - a final position update
    and a state write, so the UI reflects where the blind actually stopped
    - and then actually cancelled, not just forgotten, so it cannot keep
    running or writing state after shutdown.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    task = asyncio.ensure_future(asyncio.sleep(100))
    cover._position_update_task = task

    await cover._async_shutdown_position_tracking("test reason")

    cover.async_write_ha_state.assert_called_once()  # type: ignore[attr-defined]
    assert task.cancelled()
    assert cover._position_update_task is None


async def test_shutdown_position_tracking_skips_the_state_write_without_an_entity_id(
    hass: HomeAssistant,
) -> None:
    """A cover with no entity_id yet was never actually added to hass;
    writing state for it would raise, so the snapshot must skip that
    write while still cancelling the task.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover.entity_id = None  # type: ignore[assignment]
    task = asyncio.ensure_future(asyncio.sleep(100))
    cover._position_update_task = task

    await cover._async_shutdown_position_tracking("test reason")

    cover.async_write_ha_state.assert_not_called()  # type: ignore[attr-defined]
    assert task.cancelled()


async def test_cancel_position_tracking_swallows_a_non_cancellation_exception(
    hass: HomeAssistant,
) -> None:
    """A tracking task that converts its own cancellation into a different
    exception must not be allowed to escape and break shutdown for every
    other entity - it is logged and swallowed instead.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)

    async def _stubborn_loop() -> None:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise RuntimeError("boom") from None

    task = asyncio.ensure_future(_stubborn_loop())
    await asyncio.sleep(0)  # let it reach the sleep before cancelling it
    cover._position_update_task = task

    await cover._async_cancel_position_tracking("test reason")

    assert task.done()
    assert cover._position_update_task is None


async def test_handle_hass_stop_delegates_to_shutdown_with_its_own_reason(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._async_shutdown_position_tracking = AsyncMock()  # type: ignore[method-assign]

    await cover._async_handle_hass_stop(Mock())

    cover._async_shutdown_position_tracking.assert_awaited_once_with(
        "Home Assistant stopping"
    )


async def test_will_remove_from_hass_shuts_down_tracking_then_calls_super(
    hass: HomeAssistant,
) -> None:
    """async_will_remove_from_hass must shut down tracking under its own
    reason and still chain up to the base class afterwards (a no-op here,
    but skipping it would silently drop any cleanup a future Home
    Assistant release adds to it).
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    cover._async_shutdown_position_tracking = AsyncMock()  # type: ignore[method-assign]

    await cover.async_will_remove_from_hass()

    cover._async_shutdown_position_tracking.assert_awaited_once_with(
        "entity removal or entry unload"
    )


async def test_will_remove_from_hass_cancels_this_devices_in_flight_move(
    hass: HomeAssistant,
) -> None:
    """Regression: removing a blind's own subentry/
    device used to leave any in-flight control_blind_with_retry() loop for
    it running untouched - still able to transmit a real RF command, or
    process a stray ack, for a blind Home Assistant no longer has an
    entity for - until the *subsequent* full hub reload's unload phase
    eventually reached disconnect(), hub-wide. cancel_pending_move() is
    the same per-device mechanism async_stop_cover() already uses (see its
    own docstring in api.py); this proves async_will_remove_from_hass()
    now also calls it, for this entity's own _command_device_id, and
    before shutting down position tracking.
    """
    api = MagicMock()
    cover = _build_cover(hass, api)
    call_order: list[str] = []
    api.cancel_pending_move.side_effect = lambda _device_id: call_order.append(
        "cancel_pending_move"
    )
    cover._async_shutdown_position_tracking = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _reason: call_order.append(
            "_async_shutdown_position_tracking"
        )
    )

    await cover.async_will_remove_from_hass()

    api.cancel_pending_move.assert_called_once_with("ABCDEF")
    assert call_order == ["cancel_pending_move", "_async_shutdown_position_tracking"]
