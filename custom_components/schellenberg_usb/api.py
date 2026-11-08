"""API for Schellenberg USB Stick."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast

import serialx
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CMD_ALLOW_PAIRING,
    CMD_DOWN,
    CMD_GET_DEVICE_ID,
    CMD_GET_PARAM_P,
    CMD_LED_OFF,
    CMD_LED_ON,
    CMD_PAIR,
    CMD_STOP,
    CMD_TRANSMIT,
    CMD_UP,
    CMD_VERIFY,
    MOVE_COMMAND_EXTRA_RESENDS,
    MOVE_COMMAND_RESEND_DELAY_SECONDS,
    PAIRING_DEVICE_ENUM_START,
    PAIRING_TIMEOUT,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_EVENT_CAPTURE,
    SIGNAL_MANUAL_POSITION_SYNC,
    SIGNAL_STICK_STATUS_UPDATED,
    STATUS_DISCOVERY_TIMEOUT,
    STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS,
    STOP_COMMAND_EXTRA_RESENDS,
    STOP_COMMAND_RESEND_DELAY_SECONDS,
    VERIFY_TIMEOUT,
)
from .identities import (
    StatusIdentity,
    normalize_status_identities,
    normalize_status_identity,
    summarize_status_discovery_frames,
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0
RESET_SETTLE_DELAY = 0.25
DIAGNOSTIC_TRANSMIT_SOURCES = frozenset({"developer_tools", "service"})

# How long to wait for a transmitter-idle (t0) ACK after a write before
# giving up on that particular wait. Multi-step sequences (teach, raw
# transmit, pairing) use this between steps so they never send the next
# payload while the previous one is still in flight on the shared serial
# line.
TRANSMIT_IDLE_TIMEOUT = 5.0

# How long control_blind_with_retry() waits, after one of its own writes,
# for the stick's t1 (accepted) or tE (busy) reply before assuming neither
# arrived and retrying anyway. Real hardware answers within tens of
# milliseconds - confirmed from real logs showing write-to-t1/tE gaps under
# 30ms even when several blinds contend for the stick at once - so this is a
# generous multiple of that, not a tuned budget. A timeout here is treated
# the same as a busy reply (retry), never as success: staying silent is not
# evidence the stick accepted the write.
MOVE_ACK_TIMEOUT = 1.0

# Caps how long control_blind_with_retry() keeps retrying a single move
# once it already holds _move_lock - not how long a call waits to acquire
# that lock in the first place, which is unbounded queueing behind other
# blinds and not part of this budget at all. Without this, a stick that
# stayed persistently busy (or never acked at all) for one device_id would
# hold the hub-wide lock open-endedly and block every other blind's own
# open/close behind it - a new coupling between blinds that control_blind()
# alone never had, since each of its writes was always instant and
# independent. 30s comfortably covers several full retry cycles even at
# TRANSMIT_IDLE_TIMEOUT's and MOVE_ACK_TIMEOUT's own worst case (busy
# replies in normal operation arrive within tens of milliseconds, per real
# logs - see MOVE_ACK_TIMEOUT's own comment - so this is not a budget meant
# to be exhausted in practice) without leaving every other blind
# unresponsive for long. Giving up this way is indistinguishable, to
# callers, from any other control_blind_with_retry() failure: it latches
# _busy_latched, same as send_command()'s own exhausted-retry path, so the
# existing "busy_latched" block reason explains it in the UI without a new
# translation string. Raised from 15s to 30s (2026-11-07, project owner's
# own direction) for more headroom under this installation's real, heavier
# multi-blind contention than the original figure assumed.
MOVE_RETRY_OVERALL_TIMEOUT = 30.0

# Total time control_blind_stop() spends, combined, waiting for the
# transmitter to go idle, acquiring _move_lock (with priority - see
# _PriorityLock), and retrying on "tE" once it holds it, before giving up
# and falling back to a plain control_blind() write - see that method's
# own docstring for why it shares _move_lock rather than a second,
# independent one, and why priority is what keeps this bounded by one
# transmit cycle rather than however many other blinds' moves happen to be
# queued. A manual stop must never queue behind a move's full
# MOVE_RETRY_OVERALL_TIMEOUT budget, so this is deliberately much shorter:
# comfortably more than one full transmit cycle (~1.1-1.2s per real logs,
# the same figure MOVE_ACK_TIMEOUT's own comment cites) so the realistic
# contention case - another stop, or this blind's own just-fired move,
# still finishing its single in-flight transmission - resolves cleanly
# inside this budget, while a stick stuck retrying for seconds on end
# still leaves stop falling back quickly instead of hanging.
STOP_RETRY_BUDGET = 2.0

# Hard ceiling on how large SchellenbergProtocol's own per-connection
# receive buffer (see its data_received() below) is allowed to grow while
# waiting for a line terminator that never arrives. Every message this
# integration actually parses is well under 64 bytes - the ~20-char "ss"
# status frame is the longest fixed-format one, the "RFTU_..." verification
# banner the longest free-form one - so this is not a tuned, tight budget;
# it is a sanity ceiling normal operation should never get anywhere near. A
# flaky USB-CDC connection, a firmware glitch, a brownout, or a
# misconfigured port that stops producing a clean "\n" terminator would
# otherwise grow that buffer without limit for as long as that lasts - and
# since Python strings are immutable, every "+=" onto it also re-copies the
# whole buffer, so per-chunk CPU cost grows right along with memory, not
# just the one or the other. A reconnect already clears it (a fresh
# SchellenbergProtocol instance starts with an empty buffer), but nothing
# previously forced one on its own.
MAX_SERIAL_BUFFER_LENGTH = 4096

# Cap on how many distinct status identities _handle_message() will
# remember a "last received frame" for (see _record_last_received_message()
# below). Comfortably covers any real installation - dozens of paired
# blinds/winders sharing one stick would still fit well inside this - while
# still bounding a noisy RF environment where many *different*,
# syntactically-valid identities (someone else's remote, a neighbour's
# winder, ...) that this hub will never register an entity for could
# otherwise each mint their own permanent entry forever.
MAX_LAST_RECEIVED_MESSAGES = 64

# Cap on how many frames one status-frame capture window
# (start_status_frame_capture()/finish_status_frame_capture() below) will
# record before dropping the rest for the remainder of that same window.
# A real open/close leg or remote-discovery sequence produces at most a
# handful of frames per device - this is a generous multiple of that, not
# a tuned budget - but the window itself can stay open for a while
# (CALIBRATION_TIMEOUT/STATUS_DISCOVERY_TIMEOUT - const.py), during which
# sustained RF interference could otherwise grow this list for as long as
# the window stays open. Lower severity than MAX_LAST_RECEIVED_MESSAGES
# above: finish_status_frame_capture() already only reads real signal out
# of whatever this holds (summarize_status_discovery_frames() - garbage
# frames cannot masquerade as a real identity either way), and the window
# always closes and discards this list on its own regardless.
MAX_STATUS_DISCOVERY_FRAMES = 1000


class _PriorityLock:
    """An exclusive async lock where a "priority" acquire jumps the queue.

    Behaves exactly like asyncio.Lock for ordinary (priority=False)
    acquires: free lock with nobody already queued is taken immediately,
    otherwise the caller queues and release() wakes the oldest waiter,
    first-come-first-served. The one addition is a second queue for
    priority=True acquires, which release() always drains before the
    ordinary one - so a priority caller is served ahead of any ordinary
    caller that is still *waiting*, though never ahead of whoever
    *currently holds* the lock, and never ahead of another, earlier
    priority waiter.

    Exists only so control_blind_stop() can share exclusion with
    control_blind_with_retry() (see _move_lock's own comment in __init__
    for why a second, independent lock does not work here) without
    queueing behind some *other* blind's own already-queued move the way
    sharing a plain asyncio.Lock would - confirmed from a real log where a
    manual stop, requested before a second blind's own close even reached
    the lock, still waited out that unrelated close's entire ~1.1-1.2s
    transmit cycle, because the lock gave it no way to go first.
    has_priority_waiter() is the other half of that fix: both callers hold
    this lock across their own wait-for-the-transmitter-to-go-idle step
    (not only the write - see both their own docstrings for why), and an
    ordinary holder checks it, both before starting that wait and again
    the instant it resolves, to voluntarily step back - release without
    writing, then loop around to request the lock again - the moment a
    stop is waiting its turn, rather than finishing its own turn first.

    The implementation mirrors CPython's own asyncio.Lock fairness
    mechanics (see its acquire()/release()/_wake_up_first()): release()
    does not remove the woken waiter from its queue - the waiter removes
    itself, once it actually resumes, in a finally block - so a brand-new
    acquire() arriving in the gap between release() and that resumption
    cannot see an (incorrectly) empty queue and cut in line ahead of a
    waiter that has already been woken but not yet run.
    """

    def __init__(self) -> None:
        self._locked = False
        self._priority_waiters: deque[asyncio.Future[None]] = deque()
        self._waiters: deque[asyncio.Future[None]] = deque()

    def locked(self) -> bool:
        """Return True while some caller currently holds the lock."""
        return self._locked

    async def acquire(self, *, priority: bool = False) -> None:
        """Wait for and take the lock, optionally ahead of ordinary waiters."""
        if not self._locked and not self._has_live_waiters():
            self._locked = True
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queue = self._priority_waiters if priority else self._waiters
        queue.append(fut)
        try:
            try:
                await fut
            finally:
                queue.remove(fut)
        except asyncio.CancelledError:
            if not self._locked:
                self._wake_up_first()
            raise
        self._locked = True

    def release(self) -> None:
        """Release the lock, waking the next waiter (priority queue first)."""
        if not self._locked:
            raise RuntimeError("Lock is not acquired.")
        self._locked = False
        self._wake_up_first()

    def has_priority_waiter(self) -> bool:
        """Return True while any priority=True acquire is currently queued.

        Lets an ordinary holder voluntarily yield the lock to a higher-
        priority caller instead of finishing its own turn first - see
        control_blind_with_retry()'s own use of this.
        """
        return any(not fut.cancelled() for fut in self._priority_waiters)

    def _has_live_waiters(self) -> bool:
        return any(
            not fut.cancelled() for fut in (*self._priority_waiters, *self._waiters)
        )

    def _wake_up_first(self) -> None:
        for queue in (self._priority_waiters, self._waiters):
            if queue:
                fut = queue[0]
                if not fut.done():
                    fut.set_result(None)
                return

    async def __aenter__(self) -> _PriorityLock:
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()


def check_serial_port(port: str) -> None:
    """Quick sanity check that a serial port is reachable.

    Blocking (opens and immediately closes the port), so callers must run
    this via hass.async_add_executor_job() rather than await it directly on
    the event loop. Shared by config_flow.py and options_flow.py so the
    sanity-check logic only needs to be maintained in one place.
    """
    with serialx.serial_for_url(port):
        pass


@dataclass(frozen=True, slots=True)
class _StatusIdentityRegistration:
    """One status identity mapped to a configured cover."""

    entity_name: str
    command_device_id: str
    primary: bool


def _interpret_status_command(command: str) -> str:
    """Return the diagnostic meaning of one received command byte."""
    return {
        CMD_STOP: "stop",
        CMD_UP: "open",
        CMD_DOWN: "close",
    }.get(command.upper(), "unknown")


def _normalize_protocol_enum(value: object) -> str:
    """Normalize one- or two-digit protocol enums without changing other values."""
    text = str(value).strip()
    normalized = text.upper()
    if (
        normalized
        and len(normalized) <= 2
        and all(character in "0123456789ABCDEF" for character in normalized)
    ):
        return normalized.zfill(2)
    return text


class StopOutcome(Enum):
    """What control_blind_stop() actually knows once it returns.

    A plain bool used to flatten two very different outcomes into the
    same truthy value: the stick explicitly accepting the stop write
    ("t1"), and the STOP_RETRY_BUDGET-exhausted fallback - one last,
    completely unlocked, unretried control_blind() write, fired with
    no idea whether the stick (let alone the motor) ever saw it. Both
    returned True, and both callers (async_stop_cover() and the
    auto-stop-at-target branch in _async_position_update_loop) then
    treated "True" as "this blind has now stopped, safe to freeze/
    clear tracking" - which is correct for the first case and simply
    wrong for the second: a real 6-blind Alexa "25%" command (2026-
    11-01) produced exactly this fallback for the living-room blind,
    which then kept physically closing, uncontrolled, for several
    more seconds while the entity's reported position stayed frozen
    at the stale 68% _update_position() had last computed before
    tracking was torn down - the blind actually ended up fully
    closed, nothing in Home Assistant ever showed that.

    - CONFIRMED: the stick accepted the write ("t1"). As always, this
      still says nothing about whether the motor itself caught it
      (see STOP_COMMAND_EXTRA_RESENDS's own comment in const.py) -
      only that the one hop this integration can ever observe
      succeeded. Safe to treat as done the way the old True did.
    - UNCONFIRMED: STOP_RETRY_BUDGET ran out and the fallback plain
      write fired, but nothing confirms the stick (or the motor) ever
      received it. Callers must not finalize position tracking on
      this - either keep retrying (the auto-stop-at-target branch,
      which already has a safe "not yet done, retry next tick" path
      for FAILED and now reuses it here) or, where retrying is not
      appropriate (a manual stop should stay responsive, not hang),
      mark the resulting position as unconfirmed rather than display
      it with the same confidence as a CONFIRMED stop.
    - FAILED: not even the fallback write reached the wire (e.g. the
      serial port itself is down). Exactly the old False - nothing
      about this blind's tracking state has changed, there is
      nothing to tear down or restore.
    """

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"


class SchellenbergUsbApi:
    """Manages all communication with the Schellenberg USB stick."""

    def __init__(self, hass: HomeAssistant, port: str) -> None:
        """Initialize the API client in its disconnected, idle state.

        Sets up the transport/connection state, the registered-entity
        and last-received-frame tracking used for diagnostics and
        position updates, and the pairing/retry/busy state machines -
        each described in detail by the comment on its own attribute
        group below.
        """
        self.hass = hass
        self.port = port
        self._transport: asyncio.Transport | None = None
        self._protocol: SchellenbergProtocol | None = None
        self._registered_devices: dict[
            str, str
        ] = {}  # Dict[device_id, device_enum] for registered entities
        # The highest device_enum (as an int) initialize_next_device_enum()
        # has ever handed out on this connection, regardless of whether
        # that pairing attempt went on to register a device above - see
        # that method's own comment for why this is tracked separately
        # from _registered_devices.
        self._highest_issued_device_enum: int | None = None
        self._registered_entity_keys: dict[
            StatusIdentity, _StatusIdentityRegistration
        ] = {}
        # Exact status identity -> latest raw frame and diagnostic interpretation.
        self._last_received_messages: dict[StatusIdentity, dict[str, Any]] = {}
        self._last_primary_tracking_messages: dict[StatusIdentity, dict[str, Any]] = {}
        self._last_position_updates: dict[str, dict[str, Any]] = {}
        self._last_manual_position_syncs: dict[str, dict[str, Any]] = {}
        # One-shot handoff from a "Pair and calibrate" flow that just
        # finished to the brand-new cover entity it is about to create.
        # That entity doesn't exist yet when calibration completes, so it
        # can never receive SIGNAL_CALIBRATION_COMPLETED the way an
        # existing blind being recalibrated does - it would otherwise fall
        # back to whatever Home Assistant restored for this blind_id, which
        # can be stale (e.g. left over from a previously removed device
        # that happened to reuse the same blind_id) and so override the
        # position calibration just measured seconds ago. Keyed by
        # blind_id; see set_fresh_calibration_position()/
        # pop_fresh_calibration_position() below.
        self._fresh_calibration_positions: dict[str, int] = {}
        self._last_received_sequence = 0
        self._status_discovery_frames: list[dict[str, Any]] | None = None
        self._status_discovery_phase = "idle"
        self._status_discovery_started_at: str | None = None
        self._is_connecting = False
        self._connect_task: asyncio.Task[bool] | None = None
        self._pairing_future: asyncio.Future[str] | None = None
        self._pairing_active = False
        # Set only by pair_device_and_wait() itself, each time it starts a
        # fresh attempt (so a previous attempt's outcome never leaks into
        # this one) - see that method's own comment on where this is set.
        self._pairing_interrupted_by_disconnect = False
        self._stop_pairing_task: asyncio.Task[None] | None = (
            None  # Track task to stop pairing
        )
        self._disconnect_requested = False
        self._reconnect_handle: CALLBACK_TYPE | None = None

        # USB stick status
        self._is_connected = False
        self._device_version: str | None = None
        self._device_mode: str | None = None  # boot, initial, or listening
        self._verify_future: asyncio.Future[bool] | None = None
        self._device_id_future: asyncio.Future[str] | None = None
        self._hub_id: str | None = None

        # Minimal retry state for commands that fail with "stick busy". A
        # busy reply is retried for as long as it keeps happening - there
        # is no retry limit - so _retry_count is purely informational (the
        # attempt number in the debug log below), never compared to a cap.
        self._pending_retry_command: str | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_count = 0
        self._last_transmit_source = "internal"

        # Set on a "t1" transmitter-on ACK and cleared on "t0" (see
        # _handle_message); reflects the stick's own report of its
        # transmitter state.
        self._transmitter_active = False

        # Serializes send_command() writes/bookkeeping so two commands
        # issued from different tasks (e.g. two blinds commanded at once)
        # can never interleave their writes or stomp on each other's retry
        # tracking. _transmit_busy mirrors "the lock is currently held" for
        # callers that want a plain bool. _transmitter_idle is cleared on
        # "t1" (transmit started) and set on "t0" (transmit finished) or on
        # a fresh connection; _wait_for_transmitter_idle() lets a multi-step
        # sequence wait for it between steps. _busy_latched is a sticky
        # fault flag - set when retries are exhausted or an idle-wait times
        # out - that stays set until the next connect/disconnect.
        self._transmit_lock = asyncio.Lock()
        self._transmit_busy = False
        self._transmitter_idle = asyncio.Event()
        self._transmitter_idle.set()
        self._busy_latched = False

        # control_blind_with_retry() - the retrying path async_open_cover()/
        # async_close_cover() use - gets its own single-flight lock and ack
        # signal, deliberately separate from _transmit_lock/
        # _pending_retry_command above. Those are send_command()'s own
        # bookkeeping for teach/pairing/raw-transmit, sized for exactly one
        # in-flight command; routing blind moves through that same single
        # slot was tried before (git history) and a second contending move
        # silently clobbered the first's retry tracking, undetected because
        # it was never exercised by more than two commands close together.
        # _move_lock is held for a whole wait-for-idle-then-write-then-
        # await-ack cycle, not just the write - several entities
        # independently waiting on one shared "idle" event can all observe
        # idle between the same two ACKs and all write in the same instant,
        # confirmed from a real log showing six writes within 15ms of each
        # other and five of them answered "tE". A lock held across the full
        # cycle makes that race structurally impossible instead of merely
        # unlikely.
        #
        # control_blind_stop() shares this same lock, requesting priority
        # (see _PriorityLock above), rather than getting a second,
        # independent one of its own: two separate locks would not exclude
        # each other, so a stop-task and a move-task could still each
        # observe the shared idle event go true and race to write at the
        # same instant - exactly the race this lock exists to make
        # impossible for two moves, reopened for move-vs-stop if stop did
        # not share it. Priority alone is not enough, though: it only lets
        # a stop jump an ordinary *waiter*, never whoever already holds the
        # lock, so an ordinary holder that merely got here first could
        # still occupy the lock for an entire transmit cycle doing nothing
        # but wait, with a later-arriving stop unable to do anything but
        # wait behind it - confirmed from a real log where exactly this
        # cost a manual stop over a second, long enough to visibly
        # overshoot the target. has_priority_waiter() closes that: an
        # ordinary holder checks it - both right after acquiring and again
        # the instant its own idle-wait resolves - and voluntarily releases
        # without writing, rather than finishing its turn, the moment a
        # stop is waiting for this lock.
        self._move_lock = _PriorityLock()
        # Which blinds' moves control_blind_with_retry() is currently
        # attempting (write, maybe-retry) - read by cancel_pending_move() so
        # a stop for that same blind can end a stale retry loop instead of
        # letting it resend after the stop, the exact collision that made
        # routing moves through the busy-retry cycle get reverted before.
        # Tracked per device_id (not a single scalar) since this hub can
        # have several blinds' own calls genuinely in flight at once -
        # driving several together is this integration's whole point (see
        # README's own multi-blind example) - a single shared "the" device
        # id used to let whichever blind's call ran its own first line most
        # recently silently overwrite an earlier, still-in-flight blind's
        # own claim to it.
        #
        # Each value is the set of this device_id's own currently in-flight
        # calls' own identity tokens (a fresh object() per
        # control_blind_with_retry() call, minted below) - not a plain
        # membership set of device_ids, and not just a count either, for
        # two compounding reasons. First, two genuinely concurrent
        # control_blind_with_retry() calls for the *same* device_id (not
        # two different blinds, already covered above - a very fast
        # accidental double press of the same dashboard button, say,
        # landing before the first call has returned) used to let whichever
        # one finished first discard the device_id entirely in its own
        # `finally`, even while the other was still genuinely in flight -
        # so a cancel_pending_move() landing in that gap found nothing to
        # cancel and silently no-opped for the call that was still running.
        # A count fixed that (increment on entry, decrement in `finally`,
        # only actually clearing the device_id once it reaches zero) - but
        # a count can only say *how many* calls are in flight, never
        # *which* ones, and cancel_pending_move() below needs exactly that
        # to snapshot "cancel these specific calls" rather than "cancel
        # whatever this device_id's count happens to cover" - see
        # _move_cancel_requested's own comment below for the second,
        # narrower bug that distinction was needed to close. Each call adds
        # its own token here on entry and discards (only) that same
        # token - in its own `finally`, or, for a move whose
        # resend_task_spawned ends up True, in that same call's own
        # detached _send_move_resends_and_cleanup() task's `finally`
        # instead (see resend_task_spawned's own comment at its call
        # site for why that one case defers it) - popping the device_id
        # entirely once its set empties, so this dict never holds a
        # stale, empty entry for a device with nothing genuinely in
        # flight.
        self._moves_in_progress: dict[str, set[object]] = {}
        # Per device_id, the subset of that device's own _moves_in_progress
        # tokens (above) that cancel_pending_move() had already snapshotted
        # as "cancel these" at the moment it was called - never a token
        # added to _moves_in_progress afterwards. This used to be a flat
        # set[str] of device_ids, checked with plain membership: while at
        # most one call could ever be in flight per device, "cancelled for
        # this device" and "cancelled for whichever call that is" were the
        # same thing. Once _moves_in_progress above gained a count to stop
        # one of two concurrent same-device calls from discarding the
        # device_id out from under the other, that single device-wide flag
        # stayed pending until the *last* of the two actually exited - which
        # an independent review then caught also reaching a brand new,
        # unrelated third call for that same device_id that happened to
        # start in the window after the first of the two had already
        # exited but before the second had: that third call's own first
        # check saw the still-pending flag and gave up before ever writing,
        # even though cancel_pending_move() was never asked to cancel it.
        # Fixed by snapshotting the specific tokens active at cancellation
        # time instead of the device_id as a whole: a call registering its
        # own token only afterwards is simply not in that snapshot, however
        # long an earlier, genuinely-targeted sibling takes to actually
        # exit. Checked by each call against its own token (peeked, not
        # discarded - a cancellation is conceptually for the device, not
        # for one arbitrarily-chosen call among the several it was
        # snapshotted against), and cleared the same way as
        # _moves_in_progress above - including that same resend_task_
        # spawned exception to "its own `finally`" - each call discards
        # only its own token from its own device_id's entry, popping the
        # device_id entirely once nothing remains for it in either
        # collection.
        #
        # Unlike _moves_in_progress, never discarded in bulk on a lost
        # or intentional disconnect either: handle_connection_lost() and
        # disconnect() snapshot every device's own still-active tokens
        # in here first, the same way cancel_pending_move() does for one
        # device, instead of clearing this dict alongside that one - see
        # either method's own comment for why a cancellation recorded
        # here has to outlive a disconnect that clears _moves_in_progress
        # out from under it.
        self._move_cancel_requested: dict[str, set[object]] = {}
        # True only between a move's write and learning its outcome;
        # _handle_message's t1/tE branches only touch _move_ack_result/
        # _move_ack_event while this is True, so they never fire for a t1/
        # tE that belongs to an unrelated send_command()-tracked transmission.
        self._move_awaiting_ack = False
        self._move_ack_event = asyncio.Event()
        self._move_ack_result: bool | None = None  # True=accepted, False=busy

    async def connect(self) -> bool:
        """Establish, verify, and initialize the serial connection."""
        if self._is_connecting:
            _LOGGER.debug("Connection attempt already in progress")
            return False
        if self._transport is not None and not self._transport.is_closing():
            _LOGGER.debug("Serial connection already established")
            return self._is_connected

        self._disconnect_requested = False
        self._cancel_scheduled_reconnect()
        self._is_connecting = True
        self._transport = None
        self._protocol = None
        _LOGGER.info("Connecting to Schellenberg USB stick at %s", self.port)
        try:
            transport, protocol = await serialx.create_serial_connection(
                self.hass.loop,
                lambda: SchellenbergProtocol(self._handle_message, self),
                url=self.port,
                # 115200 8N1 is the stick's documented/standard rate. This was
                # previously 112500 (not a standard baud rate - likely a typo
                # for 115200). The stick's USB-CDC interface appears to
                # tolerate a range of requested baud rates in practice, which
                # is probably why this went unnoticed, but 115200 is the
                # correct value to request.
                baudrate=115200,
            )
            self._transport = transport
            # The factory above always creates this concrete protocol type.
            self._protocol = cast(SchellenbergProtocol, protocol)
            _LOGGER.info("Successfully connected to Schellenberg USB stick")

            if not await self.verify_device():
                _LOGGER.error(
                    "Device verification failed - not a Schellenberg USB stick"
                )
                transport.close()
                self._transport = None
                self._protocol = None
                self._is_connected = False
                self._device_mode = None
                self._schedule_reconnect()
                return False

            self._is_connected = True
            self._update_status()

            if not await self._enter_listening_mode():
                _LOGGER.error(
                    "USB stick could not enter transmit-capable listening mode"
                )
                transport.close()
                self._transport = None
                self._protocol = None
                self._is_connected = False
                self._device_mode = None
                self._update_status()
                self._schedule_reconnect()
                return False

            hub_id = await self.get_device_id()
            if hub_id:
                self._hub_id = hub_id
                _LOGGER.info("Hub device ID retrieved: %s", self._hub_id)
            else:
                _LOGGER.warning("Failed to retrieve hub device ID")
            return True
        except asyncio.CancelledError:
            # Reached when start_connecting()/disconnect() cancel this
            # call's own task while it is still awaiting real I/O above -
            # most commonly an unload/reload racing the very first connect
            # a fresh config entry starts in the background. Without this,
            # a transport opened just before the cancellation landed (set
            # above, but never reaching this method's own explicit
            # close()-on-failure branches, since cancellation skips
            # straight past them) stayed open and connected after the rest
            # of the integration had already been torn down - on a single
            # USB stick, blocking the device file from being reopened
            # until Home Assistant restarts. Close whatever got opened so
            # a cancelled connect leaves exactly the same disconnected
            # state a failed one already does, then let the cancellation
            # continue propagating as normal.
            if self._transport is not None:
                self._transport.close()
            self._transport = None
            self._protocol = None
            self._is_connected = False
            self._device_mode = None
            raise
        except (OSError, TimeoutError) as err:
            _LOGGER.error(
                "Failed to connect to %s: %s. Retrying in %.0f seconds",
                self.port,
                err,
                RECONNECT_DELAY,
            )
            self._transport = None
            self._protocol = None
            self._is_connected = False
            self._device_mode = None
            self._update_status()
            self._schedule_reconnect()
            return False
        finally:
            self._is_connecting = False

    def start_connecting(self) -> asyncio.Task[bool]:
        """Start connect() as a tracked background task.

        Used instead of a bare hass.async_create_task(self.connect(), ...)
        wherever a connect is kicked off without being awaited directly -
        a fresh config entry's own initial connect, and each scheduled
        auto-reconnect. Storing the task is what lets disconnect() below
        reliably cancel a connect() that is still in flight rather than
        letting it run to completion and open a transport after the rest
        of the integration already considers itself disconnected - see
        connect()'s own CancelledError handling for the other half of
        that fix. Also cancels any previous attempt still running, so at
        most one connect() is ever in flight regardless of how many times
        this is called in a row.
        """
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        self._connect_task = self.hass.async_create_task(
            self.connect(), name="schellenberg-connect"
        )
        return self._connect_task

    async def _enter_listening_mode(self) -> bool:
        """Put a connected stick into B:2 listening mode when needed."""
        if self._transport is None or self._transport.is_closing():
            return False
        if self._device_mode == "listening":
            _LOGGER.info("Device already in listening mode")
            return True
        if self._device_mode == "bootloader":
            _LOGGER.error("Refusing transmit initialization while in bootloader mode")
            return False

        _LOGGER.info("Device is in %s mode, entering listening mode", self._device_mode)
        if not await self.send_command("hello"):
            return False
        await asyncio.sleep(0.5)
        self._device_mode = "listening"
        self._update_status()
        _LOGGER.info("Device now in listening mode")
        return True

    def _cancel_scheduled_reconnect(self) -> None:
        """Cancel a pending automatic reconnect callback."""
        if self._reconnect_handle is not None:
            self._reconnect_handle()
            self._reconnect_handle = None

    def _schedule_reconnect(self, delay: float = RECONNECT_DELAY) -> None:
        """Schedule one reconnect unless shutdown was explicitly requested."""
        if self._disconnect_requested or self._reconnect_handle is not None:
            return
        _LOGGER.info("Scheduling serial reconnect in %.1f seconds", delay)
        self._reconnect_handle = async_call_later(
            self.hass, delay, self._start_scheduled_reconnect
        )

    @callback
    def _start_scheduled_reconnect(self, _now: datetime) -> None:
        """Start the reconnect task from its timer callback."""
        self._reconnect_handle = None
        if not self._disconnect_requested:
            self.start_connecting()

    def _record_last_received_message(
        self, identity: StatusIdentity, frame: dict[str, Any]
    ) -> None:
        """Cache the latest frame seen for one validated status identity.

        Gated on identities.normalize_status_identity() succeeding - the
        same validation every other identity in this integration goes
        through - rather than the raw, merely-uppercased substrings
        _handle_message() itself still uses for registration lookup and
        signal dispatch (those already no-op harmlessly for a bogus key,
        since nothing real is ever registered under one). Without this
        guard, a corrupted or spurious frame's own device_id/enum
        substrings - never validated as real hex here, unlike everywhere
        else identities.py's helpers are used - would still mint a
        permanent dict entry below.

        Bounded to MAX_LAST_RECEIVED_MESSAGES regardless of validity: a
        noisy RF environment can still produce many *distinct*,
        syntactically-valid identities over time that this hub will never
        register an entity for, so validation alone does not cap growth -
        only eviction does. Evicts whichever identity has gone longest
        without being re-seen, not simply the first one ever seen: a plain
        dict already preserves insertion order, and popping an existing
        key before reinserting it moves it back to the end, so the oldest
        remaining key (`next(iter(...))`) is always the one least recently
        updated. This dict previously had no validation and no cap at all.
        """
        if normalize_status_identity(*identity) is None:
            return
        self._last_received_messages.pop(identity, None)
        self._last_received_messages[identity] = frame
        if len(self._last_received_messages) > MAX_LAST_RECEIVED_MESSAGES:
            oldest_identity = next(iter(self._last_received_messages))
            del self._last_received_messages[oldest_identity]

    @callback
    def _handle_message(self, message: str) -> None:
        """Parse and dispatch one line of text received from the stick.

        This is the protocol's single entry point for everything the
        stick sends back, so it branches purely on the message's
        prefix/shape: a `RFTU_` device-verification reply (records
        firmware version and boot mode, resolves `_verify_future`);
        `t1`/`t0` transmitter start/complete ACKs (drive the retry
        latch and `_transmitter_idle`); a `tE` busy reply (schedules a
        retry - there is no limit, so a persistently busy stick is
        retried for as long as it keeps saying busy rather than being
        abandoned); a `sr` device-ID-query
        reply; a `sl` pairing/list reply; and a `ss` status frame from
        a motor, which is recorded for diagnostics/calibration and
        fanned out as dispatcher signals to any matching cover entity.
        See the comments on each branch below for the wire format and
        the reasoning behind its edge cases.
        """
        _LOGGER.debug("Received raw message: %s", message)

        # Handle device verification response (format: RFTU_V20 F:20180510_DFBD B:1)
        # RFTU_V20 = device type and version
        # F: = firmware date
        # B: = boot mode (0 = bootloader, 1 = initial/normal)
        # Note: Listening mode (B:2) is entered by sending a lowercase command in B:1
        if message.startswith("RFTU_"):
            parts = message.split()
            if parts:
                self._device_version = parts[0]  # RFTU_V20
                # Extract boot mode if present
                for part in parts:
                    if part.startswith("B:"):
                        boot_mode = part[2:]
                        if boot_mode == "0":
                            self._device_mode = "bootloader"
                        elif boot_mode == "1":
                            self._device_mode = "initial"
                        elif boot_mode == "2":
                            self._device_mode = "listening"
                        else:
                            self._device_mode = "unknown"
                        break
                else:
                    self._device_mode = "initial"

                _LOGGER.info(
                    "Device verified: version=%s, mode=%s",
                    self._device_version,
                    self._device_mode,
                )
                if self._verify_future and not self._verify_future.done():
                    self._verify_future.set_result(True)
                self._update_status()
            return

        # Handle acknowledgments. t1/t0 are the stick's own transmitter
        # on/off ACKs for the payload send_command() most recently wrote.
        # t1 marks the start of that transmission (transmitter on) and t0
        # marks its completion (transmitter off) - only t0 releases the
        # retry latch, since t1 alone doesn't yet confirm the stick finished
        # sending. _transmitter_idle mirrors this: cleared on t1, set on
        # t0, so _wait_for_transmitter_idle() can let a multi-step sequence
        # (teach, raw transmit, pairing) wait for one payload's full cycle
        # before sending the next one on the shared serial line.
        if message in ("t1", "t0"):
            is_start = message == "t1"
            level = (
                logging.WARNING
                if self._last_transmit_source in DIAGNOSTIC_TRANSMIT_SOURCES
                else logging.DEBUG
            )
            self._transmitter_active = is_start
            if is_start:
                self._transmitter_idle.clear()
                # The stick accepted a transmission, so it is not stuck:
                # _retry_count's attempt number starts over from here,
                # counting busy answers in a row rather than per connection.
                self._retry_count = 0
                if self._move_awaiting_ack:
                    # This t1 is the reply to control_blind_with_retry()'s
                    # own most recent write - see that method - never to a
                    # send_command()-tracked one, since moves and pairing
                    # traffic cannot overlap (_transmit_capability_block_
                    # reason() refuses a blind move while pairing is active).
                    self._move_ack_result = True
                    self._move_ack_event.set()
                _LOGGER.log(
                    level,
                    "ACK start response=%s source=%s",
                    message,
                    self._last_transmit_source,
                )
            else:
                self._transmitter_idle.set()
                _LOGGER.log(
                    level,
                    "ACK complete response=%s source=%s payload=%s "
                    "result=completed stick_ack_only=True motor_result=unknown",
                    message,
                    self._last_transmit_source,
                    self._pending_retry_command,
                )
                self._pending_retry_command = None
                # A clean ACK cycle completed: the source that triggered it
                # is done, so later unrelated t1/t0 traffic must not keep
                # being logged (or not logged) under a stale source - and a
                # stick that was previously latched busy has now plainly
                # recovered on its own, so stop showing a stuck-state in
                # diagnostics that no longer reflects reality. A stick that
                # is genuinely still stuck will set busy_latched again on
                # its own next failed attempt.
                self._last_transmit_source = "internal"
                self._busy_latched = False
            return

        if message == "tE":
            level = (
                logging.WARNING
                if self._last_transmit_source in DIAGNOSTIC_TRANSMIT_SOURCES
                else logging.DEBUG
            )
            if self._move_awaiting_ack:
                # Same reasoning as the t1 branch above: this tE is the
                # reply to control_blind_with_retry()'s own write, handled
                # entirely by that method's own retry loop - independent of,
                # and never touching, _pending_retry_command/_retry_task
                # below, which remain send_command()'s alone.
                self._move_ack_result = False
                self._move_ack_event.set()
            if not self._pending_retry_command:
                # control_blind() (open/close/stop) never sets this - see
                # its own docstring - so a busy reply to one of those three
                # is expected sometimes and not actionable here: there is no
                # in-flight retryable command to resend.
                _LOGGER.log(
                    level, "Transmit error - stick busy, no pending command to retry"
                )
                return
            # Always logged at DEBUG, never escalated to WARNING like the
            # other two branches above: a busy stick retrying itself back
            # to healthy is the normal, expected case under this policy
            # (see the docstring), not something worth surfacing to anyone
            # not already watching debug logs.
            _LOGGER.debug(
                "Transmit busy: command retry in 100ms source=%s payload=%s attempt=%d",
                self._last_transmit_source,
                self._pending_retry_command,
                self._retry_count + 1,
            )
            if self._retry_task is not None and not self._retry_task.done():
                # A retry is already scheduled for the current pending
                # command. A duplicate busy signal must not cancel and
                # reschedule it, or a burst of tE responses could postpone
                # the retry forever.
                return
            # No retry limit: a busy stick is retried for as long as it
            # keeps saying busy, so a colliding command is never abandoned.
            # _retry_count is kept only for the attempt number logged just
            # above - nothing compares it to a limit anymore.
            self._retry_count += 1
            command = self._pending_retry_command
            source = self._last_transmit_source
            retry_task = self.hass.async_create_task(
                self._retry_command_after_delay(command, source),
                name="schellenberg-retry-command",
            )
            # Home Assistant's task factories run a task "eagerly": it
            # starts executing synchronously, right up to its first real
            # suspension point, before async_create_task() even returns
            # the Task object here. _retry_command_after_delay's only
            # suspension points are awaits (asyncio.sleep, an idle-wait,
            # the write itself) - when none of them actually suspends
            # (for example a test that mocks asyncio.sleep to resolve
            # immediately), the whole retry runs to completion, including
            # its own `finally: self._retry_task = None`, before this line
            # runs. Assigning unconditionally afterwards would then stomp
            # that completed cleanup and leave a stale, already-done Task
            # in self._retry_task forever. Only assign when the task is
            # still genuinely in flight; otherwise leave the None its own
            # finally block already set.
            if not retry_task.done():
                self._retry_task = retry_task
            return
        # Handle device ID response (format: sr5D3E7C where 5D3E7C is the device ID)
        if message.startswith("sr") and len(message) >= 8:
            # Uppercased for consistency with every other device ID in this
            # class (see the "ss" handler below) - the stick's own responses
            # are expected to already be uppercase hex, but normalizing here
            # avoids a latent case-mismatch if that ever isn't true.
            device_id = message[2:8].upper()
            _LOGGER.debug("Received device ID response: %s", device_id)
            if self._device_id_future and not self._device_id_future.done():
                self._device_id_future.set_result(device_id)
            return

        # Handle pairing/list responses (format: sl00BEXXXXXX...)
        # sl = list/pairing response prefix
        # 00BE = 2 bytes to ignore (address prefix)
        # XXXXXX = 3 bytes device ID (the actual device ID we want)
        # Rest = can be ignored
        if message.startswith("sl") and len(message) >= 12:
            # Extract the device ID: skip "sl" (2 chars) + "00BE" (4 chars) = 6 chars
            # Then take the next 6 characters (3 bytes as hex) = 6 chars
            # (needs 12 chars total; the old ">= 8" guard let message[6:12]
            # silently return a truncated ID for any 8-11 char message)
            # Uppercased for consistency with every other device ID in this
            # class - see the matching comment on the "sr" handler above.
            device_id = message[6:12].upper()
            _LOGGER.debug(
                "Received pairing/list response: %s, extracted device ID: %s",
                message,
                device_id,
            )
            _LOGGER.debug(
                "Pairing mode active: %s",
                self._pairing_future is not None and not self._pairing_future.done(),
            )

            # If we're in pairing mode, accept ANY device response
            # because the user is explicitly trying to pair RIGHT NOW
            if self._pairing_future and not self._pairing_future.done():
                _LOGGER.info("Pairing candidate detected device_id=%s", device_id)
                self._pairing_future.set_result(device_id)
                # Don't send dispatcher signal here - let the caller handle persistence
                return
            return

        # Handle Schellenberg device messages
        # Format: ssXXYYYYYYZZZZCCPPRR
        # ss = prefix (2 chars)
        # XX = device enum (2 chars)
        # YYYYYY = device ID (6 chars)
        # ZZZZ = message incrementor (4 chars, ignored)
        # CC = command (2 chars)
        # PP = padding (2 chars, ignored)
        # RR = signal strength (2 chars, ignored)
        if message.startswith("ss") and len(message) >= 18:
            try:
                device_enum = message[2:4]
                device_id = message[4:10]
                # Skip message incrementor at positions 10:14
                command = message[14:16]

                _LOGGER.debug(
                    "Parsed: enum=%s, id=%s, cmd=%s", device_enum, device_id, command
                )
                normalized_device_id = device_id.upper()
                normalized_device_enum = device_enum.upper()
                normalized_command = command.upper()
                identity = (normalized_device_id, normalized_device_enum)
                registration = self._registered_entity_keys.get(identity)
                interpretation = _interpret_status_command(normalized_command)
                identity_role = (
                    "primary"
                    if registration is not None and registration.primary
                    else "secondary"
                    if registration is not None
                    else "unmatched"
                )
                position_tracking = bool(
                    registration is not None
                    and registration.primary
                    and interpretation != "unknown"
                )
                self._last_received_sequence += 1
                frame = {
                    "device_id": normalized_device_id,
                    "enum": normalized_device_enum,
                    "command": normalized_command,
                    "time": dt_util.now().strftime("%H:%M:%S"),
                    "sequence": self._last_received_sequence,
                    "matched": registration is not None,
                    "identity_role": identity_role,
                    "interpreted_command": interpretation,
                    "position_tracking": position_tracking,
                }
                self._record_last_received_message(identity, frame)
                raw_frame = dict(frame)
                raw_frame["phase"] = (
                    self._status_discovery_phase
                    if self._status_discovery_frames is not None
                    else "pairing"
                    if self._pairing_active
                    else "idle"
                )
                if self._status_discovery_frames is not None:
                    captured_frame = dict(raw_frame)
                    if normalized_command == CMD_STOP and captured_frame["phase"] in {
                        "opening",
                        "closing",
                    }:
                        captured_frame["phase"] = f"{captured_frame['phase']}_endstop"
                    if len(self._status_discovery_frames) < MAX_STATUS_DISCOVERY_FRAMES:
                        self._status_discovery_frames.append(captured_frame)
                    else:
                        _LOGGER.debug(
                            "Status-frame capture window at its %d-frame "
                            "cap; dropping further frames for the rest of "
                            "this window",
                            MAX_STATUS_DISCOVERY_FRAMES,
                        )
                if position_tracking:
                    # Secondary and unknown primary frames must never overwrite the
                    # last frame that can legitimately drive the position model.
                    self._last_primary_tracking_messages[identity] = dict(frame)

                # If we're in pairing mode and this is a new device
                if self._pairing_future and not self._pairing_future.done():
                    known_status_id = any(
                        key[0] == normalized_device_id
                        for key in self._registered_entity_keys
                    )
                    known_command_id = any(
                        registered_id.upper() == normalized_device_id
                        for registered_id in self._registered_devices
                    )
                    if not known_command_id and not known_status_id:
                        _LOGGER.info(
                            "Pairing candidate detected device_id=%s", device_id
                        )
                        self._pairing_future.set_result(device_id)
                        # Don't send dispatcher signal here - let the
                        # caller handle persistence
                        return

                if registration is None:
                    _LOGGER.debug(
                        "Received device_id=%s enum=%s cmd=%s matched=False "
                        "interpreted=%s; no cover has this status identity",
                        normalized_device_id,
                        normalized_device_enum,
                        normalized_command,
                        interpretation,
                    )
                else:
                    _LOGGER.debug(
                        "Received device_id=%s enum=%s cmd=%s matched=True entity=%s "
                        "identity_role=%s interpreted=%s position_tracking=%s",
                        normalized_device_id,
                        normalized_device_enum,
                        normalized_command,
                        registration.entity_name,
                        identity_role,
                        interpretation,
                        position_tracking,
                    )

                # Retain the ID-only signal for calibration before an entity exists.
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_DEVICE_EVENT}_{normalized_device_id}",
                    normalized_command,
                )
                if registration is not None:
                    async_dispatcher_send(
                        self.hass,
                        f"{SIGNAL_DEVICE_EVENT}_{normalized_device_id}_"
                        f"{normalized_device_enum}",
                        normalized_command,
                    )
                # Also broadcast on the capture-wide signal while a status-
                # frame capture window is open, independent of
                # normalized_device_id: the ID-only signal above only
                # reaches a calibration/teach flow whose selected device's
                # command/pairing identity happens to equal the identity
                # this frame itself carries. Those two can genuinely differ
                # for one physical device (see SIGNAL_DEVICE_EVENT_CAPTURE's
                # own comment in const.py), in which case the signal above
                # would never fire during that flow's calibration wait -
                # this is the fallback that lets it still see the frame.
                if self._status_discovery_frames is not None:
                    async_dispatcher_send(
                        self.hass,
                        SIGNAL_DEVICE_EVENT_CAPTURE,
                        normalized_device_id,
                        normalized_command,
                    )
            except (IndexError, ValueError) as err:
                _LOGGER.debug("Failed to parse message %s: %s", message, err)

    async def send_command(self, command: str, *, source: str = "internal") -> bool:
        """Send a command to the USB stick.

        Writing and its surrounding bookkeeping happen under
        _transmit_lock so two commands issued from different tasks can
        never interleave their writes on the shared serial line or stomp
        on each other's retry tracking (_pending_retry_command).
        """
        if self._transport is None or self._transport.is_closing():
            _LOGGER.warning("Serial port not connected. Command dropped: %s", command)
            return False

        level = (
            logging.WARNING if source in DIAGNOSTIC_TRANSMIT_SOURCES else logging.INFO
        )
        # Only RF transmissions are answered with t1/t0, or with tE while the
        # stick is busy. Other requests (device id, version, LED, ...) get
        # their own replies, so they are neither retried nor "pending".
        is_transmit = command.startswith(CMD_TRANSMIT)
        async with self._transmit_lock:
            self._transmit_busy = True
            try:
                if is_transmit:
                    # Remember who asked so a later, asynchronous t1/t0/tE can
                    # still be logged with the right source.
                    self._last_transmit_source = source
                    # Store command for potential retry on "stick busy"
                    # error. A plain request must not get here: no t0
                    # would ever clear it, and it would replace the
                    # payload of a transmission that is still in flight.
                    self._pending_retry_command = command

                full_command = f"{command}\r\n".encode("ascii")
                _LOGGER.log(
                    level,
                    "Serial write attempt source=%s payload=%s",
                    source,
                    command,
                )
                try:
                    self._transport.write(full_command)
                except (OSError, RuntimeError):
                    _LOGGER.exception(
                        "Serial write failed source=%s payload=%s result=failed",
                        source,
                        command,
                    )
                    if is_transmit:
                        self._pending_retry_command = None
                    return False
                _LOGGER.log(
                    level,
                    "Serial write succeeded source=%s payload=%s result=written",
                    source,
                    command,
                )
                return True
            finally:
                self._transmit_busy = False

    async def _wait_for_transmitter_idle(
        self,
        reason: str,
        *,
        timeout: float = TRANSMIT_IDLE_TIMEOUT,  # noqa: ASYNC109
    ) -> bool:
        """Wait for the stick to report its transmitter idle (a t0 ACK).

        Returns True immediately if it is already idle - the common case,
        since most single-shot commands (cover control) never call this at
        all and simply rely on the busy/retry mechanism instead. Multi-step
        sequences (teach, raw transmit, pairing) await this between steps
        so they never send the next payload while the previous one is
        still in flight. Returns False if the idle ACK doesn't arrive
        within `timeout` seconds.

        ASYNC109 suggests dropping `timeout` in favor of callers wrapping
        each call in `asyncio.timeout()`. Not applied here: all six call
        sites share the same bool-returning "did it go idle in time?"
        contract with the default timeout: moving the try/except to each
        of them would replace one centralized wait with six duplicates of
        it, for no real gain.
        """
        if self._transmitter_idle.is_set():
            return True
        _LOGGER.debug("Waiting up to %.1fs for transmitter idle: %s", timeout, reason)
        try:
            await asyncio.wait_for(self._transmitter_idle.wait(), timeout=timeout)
        except TimeoutError:
            _LOGGER.warning("Timed out waiting for transmitter idle before %s", reason)
            return False
        return True

    async def wait_for_transmitter_idle(
        self,
        reason: str,
        *,
        timeout: float = TRANSMIT_IDLE_TIMEOUT,  # noqa: ASYNC109
    ) -> bool:
        """Public entry point for multi-step sequences outside this module.

        config_flow.py's short motor test (async_step_test_motor) sends a
        direction command via control_blind_exclusive() and, after a
        brief hold, follows it with a stop - the same "don't start the
        next RF write before the previous one's t0" problem
        _wait_for_transmitter_idle() already solves for teach/pairing/
        raw-transmit below. control_blind() itself never awaits this (it
        is deliberately lock-free and immediate - see its own docstring),
        so the caller is responsible for waiting between its own two
        writes. Thin wrapper only: all the actual waiting logic lives in
        _wait_for_transmitter_idle().
        """
        return await self._wait_for_transmitter_idle(reason, timeout=timeout)

    async def _retry_command_after_delay(self, command: str, source: str) -> None:
        """Wait briefly, then resend a command the stick reported as busy.

        The command and source are captured by the caller at scheduling
        time rather than re-read from _pending_retry_command afterwards,
        so this still resends the right payload even if an unrelated t0
        clears that field while this retry is waiting.
        """
        try:
            await asyncio.sleep(0.1)
            if not await self._wait_for_transmitter_idle("retrying after stick busy"):
                _LOGGER.warning(
                    "Transmit retry abandoned source=%s payload=%s reason=idle_timeout",
                    source,
                    command,
                )
                self._pending_retry_command = None
                self._busy_latched = True
                self._retry_count = 0
                return
            _LOGGER.debug("Retrying command after stick busy: %s", command)
            await self.send_command(command, source=source)
        except asyncio.CancelledError:
            _LOGGER.debug("Retry task cancelled")
        finally:
            self._retry_task = None

    @callback
    def start_status_frame_capture(self, *, phase: str) -> None:
        """Start one bounded receive-frame capture for pairing or calibration.

        Raises RuntimeError, without starting anything, when a different
        capture is already open and still fresh - see
        CalibrationFlowHandler._start_calibration_capture()'s own docstring
        (options_flow_calibration.py) for why callers must not force their
        way in on that RuntimeError.

        A still-open capture older than
        STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS (const.py) is reclaimed
        here instead: nothing in Home Assistant's own flow-removal path
        calls back into this integration to release a capture its owning
        flow never finished, so a wizard closed, navigated away from, or
        otherwise abandoned between claiming this buffer and its own next
        await - start_status_frame_capture() succeeding is itself a plain,
        synchronous @callback, not an await point - used to leak it
        forever, refusing every later attempt (including retries of the
        very same blind) with no way to tell a genuinely still-running
        capture apart from a permanently abandoned one. See that constant's
        own comment for the real-world report this fixes.
        """
        if self._status_discovery_frames is not None:
            started_at = dt_util.parse_datetime(self._status_discovery_started_at or "")
            age_seconds = (
                (dt_util.now() - started_at).total_seconds()
                if started_at is not None
                else None
            )
            if (
                age_seconds is None
                or age_seconds < STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS
            ):
                raise RuntimeError("status frame capture is already active")
            _LOGGER.warning(
                "Reclaiming a stale status-frame capture phase=%s age=%.1fs - "
                "its owning flow never finished it",
                self._status_discovery_phase,
                age_seconds,
            )
        self._status_discovery_frames = []
        self._status_discovery_phase = phase
        self._status_discovery_started_at = dt_util.now().isoformat()

    @callback
    def set_status_frame_capture_phase(self, phase: str) -> None:
        """Label subsequently received frames with the current workflow phase."""
        if self._status_discovery_frames is not None:
            self._status_discovery_phase = phase

    @callback
    def finish_status_frame_capture(
        self, *, end_reason: str, exclude_device_ids: frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        """Stop capture and return grouped candidates plus raw phase-labelled frames.

        exclude_device_ids: forwarded as-is to summarize_status_discovery_
        frames() - see its own docstring. Empty by default, so the one
        async_discover_status_identities() call site (config_flow.py's
        async_step_discover_status) is unaffected (that flow deliberately
        never scopes this - overwriting an existing identity is its whole
        point); only the calibration flow
        (options_flow_calibration.py's _finish_calibration_capture())
        currently passes a real set, to keep a different, already-paired
        blind's own ordinary movement during the capture window from
        outscoring the blind actually being worked on.
        """
        frames = list(self._status_discovery_frames or [])
        started_at = self._status_discovery_started_at
        self._status_discovery_frames = None
        self._status_discovery_phase = "idle"
        self._status_discovery_started_at = None
        result = summarize_status_discovery_frames(
            frames, exclude_device_ids=exclude_device_ids
        )
        result.update(
            {
                "frames": frames,
                "started_at": started_at,
                "completed_at": dt_util.now().isoformat(),
                "end_reason": end_reason,
            }
        )
        return result

    async def async_discover_status_identities(
        self, *, capture_duration: float = STATUS_DISCOVERY_TIMEOUT
    ) -> dict[str, Any]:
        """Capture and classify frames from one guided original-remote sequence."""
        if not self._is_connected:
            raise ConnectionError("USB stick is not connected")
        if capture_duration <= 0:
            raise ValueError("status discovery timeout must be positive")

        self.start_status_frame_capture(phase="remote_discovery")
        _LOGGER.debug(
            "Status discovery started timeout=%.1fs; listening for original remote",
            capture_duration,
        )
        try:
            await asyncio.sleep(capture_duration)
        except BaseException:
            self.finish_status_frame_capture(end_reason="cancelled")
            raise
        result = self.finish_status_frame_capture(end_reason="capture_timeout_complete")

        primary = result["primary"]
        _LOGGER.debug(
            "Status discovery completed frames=%d groups=%d primary=%s "
            "secondary_count=%d position_tracking=%s",
            len(result["frames"]),
            len(result["groups"]),
            (
                f"{primary['device_id']}/{primary['enum']}"
                if primary is not None
                else "unknown"
            ),
            len(result["secondary"]),
            result["position_tracking_available"],
        )
        return result

    async def pair_device_and_wait(self) -> tuple[str, str] | None:
        """Put the stick into pairing mode and wait for a device to pair."""
        # Reset unconditionally, before any early return below, so this
        # call's own outcome is never contaminated by a previous attempt's
        # - see pairing_interrupted_by_disconnect's own docstring.
        self._pairing_interrupted_by_disconnect = False
        if self._pairing_future and not self._pairing_future.done():
            _LOGGER.warning("Pairing already in progress")
            return None
        if not self._is_transmit_capable():
            _LOGGER.error(
                "Pairing blocked because stick is not ready connected=%s mode=%s",
                self._is_connected,
                self._device_mode,
            )
            return None

        device_enum = self.initialize_next_device_enum()
        pair_command = f"{CMD_TRANSMIT}{device_enum}9{CMD_PAIR}0000"
        finish_pair_command = f"{CMD_TRANSMIT}{device_enum}9{CMD_ALLOW_PAIRING}0000"
        _LOGGER.info(
            "Initiating pairing with device enum %s teach_payload=%s finish_payload=%s",
            device_enum,
            pair_command,
            finish_pair_command,
        )

        self._pairing_active = True
        self._device_mode = "pairing"
        self._update_status()
        self._pairing_future = self.hass.loop.create_future()

        try:
            _LOGGER.debug("Entering pairing mode with command: sp")
            if not await self.send_command(CMD_GET_PARAM_P):
                raise ConnectionError("could not enter pairing mode")

            try:
                device_id = await asyncio.wait_for(
                    self._pairing_future, timeout=PAIRING_TIMEOUT
                )
            except ConnectionError:
                # disconnect() injects exactly this exception into
                # _pairing_future (see its own comment) for every reason
                # it's called: a hub-wide reload - triggered by a
                # *different* blind's own subentry add/edit, not
                # necessarily anything about this pairing attempt - the
                # integration unloading, a Core stop/restart, or
                # reset_and_reconnect()'s explicit stick reset.
                # _disconnect_requested is set only by that intentional
                # path, never by handle_connection_lost()'s own, separate
                # injection of the same exception type for a genuine,
                # unexpected connection loss - so checking it here, at the
                # exact moment this specific await was interrupted, is
                # what lets config_flow.py tell "cut short by something
                # else reloading/unloading the hub" apart from "the stick
                # itself dropped the connection" (left reporting the
                # existing generic pairing_timeout message, since that
                # case is a real, separate problem this property isn't
                # about) and from an ordinary 2-minute timeout, without
                # changing this method's own return-None-on-failure
                # contract at all - re-raised as-is for the except
                # ConnectionError branch below to log and handle exactly
                # as before.
                self._pairing_interrupted_by_disconnect = self._disconnect_requested
                raise
            _LOGGER.debug(
                "Received device ID %s, sending pairing teach_payload=%s "
                "then finish_payload=%s",
                device_id,
                pair_command,
                finish_pair_command,
            )

            # Every pairing attempt (async_step_user) sends this one
            # sequence: the 0x60 teach command followed by the 0x40
            # finish/allow-pairing command.
            pairing_phases = (
                ("teach_60", pair_command),
                ("finish_40", finish_pair_command),
            )

            for phase, payload in pairing_phases:
                if not await self.send_command(payload, source="pairing"):
                    raise ConnectionError(f"could not send pairing phase {phase}")
                if not await self._wait_for_transmitter_idle(
                    f"finishing pairing phase {phase}"
                ):
                    raise ConnectionError(
                        f"stick did not acknowledge pairing phase {phase}"
                    )
                _LOGGER.info(
                    "Pairing stick ACK completed phase=%s payload=%s "
                    "motor_authorization=unverified",
                    phase,
                    payload,
                )
        except TimeoutError:
            _LOGGER.warning("Pairing timeout - no device responded with ID")
            return None
        except ConnectionError as err:
            _LOGGER.warning("Pairing stopped because serial connection failed: %s", err)
            return None
        else:
            _LOGGER.info(
                "Pairing teach command transmitted device_id=%s enum=%s; "
                "motor authorization remains unverified until movement succeeds",
                device_id,
                device_enum,
            )
            return (device_id, device_enum)
        finally:
            self._pairing_future = None
            # Await pairing shutdown so the stick is back in listening mode
            # (or a known "unknown" mode if "sp" itself failed) before this
            # function returns control to its caller, rather than leaving a
            # background task the caller never waited for.
            stop_task = self.hass.async_create_task(
                self._stop_pairing_mode(),
                name="schellenberg-stop-pairing",
            )
            self._stop_pairing_task = stop_task
            try:
                await stop_task
            finally:
                if self._stop_pairing_task is stop_task:
                    self._stop_pairing_task = None

    async def _stop_pairing_mode(self) -> None:
        """Toggle pairing off and restore the normal listening-mode state.

        Whether the stick is considered back in listening mode is decided
        by the "sp" write alone, the same way _enter_listening_mode()
        trusts a successful "hello" write without waiting for an ACK:
        like "hello", "sp" is not an RF transmission (see send_command()'s
        own is_transmit comment), so it never gets a real t1/t0 reply. An
        earlier version of this method also awaited
        _wait_for_transmitter_idle() afterwards and only logged a
        "stopped cleanly" message if that resolved before its own
        TRANSMIT_IDLE_TIMEOUT - but that wait can only ever reflect some
        unrelated, possibly stale idle flag, never "sp" itself, and a
        version that instead let a timed-out wait downgrade
        _device_mode to "unknown" (even though "sp" had already
        succeeded) is what caused the real bug this fix addresses: since
        _transmit_capability_block_reason() then blocks every future
        pairing attempt on "wrong_mode:unknown" until a full reconnect,
        the pairing config flow step (async_step_user) would abort with
        the same "pairing_timeout" message again immediately on the
        very next attempt, before any new wait for a device even began.
        Trusting the write alone, the same way "hello" already
        does, fixes that and also drops the now-pointless wait entirely:
        the worst case if the stick genuinely is still finishing up is
        one more pairing attempt that does not find a device in time,
        not a permanent lockout.

        This also used to take a `delay` flag that, when the pairing
        sequence itself had just succeeded, slept 2 seconds before this
        "sp" write - meant to avoid racing the first post-pairing test
        command. That fixed pause ran unconditionally on every successful
        pairing with no real signal behind it (nothing here ever confirms
        the stick is actually still busy), so it has been dropped; "sp"
        is sent immediately in every case now.
        """
        sent = False
        try:
            _LOGGER.debug("Stopping pairing mode with command: sp")
            sent = await self.send_command(CMD_GET_PARAM_P)
            if sent:
                _LOGGER.info("Pairing mode stopped; stick returned to listening mode")
            else:
                _LOGGER.warning("Could not send pairing stop command")
        except OSError as err:
            _LOGGER.debug("Error stopping pairing mode (communication error): %s", err)
        finally:
            self._pairing_active = False
            if self._is_connected:
                self._device_mode = "listening" if sent else "unknown"
            self._update_status()

    async def teach_motor(
        self,
        device_enum: str,
        *,
        device_id: str | None = None,
        source: str = "developer_tools",
    ) -> bool:
        """Transmit the full remote-assisted motor teach sequence."""
        normalized_enum = _normalize_protocol_enum(device_enum)
        if reason := self._transmit_capability_block_reason():
            _LOGGER.error(
                "Motor teach blocked reason=%s source=%s device_id=%s enum=%s",
                reason,
                source,
                device_id or "unknown",
                normalized_enum,
            )
            return False

        # The motor must first be put into programming mode with an authorized
        # physical remote. The stick then transmits 0x60 (teach its identity and
        # rolling-code start), followed directly by 0x40 to finish pairing.
        teach_payload = f"{CMD_TRANSMIT}{normalized_enum}9{CMD_PAIR}0000"
        finish_payload = f"{CMD_TRANSMIT}{normalized_enum}9{CMD_ALLOW_PAIRING}0000"
        _LOGGER.warning(
            "Motor teach sequence source=%s device_id=%s enum=%s "
            "teach_payload=%s finish_payload=%s "
            "prerequisite=authorized_remote_programming_mode",
            source,
            device_id or "unknown",
            normalized_enum,
            teach_payload,
            finish_payload,
        )

        # Claim the same single-writer signal real pairing uses so
        # control_blind()/control_blind_with_retry() refuse every blind's
        # open/close/stop for as long as this two-phase 60-then-40 write is
        # in flight. Without this, _transmit_capability_block_reason() has
        # no way to tell a teach sequence is running: unlike
        # pair_device_and_wait(), teach_motor() deliberately never touches
        # _device_mode, since the stick's own hardware mode genuinely stays
        # "listening" throughout here - only the physical remote enters
        # programming mode, not the stick - so "pairing_active" is the only
        # signal available, and leaving it unset let a concurrent move race
        # this sequence's own send_command() writes for the stick's one RF
        # transmitter, the same class of bug _move_lock exists to close for
        # move-vs-move. Cleared in the finally below on every exit path,
        # including a failed write or ACK timeout, so a teach failure can
        # never leave every blind permanently blocked.
        self._pairing_active = True
        self._update_status()
        try:
            for phase, payload in (
                ("teach_60", teach_payload),
                ("finish_40", finish_payload),
            ):
                _LOGGER.warning(
                    "Motor teach phase write source=%s device_id=%s enum=%s "
                    "phase=%s payload=%s",
                    source,
                    device_id or "unknown",
                    normalized_enum,
                    phase,
                    payload,
                )
                if not await self.send_command(payload, source=source):
                    _LOGGER.error(
                        "Motor teach write failed source=%s device_id=%s enum=%s "
                        "phase=%s payload=%s",
                        source,
                        device_id or "unknown",
                        normalized_enum,
                        phase,
                        payload,
                    )
                    return False
                if not await self._wait_for_transmitter_idle(
                    f"finishing motor teach phase {phase}"
                ):
                    _LOGGER.error(
                        "Motor teach phase ACK timeout source=%s device_id=%s enum=%s "
                        "phase=%s payload=%s",
                        source,
                        device_id or "unknown",
                        normalized_enum,
                        phase,
                        payload,
                    )
                    self._busy_latched = True
                    return False
                _LOGGER.warning(
                    "Motor teach phase stick ACK completed source=%s device_id=%s "
                    "enum=%s phase=%s payload=%s stick_ack_only=True",
                    source,
                    device_id or "unknown",
                    normalized_enum,
                    phase,
                    payload,
                )

            _LOGGER.warning(
                "Motor teach sequence stick ACK completed source=%s device_id=%s "
                "enum=%s phases=60_then_40 stick_ack_only=True "
                "motor_authorization=unverified",
                source,
                device_id or "unknown",
                normalized_enum,
            )
            return True
        finally:
            self._pairing_active = False
            self._update_status()

    async def send_raw_transmit(
        self, payload: str, *, source: str = "developer_tools"
    ) -> bool:
        """Validate and send one exact 11-character Schellenberg RF payload."""
        candidate = str(payload).strip()
        normalized = f"ss{candidate[2:].upper()}" if len(candidate) >= 2 else candidate
        if (
            len(candidate) != 11
            or candidate[:2].lower() != "ss"
            or any(character not in "0123456789ABCDEF" for character in normalized[2:])
        ):
            raise ValueError(
                "raw RF payload must be exactly 'ss' plus 9 hexadecimal characters"
            )
        if reason := self._transmit_capability_block_reason():
            _LOGGER.error(
                "Raw RF transmit blocked reason=%s source=%s payload=%s",
                reason,
                source,
                normalized,
            )
            return False

        _LOGGER.warning(
            "Raw RF transmit requested source=%s payload=%s payload_chars=%d",
            source,
            normalized,
            len(normalized),
        )
        # Claim the same single-writer signal teach_motor()/
        # pair_device_and_wait() use so control_blind()/
        # control_blind_with_retry() refuse every blind's open/close/stop
        # for as long as this transmit-then-wait-for-idle cycle is in
        # flight. Without this, a raw payload sent from here could
        # physically collide on the wire with an unrelated blind's own move
        # or stop - the same class of bug _pairing_active's teach_motor()
        # fix closed for teach, left open here until now because a raw
        # payload is not teach/pairing and _device_mode genuinely does not
        # change for it either, so "pairing_active" is the only signal
        # available, same reasoning as teach_motor()'s own. Cleared in the
        # finally below on every exit path, including a failed write or
        # idle timeout, so a failed raw send can never leave every blind
        # permanently blocked.
        self._pairing_active = True
        self._update_status()
        try:
            sent = await self.send_command(normalized, source=source)
            if not sent:
                _LOGGER.error(
                    "Raw RF transmit result source=%s payload=%s "
                    "result=write_failed stick_ack_only=True motor_result=unknown",
                    source,
                    normalized,
                )
                return False

            if not await self._wait_for_transmitter_idle("finishing raw RF transmit"):
                _LOGGER.error(
                    "Raw RF transmit result source=%s payload=%s "
                    "result=ack_timeout stick_ack_only=True motor_result=unknown",
                    source,
                    normalized,
                )
                self._busy_latched = True
                return False

            _LOGGER.log(
                logging.WARNING
                if source in DIAGNOSTIC_TRANSMIT_SOURCES
                else logging.DEBUG,
                "Raw RF transmit result source=%s payload=%s result=written "
                "stick_ack_only=True motor_result=unknown",
                source,
                normalized,
            )
            return True
        finally:
            self._pairing_active = False
            self._update_status()

    async def control_blind(
        self,
        device_enum: str,
        action: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> bool:
        """Send an open/close/stop command straight to the serial port.

        One write, immediately, never queued behind _transmit_lock and never
        entered into the tE busy-retry cycle - open, close, and stop are all
        sent exactly this same way. Confirmed against real hardware that
        routing stop through a separate lock-bypassing path while open/close
        went through a locked, retried path let the two fight over the
        stick's one RF transmitter: a move's own busy-retry could still be
        re-sending its payload seconds after a stop had already gone out,
        and on exhausting its retries would latch a busy fault that had
        nothing to do with the stop. This link gives no delivery
        confirmation for any of these three commands to begin with - the
        stick's t1/t0 report only its own transmitter on/off, never what the
        motor did - so there is nothing worth queueing or retrying for:
        every call here sends once and returns.

        Still refuses to transmit when the stick fundamentally cannot right
        now (disconnected, closing transport, pairing active, not in
        listening mode) - see _transmit_capability_block_reason(). Returns
        whether the frame was handed to the transport; never raises.
        """
        if action not in (CMD_UP, CMD_DOWN, CMD_STOP):
            _LOGGER.error(
                "Blind command blocked reason=invalid_action source=%s action=%s",
                source,
                action,
            )
            return False

        normalized_enum = _normalize_protocol_enum(device_enum)
        diagnostic = source in DIAGNOSTIC_TRANSMIT_SOURCES
        visible_level = logging.WARNING if diagnostic else logging.INFO
        action_name = {CMD_UP: "open", CMD_DOWN: "close", CMD_STOP: "stop"}[action]
        _LOGGER.log(
            visible_level,
            "Blind transmit requested source=%s command=%s device_id=%s enum=%s "
            "connected=%s mode=%s ready=%s pairing=%s",
            source,
            action_name,
            device_id or "unknown",
            normalized_enum,
            self._is_connected,
            self._device_mode,
            self.transmit_ready,
            self._pairing_active,
        )
        if reason := self._transmit_capability_block_reason():
            _LOGGER.error(
                "Blind command blocked reason=%s source=%s command=%s "
                "device_id=%s enum=%s",
                reason,
                source,
                action_name,
                device_id or "unknown",
                normalized_enum,
            )
            return False

        # send_command() already does this for every command it routes
        # through _pending_retry_command/_last_transmit_
        # source, but this method's whole reason to exist (see its own
        # docstring) is writing straight to the transport instead,
        # bypassing send_command() - and with it, this assignment -
        # entirely. Without this, a t1/t0 arriving after this write (and
        # before any other, send_command()-routed one) would log, and
        # self-heal _busy_latched under, whichever *earlier* caller last
        # went through send_command() instead of this one - diagnostic/log
        # attribution only, since _busy_latched is documented as never
        # blocking a new command regardless.
        self._last_transmit_source = source

        raw_payload = f"{CMD_TRANSMIT}{normalized_enum}9{action}0000"
        _LOGGER.log(
            visible_level,
            "Blind transmit payload source=%s command=%s device_id=%s enum=%s "
            "payload=%s",
            source,
            action_name,
            device_id or "unknown",
            normalized_enum,
            raw_payload,
        )
        full_command = f"{raw_payload}\r\n".encode("ascii")
        try:
            # _transmit_capability_block_reason() above already established
            # self._transport is a live, non-closing transport.
            assert self._transport is not None
            self._transport.write(full_command)
        except (OSError, RuntimeError):
            _LOGGER.exception(
                "Blind transmit write result source=%s command=%s payload=%s "
                "result=failed",
                source,
                action_name,
                raw_payload,
            )
            return False
        _LOGGER.log(
            visible_level,
            "Blind transmit write result source=%s command=%s payload=%s "
            "result=written_no_lock_no_ack_wait",
            source,
            action_name,
            raw_payload,
        )
        return True

    @callback
    def cancel_pending_move(self, device_id: str) -> None:
        """End device_id's in-flight move retry loop, if it has one.

        Called by async_stop_cover() and the auto-stop-at-target path in
        cover.py right before they send a stop, so a
        control_blind_with_retry() call still waiting out a busy stick for
        *this same blind* gives up instead of resending the open/close
        command after the stop has already gone out - the exact collision
        that made routing moves through a busy-retry cycle get reverted
        once before (see async_stop_cover()'s own docstring). A no-op
        whenever device_id has no move currently in progress, which is the
        common case for most stops - never blocks, never raises, safe to
        call unconditionally before every stop.

        Deliberately keyed off _moves_in_progress (per device_id) rather
        than adding to _move_cancel_requested unconditionally: a stop for a
        blind with no move in flight must not leave a stale cancellation
        sitting around for that same blind's next, completely unrelated
        move to trip over. Checking this device_id's own membership, not a
        single shared "the one in-progress device", also means a second
        blind's own concurrent call can never make this check see the
        wrong device - see _moves_in_progress's own comment in __init__.

        Snapshots this device_id's own currently in-flight call tokens
        (there may be more than one - see _moves_in_progress's own comment
        in __init__) into _move_cancel_requested at this exact moment,
        rather than marking the device_id itself as cancelled: a call for
        this same device_id that only starts *after* this method returns
        gets its own, brand new token, which is simply never added here,
        so it is unaffected no matter how long one of the calls actually
        snapshotted here takes to notice and exit - see
        _move_cancel_requested's own comment in __init__ for the bug this
        closes.
        """
        active = self._moves_in_progress.get(device_id)
        if active:
            self._move_cancel_requested.setdefault(device_id, set()).update(active)

    async def control_blind_with_retry(
        self,
        device_enum: str,
        action: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> bool:
        """Open or close a blind, retrying "stick busy" until accepted.

        Never call this for CMD_STOP - a stop has its own, much shorter
        serialization (control_blind_stop()) rather than this method's
        open-ended retry budget, so it can never queue behind a move for
        anywhere near as long. This exists only for async_open_cover()/
        async_close_cover().

        Serializes hub-wide through _move_lock, held across this whole
        wait-for-idle-then-write-then-await-ack cycle, same as before this
        fix's own rework - see _move_lock's own comment in __init__ for the
        race this closes that a shared "wait for idle, then write" pattern
        does not: several blinds commanded together can all observe the
        transmitter idle between the same two ACKs and all write within the
        same ~15ms window, of which the stick then accepts exactly one and
        answers "tE" to the rest - which control_blind() neither waits for
        nor retries on its own, so those commands used to vanish rather
        than merely arrive late.

        The one addition is has_priority_waiter(): checked both right
        after acquiring the lock and again the instant the idle-wait
        resolves, and either time, a waiting control_blind_stop() call
        makes this attempt release the lock without writing and loop
        around to request it again, rather than finishing its own turn
        first. Without this, a move that merely got to the lock first
        could hold it for an entire transmit cycle doing nothing but
        wait - exactly what let a stop end up queueing behind a completely
        unrelated blind's own pending move for that whole cycle, confirmed
        from a real log where this cost a stop over a second, long enough
        for the blind being stopped to visibly overshoot its target.
        Checking twice (not only once, before the wait) matters because
        the realistic case is a stop arriving *while* this attempt is
        already parked waiting for idle - by the time that wait resolves,
        the stop has had the whole rest of that same wait to register
        itself as a priority waiter, and the second check catches it right
        before the write that would otherwise go out first.

        Retried on "tE" with no limit on the number of attempts, like
        send_command()'s own busy-retry cycle - same policy, independent
        machinery (see _move_lock's comment in __init__ for why this does
        not simply reuse _pending_retry_command): a persistently busy
        stick is retried rather than the command being abandoned after
        some fixed count, since in practice "busy" here almost always
        just means another blind's own ~1.1-1.2s transmission is still in
        flight, not a genuinely stuck stick. Elapsed time is bounded,
        though, by MOVE_RETRY_OVERALL_TIMEOUT, measured from this call's
        very first attempt - see its own comment for why unlimited attempts
        must not also mean unlimited elapsed time.

        Cancellation (cancel_pending_move()) and the overall timeout are
        both checked once per loop iteration, before that iteration's
        wait-for-idle and write: a stop issued for this same device_id, or
        the deadline passing, ends this loop (returns False) before its
        next write, rather than letting an already-queued retry resend the
        move late. Best-effort only, like _wait_for_transmitter_idle()
        itself: either arriving while already inside a long idle-wait is
        not preempted mid-wait, only picked up afterwards - in practice
        that wait almost always resolves within about 1.2s (one transmit
        cycle), not its full 5s budget, so the realistic worst case is
        short. Cancellation alone - not the overall timeout, which has no
        remaining meaning once this method has already returned - is
        checked a second time, after this same loop has already
        succeeded and this method has returned True: no longer here, but
        inside _send_move_resends_and_cleanup() (this same call's own
        detached background task, spawned the moment the write is
        accepted - see its own docstring), immediately before each of
        MOVE_COMMAND_EXTRA_RESENDS' own best-effort resends there
        instead. See that constant's own comment in const.py for why a
        resend fired on its fixed schedule regardless would otherwise be
        able to silently undo a stop requested for this device in the
        meantime. Scoped to whichever calls for this
        device_id are genuinely in flight at the moment
        cancel_pending_move() is called, never to one that starts only
        afterwards - see _move_cancel_requested's own comment in __init__
        for the narrow bug that distinction closes.
        """
        if action not in (CMD_UP, CMD_DOWN):
            # CMD_STOP, or anything else, must never reach here - see the
            # docstring above.
            _LOGGER.error(
                "control_blind_with_retry blocked reason=not_a_move source=%s "
                "action=%s",
                source,
                action,
            )
            return False
        if device_id is None:
            # Every real caller - async_open_cover()/async_close_cover(),
            # the only two - always passes its own blind's device_id (see
            # this method's own docstring: it exists only for them). The
            # per-device tracking below needs a real device to key it by,
            # so bail out the same defensive way as the not-a-move check
            # above rather than letting a hypothetical future caller that
            # forgot to pass one fall through with no device to track.
            _LOGGER.error(
                "control_blind_with_retry blocked reason=no_device_id "
                "source=%s action=%s",
                source,
                action,
            )
            return False
        # Added for this call's whole duration, not just once _move_lock is
        # actually held - cancel_pending_move() can then end this call even
        # while it is still only waiting for idle, before ever reaching the
        # lock, which is strictly more responsive than waiting for that.
        # A fresh token, not a shared scalar or a plain add() to a
        # membership set, so cancel_pending_move() can tell this call apart
        # from both a second blind's own concurrent call (driving several
        # blinds together is this integration's whole point) and a second
        # *same-device* concurrent call (see this attribute's own comment
        # in __init__ for when that happens) - see _move_cancel_requested's
        # own comment in __init__ for why identity, not just membership or
        # a count, is what cancellation actually needs here.
        move_call_token = object()
        self._moves_in_progress.setdefault(device_id, set()).add(move_call_token)
        requested_at = time.monotonic()
        retry_deadline = requested_at + MOVE_RETRY_OVERALL_TIMEOUT
        # Set just before the one `return True` below that spawns
        # _send_move_resends_and_cleanup() - see this method's own
        # `finally` for why it needs to know.
        resend_task_spawned = False
        try:
            while True:
                if move_call_token in self._move_cancel_requested.get(device_id, ()):
                    # Peeked, not discarded - see _move_cancel_requested's
                    # own comment in __init__ for why: a second, concurrent
                    # call for this same device_id may still be retrying
                    # and must see its own cancellation too, not have it
                    # consumed out from under it by whichever call happens
                    # to check first. Cleared centrally in this method's
                    # own `finally` below instead, once this specific call
                    # actually exits.
                    return False
                if time.monotonic() >= retry_deadline:
                    _LOGGER.warning(
                        "Giving up on move after retrying for %.0fs with "
                        "no accept source=%s device_id=%s action=%s",
                        MOVE_RETRY_OVERALL_TIMEOUT,
                        source,
                        device_id or "unknown",
                        action,
                    )
                    self._busy_latched = True
                    return False
                # Bounded by retry_deadline too, not just the wait-for-
                # idle/write/ack cycle after acquiring: acquire() itself
                # used to be unbounded, so a move stuck behind a
                # sufficiently long queue of other moves/stops could run
                # well past MOVE_RETRY_OVERALL_TIMEOUT before this loop
                # ever got back to checking it. On timeout, loop back to
                # the top rather than giving up here directly - by the
                # time wait_for's own timeout has elapsed, the check above
                # is guaranteed to be true, so it gives up there with its
                # usual warning/return instead of this duplicating it.
                try:
                    await asyncio.wait_for(
                        self._move_lock.acquire(),
                        timeout=retry_deadline - time.monotonic(),
                    )
                except TimeoutError:
                    continue
                try:
                    if self._move_lock.has_priority_waiter():
                        # A stop is waiting its turn - step back now
                        # instead of starting a potentially long wait on
                        # idle while holding the lock in front of it.
                        continue
                    if not await self._wait_for_transmitter_idle(
                        "sending a move command"
                    ):
                        continue
                    if self._move_lock.has_priority_waiter():
                        # A stop started waiting its turn while this call
                        # was parked above - still time to yield, one
                        # write away from going out ahead of it.
                        continue
                    self._move_ack_event.clear()
                    self._move_ack_result = None
                    self._move_awaiting_ack = True
                    sent = await self.control_blind(
                        device_enum, action, device_id=device_id, source=source
                    )
                    if not sent:
                        self._move_awaiting_ack = False
                        return False
                    # Snapshotted here, logged only below once this same
                    # write is confirmed accepted - not every attempt that
                    # merely transmitted and then got bounced "tE" (busy).
                    # A busy-retry sequence can genuinely reach this line
                    # more than once per call; only the figure for the
                    # attempt that actually succeeds is reported, so this
                    # always logs exactly once per call, for the real,
                    # final wait - not once per attempt.
                    transmit_wait = time.monotonic() - requested_at
                    try:
                        await asyncio.wait_for(
                            self._move_ack_event.wait(), timeout=MOVE_ACK_TIMEOUT
                        )
                    except TimeoutError:
                        _LOGGER.warning(
                            "No t1/tE reply to move write within %.1fs "
                            "source=%s device_id=%s - retrying",
                            MOVE_ACK_TIMEOUT,
                            source,
                            device_id or "unknown",
                        )
                    self._move_awaiting_ack = False
                finally:
                    self._move_lock.release()
                if self._move_ack_result is True:
                    _LOGGER.info(
                        "Move command transmitting after waiting %.3fs for "
                        "the shared transmitter source=%s device_id=%s "
                        "action=%s",
                        transmit_wait,
                        source,
                        device_id or "unknown",
                        action,
                    )
                    # The MOVE_COMMAND_EXTRA_RESENDS resends that used to
                    # run right here, inline, now run in a detached
                    # background task instead - see
                    # _send_move_resends_and_cleanup()'s own docstring for
                    # what they do and why, and for why this call's own
                    # `finally` below must not clean up move_call_token
                    # when that task has been spawned.
                    self.hass.async_create_task(
                        self._send_move_resends_and_cleanup(
                            device_id,
                            move_call_token,
                            device_enum,
                            action,
                            source,
                        ),
                        name="schellenberg-move-resend",
                    )
                    resend_task_spawned = True
                    return True
                # False (busy) or None (no reply at all, treated the
                # same way - see the docstring) - retry, after the same
                # brief pause send_command()'s own retry uses.
                await asyncio.sleep(0.1)
        finally:
            # Discard only this call's own token, not every token this
            # device_id has - see _moves_in_progress's own comment in
            # __init__ for why identity, not just a count, is what this
            # needs. Popping the device_id entirely once its set is empty
            # keeps this dict from accumulating stale, empty entries for
            # devices with nothing genuinely in flight any more; the same
            # discard-then-pop-if-empty applies to this call's own token in
            # _move_cancel_requested, so a sibling call still genuinely in
            # flight for this same device_id keeps seeing its own
            # cancellation for as long as it is itself still live, with no
            # regard for whether this call's own token happened to be
            # cancelled too.
            #
            # Skipped whenever resend_task_spawned is True: the detached
            # _send_move_resends_and_cleanup() task spawned above owns
            # this exact same cleanup for this exact same token in that
            # case instead, deferred until its own resends are actually
            # done - see its own docstring for why doing it here too,
            # immediately, would let a stop requested in the gap between
            # this method's own return and that task's first resend go
            # unseen by its cancellation check. (Deliberately not a bare
            # `return` to skip this block - a `return` inside `finally`
            # would silently replace this method's own `return True`/
            # `return False` with `return None`.)
            if not resend_task_spawned:
                still_in_progress = self._moves_in_progress.get(device_id)
                if still_in_progress is not None:
                    still_in_progress.discard(move_call_token)
                    if not still_in_progress:
                        self._moves_in_progress.pop(device_id, None)
                still_cancelled = self._move_cancel_requested.get(device_id)
                if still_cancelled is not None:
                    still_cancelled.discard(move_call_token)
                    if not still_cancelled:
                        self._move_cancel_requested.pop(device_id, None)

    async def _send_move_resends_and_cleanup(
        self,
        device_id: str,
        move_call_token: object,
        device_enum: str,
        action: str,
        source: str,
    ) -> None:
        """Fire a just-accepted move's MOVE_COMMAND_EXTRA_RESENDS, detached
        from control_blind_with_retry()'s own return, and retire that
        call's bookkeeping once they're done.

        Mirrors control_blind_stop()'s own extra resend after its primary
        stop is accepted - see MOVE_COMMAND_EXTRA_RESENDS's own comment in
        const.py for why this exists at all: best-effort RF reliability,
        nothing more.

        Spawned with hass.async_create_task() from
        control_blind_with_retry() right when its own primary write is
        confirmed accepted, instead of running inline there as before
        this fix. That inline version blocked control_blind_with_retry()
        from returning True for an extra MOVE_COMMAND_EXTRA_RESENDS *
        MOVE_COMMAND_RESEND_DELAY_SECONDS seconds (~0.5s) after the
        primary write's own ack - and async_open_cover()/
        async_close_cover() only stamp _move_start_time and call
        _start_position_tracking() once that call returns. Unlike the
        equivalent wait on the stop side - a one-time final freeze, using
        a fresh time.monotonic() read, so control_blind_stop()'s own
        comment at its mirror of this point is right that waiting there
        is harmless - this delay instead pushed back the very baseline
        every _update_position() call for the *entire rest of the move*
        measures its own elapsed time from. Two real-world reports traced
        to exactly this: a full up/down run landing on 92%/4% instead of
        100%/0%, and a dashboard that visibly takes longer to start
        updating than before this resend feature existed - both from the
        same ~0.5s now eating into the margin by which software's own
        elapsed-time clock used to lead the real motor's own physical
        start (see _async_position_update_loop()'s own stop_lead_percent
        derivation comment for that margin, and this fix's own CHANGELOG
        entry for the full chain). Restoring an immediate return on
        accept - while still actually sending the same resends, just no
        longer blocking anything on them - fixes both at once, since both
        came from the same shifted baseline.

        This call cannot rely on its own caller awaiting it before
        anything else touches this same device_id, any more than the old
        inline loop could: async_open_cover()/async_close_cover() do
        await control_blind_with_retry() itself, but a *different* task
        for the very same device - the background auto-stop-at-target
        loop, or a brand new async_stop_cover() from the UI/Alexa - can
        run concurrently and legitimately end this move while this task
        is still only sleeping between writes. Both of those already call
        cancel_pending_move() for exactly this device_id before doing
        anything else, so the cancellation check below, re-checked fresh
        before each resend and peeked rather than discarded (a
        cancellation is conceptually for the device, not for one
        arbitrarily-chosen call among the several it may have been
        snapshotted against - see _move_cancel_requested's own comment in
        __init__), is what keeps a resend from firing after a stop has
        already gone out and silently undoing it a moment later.

        That check only works for as long as move_call_token is still
        registered in _moves_in_progress - which is why this method, not
        control_blind_with_retry()'s own `finally`, now owns discarding it
        (see resend_task_spawned there). Cleaning it up the instant
        control_blind_with_retry() itself returns, before this task has
        gotten far enough to check for a cancellation or send anything -
        it has already started by then, per Home Assistant's own eager
        task scheduling (see _retry_command_after_delay's own comment on
        this same point), just not yet past its own first
        asyncio.sleep() - would let a stop requested in that gap find
        nothing to snapshot and so never reach _move_cancel_requested at
        all - this task would then have no way to learn about it and
        would resend anyway.

        Needs no explicit cancellation of its own on disconnect, unlike
        _connect_task/_stop_pairing_task: control_blind() below already
        refuses to transmit once disconnected (see its own docstring) and
        returns False rather than raising, so a resend that wakes up after
        a disconnect simply no-ops on its next write: this task exits and
        cleans up on its own very next iteration regardless, exactly as it
        already did while this loop still ran inline, before this split.

        The cancellation check below stays meaningful across a disconnect
        too, not merely irrelevant: handle_connection_lost() and
        disconnect() snapshot this device's own in-flight tokens into
        _move_cancel_requested rather than clearing it blindly, so a
        cancellation already recorded - or one that disconnect itself now
        stands in for - is still seen here on the very next check, even
        though _moves_in_progress itself does get cleared across that
        same disconnect. Without that snapshot, a reconnect fast enough
        to land while this loop is still sleeping could have read a wiped
        dict as "not cancelled" and resent a stale command; not reachable
        today given RECONNECT_DELAY against this loop's own total
        lifetime above, but closed either way rather than left resting on
        that margin alone.
        """
        try:
            for _ in range(MOVE_COMMAND_EXTRA_RESENDS):
                await asyncio.sleep(MOVE_COMMAND_RESEND_DELAY_SECONDS)
                if move_call_token in self._move_cancel_requested.get(device_id, ()):
                    break
                await self.control_blind(
                    device_enum, action, device_id=device_id, source=source
                )
        finally:
            still_in_progress = self._moves_in_progress.get(device_id)
            if still_in_progress is not None:
                still_in_progress.discard(move_call_token)
                if not still_in_progress:
                    self._moves_in_progress.pop(device_id, None)
            still_cancelled = self._move_cancel_requested.get(device_id)
            if still_cancelled is not None:
                still_cancelled.discard(move_call_token)
                if not still_cancelled:
                    self._move_cancel_requested.pop(device_id, None)

    async def control_blind_stop(
        self,
        device_enum: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> StopOutcome:
        """Stop a blind, served ahead of any blind's own pending move.

        Returns a StopOutcome rather than a bare bool - see that class's
        own docstring for why CONFIRMED and UNCONFIRMED must never be
        collapsed into a single truthy result, the real bug this type
        exists to make structurally impossible to reintroduce.

        async_stop_cover() used to call control_blind() directly for this -
        one immediate, unlocked write, same as open/close used to be before
        control_blind_with_retry() existed for them. That is still exactly
        what happens here once STOP_RETRY_BUDGET runs out (see below), so
        this is never worse than the old behavior; it only adds latency in
        the case the old behavior always silently lost.

        That old, lock-free write could still physically collide with
        another write already in flight: another blind's stop sent in the
        same instant, or - despite async_stop_cover() calling cancel_
        pending_move() right before this - this same blind's own move,
        whose first write can still be transmitting when the stop for it
        arrives, since cancel_pending_move() only ends a *retry* that has
        not happened yet; it cannot un-send a write already on the wire.
        A second, independent lock would not close that gap either: two
        unrelated locks still let a stop-task and a move-task each wait on
        the same shared idle event and then race each other to write, the
        exact failure mode _move_lock's own comment in __init__ describes
        for two moves sharing only a wait instead of a lock.

        So this shares _move_lock itself, requesting priority (see
        _PriorityLock above) so it is served ahead of any *other* blind's
        own pending move still only waiting for its turn - critical,
        because this method acquires the lock *before* waiting for the
        transmitter to go idle, same as control_blind_with_retry() does
        (both hold it across the whole wait, not only the write - see that
        method's own docstring for why a plain, unlocked wait does not
        work and why has_priority_waiter() is still needed on top of
        that). Acquiring with priority means this never queues behind an
        ordinary move that is merely *waiting* for the lock, only ever
        behind whoever actually holds it right now - and that in turn is
        normally for nothing more than whatever is on the wire right now
        (this blind's own just-fired move, in the collision this fix
        exists for, or someone else's single transmit cycle), not an
        unrelated blind's whole queued-up move, since has_priority_
        waiter() makes an ordinary holder step back for this call rather
        than finish its own wait-then-write first. STOP_RETRY_BUDGET
        bounds the combined time spent acquiring the lock, waiting for
        idle, and any "tE" retries once writing - comfortably more than
        one full transmit cycle (~1.1-1.2s per real logs), but nowhere
        near a move's own, much longer MOVE_RETRY_OVERALL_TIMEOUT, so a
        manual stop can still never queue behind a move's full retry
        budget. Exhausted - the lock or the wire stayed busy throughout,
        or "tE" kept coming back - this falls back to the plain, unlocked
        control_blind() write described above, logged as a warning rather
        than the stop simply vanishing the way it used to before this
        whole fix existed.

        Once writing, reuses the same _move_awaiting_ack/_move_ack_event/
        _move_ack_result bookkeeping a move does - safe here specifically
        because holding _move_lock guarantees no move can be in its own
        write-and-await-ack phase at the same time.
        """
        requested_at = time.monotonic()
        deadline = requested_at + STOP_RETRY_BUDGET
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._move_lock.acquire(priority=True), timeout=remaining
                )
            except TimeoutError:
                break
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if not await self._wait_for_transmitter_idle(
                    "sending a stop command", timeout=remaining
                ):
                    continue
                self._move_ack_event.clear()
                self._move_ack_result = None
                self._move_awaiting_ack = True
                sent = await self.control_blind(
                    device_enum, CMD_STOP, device_id=device_id, source=source
                )
                if not sent:
                    self._move_awaiting_ack = False
                    return StopOutcome.FAILED
                # Snapshotted here, logged only below once this same write
                # is confirmed accepted - not every attempt that merely
                # transmitted and then got bounced "tE" (busy). A
                # busy-retry sequence can genuinely reach this line more
                # than once per call; only the figure for the attempt that
                # actually succeeds is reported, so this always logs
                # exactly once per call, for the real, final wait - not
                # once per attempt.
                transmit_wait = time.monotonic() - requested_at
                # Bounded by whatever is left of STOP_RETRY_BUDGET, not
                # unconditionally the full MOVE_ACK_TIMEOUT - lock
                # contention from another near-simultaneous blind command
                # (plus acquiring the lock and waiting for idle above) can
                # already have spent most of the budget by this point, and
                # waiting the full 1.0s regardless let this method overrun
                # its own documented budget by up to that same margin.
                # Never negative: asyncio.wait_for(timeout=0) still returns
                # immediately (TimeoutError if not already set) rather
                # than raising on its own.
                ack_wait_timeout = min(
                    MOVE_ACK_TIMEOUT, max(deadline - time.monotonic(), 0.0)
                )
                try:
                    await asyncio.wait_for(
                        self._move_ack_event.wait(), timeout=ack_wait_timeout
                    )
                except TimeoutError:
                    _LOGGER.warning(
                        "No t1/tE reply to stop write within %.1fs "
                        "source=%s device_id=%s - retrying",
                        ack_wait_timeout,
                        source,
                        device_id or "unknown",
                    )
                self._move_awaiting_ack = False
            finally:
                self._move_lock.release()
            if self._move_ack_result is True:
                _LOGGER.info(
                    "Stop command transmitting after waiting %.3fs for the "
                    "shared transmitter source=%s device_id=%s",
                    transmit_wait,
                    source,
                    device_id or "unknown",
                )
                # The stick has accepted this stop - as far as this
                # integration can ever observe, it is done (motor_result
                # stays unknown either way; see STOP_COMMAND_EXTRA_
                # RESENDS's own comment in const.py for why that one-hop
                # ack is not the same as knowing the motor caught it).
                # Fire the configured number of extra, best-effort frames
                # before returning - each a plain control_blind() write
                # with no lock and no ack-wait of its own, exactly the
                # unlocked write the give-up fallback below already uses,
                # just taken proactively here instead of only once giving
                # up. Done synchronously, inside this same call, so there
                # is no window for a new move on this same device_id to
                # start in between and race a resend fired later from the
                # background - the caller (async_stop_cover(), or the
                # auto-stop-at-target branch) awaits this whole method
                # before touching this device_id again. Position tracking
                # is unaffected: it is read only after this method
                # returns (see async_stop_cover()'s own docstring), so the
                # small, fixed delay below is folded into the estimate
                # exactly like any other stop latency already is, not
                # left unaccounted for.
                #
                # Unconditional - every configured resend fires regardless
                # of whether a real STOPPED status frame has already
                # arrived for this device_id. An earlier revision (2026-
                # 11-07 to 2026-11-08) skipped the remaining resends once
                # one had, on the reasoning that they would only be
                # reinforcing an already-confirmed stop; reverted at the
                # project owner's own explicit request, since relying on
                # that status frame for this decision was not reliable
                # enough in practice. This never consulted the frame for
                # anything else - StopOutcome.CONFIRMED here still means
                # only "the stick accepted the write", exactly as before
                # this revert.
                for _ in range(STOP_COMMAND_EXTRA_RESENDS):
                    await asyncio.sleep(STOP_COMMAND_RESEND_DELAY_SECONDS)
                    await self.control_blind(
                        device_enum, CMD_STOP, device_id=device_id, source=source
                    )
                return StopOutcome.CONFIRMED
            await asyncio.sleep(0.1)
        _LOGGER.warning(
            "Giving up on stop after %.1fs with no accept, falling back to "
            "a plain write source=%s device_id=%s",
            STOP_RETRY_BUDGET,
            source,
            device_id or "unknown",
        )
        # This write is exactly as uncertain as the one STOP_RETRY_BUDGET
        # just gave up waiting on - unlocked, unretried, no ack awaited -
        # so even a truthy "bytes went on the wire" result here can only
        # ever mean UNCONFIRMED, never CONFIRMED. See StopOutcome's own
        # docstring for why collapsing this into a plain bool was the bug.
        fallback_sent = await self.control_blind(
            device_enum, CMD_STOP, device_id=device_id, source=source
        )
        return StopOutcome.UNCONFIRMED if fallback_sent else StopOutcome.FAILED

    async def control_blind_exclusive(
        self,
        device_enum: str,
        action: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> bool:
        """Send one open/close/stop command, serialized against every
        other write sharing the hub-wide move-ack slot.

        For callers outside the regular cover-entity move/stop path -
        motor-direction tests, the test_command service - that still
        write straight to the wire via control_blind() and so share its
        single _move_awaiting_ack/_move_ack_event/_move_ack_result slot
        with control_blind_with_retry()/control_blind_stop(). Those two
        already hold _move_lock across their own whole write-then-await-
        ack window (see _move_lock's own comment in __init__); calling
        control_blind() directly, unlocked, let a test's write go out -
        and have the stick's reply misattributed - while a completely
        different blind's real move or stop was still waiting on that
        same slot: a genuine asyncio race (two coroutines actually
        overlapping at a real await), confirmed reachable through every
        one of these callers - see CHANGELOG.md's v1.3.0 entry on closing
        the hub-wide move-ack slot to every caller that used to bypass it.

        Takes the lock ordinary (non-priority), exactly like an ordinary
        move - a test command is not the safety-critical operation
        control_blind_stop()'s priority queueing exists for, so this
        queues fairly behind whatever is already using the wire rather
        than cutting ahead of it.

        Unlike control_blind_with_retry()/control_blind_stop(), this
        does not retry on a "busy" reply, or treat one as failure -
        control_blind() itself never gave any of these four callers ack
        feedback to begin with (see its own docstring), and adding retry
        semantics nobody asked for would risk exactly the kind of
        transmitter contention control_blind()'s "one write,
        immediately" design was built to avoid (see that docstring's own
        hardware-confirmed history). This only waits up to
        MOVE_ACK_TIMEOUT for *some* reply - accepted or busy, it does
        not matter which - before releasing the lock, so that reply is
        consumed here rather than risking arriving just late enough to
        be misattributed to whichever move or stop acquires the lock
        right after this one releases it.
        """
        async with self._move_lock:
            self._move_ack_event.clear()
            self._move_ack_result = None
            self._move_awaiting_ack = True
            sent = await self.control_blind(
                device_enum, action, device_id=device_id, source=source
            )
            if not sent:
                self._move_awaiting_ack = False
                return False
            try:
                await asyncio.wait_for(
                    self._move_ack_event.wait(), timeout=MOVE_ACK_TIMEOUT
                )
            except TimeoutError:
                _LOGGER.warning(
                    "No t1/tE reply to test write within %.1fs source=%s device_id=%s",
                    MOVE_ACK_TIMEOUT,
                    source,
                    device_id or "unknown",
                )
            self._move_awaiting_ack = False
            return True

    def initialize_next_device_enum(self) -> str:
        """Get the next available device enum based on registered devices.

        Returns the next available device enumerator as a hex string (e.g., "10").

        Finds the highest enum already known to be in use - by a
        registered device, or by any *earlier* call to this same method
        on this same running connection, even one whose pairing attempt
        was later aborted or simply never finished - and returns one
        higher than that.

        The "earlier call" half of that is deliberate, not an incidental
        side effect. _registered_devices only gains an entry once a
        pairing flow reaches _finish_pairing() (config_flow.py) and the
        cover platform registers the saved subentry (cover.py's
        async_setup_entry()/register_entity()) - but pair_device_and_wait()
        transmits the real teach command for whatever enum this method
        hands it well before that, and a physical motor can adopt it
        right then. There is no "unteach" in this protocol (see
        pair_device_and_wait()'s own comment) - only ever a fresh teach
        to some other enum - so once that command has gone out, nothing
        can recall it. Pairing a test blind, watching it move, and
        aborting instead of saving it - a reasonable way to sanity-check
        a blind before committing to it - used to leave this method with
        no memory that the enum it had just handed out was already
        taught to a real motor: the very next pairing attempt would
        compute and re-teach that identical "next" enum to whatever motor
        was next in programming mode, binding two different physical
        motors to the same address with neither this method nor anything
        else aware of the collision - every command to either one would
        then move both. Remembering every enum issued here, not just
        every one that made it into a saved subentry, closes that
        specific window for as long as this connection stays up. It
        cannot help across a hub reload/restart, since a fresh connection
        has no way to know what an earlier one taught and then abandoned
        - only a pairing that actually reaches _finish_pairing() is ever
        durable.
        """
        highest_in_use = PAIRING_DEVICE_ENUM_START - 1
        for device_enum in self._registered_devices.values():
            try:
                enum_value = int(device_enum, 16)
                highest_in_use = max(highest_in_use, enum_value)
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Invalid enum value for device: %s", err)
        if self._highest_issued_device_enum is not None:
            highest_in_use = max(highest_in_use, self._highest_issued_device_enum)

        # Next enum is 1 higher than the highest known-in-use value.
        next_enum = highest_in_use + 1
        if next_enum > 0xFF:
            next_enum = PAIRING_DEVICE_ENUM_START
            _LOGGER.warning(
                "Next enum exceeded 0xFF, wrapping back to %s",
                f"{PAIRING_DEVICE_ENUM_START:02X}",
            )

        self._highest_issued_device_enum = next_enum
        result = f"{next_enum:02X}"
        _LOGGER.debug(
            "Computed next device enum as %s (highest known in use: %s)",
            result,
            f"{highest_in_use:02X}",
        )
        return result

    def _register_status_identity(
        self,
        identity: StatusIdentity,
        *,
        entity_name: str,
        command_device_id: str,
        primary: bool,
    ) -> None:
        """Register one normalized primary or secondary status identity."""
        existing = self._registered_entity_keys.get(identity)
        if existing is not None and existing.entity_name != entity_name:
            _LOGGER.warning(
                "Status identity %s/%s reassigned from entity=%s to entity=%s",
                identity[0],
                identity[1],
                existing.entity_name,
                entity_name,
            )
        self._registered_entity_keys[identity] = _StatusIdentityRegistration(
            entity_name=entity_name,
            command_device_id=command_device_id.upper(),
            primary=primary,
        )
        if last_message := self._last_received_messages.get(identity):
            interpretation = str(last_message["interpreted_command"])
            last_message.update(
                {
                    "matched": True,
                    "identity_role": "primary" if primary else "secondary",
                    "position_tracking": primary and interpretation != "unknown",
                }
            )
            if bool(last_message["position_tracking"]):
                self._last_primary_tracking_messages[identity] = dict(last_message)

    def register_entity(
        self,
        status_device_id: str | None,
        status_enum: str | None,
        entity_name: str | None = None,
        *,
        command_device_id: str | None = None,
        command_enum: str | None = None,
        secondary_status_identities: object = None,
    ) -> None:
        """Register command, primary status, and diagnostic secondary identities."""
        command_id = command_device_id or status_device_id
        command_slot = command_enum or status_enum
        if command_id is None or command_slot is None:
            _LOGGER.error(
                "Cannot register cover without command identity entity=%s",
                entity_name or "unknown",
            )
            return
        display_name = entity_name or status_device_id or command_id
        self._registered_devices[command_id] = command_slot
        primary_identity = normalize_status_identity(status_device_id, status_enum)
        if primary_identity is not None:
            self._register_status_identity(
                primary_identity,
                entity_name=display_name,
                command_device_id=command_id,
                primary=True,
            )
        secondary_identities = normalize_status_identities(secondary_status_identities)
        for identity in secondary_identities:
            if identity != primary_identity:
                self._register_status_identity(
                    identity,
                    entity_name=display_name,
                    command_device_id=command_id,
                    primary=False,
                )
        _LOGGER.debug(
            "Registered entity=%s command=%s/%s primary_status=%s/%s "
            "secondary_statuses=%s",
            display_name,
            command_id,
            command_slot,
            status_device_id,
            status_enum,
            secondary_identities,
        )

    def set_fresh_calibration_position(self, blind_id: str, position: int) -> None:
        """Record that blind_id's "Pair and calibrate" run just ended at position.

        Called only when creating a brand-new subentry from a fresh
        pairing - never for a recalibration of an existing blind, which
        already updates its live entity directly via
        SIGNAL_CALIBRATION_COMPLETED and never consults this at all.
        """
        self._fresh_calibration_positions[blind_id] = position

    def pop_fresh_calibration_position(self, blind_id: str) -> int | None:
        """Return and clear blind_id's just-completed calibration position.

        Popping makes this strictly one-shot: it only ever wins on the
        entity's very first add after this same pairing, and a later
        restart (with nothing left to pop) falls straight back to the
        normal restored-state priority.
        """
        return self._fresh_calibration_positions.pop(blind_id, None)

    @callback
    def get_last_received_for_identities(
        self, identities: object
    ) -> dict[str, Any] | None:
        """Return the newest frame matching any supplied status identity."""
        candidates = [
            self._last_received_messages[identity]
            for identity in normalize_status_identities(identities)
            if identity in self._last_received_messages
        ]
        if not candidates:
            return None
        return dict(max(candidates, key=lambda message: int(message["sequence"])))

    @callback
    def get_last_primary_tracking_frame(
        self, status_device_id: str, status_enum: str
    ) -> dict[str, Any] | None:
        """Return the last recognized 00/01/02 frame on the primary identity."""
        identity = normalize_status_identity(status_device_id, status_enum)
        message = (
            self._last_primary_tracking_messages.get(identity) if identity else None
        )
        return dict(message) if message is not None else None

    @callback
    def get_last_secondary_frame(self, identities: object) -> dict[str, Any] | None:
        """Return the newest frame among configured secondary identities.

        A thin, more specifically-named wrapper around
        get_last_received_for_identities() - kept as its own method because
        callers (and the diagnostics dump's own section headings) treat
        "last matched frame overall" and "last secondary frame" as two
        distinct, meaningfully different views, even though the lookup
        itself is identical either way.
        """
        return self.get_last_received_for_identities(identities)

    @callback
    def record_position_update(
        self,
        command_device_id: str,
        *,
        source: str,
        direction: str,
        previous_position: int | None,
        new_position: int | None,
        status: str,
        position_source: str | None = None,
        confirmed_since_restart: bool | None = None,
    ) -> None:
        """Store the latest cover position calculation for diagnostics."""
        update = {
            "source": source,
            "direction": direction,
            "previous_position": previous_position,
            "new_position": new_position,
            "status": status,
            "time": dt_util.now().strftime("%H:%M:%S"),
            "position_source": position_source or source,
            "confirmed_since_restart": bool(confirmed_since_restart),
        }
        normalized_id = command_device_id.upper()
        self._last_position_updates[normalized_id] = update
        if status == "confirmed/manual":
            self._last_manual_position_syncs[normalized_id] = dict(update)

    @callback
    def get_last_position_update(self, command_device_id: str) -> dict[str, Any] | None:
        """Return the latest position calculation for a command identity."""
        update = self._last_position_updates.get(command_device_id.upper())
        return dict(update) if update is not None else None

    @callback
    def get_last_manual_position_sync(
        self, command_device_id: str
    ) -> dict[str, Any] | None:
        """Return the most recent manual position correction for a cover."""
        update = self._last_manual_position_syncs.get(command_device_id.upper())
        return dict(update) if update is not None else None

    @callback
    def manual_sync_position(self, command_device_id: str, position: int) -> bool:
        """Request an immediate manual position correction on a live cover."""
        if position < 0 or position > 100:
            raise ValueError("manual position must be between 0 and 100")
        normalized_id = command_device_id.upper()
        if not any(
            registered_id.upper() == normalized_id
            for registered_id in self._registered_devices
        ):
            _LOGGER.error(
                "Manual position sync blocked command_device_id=%s position=%d "
                "reason=cover_not_registered",
                normalized_id,
                position,
            )
            return False
        _LOGGER.warning(
            "Manual position sync requested command_device_id=%s position=%d",
            normalized_id,
            position,
        )
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_MANUAL_POSITION_SYNC}_{normalized_id}",
            position,
        )
        return True

    async def verify_device(self) -> bool:
        """Verify that the connected serial device is a Schellenberg stick."""
        if self._verify_future and not self._verify_future.done():
            _LOGGER.warning("Device verification already in progress")
            return False

        _LOGGER.debug("Verifying Schellenberg USB device")
        self._verify_future = self.hass.loop.create_future()
        try:
            if not await self.send_command(CMD_VERIFY):
                return False
            result = await asyncio.wait_for(self._verify_future, timeout=VERIFY_TIMEOUT)
        except TimeoutError:
            _LOGGER.error("Device verification timeout - device did not respond to !?")
            return False
        except ConnectionError as err:
            _LOGGER.error("Device verification interrupted: %s", err)
            return False
        else:
            _LOGGER.info("Device verification successful")
            return result
        finally:
            self._verify_future = None

    @callback
    def _update_status(self) -> None:
        """Update device status and notify listeners."""
        async_dispatcher_send(self.hass, SIGNAL_STICK_STATUS_UPDATED)

    @callback
    def handle_connection_lost(
        self, protocol: SchellenbergProtocol, exc: Exception | None
    ) -> None:
        """Clear dead serial state and schedule reconnection."""
        if protocol is not self._protocol:
            _LOGGER.debug("Ignoring connection_lost from a stale serial protocol")
            return

        if self._disconnect_requested:
            _LOGGER.info("Serial port closed intentionally")
        else:
            _LOGGER.warning("Serial port connection lost: %s", exc)

        self._transport = None
        self._protocol = None
        self._is_connected = False
        self._device_mode = None
        self._pairing_active = False
        self._pending_retry_command = None
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._transmit_busy = False
        self._busy_latched = False
        self._retry_count = 0
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = None
        # Every other piece of per-connection state above is already
        # cleared on a reconnect; this cache of the latest frame
        # per status identity previously was not, so a stale pre-reconnect
        # frame (and, before this cache was also given a cap of its own
        # above, an unbounded number of them) could linger in diagnostics
        # indefinitely.
        self._last_received_messages.clear()

        # Unstick a control_blind_with_retry() loop that may be waiting on
        # _move_ack_event right now: treating this as "busy" wakes it
        # immediately instead of leaving it to time out on its own (up to
        # MOVE_ACK_TIMEOUT). It loops back to control_blind(), which now
        # fails outright on the disconnected state above, so it returns
        # False on its very next iteration rather than retrying forever.
        if self._move_awaiting_ack:
            self._move_ack_result = False
            self._move_ack_event.set()
        # Snapshot every device's own currently in-flight tokens into
        # _move_cancel_requested before discarding _moves_in_progress,
        # rather than a blind .clear() of both - the same snapshot
        # cancel_pending_move() above already takes for one device,
        # applied here to every device at once, since a lost connection
        # cancels every move still in flight. A blind .clear() here
        # would silently revoke a cancellation already recorded moments
        # earlier, and would leave a call whose
        # _send_move_resends_and_cleanup() task is still sleeping
        # through its own detached resend loop - unlike a call still
        # waiting on _move_ack_event above, nothing wakes that task for
        # this - with no way to ever learn it should be treated as
        # cancelled: on an eventual reconnect it would then read a
        # wiped dict as "not cancelled" and resend a stale open/close
        # that should have stayed cancelled. Not reachable today - see
        # _send_move_resends_and_cleanup()'s own docstring for why that
        # task always already exited, on its own, before a reconnect
        # could complete - but kept closed anyway rather than left
        # standing on that timing margin alone. Each token snapshotted
        # here is still removed from _move_cancel_requested exactly as
        # always, by its own owning call's `finally` (see
        # resend_task_spawned there for which one that is), so nothing
        # added here outlives the call it was snapshotted for.
        for device_id, active in self._moves_in_progress.items():
            self._move_cancel_requested.setdefault(device_id, set()).update(active)
        self._moves_in_progress.clear()

        error = ConnectionError(f"serial connection lost: {exc}")
        for future in (
            self._verify_future,
            self._device_id_future,
            self._pairing_future,
        ):
            if future is not None and not future.done():
                future.set_exception(error)

        self._update_status()
        if not self._disconnect_requested:
            self._schedule_reconnect()

    def _transmit_capability_block_reason(self) -> str | None:
        """Return why RF transmission is currently unavailable, as a stable code.

        Returns a short, machine-stable code (e.g. "disconnected",
        "wrong_mode:initial") instead of a ready-made English sentence, so
        every caller can show a properly translated message via
        runtime_translation_text.translate_block_reason() rather than leaking
        hardcoded English into a non-English Home Assistant UI.
        """
        if not self._is_connected:
            return "disconnected"
        if self._transport is None:
            return "transport_unavailable"
        if self._transport.is_closing():
            return "transport_closing"
        if self._pairing_active:
            return "pairing_active"
        if self._device_mode != "listening":
            return f"wrong_mode:{self._device_mode or 'unknown'}"
        return None

    def _is_transmit_capable(self) -> bool:
        """Return whether normal RF commands may use the current stick mode."""
        return self._transmit_capability_block_reason() is None

    @property
    def is_connected(self) -> bool:
        """Return whether the USB stick is connected."""
        return self._is_connected

    @property
    def device_version(self) -> str | None:
        """Return the device firmware version."""
        return self._device_version

    @property
    def device_mode(self) -> str | None:
        """Return the device mode (bootloader, initial, listening, or pairing)."""
        return self._device_mode

    @property
    def hub_id(self) -> str | None:
        """Return the hub device ID."""
        return self._hub_id

    @property
    def pairing_active(self) -> bool:
        """Return whether a pairing workflow currently owns the stick."""
        return self._pairing_active

    @property
    def pairing_interrupted_by_disconnect(self) -> bool:
        """Return whether the last pair_device_and_wait() call was cut short.

        True only when disconnect() injected its ConnectionError into
        _pairing_future while still waiting for a device ID - covers an
        intentional disconnect for any reason (a hub-wide reload
        triggered by a *different* blind's own subentry add/edit, the
        integration unloading, a Core stop/restart, or an explicit stick
        reset), never a plain timeout, a refusal to start (already
        pairing, not transmit-capable), or the stick genuinely dropping
        its connection unexpectedly (handle_connection_lost() leaves
        _disconnect_requested False, so that case is deliberately left
        reporting the existing generic pairing_timeout message - a real
        connection loss isn't the scenario this property exists for).
        config_flow.py reads this immediately after pair_device_and_wait()
        returns None, to show a message naming the real cause instead of
        the generic "no device responded" one.
        """
        return self._pairing_interrupted_by_disconnect

    @property
    def busy_latched(self) -> bool:
        """Return whether transmission is stuck and needs a manual reset.

        Set when a command's busy (tE) retries are exhausted, or when a
        sent command's transmitter-idle (t0) ACK never arrives within the
        wait timeout. Self-heals on the next clean transmit cycle (a t0
        ACK with nothing ambiguous pending), as well as on connect/
        disconnect/reconnect, so a stick that recovered on its own is not
        left showing a stale stuck-state in diagnostics forever. A stick
        that is genuinely still stuck sets this again on its own next
        failed attempt - it is diagnostic-only and never blocks a new
        command from being attempted.
        """
        return self._busy_latched

    @property
    def transmitter_active(self) -> bool:
        """Return whether the stick last reported its transmitter as on."""
        return self._transmitter_active

    @property
    def move_lock_busy(self) -> bool:
        """Return whether some move or stop currently holds the shared lock.

        cover.py's own auto-stop-at-target check
        (_async_position_update_loop()) widens its latency compensation
        when this is True - see that call site's own comment. Real log
        evidence (2026-10-31): two blinds closing together, one stopping
        right after the other - the first blind's stop reached the wire
        0.39s after the software decided to stop it; the second blind's
        own stop, queued behind the first's whole wait-for-idle-then-
        write-then-ack-wait cycle on this same lock, took 1.37s - about
        one more full STOP_COMMAND_LATENCY_SECONDS-sized cycle than the
        uncontended case, matching the second blind's own measured
        overshoot almost exactly. control_blind_stop() already requests
        priority=True (see _PriorityLock above), so a stop like the
        second blind's never queues behind some *other*, unrelated
        blind's move merely *waiting* for this lock - only ever behind
        whoever currently *holds* it, which is exactly what this
        property reflects.
        """
        return self._move_lock.locked()

    @property
    def transmit_block_reason(self) -> str | None:
        """Return why a new diagnostic command cannot be sent, as a stable code.

        Returns a short, machine-stable code instead of a ready-made
        English sentence - see _transmit_capability_block_reason(). Every
        caller shows a translated message via
        runtime_translation_text.translate_block_reason(hass, reason).
        """
        if reason := self._transmit_capability_block_reason():
            return reason
        if self._busy_latched:
            return "busy_latched"
        if self._pending_retry_command is not None:
            return f"pending_retry:{self._pending_retry_command}"
        return None

    @property
    def transmit_ready(self) -> bool:
        """Return whether a new Developer Tools command can be sent immediately."""
        return self.transmit_block_reason is None

    # LED Control Methods
    async def led_on(self) -> bool:
        """Turn the USB stick LED on."""
        _LOGGER.debug("Turning LED on")
        return await self.send_command(CMD_LED_ON)

    async def led_off(self) -> bool:
        """Turn the USB stick LED off."""
        _LOGGER.debug("Turning LED off")
        return await self.send_command(CMD_LED_OFF)

    # USB Stick System Commands
    async def get_device_id(self) -> str | None:
        """Get the USB stick's unique device ID."""
        if self._device_id_future and not self._device_id_future.done():
            _LOGGER.warning("Device ID request already in progress")
            return None

        _LOGGER.debug("Requesting device ID")
        self._device_id_future = self.hass.loop.create_future()
        try:
            if not await self.send_command(CMD_GET_DEVICE_ID):
                return None
            device_id = await asyncio.wait_for(self._device_id_future, timeout=5)
        except TimeoutError:
            _LOGGER.error("Device ID request timeout - device did not respond")
            return None
        except ConnectionError as err:
            _LOGGER.error("Device ID request interrupted: %s", err)
            return None
        else:
            _LOGGER.info("Device ID retrieved successfully: %s", device_id)
            return device_id
        finally:
            self._device_id_future = None

    async def disconnect(self) -> None:
        """Disconnect intentionally and cancel all pending serial work."""
        self._disconnect_requested = True
        self._cancel_scheduled_reconnect()
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        self._connect_task = None

        self._pending_retry_command = None
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._transmit_busy = False
        self._busy_latched = False
        self._retry_count = 0
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = None
        # Same reasoning as handle_connection_lost()'s own identical line.
        self._last_received_messages.clear()
        # Same reasoning as handle_connection_lost(): wake a
        # control_blind_with_retry() loop that may be waiting right now
        # instead of leaving it to its own timeout.
        if self._move_awaiting_ack:
            self._move_ack_result = False
            self._move_ack_event.set()
        # Same reasoning as handle_connection_lost()'s own identical
        # block: snapshot every device's in-flight tokens into
        # _move_cancel_requested before discarding _moves_in_progress,
        # instead of a blind .clear() of both.
        for device_id, active in self._moves_in_progress.items():
            self._move_cancel_requested.setdefault(device_id, set()).update(active)
        self._moves_in_progress.clear()
        stop_task = self._stop_pairing_task
        self._stop_pairing_task = None
        if (
            stop_task is not None
            and stop_task is not asyncio.current_task()
            and not stop_task.done()
        ):
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

        error = ConnectionError("serial connection closed")
        for future in (
            self._verify_future,
            self._device_id_future,
            self._pairing_future,
        ):
            if future is not None and not future.done():
                future.set_exception(error)

        transport = self._transport
        self._transport = None
        self._protocol = None
        self._is_connected = False
        self._device_mode = None
        self._pairing_active = False
        if transport is not None:
            transport.close()
        self._update_status()
        _LOGGER.info("Disconnected from Schellenberg USB stick")

    async def reset_and_reconnect(self) -> bool:
        """Close and reopen the serial port, restoring listening mode."""
        _LOGGER.warning(
            "Resetting Schellenberg stick serial state connected=%s mode=%s pairing=%s",
            self._is_connected,
            self._device_mode,
            self._pairing_active,
        )
        await self.disconnect()
        await asyncio.sleep(RESET_SETTLE_DELAY)
        connected = await self.connect()
        ready = connected and self.transmit_ready
        _LOGGER.log(
            logging.INFO if ready else logging.ERROR,
            "Stick reset/reconnect completed connected=%s mode=%s ready=%s",
            self._is_connected,
            self._device_mode,
            ready,
        )
        return ready


class SchellenbergProtocol(asyncio.Protocol):
    """Serial protocol for reading newline-terminated messages."""

    def __init__(
        self, message_callback: Callable[[str], None], api: SchellenbergUsbApi
    ) -> None:
        """Initialize the protocol."""
        self.message_callback = message_callback
        self.api = api
        self.buffer = ""
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Called when a connection is made."""
        self.transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        """Called with new data from the serial port."""
        _LOGGER.debug("Received from serial device: %s", data)
        self.buffer += data.decode("ascii", errors="ignore")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                _LOGGER.debug("Parsed message from serial device: %s", line.strip())
                self.message_callback(line.strip())
        # See MAX_SERIAL_BUFFER_LENGTH's own comment: only genuinely runaway
        # growth - nothing resembling a real line terminator for 4096+ bytes
        # - trips this, never an ordinary partial line still waiting on the
        # rest of its own, much shorter, message.
        if len(self.buffer) > MAX_SERIAL_BUFFER_LENGTH:
            _LOGGER.warning(
                "Serial receive buffer exceeded %d bytes with no line "
                "terminator seen; discarding it and starting fresh - the "
                "connection may be stuck or receiving garbage",
                MAX_SERIAL_BUFFER_LENGTH,
            )
            self.buffer = ""

    def connection_lost(self, exc: Exception | None) -> None:
        """Called when the connection is lost."""
        self.api.handle_connection_lost(self, exc)
