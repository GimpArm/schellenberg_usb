"""Core protocol tests for SchellenbergUsbApi.

Covers the transmit lock (every command, blind moves included, fully
serializes through it), the busy/retry cycle a blind move answered with tE
now goes through (including the eager-task-start regression described
below), the connection-loss/disconnect cleanup of that same retry state, and
the three multi-phase RF sequences
(teach_motor, pair_device_and_wait, send_raw_transmit) - these were
previously only exercised indirectly through tests/test_config_flow.py's
config-flow-level tests (real API + FakeTransport, but driven through the
flow rather than called directly), so failure branches specific to each
function's own retry/ACK/block handling had no direct coverage here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

import custom_components.schellenberg_usb.api as api_module
from custom_components.schellenberg_usb.api import (
    TRANSMIT_MAX_RETRIES,
    SchellenbergUsbApi,
)
from custom_components.schellenberg_usb.const import (
    CMD_ALLOW_PAIRING,
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


async def test_control_blind_waits_for_the_transmit_lock(
    connected_api: SchellenbergUsbApi,
) -> None:
    """control_blind() must go through the lock, not write around it.

    Regression: confirmed against real hardware that a blind move written
    straight to the transport while another move was still in flight was
    answered with tE ("stick busy") and then dropped - nothing had registered
    it as the pending transmission, so the tE handler had nothing to resend,
    while the cover still reported as moving. Routing the move through
    send_command() serialises its write behind _transmit_lock and registers
    it as that pending transmission, which is what this pins: a held lock
    delays the write instead of being ignored.
    """
    await connected_api._transmit_lock.acquire()
    task = asyncio.ensure_future(
        connected_api.control_blind("10", CMD_STOP, device_id="ABCDEF")
    )
    try:
        await asyncio.sleep(0)  # let the task run up to the lock and block
        assert not task.done()
        assert written(connected_api) == []
    finally:
        connected_api._transmit_lock.release()

    assert await task is True
    expected = f"{CMD_TRANSMIT}109{CMD_STOP}0000\r\n".encode("ascii")
    assert written(connected_api) == [expected]


async def test_control_blind_retries_a_busy_stick_instead_of_dropping(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move answered with tE ("stick busy") must be resent, not dropped.

    This is the real-hardware regression: two covers told to close in the
    same second. The stick has a single RF transmitter, so the second frame
    is answered tE while the first is still on the air. control_blind() used
    to write straight to the transport and return, leaving nothing
    registered as the pending transmission, so the tE handler had nothing to
    resend and the move was silently lost - while the cover still reported
    as moving, because the write itself had succeeded.

    asyncio.sleep is mocked to resolve instantly so the retry completes
    inside the eager task start and can be asserted synchronously, the same
    way test_busy_retry_stops_after_max_attempts does it.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    assert await connected_api.control_blind("10", CMD_UP, device_id="ABCDEF") is True
    assert len(written(connected_api)) == 1
    # The move registered itself - that is what makes the tE actionable.
    assert connected_api._pending_retry_command == f"{CMD_TRANSMIT}109{CMD_UP}0000"

    connected_api._handle_message("tE")

    assert connected_api._retry_count == 1
    assert connected_api.busy_latched is False
    # The original frame, plus one resend of that very same payload.
    assert len(written(connected_api)) == 2
    assert written(connected_api)[1] == written(connected_api)[0]


async def test_control_blind_write_without_a_pending_command_stays_unflagged(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A blind move must not set the ambiguous-tE conflict flag.

    Companion to test_control_blind_retries_a_busy_stick_instead_of_dropping
    above: control_blind() now goes through send_command() like every other
    transmission, so there is no out-of-band write left for a tE to be
    ambiguous about and the flag stays False.
    """
    assert await connected_api.control_blind("10", CMD_UP, device_id="ABCDEF") is True

    assert connected_api._control_blind_write_pending_conflict is False


async def test_a_fresh_pending_command_clears_a_stale_conflict_flag(
    connected_api: SchellenbergUsbApi,
) -> None:
    """A new pending command's own window must start clean.

    _control_blind_write_pending_conflict must not leak from a previous,
    already-finished pending command into a completely new one that has not
    raced with control_blind() at all.
    """
    connected_api._control_blind_write_pending_conflict = True

    assert await connected_api.send_command(f"{CMD_TRANSMIT}109{CMD_UP}0000") is True

    assert connected_api._control_blind_write_pending_conflict is False


async def test_busy_retry_stops_after_max_attempts(
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stick that stays busy is retried up to the limit, then latched.

    Uses a raw transmit sent through send_command() rather than
    control_blind() purely to keep the payload obvious - a blind move now
    enters this same cycle, see
    test_control_blind_retries_a_busy_stick_instead_of_dropping above.

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
    forever instead of None.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    assert await connected_api.send_command(f"{CMD_TRANSMIT}109{CMD_UP}0000") is True
    assert len(written(connected_api)) == 1

    for _ in range(TRANSMIT_MAX_RETRIES + 1):
        connected_api._handle_message("tE")

    assert connected_api.busy_latched is True
    assert connected_api._retry_count == 0
    assert connected_api._retry_task is None
    # The initial write, plus one resend per retry - the final "tE" (the
    # +1 above) finds the limit already reached and abandons without
    # resending again.
    assert len(written(connected_api)) == 1 + TRANSMIT_MAX_RETRIES


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
    connected_api: SchellenbergUsbApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stick that later answers cleanly must clear a stale busy latch.

    test_handle_connection_lost_resets_retry_and_busy_state already covers
    the reset-on-disconnect path; this covers the other way busy_latched
    comes back down - the stick simply recovering on its own and
    completing a transmission normally, with no disconnect in between.
    """

    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    assert await connected_api.send_command(f"{CMD_TRANSMIT}109{CMD_UP}0000") is True
    for _ in range(TRANSMIT_MAX_RETRIES + 1):
        connected_api._handle_message("tE")
    assert connected_api.busy_latched is True

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
    connected_api._control_blind_write_pending_conflict = True

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
    assert connected_api._control_blind_write_pending_conflict is False
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

    # Pairing succeeding makes the "finally" cleanup sleep for real (see
    # _stop_pairing_mode(delay=True)); patch only now, after the task is
    # already parked on the still-unresolved pairing future, so this does
    # not swallow the real yield above.
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

    frame = connected_api.get_last_received("ABCDEF", "10")
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

    frame = connected_api.get_last_received("FEDCBA", "20")
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
