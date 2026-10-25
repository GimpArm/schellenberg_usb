"""Core protocol tests for SchellenbergUsbApi.

Covers the transmit lock (regular, non-movement commands must still fully
serialize through it), the direct unlocked write used for blind moves
(open/close/stop - see control_blind), the busy/retry cycle (including the
eager-task-start regression described below), the connection-loss/disconnect
cleanup of that same retry state, and the three multi-phase RF sequences
(teach_motor, pair_device_and_wait, send_raw_transmit) - these were
previously only exercised indirectly through tests/test_config_flow.py's
config-flow-level tests (real API + FakeTransport, but driven through the
flow rather than called directly), so failure branches specific to each
function's own retry/ACK/block handling had no direct coverage here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

import custom_components.schellenberg_usb.api as api_module
from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CMD_ALLOW_PAIRING,
    CMD_DOWN,
    CMD_GET_PARAM_P,
    CMD_PAIR,
    CMD_STOP,
    CMD_TRANSMIT,
    CMD_UP,
    PAIRING_DEVICE_ENUM_START,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_EVENT_CAPTURE,
)
from tests.conftest import FakeTransport, written

# ---------------------------------------------------------------------------
# connect(): opens the serial port, verifies the stick, enters listening
# mode, then fetches the hub's device ID. Previously exercised only via the
# connected_api fixture (which sets the already-connected state directly,
# bypassing connect() entirely) or indirectly through a real failed handshake
# against a nonexistent port (Developer Tools' "Reset stick", which only
# reaches the outer connection exception, never verify_device()'s or
# _enter_listening_mode()'s own failure handling). verify_device(),
# _enter_listening_mode(), and get_device_id() are mocked here - their own
# wire-level behavior is a separate concern - so these tests are purely
# about connect() correctly sequencing and reacting to them.
# ---------------------------------------------------------------------------


async def test_connect_success_initializes_state_and_fetches_hub_id(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")
    fake_transport = FakeTransport()
    monkeypatch.setattr(
        api_module.serialx,  # type: ignore[attr-defined]
        "create_serial_connection",
        AsyncMock(return_value=(fake_transport, Mock())),
    )
    monkeypatch.setattr(api, "verify_device", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "_enter_listening_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "get_device_id", AsyncMock(return_value="A1B2C3"))

    result = await api.connect()

    assert result is True
    assert api._is_connected is True
    assert api._hub_id == "A1B2C3"
    assert api._transport is fake_transport  # type: ignore[comparison-overlap]


async def test_connect_resets_state_when_verification_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stick that answers but fails verify_device() - not a genuine
    Schellenberg stick - must not be left half-connected.
    """
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")
    fake_transport = FakeTransport()
    monkeypatch.setattr(
        api_module.serialx,  # type: ignore[attr-defined]
        "create_serial_connection",
        AsyncMock(return_value=(fake_transport, Mock())),
    )
    monkeypatch.setattr(api, "verify_device", AsyncMock(return_value=False))

    result = await api.connect()

    assert result is False
    assert api._is_connected is False
    assert api._transport is None
    assert fake_transport.is_closing() is True
    # A real failed handshake schedules a retry via async_call_later, which
    # would otherwise outlive this test as a "lingering timer" - clean it up
    # the same way test_developer_tools_reset_stick already does.
    await api.disconnect()


async def test_connect_resets_state_when_listening_mode_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same recovery as the verification-failure case above, for the other
    way a freshly opened port can fail to become usable: the device
    answers and verifies, but never actually reaches listening mode.
    """
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")
    fake_transport = FakeTransport()
    monkeypatch.setattr(
        api_module.serialx,  # type: ignore[attr-defined]
        "create_serial_connection",
        AsyncMock(return_value=(fake_transport, Mock())),
    )
    monkeypatch.setattr(api, "verify_device", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "_enter_listening_mode", AsyncMock(return_value=False))

    result = await api.connect()

    assert result is False
    assert api._is_connected is False
    assert api._transport is None
    assert fake_transport.is_closing() is True
    await api.disconnect()


async def test_connect_closes_transport_when_cancelled_after_opening_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: cancelling a connect() that already opened a
    transport but has not finished verifying/listening/fetching the hub ID
    yet must not leave that transport open.

    This is exactly what can happen when disconnect() (an unload/reload)
    or a newer start_connecting() attempt cancels an in-flight connect():
    without this, the real serial port stays open with nothing left
    referencing it, invisible to any later connect attempt.
    """
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")
    fake_transport = FakeTransport()
    monkeypatch.setattr(
        api_module.serialx,  # type: ignore[attr-defined]
        "create_serial_connection",
        AsyncMock(return_value=(fake_transport, Mock())),
    )

    reached_verify = asyncio.Event()

    async def hang_in_verify() -> bool:
        reached_verify.set()
        await asyncio.sleep(100)
        return True

    monkeypatch.setattr(api, "verify_device", hang_in_verify)

    task = asyncio.ensure_future(api.connect())
    await asyncio.wait_for(reached_verify.wait(), timeout=1)
    assert api._transport is fake_transport  # type: ignore[comparison-overlap]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert api._transport is None
    assert api._protocol is None
    assert api._is_connected is False
    assert fake_transport.is_closing() is True


async def test_start_connecting_creates_a_tracked_task(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_connecting() is the one place that fires connect() as a
    background task, so disconnect() (or a later start_connecting() call)
    always has a single task it can find and cancel.
    """
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")
    monkeypatch.setattr(api, "connect", AsyncMock(return_value=True))

    task = api.start_connecting()

    assert api._connect_task is task
    assert await task is True


async def test_start_connecting_cancels_a_previous_still_running_attempt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most one connect() may ever be in flight. Starting a new attempt
    (for instance a scheduled reconnect firing while the very first
    connect from setup is still hanging) must cancel the old one rather
    than let both run concurrently against the same serial port.
    """
    api = SchellenbergUsbApi(hass, "/dev/fake-schellenberg")

    async def hang_connect() -> bool:
        await asyncio.sleep(100)
        return True

    monkeypatch.setattr(api, "connect", hang_connect)

    first_task = api.start_connecting()
    await asyncio.sleep(0)
    second_task = api.start_connecting()

    assert api._connect_task is second_task
    assert first_task is not second_task
    with pytest.raises(asyncio.CancelledError):
        await first_task

    # Clean up the still-pending second task rather than leak it past the
    # test - nothing here is meant to exercise its own completion.
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task


async def test_send_command_writes_ascii_with_crlf_terminator(
    connected_api: SchellenbergUsbApi,
) -> None:
    assert await connected_api.send_command("!?") is True
    assert written(connected_api) == [b"!?\r\n"]


async def test_control_blind_rejects_an_invalid_action(
    connected_api: SchellenbergUsbApi,
) -> None:
    assert await connected_api.control_blind("10", "99", device_id="ABCDEF") is False
    assert written(connected_api) == []


async def test_control_blind_is_blocked_while_disconnected(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._is_connected = False

    assert (
        await connected_api.control_blind("10", CMD_STOP, device_id="ABCDEF") is False
    )
    assert written(connected_api) == []


async def test_regular_commands_still_fully_serialize_through_the_transmit_lock(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The normal lock logic must stay completely intact for regular commands."""
    await connected_api._transmit_lock.acquire()
    task = asyncio.ensure_future(connected_api.send_command("!?"))
    try:
        await asyncio.sleep(0)  # let the task run up to the lock and block
        assert not task.done()
        assert written(connected_api) == []
    finally:
        connected_api._transmit_lock.release()

    assert await task is True
    assert written(connected_api) == [b"!?\r\n"]


async def test_control_blind_sends_one_direct_unlocked_write(
    connected_api: SchellenbergUsbApi,
) -> None:
    """control_blind() must reach the wire immediately, never queued behind the lock.

    Regression: confirmed against real hardware that routing a blind move
    through the transmit lock and busy-retry cycle, while stop went through
    a separate lock-bypassing path, let the two fight over the stick's one
    RF transmitter - a move's own busy-retry could still be resending its
    payload seconds after a stop had already gone out, and on exhausting its
    retries would latch a busy fault that had nothing to do with the stop.
    control_blind() now writes directly, holding the transmit lock for
    nothing and never becoming eligible for that retry cycle in the first
    place - proven here by a held lock not blocking it at all.
    """
    await connected_api._transmit_lock.acquire()
    try:
        result = await asyncio.wait_for(
            connected_api.control_blind("10", CMD_STOP, device_id="ABCDEF"),
            timeout=1,
        )
    finally:
        connected_api._transmit_lock.release()

    assert result is True
    expected = f"{CMD_TRANSMIT}109{CMD_STOP}0000\r\n".encode("ascii")
    assert written(connected_api) == [expected]


async def test_control_blind_updates_last_transmit_source(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression for audit finding 24: unlike send_command(), control_
    blind() used to never update _last_transmit_source at all - it writes
    straight to the transport instead of going through send_command() (see
    this class's own docstring above for why). Diagnostic/log-attribution
    only: a t1/t0 arriving after this write would log, and self-heal
    _busy_latched under, whatever the *previous* caller's source was,
    rather than this one's.
    """
    connected_api._last_transmit_source = "internal"

    await connected_api.control_blind(
        "10", CMD_UP, device_id="ABCDEF", source="developer_tools"
    )

    assert connected_api._last_transmit_source == "developer_tools"


async def test_control_blind_does_not_enter_the_busy_retry_cycle(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A blind move must never be retried or latch a busy fault on tE.

    This link gives no delivery confirmation for a move to begin with (the
    USB stick's t1/t0/tE responses report only its own transmitter state,
    never what the motor did), so there is nothing worth retrying for open,
    close, or stop: control_blind() writes once, straight to the transport,
    and never sets _pending_retry_command - a tE that follows it must be a
    no-op, not the start of a multi-second retry-then-latch sequence.
    """
    assert await connected_api.control_blind("10", CMD_UP, device_id="ABCDEF") is True
    assert len(written(connected_api)) == 1

    connected_api._handle_message("tE")

    assert connected_api._retry_task is None
    assert connected_api.busy_latched is False
    assert len(written(connected_api)) == 1


async def test_busy_retry_has_no_limit_and_keeps_going(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stick that stays busy is retried forever - never abandoned.

    Uses a raw transmit sent through send_command() rather than
    control_blind() - a blind move never enters this cycle at all (see
    test_control_blind_does_not_enter_the_busy_retry_cycle below); this
    test is about the generic machinery that teach/pairing/raw-transmit
    still rely on. Sends far more "tE" replies in a row than the old
    3-attempt cap ever allowed, and checks that none of them abandon the
    command or latch a busy fault - only the stick itself answering
    cleanly (t0/t1), or a disconnect, ends this.

    Regression: Home Assistant's task factories start a created task
    "eagerly" - it runs synchronously up to its first real suspension point
    before async_create_task() even returns the Task object. When nothing
    inside _retry_command_after_delay actually suspends (as here, where
    asyncio.sleep is mocked to resolve instantly and the transmit lock and
    transmitter-idle event are both immediately available), the whole retry
    - including its own `finally: self._retry_task = None` - runs to
    completion before that assignment line. Overwriting self._retry_task
    unconditionally afterwards would then stomp that already-finished
    cleanup and leave a stale, already-done Task sitting in self._retry_task
    forever instead of None - which is exactly what would make each
    successive "tE" below see a not-yet-done task and skip its retry.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    assert await connected_api.send_command(f"{CMD_TRANSMIT}109{CMD_UP}0000") is True
    assert len(written(connected_api)) == 1

    attempts = 10  # comfortably past the old TRANSMIT_MAX_RETRIES of 3
    for _ in range(attempts):
        connected_api._handle_message("tE")

    assert connected_api.busy_latched is False
    assert connected_api._pending_retry_command == f"{CMD_TRANSMIT}109{CMD_UP}0000"
    assert connected_api._retry_task is None
    assert connected_api._retry_count == attempts
    # The initial write, plus one resend per "tE" - none of them abandoned.
    assert len(written(connected_api)) == 1 + attempts


async def test_busy_retry_scheduled_message_logs_at_debug_even_for_developer_tools(
    connected_api: SchellenbergUsbApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The "will retry" message must stay at DEBUG, unlike its two siblings.

    The other two branches below this one in _handle_message - "no pending
    command to retry" and the ambiguous-conflict skip - still escalate to
    WARNING for a developer_tools/service source (DIAGNOSTIC_TRANSMIT_SOURCES),
    same as before. This one must not: a busy stick retrying itself back to
    healthy is the normal, expected case under the no-limit retry policy
    (see test_busy_retry_has_no_limit_and_keeps_going), not something worth
    surfacing by default regardless of what triggered the transmission -
    only someone already watching debug logs needs to see each attempt.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.api")

    assert (
        await connected_api.send_command(
            f"{CMD_TRANSMIT}109{CMD_UP}0000", source="developer_tools"
        )
        is True
    )

    connected_api._handle_message("tE")

    retry_records = [
        record
        for record in caplog.records
        if "command retry in 100ms" in record.message
    ]
    assert len(retry_records) == 1
    assert retry_records[0].levelno == logging.DEBUG


async def test_a_stick_accepting_a_transmission_resets_the_retry_count(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "t1" (transmit accepted) marks the stick as no longer stuck.

    Only the retry count is this test's concern - busy_latched is a
    separate flag that t1 never touches (see _handle_message: only t0,
    the matching completion ACK, clears it). A trailing
    `assert busy_latched is False` used to sit here too, but busy_latched
    was never True in this scenario to begin with (only one "tE", far
    under the retry limit), so that assertion was trivially true and
    wrongly implied t1 was responsible for clearing it. The real
    self-healing behaviour - busy_latched flipping from True back to
    False - is covered on its own by
    test_t0_self_heals_busy_latched_without_a_disconnect below.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    await connected_api.send_command(f"{CMD_TRANSMIT}109{CMD_UP}0000")
    connected_api._handle_message("tE")
    assert connected_api._retry_count == 1

    connected_api._handle_message("t1")

    assert connected_api._retry_count == 0


async def test_t0_resets_the_last_transmit_source_to_internal(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A completed ACK cycle ("t0") must not leave a stale source behind.

    Otherwise later, unrelated t1/t0/tE traffic would keep being logged
    (or not logged) under a source that has nothing to do with it - see
    the comment above this branch in _handle_message.
    """
    assert (
        await connected_api.send_command(
            f"{CMD_TRANSMIT}109{CMD_UP}0000", source="developer_tools"
        )
        is True
    )
    assert connected_api._last_transmit_source == "developer_tools"

    connected_api._handle_message("t0")

    assert connected_api._last_transmit_source == "internal"


async def test_t0_self_heals_busy_latched_without_a_disconnect(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A stick that later answers cleanly must clear a stale busy latch.

    test_handle_connection_lost_resets_retry_and_busy_state already covers
    the reset-on-disconnect path; this covers the other way busy_latched
    comes back down - the stick simply recovering on its own and
    completing a transmission normally, with no disconnect in between.

    busy_latched is set directly rather than produced through a real
    failure path: a "tE" busy reply no longer sets it at all now that the
    retry has no limit (see test_busy_retry_has_no_limit_and_keeps_going),
    so the only remaining way to latch it is an idle-wait timeout in
    teach_motor/send_raw_transmit/pairing - already covered on its own by
    test_teach_motor_latches_busy_when_ack_wait_times_out below. This test
    is only about what t0 does once busy_latched is already True, not
    about how it got there.
    """
    connected_api._busy_latched = True

    connected_api._handle_message("t0")

    assert connected_api.busy_latched is False


def test_handle_connection_lost_resets_retry_and_busy_state(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._pending_retry_command = f"{CMD_TRANSMIT}10900000"
    connected_api._busy_latched = True
    connected_api._retry_count = 2
    connected_api._transmitter_active = True
    connected_api._transmitter_idle.clear()

    connected_api.handle_connection_lost(
        connected_api._protocol,  # type: ignore[arg-type]
        Exception("gone"),
    )

    assert connected_api.is_connected is False
    assert connected_api.busy_latched is False
    assert connected_api._retry_count == 0
    assert connected_api._pending_retry_command is None
    assert connected_api.transmitter_active is False
    assert connected_api._transmitter_idle.is_set() is True
    # A reconnect was scheduled as a result; don't leave it pending past
    # the end of the test.
    connected_api._cancel_scheduled_reconnect()


async def test_disconnect_cancels_a_pending_retry_task(
    connected_api: SchellenbergUsbApi,
) -> None:
    retry_task = asyncio.ensure_future(asyncio.sleep(100))
    connected_api._retry_task = retry_task

    await connected_api.disconnect()

    assert connected_api._retry_task is None
    with pytest.raises(asyncio.CancelledError):
        await retry_task


async def test_disconnect_cancels_a_pending_connect_task(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Mirrors test_disconnect_cancels_a_pending_retry_task for _connect_task.

    Regression guard: an unload/reload racing the very first connect a
    fresh config entry starts in the background used to leave that
    connect() attempt running untracked - nothing could cancel it, so it
    could finish after "unload" and leave a live serial connection open
    with no config entry left referencing it.
    """

    async def _hang() -> bool:
        await asyncio.sleep(100)
        return True

    connect_task = asyncio.ensure_future(_hang())
    connected_api._connect_task = connect_task

    await connected_api.disconnect()

    assert connected_api._connect_task is None
    with pytest.raises(asyncio.CancelledError):
        await connect_task


async def test_handle_connection_lost_also_unblocks_a_waiting_move_retry(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A move retry loop parked on _move_ack_event must not wait out the
    full MOVE_ACK_TIMEOUT just because the port died.

    Mirrors test_handle_connection_lost_resets_retry_and_busy_state above,
    but for control_blind_with_retry()'s own, deliberately separate state
    (see _move_lock's comment in __init__) rather than send_command()'s.
    Set up by hand, exactly as that test sets up _pending_retry_command
    etc. by hand, rather than by actually running control_blind_with_retry()
    as a task - this is only about handle_connection_lost()'s own cleanup,
    which test_control_blind_with_retry_wakes_promptly_on_connection_lost
    below exercises end-to-end through a real task.
    """
    connected_api._moves_in_progress.add("ABCDEF")
    connected_api._move_awaiting_ack = True
    connected_api._move_ack_result = None

    connected_api.handle_connection_lost(
        connected_api._protocol,  # type: ignore[arg-type]
        Exception("gone"),
    )

    assert connected_api._move_ack_event.is_set() is True
    assert connected_api._move_ack_result is False
    assert connected_api._moves_in_progress == set()
    assert connected_api._move_cancel_requested == set()
    connected_api._cancel_scheduled_reconnect()


async def test_disconnect_also_unblocks_a_waiting_move_retry(
    connected_api: SchellenbergUsbApi,
) -> None:
    """disconnect()'s own copy of the same cleanup as the test above."""
    connected_api._moves_in_progress.add("ABCDEF")
    connected_api._move_awaiting_ack = True
    connected_api._move_ack_result = None

    await connected_api.disconnect()

    assert connected_api._move_ack_event.is_set() is True
    assert connected_api._move_ack_result is False
    assert connected_api._moves_in_progress == set()
    assert connected_api._move_cancel_requested == set()


def test_handle_connection_lost_clears_the_last_received_message_cache(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression for audit finding 13: every other piece of per-connection
    state has long been cleared here (see
    test_handle_connection_lost_resets_retry_and_busy_state and
    test_handle_connection_lost_also_unblocks_a_waiting_move_retry above) -
    this cache of the latest frame per status identity previously was not,
    so a stale pre-reconnect frame could linger in diagnostics forever.
    """
    connected_api._last_received_messages[("ABCDEF", "10")] = {"command": "01"}

    connected_api.handle_connection_lost(
        connected_api._protocol,  # type: ignore[arg-type]
        Exception("gone"),
    )

    assert connected_api._last_received_messages == {}
    connected_api._cancel_scheduled_reconnect()


async def test_disconnect_clears_the_last_received_message_cache(
    connected_api: SchellenbergUsbApi,
) -> None:
    """disconnect()'s own copy of the same cleanup as the test above."""
    connected_api._last_received_messages[("ABCDEF", "10")] = {"command": "01"}

    await connected_api.disconnect()

    assert connected_api._last_received_messages == {}


# ---------------------------------------------------------------------------
# control_blind_with_retry()/cancel_pending_move(): the real one-at-a-time
# serialization fix for blind moves (open/close). control_blind() itself
# (used directly by stop, and by this method for each of its own attempts)
# stays exactly as covered above - one unlocked, unretried write. What is
# new here is the lock held across a whole write-then-retry-until-accepted
# cycle, and the cooperating cancellation hook stop uses to end a loop
# that's still retrying for its own blind.
# ---------------------------------------------------------------------------


async def _run_until(
    condition: Callable[[], bool], *, max_iterations: int = 100
) -> None:
    """Pump the event loop until `condition()` is true.

    control_blind_with_retry() runs as a genuinely separate asyncio.Task
    (unlike send_command()'s own retry, which Home Assistant's eager task
    factory runs synchronously to completion inside _handle_message()
    itself before that call even returns - see
    test_busy_retry_has_no_limit_and_keeps_going's own docstring above).
    Waking it (an Event.set()) only schedules it to resume; it then needs
    a handful of real event-loop iterations - through asyncio.wait_for's
    own internal task-wrapping, not just one - to actually act on that.
    How many is an implementation detail of asyncio itself, not part of
    control_blind_with_retry()'s own contract and not worth pinning down
    exactly, so this polls instead of hard-coding a tick count.
    """
    for _ in range(max_iterations):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"condition not met after {max_iterations} event-loop iterations"
    )


async def test_control_blind_with_retry_rejects_a_non_move_action(
    connected_api: SchellenbergUsbApi,
) -> None:
    """CMD_STOP (or anything else) must never enter this retry cycle.

    See control_blind_with_retry()'s own docstring: stop has to stay
    exactly as instant and unretried as control_blind() itself - this
    method exists only for async_open_cover()/async_close_cover().
    """
    assert (
        await connected_api.control_blind_with_retry("10", CMD_STOP, device_id="ABCDEF")
        is False
    )
    assert written(connected_api) == []


async def test_control_blind_with_retry_succeeds_on_first_accept(
    connected_api: SchellenbergUsbApi,
) -> None:
    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    await asyncio.sleep(0)  # let it reach the write and park on the ack wait
    assert len(written(connected_api)) == 1

    connected_api._handle_message("t1")

    assert await asyncio.wait_for(task, timeout=1) is True


async def test_control_blind_with_retry_retries_on_busy_with_no_attempt_limit(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stick that keeps saying busy is retried regardless of how many
    attempts that takes, same no-attempt-limit policy as send_command()'s
    own busy-retry cycle (see test_busy_retry_has_no_limit_and_keeps_going
    in the section above) - independent machinery, same policy. Sends far
    more "tE" replies in a row than would matter in practice, to prove
    none of them on their own give up.

    Elapsed time, unlike attempt count, IS bounded - see
    test_control_blind_with_retry_gives_up_after_the_overall_timeout right
    below. This test's own attempts stay well inside that budget: the
    patched-instant asyncio.sleep below only skips real delay, never
    advances time.monotonic(), so MOVE_RETRY_OVERALL_TIMEOUT is never in
    reach here however many attempts run.
    """
    real_sleep = asyncio.sleep

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        # A bare `return None` (the pattern the other retry tests above
        # use) never actually yields to the event loop, which is fine for
        # them - send_command()'s retry runs synchronously to completion
        # inside _handle_message() itself (see _run_until's own docstring)
        # - but control_blind_with_retry() below is a genuinely separate
        # task that needs real event-loop iterations to make progress, so
        # this still yields once via the real sleep(0), just with no
        # actual delay.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    def written_count_reached(expected: int) -> Callable[[], bool]:
        # A plain `lambda: len(written(connected_api)) == 1 + attempt`
        # inside the loop below would close over the loop variable `attempt`
        # itself, seeing whatever it has become by the time _run_until()
        # actually calls it rather than its value on that iteration -
        # `expected` here is instead a fresh parameter binding on every
        # call, one per iteration, not a loop variable at all.
        return lambda: len(written(connected_api)) == expected

    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)

    attempts = 10
    for attempt in range(1, attempts + 1):
        connected_api._handle_message("tE")
        await _run_until(written_count_reached(1 + attempt))

    assert not task.done()

    connected_api._handle_message("t1")
    assert await asyncio.wait_for(task, timeout=1) is True


class _FakeClock:
    """A controllable stand-in for time.monotonic().

    Mirrors tests/test_cover.py's identical helper: control_blind_with_
    retry()'s overall-timeout check only cares about *elapsed* time, so a
    fake clock that advances only when the test tells it to reproduces
    that deterministically and instantly, without any real waiting - a
    patched-instant asyncio.sleep (see instant_sleep just above) skips
    real delay but never advances time.monotonic() on its own.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


async def test_control_blind_with_retry_gives_up_after_the_overall_timeout(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Elapsed time is bounded even though attempt count is not.

    See MOVE_RETRY_OVERALL_TIMEOUT's own comment: a hub-wide _move_lock
    held open-endedly by one persistently busy/unresponsive blind would
    otherwise block every other blind's own open/close behind it - a new
    coupling between blinds that control_blind() alone, always instant
    and independent, never had. The deadline is only checked once per
    loop iteration (same as cancellation - see control_blind_with_
    retry()'s own docstring), so this jumps the clock straight past it in
    one step rather than simulating real elapsed time, then feeds one
    more "tE" to wake the loop and let it notice on its next pass.
    """
    real_sleep = asyncio.sleep

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    clock = _FakeClock(1_000.0)
    monkeypatch.setattr(time, "monotonic", clock)

    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)

    clock.advance(api_module.MOVE_RETRY_OVERALL_TIMEOUT + 1.0)
    connected_api._handle_message("tE")

    assert await asyncio.wait_for(task, timeout=1) is False
    # No further write was attempted once the deadline had passed.
    assert len(written(connected_api)) == 1
    assert connected_api.busy_latched is True


async def test_control_blind_with_retry_gives_up_while_still_waiting_for_the_lock(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overall timeout must also bound acquire() itself, not only the
    wait-for-idle/write/ack cycle that runs after it is granted.

    Regression: that acquire() used to be a bare `await self._move_lock.
    acquire()`, outside any timeout - so a move stuck behind a long enough
    queue for the lock could block indefinitely, well past
    MOVE_RETRY_OVERALL_TIMEOUT, before ever reaching the deadline check
    above that is supposed to bound it. Reproduced here by having the test
    itself hold _move_lock for the call's whole duration, so it can only
    ever finish through the new acquire()-level timeout, never by actually
    being granted the lock.

    MOVE_RETRY_OVERALL_TIMEOUT is patched down to a short but non-zero
    value, not faked via _FakeClock like the test above: this path's
    timeout is enforced by asyncio.wait_for's own real-time timer (see
    test_control_blind_with_retry_treats_a_silent_ack_timeout_like_busy's
    docstring on why patched-instant sleep/a clock jump cannot substitute
    for that), so it needs real elapsed time to fire - and zero would
    instead trip the plain `time.monotonic() >= retry_deadline` check
    above on the very first pass, before this call ever reaches the new
    timeout at all.
    """
    monkeypatch.setattr(api_module, "MOVE_RETRY_OVERALL_TIMEOUT", 0.1)
    await connected_api._move_lock.acquire()  # held for good - never released

    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )

    assert await asyncio.wait_for(task, timeout=1) is False
    # Gave up waiting for the lock itself - no write was ever attempted.
    assert written(connected_api) == []
    assert connected_api.busy_latched is True

    connected_api._move_lock.release()


async def test_control_blind_with_retry_treats_a_silent_ack_timeout_like_busy(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither "t1" nor "tE" arriving at all must still retry, not hang.

    MOVE_ACK_TIMEOUT is patched down to (near) instant: asyncio.wait_for's
    own timeout path needs real elapsed time (it schedules its callback via
    loop.call_later, independent of asyncio.sleep), so patching
    asyncio.sleep alone - as the other retry tests above do - would not be
    enough on its own to make this one fast.
    """
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    real_sleep = asyncio.sleep

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        # See test_control_blind_with_retry_retries_on_busy_with_no_attempt_limit's
        # own comment on this same pattern just above.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    # No reply fed at all - let the zeroed-out ack-wait time out and retry
    # on its own, repeatedly, with nothing else driving it.
    await _run_until(lambda: len(written(connected_api)) >= 3, max_iterations=500)
    assert not task.done()

    connected_api._handle_message("t1")
    assert await asyncio.wait_for(task, timeout=1) is True


async def test_control_blind_with_retry_returns_false_when_the_write_itself_fails(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A control_blind() write that fails outright (e.g. disconnected) must
    end the loop immediately - there is nothing to wait for an ack on.
    """
    connected_api._is_connected = False

    assert (
        await connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
        is False
    )
    assert written(connected_api) == []


async def test_control_blind_with_retry_serializes_two_blinds_instead_of_racing(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The actual bug fix: two blinds commanded together must queue, not race.

    Regression for the real-world symptom (3 of 6 blinds moving almost in
    parallel): async_open_cover()/async_close_cover() used to each
    independently await wait_for_transmitter_idle() and then write, so when
    the shared _transmitter_idle event fired, every blind waiting on it
    woke in the same instant and wrote within the same ~15ms window -
    confirmed from a real log - of which the stick accepted exactly one and
    answered "tE" to the rest, which control_blind() neither waited for nor
    retried. control_blind_with_retry() closes this by holding _move_lock
    across the *entire* write-then-retry-until-accepted cycle, not just the
    write: blind B's call below must not reach the wire at all until blind
    A's first attempt has learned its outcome ("t1"), not merely until A
    has finished writing.
    """
    task_a = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="AAAAAA")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)
    # A has written and is now awaiting its ack.

    task_b = asyncio.ensure_future(
        connected_api.control_blind_with_retry("20", CMD_DOWN, device_id="BBBBBB")
    )
    # Give B plenty of chances to (wrongly) proceed - it cannot, no matter
    # how many event-loop iterations pass, since _move_lock.acquire() only
    # resolves once A actually releases it, which A has not done yet.
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_a.done()
    assert not task_b.done()
    assert len(written(connected_api)) == 1

    connected_api._handle_message("t1")  # A's write is accepted
    assert await asyncio.wait_for(task_a, timeout=1) is True

    # A's own call is done, but the *transmitter* only goes idle again on
    # "t0" (transmission finished), not "t1" (transmission started) - see
    # _handle_message()'s own comment on _transmitter_idle. B is still
    # parked inside its own _wait_for_transmitter_idle() call at this
    # point, exactly where A itself was when this test began, so it needs
    # that "t0" before it can reach control_blind() at all - without it,
    # B's wait blocks on a real TRANSMIT_IDLE_TIMEOUT that no amount of
    # event-loop pumping (as opposed to actual elapsed time) ever resolves.
    connected_api._handle_message("t0")

    # B's now-unblocked call acquires the lock and writes.
    await _run_until(lambda: len(written(connected_api)) == 2)

    connected_api._handle_message("t1")  # B's write is accepted
    assert await asyncio.wait_for(task_b, timeout=1) is True


async def test_control_blind_with_retry_wakes_promptly_on_connection_lost(
    connected_api: SchellenbergUsbApi,
) -> None:
    """handle_connection_lost() must wake a parked retry loop immediately.

    Companion, end-to-end version of
    test_handle_connection_lost_also_unblocks_a_waiting_move_retry above:
    this runs control_blind_with_retry() as a real task so the wake-up
    actually has to carry it through control_blind()'s own now-disconnected
    rejection and back out, not just flip the flags handle_connection_lost()
    touches directly.
    """
    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    await asyncio.sleep(0)
    assert len(written(connected_api)) == 1

    connected_api.handle_connection_lost(
        connected_api._protocol,  # type: ignore[arg-type]
        Exception("gone"),
    )

    assert await asyncio.wait_for(task, timeout=1) is False
    # Nothing left to check via written() here - handle_connection_lost()
    # sets _transport to None, and written() deliberately reports [] once
    # that happens (see its own docstring) rather than reflecting whether
    # a retry write was attempted. The real proof that no further retry
    # got through is `task` resolving to False above: control_blind()
    # rejects on the disconnected-state check at the top of its own next
    # loop iteration, not by timing out.
    connected_api._cancel_scheduled_reconnect()


async def test_control_blind_stop_serializes_two_stops_instead_of_racing(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The stop-vs-stop collision fix: two blinds stopped together must queue.

    Mirrors test_control_blind_with_retry_serializes_two_blinds_instead_of_
    racing above, but for two concurrent control_blind_stop() calls: both
    used to go through the plain, lock-free control_blind() and could write
    within the same instant, one of them answered "tE" and silently lost
    since control_blind() itself never waits for or retries that reply.
    control_blind_stop() closes this by sharing _move_lock (see its own
    docstring), so blind B's write must not reach the wire at all until
    blind A's first attempt has learned its outcome.
    """
    task_a = asyncio.ensure_future(
        connected_api.control_blind_stop("10", device_id="AAAAAA")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)
    # A has written and is now awaiting its ack.

    task_b = asyncio.ensure_future(
        connected_api.control_blind_stop("20", device_id="BBBBBB")
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_a.done()
    assert not task_b.done()
    assert len(written(connected_api)) == 1

    connected_api._handle_message("t1")  # A's stop is accepted
    assert await asyncio.wait_for(task_a, timeout=1) is True

    # Same reasoning as the move-vs-move test: B is parked waiting for the
    # transmitter to actually go idle ("t0"), not merely for A's own task
    # to finish ("t1").
    connected_api._handle_message("t0")

    await _run_until(lambda: len(written(connected_api)) == 2)

    connected_api._handle_message("t1")  # B's stop is accepted
    assert await asyncio.wait_for(task_b, timeout=1) is True


async def test_control_blind_stop_waits_for_its_own_blinds_in_flight_transmission(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The Kuechenrolladen case: a stop must not collide with its own move's write.

    Regression for a real log pattern: a blind's own close command's first
    write was still transmitting (no "t0" yet) when its stop arrived right
    after. cancel_pending_move() only ends a *retry* that has not happened
    yet, it cannot un-send a write already on the wire - so the fix has to
    be control_blind_stop() itself waiting for the transmitter to clear
    before writing, exactly like a move already does.
    """
    # Simulate this blind's own close write still being in flight: nothing
    # needs to be awaiting ack for a bare "t1" to clear _transmitter_idle.
    connected_api._handle_message("t1")

    task = asyncio.ensure_future(
        connected_api.control_blind_stop("10", device_id="ABCDEF")
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done()
    assert written(connected_api) == []

    # The in-flight transmission finally finishes.
    connected_api._handle_message("t0")

    await _run_until(lambda: len(written(connected_api)) == 1)
    connected_api._handle_message("t1")  # the stop itself is accepted
    assert await asyncio.wait_for(task, timeout=1) is True


async def test_control_blind_stop_falls_back_to_a_plain_write_when_the_lock_is_busy(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop must never queue for long behind a move's own retry budget.

    STOP_RETRY_BUDGET is patched down to a near-instant value so this does
    not need a real multi-second wait: _move_lock.acquire() wrapped in
    asyncio.wait_for() needs genuinely elapsed time to time out (same
    caveat as test_control_blind_with_retry_treats_a_silent_ack_timeout_
    like_busy's own docstring - patching asyncio.sleep alone would not be
    enough), unlike the monotonic-deadline checks a fake clock can jump.
    """
    monkeypatch.setattr(api_module, "STOP_RETRY_BUDGET", 0.05)
    await connected_api._move_lock.acquire()
    try:
        assert (
            await asyncio.wait_for(
                connected_api.control_blind_stop("10", device_id="ABCDEF"),
                timeout=1,
            )
            is True
        )
        # Exactly the old, plain, unlocked, unretried control_blind() write -
        # never worse than before control_blind_stop() existed.
        assert len(written(connected_api)) == 1
    finally:
        connected_api._move_lock.release()


async def test_control_blind_stop_bounds_the_ack_wait_to_the_remaining_budget(
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard for audit finding 5.

    The ack-wait used to always wait the full MOVE_ACK_TIMEOUT (1.0s)
    regardless of how much of STOP_RETRY_BUDGET lock contention and the
    idle-wait had already spent, letting this method overrun its own
    documented budget by up to that same margin. Here, waiting for the
    transmitter to go idle is made to (realistically) eat most of a
    shrunk budget; with no ack ever arriving, the whole call must still
    finish close to STOP_RETRY_BUDGET, not budget-plus-the-full-1.0s.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.schellenberg_usb.api")
    monkeypatch.setattr(api_module, "STOP_RETRY_BUDGET", 0.3)

    async def slow_idle_wait(*_args: object, **_kwargs: object) -> bool:
        await asyncio.sleep(0.2)
        return True

    monkeypatch.setattr(connected_api, "_wait_for_transmitter_idle", slow_idle_wait)

    start = time.monotonic()
    result = await asyncio.wait_for(
        connected_api.control_blind_stop("10", device_id="ABCDEF"), timeout=2
    )
    elapsed = time.monotonic() - start

    # The write itself still goes out and is never acked - same fallback
    # behavior as before this fix, see control_blind_stop()'s own
    # docstring - so this still returns True.
    assert result is True
    # Before this fix: ~0.2s idle wait + an unconditional, unbounded 1.0s
    # ack wait + the loop's own 0.1s backoff sleep = ~1.3s, well past the
    # 0.3s budget. Generous margin for CI jitter, still nowhere near that.
    assert elapsed < 0.6
    assert not any("within 1.0s" in record.message for record in caplog.records)


def _blind_payload(enum: str, action: str) -> bytes:
    """Build the exact bytes control_blind() writes for one command.

    Mirrors control_blind()'s own `raw_payload` f-string so tests can
    assert which of two concurrently-queued commands actually reached the
    wire first, without depending on the particular command_id/source
    values logged around the write.
    """
    return f"{CMD_TRANSMIT}{enum}9{action}0000\r\n".encode("ascii")


async def test_control_blind_stop_goes_before_an_unrelated_blinds_queued_move(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression for the real log: a stop must not wait out another
    blind's own already-queued move.

    An earlier version of this fix held _move_lock across the whole
    wait-for-idle-then-write cycle, so whichever call acquired the lock
    first - even if it then had nothing to do but wait out the wire -
    blocked every later caller, a stop included, for that entire transmit
    cycle (confirmed from a real log: a manual stop requested before a
    second blind's own close even reached the lock still waited out that
    close's whole ~1.1-1.2s transmit cycle). Priority (see _PriorityLock)
    fixes this: once the wire actually clears, a waiting stop goes before
    a waiting move, regardless of which one asked for the lock earlier.
    """
    # Some other transmission is already in flight - stands in for this
    # same blind's own just-fired move in the real log.
    connected_api._handle_message("t1")

    # Blind B's own close is queued behind the busy wire - an ordinary
    # waiter, exactly like Kuechenrolladen's second close in the real log.
    task_b = asyncio.ensure_future(
        connected_api.control_blind_with_retry("20", CMD_DOWN, device_id="BBBBBB")
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_b.done()
    assert written(connected_api) == []

    # Blind A's stop now also arrives, queued behind the same busy wire.
    task_stop = asyncio.ensure_future(
        connected_api.control_blind_stop("10", device_id="AAAAAA")
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_stop.done()
    assert written(connected_api) == []

    # The wire clears - both are waiting on the same idle event, but the
    # stop, not B's move, must be the one that actually gets to write next.
    connected_api._handle_message("t0")

    await _run_until(lambda: len(written(connected_api)) == 1)
    assert written(connected_api)[-1] == _blind_payload("10", CMD_STOP)
    assert not task_b.done()

    connected_api._handle_message("t1")  # the stop is accepted
    assert await asyncio.wait_for(task_stop, timeout=1) is True

    # Only now, once the stop's own transmission has also finished, does
    # B's move get its turn.
    connected_api._handle_message("t0")
    await _run_until(lambda: len(written(connected_api)) == 2)
    assert written(connected_api)[-1] == _blind_payload("20", CMD_DOWN)
    connected_api._handle_message("t1")
    assert await asyncio.wait_for(task_b, timeout=1) is True


# ---------------------------------------------------------------------------
# control_blind_exclusive(): the lock-aware wrapper around control_blind()
# for callers outside the regular cover-entity move/stop path (motor-
# direction tests, developer-tools tests, the post-teach test, the
# test_command service) that still need to share the hub-wide move-ack slot
# safely - see audit finding 7.
# ---------------------------------------------------------------------------


async def test_control_blind_exclusive_sends_and_returns_true_on_accept(
    connected_api: SchellenbergUsbApi,
) -> None:
    task = asyncio.ensure_future(
        connected_api.control_blind_exclusive("10", CMD_STOP, device_id="ABCDEF")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)
    assert written(connected_api) == [_blind_payload("10", CMD_STOP)]

    connected_api._handle_message("t1")
    assert await asyncio.wait_for(task, timeout=1) is True
    assert connected_api._move_awaiting_ack is False
    assert connected_api._move_lock.locked() is False


async def test_control_blind_exclusive_returns_false_when_the_write_itself_fails(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A control_blind() write that fails outright (e.g. disconnected) must
    end this immediately, same as control_blind_with_retry() - there is
    nothing to wait for an ack on, and the lock must still be released.
    """
    connected_api._is_connected = False

    assert (
        await connected_api.control_blind_exclusive("10", CMD_UP, device_id="ABCDEF")
        is False
    )
    assert written(connected_api) == []
    assert connected_api._move_awaiting_ack is False
    assert connected_api._move_lock.locked() is False


async def test_control_blind_exclusive_returns_true_on_a_silent_ack_timeout(
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unlike control_blind_with_retry()/control_blind_stop(), a missing or
    "busy" reply here is neither retried nor treated as failure -
    control_blind() itself never gave any of these callers ack feedback to
    begin with (see control_blind_exclusive()'s own docstring). The wait
    only exists so that reply, whenever it does arrive, is consumed here
    rather than leaking into whichever call acquires the lock next.
    MOVE_ACK_TIMEOUT is patched down so this does not need a real 1s wait -
    same caveat as test_control_blind_with_retry_treats_a_silent_ack_
    timeout_like_busy above: asyncio.wait_for's own timeout path needs
    genuinely elapsed time, independent of asyncio.sleep.
    """
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    caplog.set_level(logging.WARNING, logger="custom_components.schellenberg_usb.api")

    result = await asyncio.wait_for(
        connected_api.control_blind_exclusive("10", CMD_STOP, device_id="ABCDEF"),
        timeout=1,
    )

    assert result is True
    assert written(connected_api) == [_blind_payload("10", CMD_STOP)]
    assert connected_api._move_awaiting_ack is False
    assert connected_api._move_lock.locked() is False
    assert any(
        "No t1/tE reply to test write" in record.message for record in caplog.records
    )


async def test_control_blind_exclusive_does_not_steal_another_blinds_ack_slot(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression guard for audit finding 7.

    Before this fix, every one of control_blind_exclusive()'s current
    callers (the pairing wizard's motor-direction test, the reconfigure
    wizard's developer-tools test, the post-teach open+stop test, and the
    test_command service) called control_blind() directly - no lock at
    all - even though the wire protocol carries no correlation id and
    _handle_message() attributes any t1/t0/tE reply to whichever call
    currently has _move_awaiting_ack set. Blind A's ordinary move, mid-
    ack-wait, could have a totally unrelated test of blind B write right
    over it: B's write going out while A's slot is still marked
    outstanding means the stick's reply for B gets attributed to A
    instead - exactly the concrete trace in AUDIT_FINDINGS.md finding 7,
    reproduced here with a real control_blind_with_retry() call standing
    in for blind A's own in-flight move.
    """
    task_a = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="AAAAAA")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)
    # A has written and is now the sole owner of the hub-wide ack slot.
    assert connected_api._move_awaiting_ack is True

    task_b = asyncio.ensure_future(
        connected_api.control_blind_exclusive("20", CMD_STOP, device_id="BBBBBB")
    )
    # However many event-loop iterations pass, B cannot steal the slot -
    # _move_lock.acquire() only resolves once A actually releases it.
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_a.done()
    assert not task_b.done()
    assert len(written(connected_api)) == 1

    connected_api._handle_message("t1")  # correctly attributed to A
    assert await asyncio.wait_for(task_a, timeout=1) is True
    assert connected_api._move_awaiting_ack is False

    # Only now may B's write reach the wire.
    await _run_until(lambda: len(written(connected_api)) == 2)
    assert written(connected_api)[-1] == _blind_payload("20", CMD_STOP)
    connected_api._handle_message("t1")  # correctly attributed to B
    assert await asyncio.wait_for(task_b, timeout=1) is True


async def test_control_blind_exclusive_serializes_two_calls_instead_of_racing(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Two concurrent test/service calls must also queue through the same
    lock, not just a test call against a real move/stop (see the steal-
    protection test above) - the hub-wide ack slot is exactly as unsafe to
    double-book between two of these calls as between one of them and a
    real move.
    """
    task_a = asyncio.ensure_future(
        connected_api.control_blind_exclusive("10", CMD_UP, device_id="AAAAAA")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)

    task_b = asyncio.ensure_future(
        connected_api.control_blind_exclusive("20", CMD_DOWN, device_id="BBBBBB")
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_a.done()
    assert not task_b.done()
    assert len(written(connected_api)) == 1

    connected_api._handle_message("t1")  # A's write is accepted
    assert await asyncio.wait_for(task_a, timeout=1) is True

    # Unlike control_blind_with_retry()/control_blind_stop(), this does not
    # wait for the transmitter to go idle before writing - control_blind()
    # itself never did (see its own docstring) - so B's write goes out as
    # soon as it holds the lock, with no "t0" needed first.
    await _run_until(lambda: len(written(connected_api)) == 2)

    connected_api._handle_message("t1")  # B's write is accepted
    assert await asyncio.wait_for(task_b, timeout=1) is True


# ---------------------------------------------------------------------------
# _PriorityLock: the custom lock control_blind_stop() shares with
# control_blind_with_retry() via priority=True, exercised directly rather
# than only indirectly through the two methods above.
# ---------------------------------------------------------------------------


async def test_priority_lock_free_acquire_is_immediate() -> None:
    lock = api_module._PriorityLock()
    assert lock.locked() is False
    await lock.acquire()
    assert lock.locked() is True
    lock.release()
    assert lock.locked() is False


async def test_priority_lock_release_without_acquire_raises() -> None:
    lock = api_module._PriorityLock()
    with pytest.raises(RuntimeError):
        lock.release()


async def test_priority_lock_ordinary_waiters_stay_fifo() -> None:
    """Two ordinary acquires queue in arrival order, like asyncio.Lock."""
    lock = api_module._PriorityLock()
    await lock.acquire()

    order: list[str] = []

    async def waiter(name: str) -> None:
        await lock.acquire()
        order.append(name)
        lock.release()

    task_a = asyncio.ensure_future(waiter("a"))
    await asyncio.sleep(0)
    task_b = asyncio.ensure_future(waiter("b"))
    await asyncio.sleep(0)

    lock.release()  # hands off to whichever queued first
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1)

    assert order == ["a", "b"]


async def test_priority_lock_priority_jumps_ordinary_waiters() -> None:
    """A priority acquire is served before an ordinary one still queued.

    The actual bug fix this exists for: control_blind_stop() must not
    wait behind an unrelated blind's own move that merely asked for the
    lock earlier.
    """
    lock = api_module._PriorityLock()
    await lock.acquire()

    order: list[str] = []

    async def ordinary_waiter() -> None:
        await lock.acquire()
        order.append("ordinary")
        lock.release()

    async def priority_waiter() -> None:
        await lock.acquire(priority=True)
        order.append("priority")
        lock.release()

    ordinary_task = asyncio.ensure_future(ordinary_waiter())
    await asyncio.sleep(0)  # ordinary waiter queues first
    priority_task = asyncio.ensure_future(priority_waiter())
    await asyncio.sleep(0)

    lock.release()
    await asyncio.wait_for(asyncio.gather(ordinary_task, priority_task), timeout=1)

    assert order == ["priority", "ordinary"]


async def test_priority_lock_priority_never_preempts_the_current_holder() -> None:
    """Priority jumps other *waiters*, never whoever already holds the lock."""
    lock = api_module._PriorityLock()
    await lock.acquire()
    assert lock.locked() is True

    priority_task = asyncio.ensure_future(lock.acquire(priority=True))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not priority_task.done()  # still held by the first acquire

    lock.release()
    await asyncio.wait_for(priority_task, timeout=1)
    assert lock.locked() is True
    lock.release()


async def test_priority_lock_has_priority_waiter() -> None:
    """has_priority_waiter() is what lets an ordinary holder step back.

    See control_blind_with_retry()'s own use of this: it is what actually
    keeps a holder that merely got to the lock first from occupying it for
    an entire transmit cycle doing nothing but wait once a higher-priority
    control_blind_stop() call is waiting its turn.
    """
    lock = api_module._PriorityLock()
    assert lock.has_priority_waiter() is False

    await lock.acquire()
    assert lock.has_priority_waiter() is False  # nothing queued yet

    priority_task = asyncio.ensure_future(lock.acquire(priority=True))
    await asyncio.sleep(0)
    assert lock.has_priority_waiter() is True

    lock.release()
    await asyncio.wait_for(priority_task, timeout=1)
    assert lock.has_priority_waiter() is False  # served, no longer queued
    lock.release()


async def test_priority_lock_two_priority_waiters_stay_fifo_among_themselves() -> None:
    lock = api_module._PriorityLock()
    await lock.acquire()

    order: list[str] = []

    async def waiter(name: str) -> None:
        await lock.acquire(priority=True)
        order.append(name)
        lock.release()

    task_a = asyncio.ensure_future(waiter("a"))
    await asyncio.sleep(0)
    task_b = asyncio.ensure_future(waiter("b"))
    await asyncio.sleep(0)

    lock.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1)

    assert order == ["a", "b"]


async def test_priority_lock_async_context_manager() -> None:
    lock = api_module._PriorityLock()
    async with lock:
        assert lock.locked() is True
    assert lock.locked() is False


async def test_priority_lock_cancelled_waiter_does_not_strand_the_next_one() -> None:
    """A waiter cancelled before its turn must not block whoever is next.

    Exercises the queue-removal/_wake_up_first() interaction release()
    relies on (see _PriorityLock's own docstring on mirroring asyncio.
    Lock's fairness mechanics): if a cancelled waiter were not cleaned up
    correctly, the next waiter could be left waiting forever even after
    release() runs.
    """
    lock = api_module._PriorityLock()
    await lock.acquire()

    victim = asyncio.ensure_future(lock.acquire())
    await asyncio.sleep(0)
    survivor = asyncio.ensure_future(lock.acquire())
    await asyncio.sleep(0)

    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim

    lock.release()
    await asyncio.wait_for(survivor, timeout=1)
    assert lock.locked() is True
    lock.release()


async def test_cancel_pending_move_is_a_noop_without_a_move_in_progress(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The common case for most stops - nothing to cancel - must not raise
    or leave a stale cancellation sitting around (see cancel_pending_move()'s
    own docstring for why that would matter for this device's next move).
    """
    connected_api.cancel_pending_move("ABCDEF")

    assert connected_api._move_cancel_requested == set()


async def test_cancel_pending_move_ends_a_waiting_retry_loop_for_that_device(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop for the same blind must end its own still-retrying move.

    This is the collision that made routing moves through a busy-retry
    cycle get reverted once before (see cancel_pending_move()'s and
    async_stop_cover()'s own docstrings): without this, a retry already
    queued for blind X could resend open/close after a stop for X had
    already gone out.
    """

    real_sleep = asyncio.sleep

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        # See test_control_blind_with_retry_retries_on_busy_with_no_attempt_limit's
        # own comment on why a bare `return None` won't do here.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="ABCDEF")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)

    # Both calls are plain synchronous state changes, made back to back
    # before the task gets another chance to run - by the time it next
    # looks, it sees both: the busy reply that sends it around the loop
    # again, and the cancellation waiting at the top of that next iteration.
    connected_api._handle_message("tE")  # busy - loop goes around to retry
    connected_api.cancel_pending_move("ABCDEF")

    assert await asyncio.wait_for(task, timeout=1) is False
    # No resend after the busy reply - cancellation was picked up at the
    # next loop-iteration boundary instead.
    assert len(written(connected_api)) == 1


async def test_cancel_pending_move_does_not_affect_a_different_devices_move(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A stop for blind Y must never cancel blind X's own in-flight move."""
    task = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="AAAAAA")
    )
    await asyncio.sleep(0)

    connected_api.cancel_pending_move("BBBBBB")  # unrelated device_id

    connected_api._handle_message("t1")
    assert await asyncio.wait_for(task, timeout=1) is True


async def test_cancel_pending_move_is_not_clobbered_by_a_concurrent_second_move(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second blind's own call starting must never steal blind A's spot.

    Found during an independent audit: control_blind_with_retry() used to
    track "which move is in progress" (and cancel_pending_move() its
    cancellation target) as a single hub-wide device_id rather than per
    device, even though driving several blinds together is this
    integration's whole point (see README's own multi-blind example).
    Blind B merely *starting* its own call - even while still queued
    behind blind A and long before B ever reaches the wire - ran its own
    first line and silently overwrote that single shared device_id with
    B's, with no regard for A's own call still genuinely in flight. A stop
    for A arriving after that found the wrong device_id there and
    cancel_pending_move() silently no-opped - exactly the collision this
    mechanism exists to prevent (see its own docstring) - so A's retry
    loop would have resent its own open/close command after A's own stop
    had already gone out.

    Unlike test_cancel_pending_move_does_not_affect_a_different_devices_move
    above, which only ever starts one call and so never actually exercises
    the overwrite, this drives two blinds' own calls concurrently - as
    test_control_blind_with_retry_serializes_two_blinds_instead_of_racing
    does - and cancels the *first* one while the *second* is also tracked.

    This goes further than calling cancel_pending_move() in isolation: A
    starts, then B starts right after while A is still tracked as
    in-flight - the actual clobbering window - and the test does not end
    until *both* have been brought to their own, different target by an
    actual control_blind_stop() call each, cancel_pending_move() first for
    both, mirroring cover.py's own async_stop_cover() sequence exactly.
    A's own first attempt is made to hit a busy reply so its retry loop -
    and with it _moves_in_progress still genuinely holding "AAAAAA" - is
    still live at the moment its cancellation lands; without that,
    cancel_pending_move("AAAAAA") would find nothing to cancel and this
    would collapse into the always-a-no-op case the test named above
    already covers, proving nothing about the overwrite at all.
    """
    real_sleep = asyncio.sleep

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        # See test_control_blind_with_retry_retries_on_busy_with_no_attempt_limit's
        # own comment on why a bare `return None` won't do here.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    task_a = asyncio.ensure_future(
        connected_api.control_blind_with_retry("10", CMD_UP, device_id="AAAAAA")
    )
    await _run_until(lambda: len(written(connected_api)) == 1)
    # A has written and is now awaiting its ack, still holding _move_lock.

    task_b = asyncio.ensure_future(
        connected_api.control_blind_with_retry("20", CMD_DOWN, device_id="BBBBBB")
    )
    # B cannot reach the wire yet (A still holds _move_lock), but it has
    # already run its own first line - exactly where the old, single
    # shared device_id got clobbered to "BBBBBB", with no regard for A's
    # own still-pending call.
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task_a.done()
    assert not task_b.done()
    assert len(written(connected_api)) == 1

    # Both calls below are plain synchronous state changes, made back to
    # back before task_a gets another chance to run - see
    # test_cancel_pending_move_ends_a_waiting_retry_loop_for_that_device's
    # own comment above for why this ordering is safe.
    connected_api._handle_message("tE")  # busy - sends A's loop around again
    connected_api.cancel_pending_move("AAAAAA")

    assert await asyncio.wait_for(task_a, timeout=1) is False
    # A's own open command was written exactly once - no resend after the
    # busy reply - no matter what B itself has done by now: "tE" never
    # touches _transmitter_idle (see _handle_message()'s own t1/t0/tE
    # comment), so B may already have raced ahead on its own account and
    # written its own, unrelated command while A was being cancelled.
    assert written(connected_api).count(_blind_payload("10", CMD_UP)) == 1

    # B's own call is unaffected by A's cancellation and still completes
    # normally - proving this isn't just A winning at B's expense instead.
    # Bringing B home works the same way regardless of which of the two
    # possible points it reached above (already written and awaiting its
    # own ack, or still parked awaiting the transmitter): "t0" only ever
    # touches _transmitter_idle, never the ack mechanism (same comment),
    # so sending it is harmless even if B already has what it needs.
    connected_api._handle_message("t0")
    await _run_until(
        lambda: written(connected_api).count(_blind_payload("20", CMD_DOWN)) == 1
    )
    connected_api._handle_message("t1")  # B's write is accepted
    assert await asyncio.wait_for(task_b, timeout=1) is True

    # A has now reached its own target and gets its own real stop, exactly
    # the way cover.py's async_stop_cover() drives one: cancel first, then
    # the stop itself. The cancel is already a no-op by now (A's retry
    # loop is long since finished) - this only proves cancel_pending_move()
    # stays harmless when called unconditionally, per its own docstring,
    # once there is genuinely nothing left for it to cancel. The last "t1"
    # above (B's own accepted write) cleared _transmitter_idle - see
    # _handle_message()'s own comment, any accepted write does that, not
    # only a move's - so the stop below needs a "t0" before it can reach
    # the wire, the same as any other call would.
    connected_api.cancel_pending_move("AAAAAA")
    stop_a = asyncio.ensure_future(
        connected_api.control_blind_stop("10", device_id="AAAAAA")
    )
    connected_api._handle_message("t0")
    await _run_until(
        lambda: written(connected_api).count(_blind_payload("10", CMD_STOP)) == 1
    )
    connected_api._handle_message("t1")  # A's own stop is accepted
    assert await asyncio.wait_for(stop_a, timeout=1) is True

    # B then reaches its own, different target and gets its own stop too -
    # unaffected by anything done for A above. The test ends only once
    # this, B's own stop, has actually been accepted as well.
    connected_api.cancel_pending_move("BBBBBB")  # same unconditional no-op
    stop_b = asyncio.ensure_future(
        connected_api.control_blind_stop("20", device_id="BBBBBB")
    )
    connected_api._handle_message("t0")  # A's own stop-ack above cleared this
    await _run_until(
        lambda: written(connected_api).count(_blind_payload("20", CMD_STOP)) == 1
    )
    connected_api._handle_message("t1")  # B's own stop is accepted
    assert await asyncio.wait_for(stop_b, timeout=1) is True


# ---------------------------------------------------------------------------
# teach_motor(): the two-phase (teach_60, finish_40) remote-assisted teach
# sequence used by Developer Tools' own teach_motor step - config_flow.py's
# own tests only ever exercise this through that call site, never
# teach_motor() itself in isolation.
# ---------------------------------------------------------------------------


async def test_teach_motor_sends_teach_then_finish_and_returns_true(
    connected_api: SchellenbergUsbApi,
) -> None:
    assert (
        await connected_api.teach_motor(
            "10", device_id="ABCDEF", source="developer_tools"
        )
        is True
    )
    assert written(connected_api) == [
        f"{CMD_TRANSMIT}109{CMD_PAIR}0000\r\n".encode("ascii"),
        f"{CMD_TRANSMIT}109{CMD_ALLOW_PAIRING}0000\r\n".encode("ascii"),
    ]


async def test_teach_motor_blocked_while_disconnected(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._is_connected = False

    assert await connected_api.teach_motor("10", device_id="ABCDEF") is False
    assert written(connected_api) == []


async def test_teach_motor_returns_false_when_first_phase_write_fails(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A transport failure on teach_60 itself must stop the sequence there.

    finish_40 must never be attempted once the first phase's own write
    failed - sending it alone, without the matching teach_60, would not
    match the documented 60-then-40 pairing sequence.
    """
    connected_api._transport.write = Mock(  # type: ignore[method-assign,union-attr]
        side_effect=OSError("gone")
    )

    assert await connected_api.teach_motor("10", device_id="ABCDEF") is False
    assert written(connected_api) == []


async def test_teach_motor_latches_busy_when_ack_wait_times_out(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teach must latch busy the same way send_command()'s own retry cycle
    does when the stick never reports its transmitter idle - here that is
    teach_motor()'s own _wait_for_transmitter_idle() call between phases,
    not the busy/retry cycle, so it needs its own coverage.
    """

    async def _time_out_without_awaiting(
        coro: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any
    ) -> None:
        # asyncio.wait_for is replaced wholesale below, so the coroutine the
        # real wait_for would normally await (self._transmitter_idle.wait())
        # is instead just handed to this side_effect and never awaited by
        # anything. Closing it here disposes of it cleanly, avoiding a
        # "coroutine was never awaited" warning, while still raising
        # TimeoutError to simulate an immediate timeout.
        coro.close()
        raise TimeoutError

    connected_api._transmitter_idle.clear()
    monkeypatch.setattr(
        asyncio, "wait_for", AsyncMock(side_effect=_time_out_without_awaiting)
    )

    assert connected_api.busy_latched is False
    assert await connected_api.teach_motor("10", device_id="ABCDEF") is False
    assert connected_api.busy_latched is True
    # finish_40 must never be sent once teach_60's own ACK wait timed out.
    assert written(connected_api) == [
        f"{CMD_TRANSMIT}109{CMD_PAIR}0000\r\n".encode("ascii")
    ]


async def test_teach_motor_blocks_a_concurrent_move_until_it_finishes(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A move racing teach_motor()'s own two-phase write must be refused.

    teach_motor() deliberately never touches _device_mode - the stick's
    own hardware mode genuinely does not change here, only the physical
    remote's does, see teach_motor()'s own comment - so pairing_active is
    the only signal _transmit_capability_block_reason() has to tell a
    teach sequence is still in flight. Before this was added, control_blind()
    saw neither signal set and sent straight through, racing teach_motor()'s
    own send_command() writes for the stick's one RF transmitter - the same
    class of bug _move_lock exists to close for move-vs-move, just never
    closed for teach-vs-move.
    """
    connected_api._transmitter_idle.clear()  # force a park between phases

    assert connected_api.pairing_active is False
    task = asyncio.ensure_future(connected_api.teach_motor("10", device_id="ABCDEF"))
    await asyncio.sleep(0)  # real yield: let teach_60 write, then park on idle

    assert connected_api.pairing_active is True
    # A different, otherwise-unrelated blind must be blocked too - this is
    # a stick-wide signal, not scoped to the device being taught.
    assert await connected_api.control_blind("20", CMD_UP) is False
    assert written(connected_api) == [
        f"{CMD_TRANSMIT}109{CMD_PAIR}0000\r\n".encode("ascii")
    ]

    connected_api._transmitter_idle.set()  # let the parked wait resolve
    assert await task is True

    # Cleared again once the sequence finishes, so a blocked teach can
    # never leave every blind permanently stuck behind it.
    assert connected_api.pairing_active is False
    assert await connected_api.control_blind("20", CMD_UP) is True


async def test_teach_motor_clears_pairing_active_even_when_a_phase_fails(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A failed teach must not leave every blind permanently blocked.

    Covers the finally block's own reason for existing: an early return
    from inside the try (a failed write here) must still clear
    pairing_active, the same way it already does on the clean-success
    path above.
    """
    connected_api._transport.write = Mock(  # type: ignore[method-assign,union-attr]
        side_effect=OSError("gone")
    )

    assert await connected_api.teach_motor("10", device_id="ABCDEF") is False
    assert connected_api.pairing_active is False

    connected_api._transport.write = Mock()  # type: ignore[method-assign,union-attr]
    assert await connected_api.control_blind("20", CMD_UP) is True


# ---------------------------------------------------------------------------
# pair_device_and_wait(): listens for an unknown transmitter's frame, then
# runs the same teach_60/finish_40 sequence on a freshly assigned enum.
# Every config_flow.py pairing test monkeypatches this method outright
# (a controlled (device_id, enum) return value for branching), so its own
# wait/timeout/teach logic had no coverage anywhere before this.
# ---------------------------------------------------------------------------


async def test_pair_device_and_wait_success_sends_teach_then_finish(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = asyncio.ensure_future(connected_api.pair_device_and_wait())
    await asyncio.sleep(0)  # real yield: let it send "sp" and park on the future
    assert written(connected_api) == [f"{CMD_GET_PARAM_P}\r\n".encode("ascii")]

    # The teach/finish phase pacing (and the stick ACK waits backing it)
    # would otherwise sleep for real here; patch only now, after the task
    # is already parked on the still-unresolved pairing future, so this
    # does not swallow the real yield above.
    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    # Simulates an "sl" pairing/list response frame from a new transmitter.
    connected_api._handle_message("sl00BEABCDEF")

    result = await task

    assert result == ("ABCDEF", f"{PAIRING_DEVICE_ENUM_START:02X}")
    enum = result[1]
    assert written(connected_api) == [
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),
        f"{CMD_TRANSMIT}{enum}9{CMD_PAIR}0000\r\n".encode("ascii"),
        f"{CMD_TRANSMIT}{enum}9{CMD_ALLOW_PAIRING}0000\r\n".encode("ascii"),
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),  # "sp" again: stop-pairing cleanup
    ]
    assert connected_api.pairing_active is False


async def test_pair_device_and_wait_returns_none_when_not_transmit_capable(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._is_connected = False

    assert await connected_api.pair_device_and_wait() is None
    assert written(connected_api) == []


async def test_pair_device_and_wait_refuses_a_second_concurrent_pairing(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._pairing_future = connected_api.hass.loop.create_future()

    assert await connected_api.pair_device_and_wait() is None
    assert written(connected_api) == []


async def test_pair_device_and_wait_times_out_when_nothing_responds(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "PAIRING_TIMEOUT", 0.01)

    result = await connected_api.pair_device_and_wait()

    assert result is None
    assert connected_api.pairing_active is False


async def test_stop_pairing_mode_restores_listening_from_the_write_alone(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Leaving pairing mode must not get the stick stuck on "unknown" mode.

    "sp" (CMD_GET_PARAM_P) is not an RF transmission (see send_command()'s
    own is_transmit comment), so it never gets a real t1/t0 ACK back.
    _stop_pairing_mode() therefore decides _device_mode from the "sp"
    write succeeding alone, with no ACK wait at all - the same way
    _enter_listening_mode() already trusts a successful "hello" write.
    An earlier version instead waited for an idle confirmation that "sp"
    structurally can't produce, and downgraded _device_mode to "unknown"
    whenever that wait timed out even though "sp" itself had already
    succeeded, which then blocked every later pairing attempt via
    _transmit_capability_block_reason()'s "wrong_mode:unknown".
    """
    connected_api._pairing_active = True
    connected_api._device_mode = "pairing"

    await connected_api._stop_pairing_mode()

    assert written(connected_api) == [f"{CMD_GET_PARAM_P}\r\n".encode("ascii")]
    assert connected_api.pairing_active is False
    assert connected_api._device_mode == "listening"


async def test_pair_device_and_wait_timeout_leaves_stick_able_to_pair_again(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the exact user-visible bug.

    After a 2-minute pairing timeout, reopening "Pairing and calibrating
    device" (or "Add roller shutter from remote control" - both pairing
    entry points share this same pair_device_and_wait()/_stop_pairing_mode()
    code path) used to immediately re-show the identical "pairing_timeout"
    abort message, before any new attempt to find a device even began.
    Cause: _stop_pairing_mode()'s cleanup used to leave _device_mode stuck
    on "unknown" purely because an idle-confirmation wait it awaited timed
    out, even though the "sp" stop-pairing write itself had already
    succeeded - and _is_transmit_capable() then rejected every subsequent
    pair_device_and_wait() call instantly on "wrong_mode:unknown". That
    wait is gone now (see _stop_pairing_mode's docstring), but this test
    stays as a regression guard on the user-visible symptom: a timeout
    must never block the next attempt.
    """
    monkeypatch.setattr(api_module, "PAIRING_TIMEOUT", 0.01)

    first_result = await connected_api.pair_device_and_wait()

    assert first_result is None
    assert connected_api._device_mode == "listening"
    assert connected_api._is_transmit_capable() is True

    # The actual symptom: a second attempt must genuinely retry (send "sp"
    # to enter pairing again) instead of being rejected before it starts.
    second_result = await connected_api.pair_device_and_wait()

    assert second_result is None
    assert written(connected_api) == [
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),  # 1st attempt: enter pairing
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),  # 1st attempt: stop-pairing cleanup
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),  # 2nd attempt: enter pairing again
        f"{CMD_GET_PARAM_P}\r\n".encode("ascii"),  # 2nd attempt: stop-pairing cleanup
    ]


# ---------------------------------------------------------------------------
# send_raw_transmit(): Developer Tools' raw-payload escape hatch. Also only
# ever exercised through config_flow.py's send_raw_command step before.
# ---------------------------------------------------------------------------


async def test_send_raw_transmit_rejects_an_invalid_payload(
    connected_api: SchellenbergUsbApi,
) -> None:
    with pytest.raises(ValueError):
        await connected_api.send_raw_transmit("not-a-valid-payload")
    assert written(connected_api) == []


async def test_send_raw_transmit_blocked_while_disconnected(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api._is_connected = False

    payload = f"{CMD_TRANSMIT}109{CMD_UP}0000"
    assert await connected_api.send_raw_transmit(payload) is False
    assert written(connected_api) == []


async def test_send_raw_transmit_success_normalizes_case_and_sends_payload(
    connected_api: SchellenbergUsbApi,
) -> None:
    payload = f"{CMD_TRANSMIT}109{CMD_UP}0000"
    mixed_case = "Ss" + payload[2:].lower()

    assert await connected_api.send_raw_transmit(mixed_case) is True
    assert written(connected_api) == [f"{payload}\r\n".encode("ascii")]


async def test_send_raw_transmit_blocks_a_concurrent_move_until_it_finishes(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A move racing send_raw_transmit()'s own write must be refused.

    send_raw_transmit() is not teach/pairing and never touches _device_mode
    either, so pairing_active is the only signal _transmit_capability_
    block_reason() has to tell a raw send is still in flight - the same
    reasoning, and the same fix, as teach_motor()'s own equivalent test.
    Before this was added, control_blind() saw neither signal set and sent
    straight through, racing this method's own send_command() write for the
    stick's one RF transmitter.
    """
    connected_api._transmitter_idle.clear()  # force a park after the write
    payload = f"{CMD_TRANSMIT}109{CMD_UP}0000"

    assert connected_api.pairing_active is False
    task = asyncio.ensure_future(connected_api.send_raw_transmit(payload))
    await asyncio.sleep(0)  # real yield: let it write, then park on idle

    assert connected_api.pairing_active is True
    # A different, otherwise-unrelated blind must be blocked too - this is
    # a stick-wide signal, not scoped to the raw payload's own device/enum.
    assert await connected_api.control_blind("20", CMD_UP) is False
    assert written(connected_api) == [f"{payload}\r\n".encode("ascii")]

    connected_api._transmitter_idle.set()  # let the parked wait resolve
    assert await task is True

    # Cleared again once the send finishes, so a blocked raw send can never
    # leave every blind permanently stuck behind it.
    assert connected_api.pairing_active is False
    assert await connected_api.control_blind("20", CMD_UP) is True


async def test_send_raw_transmit_clears_pairing_active_even_when_the_write_fails(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A failed raw send must not leave every blind permanently blocked.

    Covers the finally block's own reason for existing: an early return
    from inside the try (a failed write here) must still clear
    pairing_active, the same way it already does on the clean-success path
    above.
    """
    connected_api._transport.write = Mock(  # type: ignore[method-assign,union-attr]
        side_effect=OSError("gone")
    )
    payload = f"{CMD_TRANSMIT}109{CMD_UP}0000"

    assert await connected_api.send_raw_transmit(payload) is False
    assert connected_api.pairing_active is False

    connected_api._transport.write = Mock()  # type: ignore[method-assign,union-attr]
    assert await connected_api.control_blind("20", CMD_UP) is True


# ---------------------------------------------------------------------------
# _handle_message(): the "ss" status frame and the "RFTU_" verification
# reply. Every other test above feeds _handle_message() an ACK ("t1"/"t0"/
# "tE"), a device-ID reply ("sr..."), or a pairing-list reply ("sl...") -
# the "ss" frame (a real remote press or motor echo overheard on the
# channel) and "RFTU_" (the device-verification handshake's own reply) were
# never fed to it by any test. test_cover.py's own activity-event-handler
# tests fire the SIGNAL_DEVICE_EVENT dispatcher signal directly instead of
# through this parser, so the parsing itself - the only path by which a
# real remote press ever reaches a cover entity - had no coverage at all.
# ---------------------------------------------------------------------------


async def test_handle_message_ss_frame_updates_tracking_and_dispatches_signal(
    connected_api: SchellenbergUsbApi,
) -> None:
    connected_api.register_entity("ABCDEF", "10", "Living Room Blind")
    received: list[str] = []

    # A plain received.append would be seen as a non-@callback listener, so
    # async_dispatcher_send() hands it to the executor instead of calling it
    # inline - making the assertion below a race against that executor
    # thread. @callback marks it event-loop-safe so it runs synchronously.
    @callback
    def _on_event(command: str) -> None:
        received.append(command)

    async_dispatcher_connect(
        connected_api.hass, f"{SIGNAL_DEVICE_EVENT}_ABCDEF_10", _on_event
    )

    # ss + enum(10) + device_id(ABCDEF) + incrementor(0000, ignored) +
    # command(CMD_UP) + padding(00, ignored) + signal(00, ignored)
    connected_api._handle_message("ss" + "10" + "ABCDEF" + "0000" + CMD_UP + "0000")

    frame = connected_api.get_last_received_for_identities([("ABCDEF", "10")])
    assert frame is not None
    assert frame["matched"] is True
    assert frame["identity_role"] == "primary"
    assert frame["interpreted_command"] == "open"
    assert frame["position_tracking"] is True
    tracking_frame = connected_api.get_last_primary_tracking_frame("ABCDEF", "10")
    assert tracking_frame is not None
    assert tracking_frame["command"] == CMD_UP
    assert received == [CMD_UP]


async def test_handle_message_ss_frame_unmatched_device_skips_the_per_entity_signal(
    connected_api: SchellenbergUsbApi,
) -> None:
    """An "ss" frame from a device nobody registered still gets recorded
    (matched=False, for diagnostics) and still dispatches the ID-only
    signal - used for calibration before any entity exists - but the
    second, entity-specific signal only fires once something is actually
    registered for that exact device_id/enum (see the test above).
    """
    id_only: list[str] = []
    id_and_enum: list[str] = []

    # See the @callback note in the test above - without it these listeners
    # would run on the executor, racing the assertions below instead of
    # being guaranteed to have run by the time _handle_message() returns.
    @callback
    def _on_id_only(command: str) -> None:
        id_only.append(command)

    @callback
    def _on_id_and_enum(command: str) -> None:
        id_and_enum.append(command)

    async_dispatcher_connect(
        connected_api.hass, f"{SIGNAL_DEVICE_EVENT}_FEDCBA", _on_id_only
    )
    async_dispatcher_connect(
        connected_api.hass, f"{SIGNAL_DEVICE_EVENT}_FEDCBA_20", _on_id_and_enum
    )

    connected_api._handle_message("ss" + "20" + "FEDCBA" + "0000" + CMD_STOP + "0000")

    frame = connected_api.get_last_received_for_identities([("FEDCBA", "20")])
    assert frame is not None
    assert frame["matched"] is False
    assert frame["identity_role"] == "unmatched"
    assert id_only == [CMD_STOP]
    assert id_and_enum == []


async def test_handle_message_unmatched_frame_logs_at_debug_only(
    connected_api: SchellenbergUsbApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unmatched frame must log at debug, even with zero covers registered.

    Regression test: this used to log at WARNING whenever
    _registered_entity_keys was empty - which includes every fresh
    connection, before any cover has registered itself - so a household
    with other remotes sharing the same band logged a WARNING for every
    single one of their button presses, not just this integration's own.
    There is nothing actionable about an unmatched frame; it is routine RF
    traffic, not a problem to surface by default.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.schellenberg_usb.api")
    assert (
        not connected_api._registered_entity_keys
    )  # the exact state that used to warn

    connected_api._handle_message("ss" + "20" + "FEDCBA" + "0000" + CMD_STOP + "0000")

    unmatched_records = [
        record
        for record in caplog.records
        if "no cover has this status identity" in record.message
    ]
    assert len(unmatched_records) == 1
    assert unmatched_records[0].levelno == logging.DEBUG


async def test_handle_message_ss_frame_resolves_pairing_future_for_unknown_device(
    connected_api: SchellenbergUsbApi,
) -> None:
    """While pair_device_and_wait() is listening, an "ss" frame from a
    device nobody has registered yet is treated as the pairing candidate:
    the pairing future resolves, and the function returns immediately
    afterwards - the diagnostic frame above is still recorded, but the
    dispatcher signal further down is never reached for a device that
    isn't configured as a cover yet.
    """
    received: list[str] = []
    async_dispatcher_connect(
        connected_api.hass, f"{SIGNAL_DEVICE_EVENT}_112233", received.append
    )
    connected_api._pairing_future = connected_api.hass.loop.create_future()

    connected_api._handle_message("ss" + "30" + "112233" + "0000" + CMD_STOP + "0000")

    assert connected_api._pairing_future.done()
    assert connected_api._pairing_future.result() == "112233"
    assert received == []


async def test_handle_message_ss_frame_skips_capture_signal_when_no_capture_active(
    connected_api: SchellenbergUsbApi,
) -> None:
    """No status-frame capture window is open, so SIGNAL_DEVICE_EVENT_CAPTURE
    must not fire - only the two existing, identity-specific signals may.
    """
    received: list[tuple[str, str]] = []

    @callback
    def _on_capture_event(device_id: str, command: str) -> None:
        received.append((device_id, command))

    async_dispatcher_connect(
        connected_api.hass, SIGNAL_DEVICE_EVENT_CAPTURE, _on_capture_event
    )

    connected_api._handle_message("ss" + "10" + "ABCDEF" + "0000" + CMD_UP + "0000")

    assert received == []


async def test_handle_message_ss_frame_broadcasts_capture_signal_during_capture(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression test for the calibration root-cause fix: while a status-
    frame capture window is open (calibration, remote-status discovery,
    teach_motor), every "ss" frame must also go out on the capture-wide
    SIGNAL_DEVICE_EVENT_CAPTURE signal - independent of normalized_device_id
    and of whether anything is registered for it - so
    options_flow_calibration.py's movement-wait listeners can still see a
    device whose status identity differs from the command/pairing identity
    they are keyed to (see SIGNAL_DEVICE_EVENT_CAPTURE's own comment in
    const.py).
    """
    received: list[tuple[str, str]] = []

    @callback
    def _on_capture_event(device_id: str, command: str) -> None:
        received.append((device_id, command))

    async_dispatcher_connect(
        connected_api.hass, SIGNAL_DEVICE_EVENT_CAPTURE, _on_capture_event
    )
    connected_api.start_status_frame_capture(phase="opening")

    connected_api._handle_message("ss" + "10" + "FEDCBA" + "0000" + CMD_UP + "0000")

    assert received == [("FEDCBA", CMD_UP)]


async def test_handle_message_caps_the_status_discovery_frame_list(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression for audit finding 14: before this fix, a status-frame
    capture window (calibration, remote-status discovery, teach_motor) had
    no limit on how many frames it would record while open, so sustained
    RF interference for as long as the window stayed open (up to
    CALIBRATION_TIMEOUT/STATUS_DISCOVERY_TIMEOUT - const.py) could grow it
    without bound.
    """
    connected_api.start_status_frame_capture(phase="opening")

    for _ in range(api_module.MAX_STATUS_DISCOVERY_FRAMES + 50):
        connected_api._handle_message(
            "ss" + "10" + "FEDCBA" + "0000" + CMD_UP + "0000"
        )

    assert connected_api._status_discovery_frames is not None
    assert len(connected_api._status_discovery_frames) == (
        api_module.MAX_STATUS_DISCOVERY_FRAMES
    )


async def test_handle_message_rftu_resolves_verify_future_and_records_mode(
    connected_api: SchellenbergUsbApi,
) -> None:
    """The device-verification handshake's own reply line. connect()'s own
    tests above mock verify_device() itself rather than feeding it this
    wire-level reply, so this parsing had no coverage anywhere.
    """
    connected_api._verify_future = connected_api.hass.loop.create_future()

    connected_api._handle_message("RFTU_V20 F:20180510_DFBD B:1")

    assert connected_api._device_version == "RFTU_V20"
    assert connected_api._device_mode == "initial"  # B:1
    assert connected_api._verify_future.done()
    assert connected_api._verify_future.result() is True


async def test_pop_fresh_calibration_position_returns_it_once_then_none(
    connected_api: SchellenbergUsbApi,
) -> None:
    """One-shot handoff from a fresh "Pair and calibrate" run to the
    brand-new cover entity it creates (see cover.py's async_added_to_hass):
    popping must consume the value, so a later restart - which calls this
    again with nothing left to give - falls through to the entity's normal
    restored-state/CONF_INITIAL_POSITION priority instead of reusing it.
    """
    connected_api.set_fresh_calibration_position("BLIND1", 100)

    assert connected_api.pop_fresh_calibration_position("BLIND1") == 100
    assert connected_api.pop_fresh_calibration_position("BLIND1") is None


async def test_pop_fresh_calibration_position_without_a_prior_set_is_none(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A blind_id nothing was ever set for pops to None, not a KeyError."""
    assert connected_api.pop_fresh_calibration_position("NEVER_SET") is None


# ---------------------------------------------------------------------------
# _record_last_received_message() (via _handle_message()'s own "ss" branch):
# validation and the cap on _last_received_messages - audit finding 13.
# Previously this cache had neither: any raw, merely-uppercased device_id/
# enum substring became a permanent key, regardless of whether it was ever
# real hex.
# ---------------------------------------------------------------------------


def test_handle_message_does_not_cache_an_ss_frame_with_a_non_hex_device_id(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A corrupted frame (RF noise, a brownout, ...) whose device_id
    substring isn't real hex must never mint a cache entry - unlike the
    raw, merely-uppercased identity _handle_message() itself still uses for
    registration lookup and signal dispatch (both already no-op harmlessly
    for a bogus key, since nothing real is ever registered under one).
    """
    connected_api._handle_message("ss10ZZZZZZ0000010000")

    assert connected_api._last_received_messages == {}


def test_handle_message_evicts_the_oldest_identity_once_the_cache_is_full(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Before this fix, _last_received_messages had no cap at all, so a
    noisy RF environment producing many distinct, syntactically-valid
    identities this hub will never register an entity for - not corrupted
    garbage, which the test above covers - could still accumulate forever.
    """
    for index in range(api_module.MAX_LAST_RECEIVED_MESSAGES + 1):
        device_id = f"{index:06X}"
        connected_api._handle_message(f"ss10{device_id}0000010000")

    assert len(connected_api._last_received_messages) == (
        api_module.MAX_LAST_RECEIVED_MESSAGES
    )
    first_seen = ("000000", "10")
    last_seen = (f"{api_module.MAX_LAST_RECEIVED_MESSAGES:06X}", "10")
    assert first_seen not in connected_api._last_received_messages
    assert last_seen in connected_api._last_received_messages


def test_handle_message_moves_a_re_seen_identity_back_to_the_front_of_the_cache(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Eviction order is recency of last update, not first-ever-seen order:
    re-seeing "000000" after it was already cached must protect it from
    eviction exactly as if it were new, rather than leaving it stuck at the
    front of the queue under its original, now-stale insertion position.
    """
    connected_api._handle_message(f"ss10{'000000'}0000010000")
    for index in range(1, api_module.MAX_LAST_RECEIVED_MESSAGES):
        device_id = f"{index:06X}"
        connected_api._handle_message(f"ss10{device_id}0000010000")
    # Cache is now exactly full (MAX_LAST_RECEIVED_MESSAGES entries: "000000"
    # plus indices 1..MAX_LAST_RECEIVED_MESSAGES-1). Re-seeing "000000" here
    # must not grow it further, and must mark "000000" as freshly used.
    connected_api._handle_message(f"ss10{'000000'}0000010000")

    # One more, brand-new identity now has to evict *something*.
    overflow_device_id = f"{api_module.MAX_LAST_RECEIVED_MESSAGES:06X}"
    connected_api._handle_message(f"ss10{overflow_device_id}0000010000")

    assert len(connected_api._last_received_messages) == (
        api_module.MAX_LAST_RECEIVED_MESSAGES
    )
    # "000000" survives (recently re-seen); "000001" - the next-oldest,
    # never re-seen - is the one actually evicted instead.
    assert ("000000", "10") in connected_api._last_received_messages
    assert ("000001", "10") not in connected_api._last_received_messages


# ---------------------------------------------------------------------------
# get_last_received_for_identities()/get_last_secondary_frame(): previously
# exercised only through MagicMock stand-ins in
# tests/test_pairing_subentry_flow.py, never against a real
# SchellenbergUsbApi with real frame data - including the "pick the newest
# of several matching identities" branch, which no test anywhere reached.
# ---------------------------------------------------------------------------


def test_get_last_received_for_identities_returns_the_newest_matching_frame(
    connected_api: SchellenbergUsbApi,
) -> None:
    """Of several identities supplied at once, the one with the highest
    recorded sequence number wins, regardless of dict insertion order.
    """
    connected_api._last_received_messages[("ABCDEF", "10")] = {
        "device_id": "ABCDEF",
        "enum": "10",
        "command": "01",
        "sequence": 5,
    }
    connected_api._last_received_messages[("ABCDEF", "20")] = {
        "device_id": "ABCDEF",
        "enum": "20",
        "command": "02",
        "sequence": 7,
    }

    result = connected_api.get_last_received_for_identities(
        (("ABCDEF", "10"), ("ABCDEF", "20"))
    )

    assert result is not None
    assert result["enum"] == "20"
    assert result["sequence"] == 7


def test_get_last_received_for_identities_returns_none_without_a_match(
    connected_api: SchellenbergUsbApi,
) -> None:
    """No frame recorded for any supplied identity - not even an empty dict,
    which int(message["sequence"]) would otherwise crash comparing against.
    """
    connected_api._last_received_messages[("ABCDEF", "10")] = {
        "device_id": "ABCDEF",
        "enum": "10",
        "command": "01",
        "sequence": 1,
    }

    assert connected_api.get_last_received_for_identities((("FEDCBA", "99"),)) is None
    assert connected_api.get_last_received_for_identities(()) is None


def test_get_last_secondary_frame_delegates_to_get_last_received_for_identities(
    connected_api: SchellenbergUsbApi,
) -> None:
    """get_last_secondary_frame() is a thin wrapper (see its own docstring) -
    this is the regression test for that delegation itself, not just the
    lookup logic it delegates to (covered directly above).
    """
    connected_api._last_received_messages[("ABCDEF", "30")] = {
        "device_id": "ABCDEF",
        "enum": "30",
        "command": "00",
        "sequence": 1,
    }

    assert connected_api.get_last_secondary_frame(
        (("ABCDEF", "30"),)
    ) == connected_api.get_last_received_for_identities((("ABCDEF", "30"),))


# ---------------------------------------------------------------------------
# SchellenbergProtocol.data_received(): the raw serial receive buffer -
# previously completely untested; every other test in this file exercises
# message handling through SchellenbergUsbApi._handle_message() directly
# (via FakeTransport, bypassing the asyncio.Protocol layer entirely), never
# through the actual newline-splitting/buffering this class does on raw
# bytes. Each test constructs SchellenbergProtocol directly - no hass or
# connected_api fixture needed, since data_received() never touches
# self.api, only connection_lost() does.
# ---------------------------------------------------------------------------


def test_data_received_parses_a_message_split_across_two_chunks() -> None:
    received: list[str] = []
    protocol = api_module.SchellenbergProtocol(received.append, api=Mock())

    protocol.data_received(b"ss10ABCDE")
    assert received == []  # no terminator yet - nothing dispatched
    protocol.data_received(b"F0000010000\n")

    assert received == ["ss10ABCDEF0000010000"]
    assert protocol.buffer == ""


def test_data_received_parses_multiple_messages_in_one_chunk() -> None:
    received: list[str] = []
    protocol = api_module.SchellenbergProtocol(received.append, api=Mock())

    protocol.data_received(b"t1\nt0\n")

    assert received == ["t1", "t0"]


def test_data_received_keeps_buffering_a_partial_line_under_the_cap() -> None:
    """An ordinary, merely-not-yet-terminated line must not be discarded
    just for being the only thing data_received() has seen so far - only
    genuinely runaway growth (tested below) should ever reset the buffer.
    """
    protocol = api_module.SchellenbergProtocol(Mock(), api=Mock())

    protocol.data_received(b"ss10ABCDEF0000")

    assert protocol.buffer == "ss10ABCDEF0000"


def test_data_received_resets_the_buffer_once_it_exceeds_the_cap_without_a_terminator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for audit finding 12: a flaky connection, a firmware
    glitch, a brownout, or a misconfigured port that stops producing a
    clean "\\n" terminator used to grow this buffer without limit for as
    long as that lasted, re-copying the whole (ever-larger) string on every
    single chunk along the way. MAX_SERIAL_BUFFER_LENGTH (api.py) is
    nowhere near any real message - the longest one actually parsed is a
    few dozen bytes - so this never fires during normal operation.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.schellenberg_usb.api")
    received: list[str] = []
    protocol = api_module.SchellenbergProtocol(received.append, api=Mock())

    garbage = "X" * (api_module.MAX_SERIAL_BUFFER_LENGTH + 1)
    protocol.data_received(garbage.encode("ascii"))

    assert protocol.buffer == ""
    assert received == []  # never a valid line - nothing to dispatch
    warning_records = [
        record for record in caplog.records if "buffer exceeded" in record.message
    ]
    assert len(warning_records) == 1
    assert warning_records[0].levelno == logging.WARNING


def test_data_received_cap_is_measured_after_draining_complete_lines() -> None:
    """A chunk that both completes a backlog of real lines *and* leaves a
    new, still-partial tail under the cap must keep that tail rather than
    discarding it - the cap only ever judges what is left after every
    complete line already found has been drained and dispatched.
    """
    received: list[str] = []
    protocol = api_module.SchellenbergProtocol(received.append, api=Mock())

    protocol.data_received(b"t1\nt0\nss10ABCDEF0000")

    assert received == ["t1", "t0"]
    assert protocol.buffer == "ss10ABCDEF0000"
