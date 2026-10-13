"""API for Schellenberg USB Stick."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
    PAIRING_DEVICE_ENUM_START,
    PAIRING_TIMEOUT,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_EVENT_CAPTURE,
    SIGNAL_MANUAL_POSITION_SYNC,
    SIGNAL_STICK_STATUS_UPDATED,
    STATUS_DISCOVERY_TIMEOUT,
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

# A "stick busy" (tE) response is retried after a short delay, up to this many
# times in a row, before the command is abandoned and busy_latched is set so the
# integration surfaces a stuck stick instead of retrying forever. The count
# starts over whenever the stick accepts a transmission (t1).
TRANSMIT_MAX_RETRIES = 3
# How long to wait for a transmitter-idle (t0) ACK after a write before
# giving up on that particular wait. Multi-step sequences (teach, raw
# transmit, pairing) use this between steps so they never send the next
# payload while the previous one is still in flight on the shared serial
# line.
TRANSMIT_IDLE_TIMEOUT = 5.0


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
        self._pairing_future: asyncio.Future[str] | None = None
        self._pairing_active = False
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

        # Minimal retry state for commands that fail with "stick busy"
        self._pending_retry_command: str | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_count = 0
        self._last_transmit_source = "internal"

        # control_blind() (open/close/stop) deliberately writes straight to
        # the transport with no lock and no retry - see its own docstring -
        # so it never touches _pending_retry_command either. That leaves one
        # real ambiguity: the stick's tE/t1/t0 replies carry no id saying
        # which write they are about, so if a control_blind() write lands
        # while a send_command()-tracked transmission (teach, pairing,
        # raw-transmit, verify) is still waiting on its own t0, a tE that
        # arrives next could be about either one - not necessarily the
        # pending command _handle_message would otherwise assume it is.
        # Blindly retrying the pending command in that situation risks
        # resending it for no reason; for teach specifically, its own
        # docstring already warns that repeating command 60 outside the
        # motor programming sequence can change the motor's rotation
        # direction. This flag lets _handle_message tell the ambiguous case
        # apart from the ordinary one instead of guessing: control_blind()
        # sets it when it writes while a command is pending, send_command()
        # clears it when a fresh pending command's own window opens, and
        # _handle_message clears it again after acting on one ambiguous tE,
        # so only that one reply is affected - a later, unambiguous tE for
        # the same still-pending command is retried normally.
        self._control_blind_write_pending_conflict = False

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
        except (OSError, TimeoutError, serialx.SerialException) as err:
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
            self.hass.async_create_task(self.connect(), name="schellenberg-reconnect")

    @callback
    def _handle_message(self, message: str) -> None:
        """Parse and dispatch one line of text received from the stick.

        This is the protocol's single entry point for everything the
        stick sends back, so it branches purely on the message's
        prefix/shape: a `RFTU_` device-verification reply (records
        firmware version and boot mode, resolves `_verify_future`);
        `t1`/`t0` transmitter start/complete ACKs (drive the retry
        latch and `_transmitter_idle`); a `tE` busy reply (schedules a
        retry, or gives up and sets `_busy_latched` once
        `TRANSMIT_MAX_RETRIES` is exceeded); a `sr` device-ID-query
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
                # The stick accepted a transmission, so it is not stuck: the
                # retry limit counts busy answers in a row, not per connection.
                self._retry_count = 0
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
            if not self._pending_retry_command:
                # Nothing retryable is in flight: a busy reply to a
                # non-transmit request, or one that arrived after the pending
                # command was already cleared by its own t0.
                _LOGGER.log(
                    level, "Transmit error - stick busy, no pending command to retry"
                )
                return
            if self._control_blind_write_pending_conflict:
                # A control_blind() write went out while this command was
                # still waiting on its own t0 (see
                # _control_blind_write_pending_conflict's comment in
                # __init__), so this busy reply could be about either
                # transmission - the protocol gives no way to tell which.
                # Retrying here risks resending the pending command for no
                # reason (harmful for teach specifically, whose own
                # docstring warns that repeating it can change the motor's
                # rotation direction), so this one ambiguous reply is
                # skipped instead of guessed at. Only this reply is
                # affected: the flag is cleared here, so a later,
                # unambiguous tE for the same still-pending command is
                # retried normally.
                self._control_blind_write_pending_conflict = False
                _LOGGER.log(
                    level,
                    "Transmit error - stick busy, but a blind command was also "
                    "sent while source=%s payload=%s was still pending; "
                    "ambiguous which one this is about, not retrying",
                    self._last_transmit_source,
                    self._pending_retry_command,
                )
                return
            _LOGGER.log(level, "Transmit error - stick busy, will retry in 100ms")
            if self._retry_task is not None and not self._retry_task.done():
                # A retry is already scheduled for the current pending
                # command. A duplicate busy signal must not cancel and
                # reschedule it, or a burst of tE responses could postpone
                # the retry forever.
                return
            if self._retry_count >= TRANSMIT_MAX_RETRIES:
                _LOGGER.error(
                    "Transmit retry limit reached source=%s payload=%s "
                    "attempts=%d; abandoning and latching busy state",
                    self._last_transmit_source,
                    self._pending_retry_command,
                    self._retry_count,
                )
                self._pending_retry_command = None
                self._busy_latched = True
                self._retry_count = 0
                return
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
                self._last_received_messages[identity] = frame
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
                    self._status_discovery_frames.append(captured_frame)
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
                    # Fresh window for this pending command: any conflict
                    # flagged by control_blind() belonged to a previous
                    # pending command, if any, and does not apply here.
                    self._control_blind_write_pending_conflict = False

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
        direction command via control_blind() and, after a brief hold,
        follows it with a stop - the same "don't start the next RF write
        before the previous one's t0" problem _wait_for_transmitter_idle()
        already solves for teach/pairing/raw-transmit below. control_blind()
        itself never awaits this (it is deliberately lock-free and
        immediate - see its own docstring), so the caller is responsible
        for waiting between its own two writes. Thin wrapper only: all the
        actual waiting logic lives in _wait_for_transmitter_idle().
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
        """Start one bounded receive-frame capture for pairing or calibration."""
        if self._status_discovery_frames is not None:
            raise RuntimeError("status frame capture is already active")
        self._status_discovery_frames = []
        self._status_discovery_phase = phase
        self._status_discovery_started_at = dt_util.now().isoformat()

    @callback
    def set_status_frame_capture_phase(self, phase: str) -> None:
        """Label subsequently received frames with the current workflow phase."""
        if self._status_discovery_frames is not None:
            self._status_discovery_phase = phase

    @callback
    def finish_status_frame_capture(self, *, end_reason: str) -> dict[str, Any]:
        """Stop capture and return grouped candidates plus raw phase-labelled frames."""
        frames = list(self._status_discovery_frames or [])
        started_at = self._status_discovery_started_at
        self._status_discovery_frames = None
        self._status_discovery_phase = "idle"
        self._status_discovery_started_at = None
        result = summarize_status_discovery_frames(frames)
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
        _LOGGER.warning(
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
        _LOGGER.warning(
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
        paired = False

        try:
            _LOGGER.debug("Entering pairing mode with command: sp")
            if not await self.send_command(CMD_GET_PARAM_P):
                raise ConnectionError("could not enter pairing mode")

            device_id = await asyncio.wait_for(
                self._pairing_future, timeout=PAIRING_TIMEOUT
            )
            _LOGGER.debug(
                "Received device ID %s, sending pairing teach_payload=%s "
                "then finish_payload=%s",
                device_id,
                pair_command,
                finish_pair_command,
            )

            # Both pairing entry points (pair_device and
            # add_shutter_from_remote) share this one sequence: the 0x60
            # teach command followed by the 0x40 finish/allow-pairing
            # command.
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
            paired = True
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
            # Await pairing shutdown. A delayed background `sp` used to race the
            # first test command and could leave the stick busy/programming.
            stop_task = self.hass.async_create_task(
                self._stop_pairing_mode(delay=paired),
                name="schellenberg-stop-pairing",
            )
            self._stop_pairing_task = stop_task
            try:
                await stop_task
            finally:
                if self._stop_pairing_task is stop_task:
                    self._stop_pairing_task = None

    async def _stop_pairing_mode(self, delay: bool = False) -> None:
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
        the pairing config flow step (for both "pair_device" and
        "add_shutter_from_remote", which share this same cleanup) would
        abort with the same "pairing_timeout" message again immediately
        on the very next attempt, before any new wait for a device even
        began. Trusting the write alone, the same way "hello" already
        does, fixes that and also drops the now-pointless wait entirely:
        the worst case if the stick genuinely is still finishing up is
        one more pairing attempt that does not find a device in time,
        not a permanent lockout.
        """
        sent = False
        try:
            if delay:
                await asyncio.sleep(2)
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
        sent = await self.send_command(normalized, source=source)
        if not sent:
            _LOGGER.error(
                "Raw RF transmit result source=%s payload=%s result=write_failed "
                "stick_ack_only=True motor_result=unknown",
                source,
                normalized,
            )
            return False

        if not await self._wait_for_transmitter_idle("finishing raw RF transmit"):
            _LOGGER.error(
                "Raw RF transmit result source=%s payload=%s result=ack_timeout "
                "stick_ack_only=True motor_result=unknown",
                source,
                normalized,
            )
            self._busy_latched = True
            return False

        _LOGGER.log(
            logging.WARNING if source in DIAGNOSTIC_TRANSMIT_SOURCES else logging.DEBUG,
            "Raw RF transmit result source=%s payload=%s result=written "
            "stick_ack_only=True motor_result=unknown",
            source,
            normalized,
        )
        return True

    async def control_blind(
        self,
        device_enum: str,
        action: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> bool:
        """Send an open/close/stop command through the normal command path.

        The frame is handed to send_command() rather than written to the
        transport here, so it is serialised behind _transmit_lock and
        registered as the pending transmission. Both matter on real
        hardware: the stick has a single RF transmitter, so two moves issued
        in the same second - e.g. two covers closing together - cannot both
        be on the air at once, and the second one is answered with tE
        ("stick busy") instead. Only a registered pending command makes that
        reply actionable; without one the move was dropped silently while
        the cover still reported as moving. See _handle_message's tE branch,
        which resends the pending payload once the stick reports its
        transmitter idle again.

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
        # Delegating rather than writing here is what makes the stick's tE
        # ("busy") reply actionable: send_command() holds _transmit_lock
        # across the write and records this payload as the pending
        # transmission, which _handle_message's tE branch then resends once
        # the stick is free again.
        if not await self.send_command(raw_payload, source=source):
            _LOGGER.error(
                "Blind transmit write result source=%s command=%s payload=%s "
                "result=failed",
                source,
                action_name,
                raw_payload,
            )
            return False
        return True

    def initialize_next_device_enum(self) -> str:
        """Get the next available device enum based on registered devices.

        Returns the next available device enumerator as a hex string (e.g., "10").

        This is a stateless method that computes the next available enum
        by finding the highest enum in registered devices and returning one higher.
        """
        if not self._registered_devices:
            _LOGGER.debug(
                "No registered devices found, starting enum at %s",
                f"{PAIRING_DEVICE_ENUM_START:02X}",
            )
            return f"{PAIRING_DEVICE_ENUM_START:02X}"

        # Find the highest enum value from registered devices
        max_enum = PAIRING_DEVICE_ENUM_START - 1
        for device_enum in self._registered_devices.values():
            try:
                enum_value = int(device_enum, 16)
                max_enum = max(max_enum, enum_value)
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Invalid enum value for device: %s", err)

        # Next enum is 1 higher than the highest
        next_enum = max_enum + 1
        if next_enum > 0xFF:
            next_enum = PAIRING_DEVICE_ENUM_START
            _LOGGER.warning(
                "Next enum exceeded 0xFF, wrapping back to %s",
                f"{PAIRING_DEVICE_ENUM_START:02X}",
            )

        result = f"{next_enum:02X}"
        _LOGGER.debug(
            "Computed next device enum as %s (highest existing: %s)",
            result,
            f"{max_enum:02X}",
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

    def register_existing_devices(self, devices: list[dict[str, Any]]) -> None:
        """Bulk-register command devices restored from storage at startup.

        Each entry needs only "id" and "enum" (a "name" is accepted for
        readability in the caller but not required here). Use
        register_entity() instead when a status identity, entity name, or
        secondary identities also need registering.
        """
        for device in devices:
            self._registered_devices[device["id"]] = device["enum"]
        _LOGGER.debug(
            "Registered %d existing device(s): %s",
            len(devices),
            ", ".join(str(device.get("id")) for device in devices),
        )

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
    def get_last_received(
        self, status_device_id: str, status_enum: str
    ) -> dict[str, Any] | None:
        """Return a copy of the latest frame for an exact status identity."""
        identity = normalize_status_identity(status_device_id, status_enum)
        message = self._last_received_messages.get(identity) if identity else None
        return dict(message) if message is not None else None

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
        """Return the newest frame among configured secondary identities."""
        candidates = [
            self._last_received_messages[identity]
            for identity in normalize_status_identities(identities)
            if identity in self._last_received_messages
        ]
        if not candidates:
            return None
        return dict(max(candidates, key=lambda message: int(message["sequence"])))

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
        self._control_blind_write_pending_conflict = False
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._transmit_busy = False
        self._busy_latched = False
        self._retry_count = 0
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = None

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

        self._pending_retry_command = None
        self._control_blind_write_pending_conflict = False
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._transmit_busy = False
        self._busy_latched = False
        self._retry_count = 0
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = None
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

    def connection_lost(self, exc: Exception | None) -> None:
        """Called when the connection is lost."""
        self.api.handle_connection_lost(self, exc)
