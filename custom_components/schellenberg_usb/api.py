"""API for Schellenberg USB Stick."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import serial
import serial_asyncio_fast
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CMD_ALLOW_PAIRING,
    CMD_DOWN,
    CMD_ECHO_OFF,
    CMD_ECHO_ON,
    CMD_ENTER_BOOTLOADER,
    CMD_ENTER_INITIAL,
    CMD_GET_DEVICE_ID,
    CMD_GET_PARAM_P,
    CMD_LED_BLINK_1,
    CMD_LED_BLINK_2,
    CMD_LED_BLINK_3,
    CMD_LED_BLINK_4,
    CMD_LED_BLINK_5,
    CMD_LED_BLINK_6,
    CMD_LED_BLINK_7,
    CMD_LED_BLINK_8,
    CMD_LED_BLINK_9,
    CMD_LED_OFF,
    CMD_LED_ON,
    CMD_MANUAL_DOWN,
    CMD_MANUAL_UP,
    CMD_PAIR,
    CMD_REBOOT,
    CMD_SET_LOWER_ENDPOINT,
    CMD_SET_UPPER_ENDPOINT,
    CMD_STOP,
    CMD_TRANSMIT,
    CMD_UP,
    CMD_VERIFY,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    PAIRING_DEVICE_ENUM_START,
    PAIRING_TIMEOUT,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_STICK_STATUS_UPDATED,
    VERIFY_TIMEOUT,
)
from .identities import (
    StatusIdentity,
    normalize_status_identities,
    normalize_status_identity,
)

_LOGGER = logging.getLogger(__name__)

TRANSMIT_RETRY_DELAY = 0.05
TRANSMIT_MAX_RETRIES = 3
TRANSMIT_IDLE_TIMEOUT = 3.0
RECONNECT_DELAY = 5.0
RESET_SETTLE_DELAY = 0.25
DIAGNOSTIC_TRANSMIT_SOURCES = frozenset({"developer_tools", "service"})


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
        """Initialize the Schellenberg USB API."""
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
        self._last_received_sequence = 0
        self._is_connecting = False
        self._pairing_future: asyncio.Future[str] | None = None
        self._pairing_active = False
        self._stop_pairing_task: asyncio.Task[None] | None = (
            None  # Track task to stop pairing
        )
        self._disconnect_requested = False
        self._reconnect_handle: asyncio.TimerHandle | None = None

        # USB stick status
        self._is_connected = False
        self._device_version: str | None = None
        self._device_mode: str | None = None  # boot, initial, or listening
        self._verify_future: asyncio.Future[bool] | None = None
        self._device_id_future: asyncio.Future[str] | None = None
        self._hub_id: str | None = None

        # Retry queue for commands that failed with "stick busy"
        self._pending_retry_command: str | None = None
        self._pending_transmit_source: str | None = None
        self._transmit_retry_count = 0
        self._retry_task: asyncio.Task[None] | None = None
        self._transmit_lock = asyncio.Lock()
        self._transmit_busy = False

        self._transmitter_active = False
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
            transport, protocol = await serial_asyncio_fast.create_serial_connection(
                self.hass.loop,
                lambda: SchellenbergProtocol(self._handle_message, self),
                self.port,
                baudrate=112500,
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
            self._busy_latched = False
            self._transmitter_active = False
            self._transmitter_idle.set()
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
        except (serial.SerialException, OSError) as err:
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
            self._reconnect_handle.cancel()
            self._reconnect_handle = None

    def _schedule_reconnect(self, delay: float = RECONNECT_DELAY) -> None:
        """Schedule one reconnect unless shutdown was explicitly requested."""
        if self._disconnect_requested or self._reconnect_handle is not None:
            return
        _LOGGER.info("Scheduling serial reconnect in %.1f seconds", delay)
        self._reconnect_handle = self.hass.loop.call_later(
            delay, self._start_scheduled_reconnect
        )

    @callback
    def _start_scheduled_reconnect(self) -> None:
        """Start the reconnect task from its timer callback."""
        self._reconnect_handle = None
        if not self._disconnect_requested:
            self.hass.loop.create_task(self.connect())

    @callback
    def _handle_message(self, message: str) -> None:
        """Handle incoming messages from the protocol."""
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

        # t1 means the RF transmitter started; it is not idle until t0.
        if message == "t1":
            self._transmitter_active = True
            self._transmitter_idle.clear()
            self._busy_latched = False
            source = self._pending_transmit_source or "internal"
            level = (
                logging.WARNING
                if source in DIAGNOSTIC_TRANSMIT_SOURCES
                else logging.INFO
            )
            _LOGGER.log(
                level,
                "Serial transmit ACK start response=t1 source=%s mode=%s "
                "payload=%s retries=%d stick_ack_only=True motor_result=unknown",
                source,
                self._device_mode,
                self._pending_retry_command,
                self._transmit_retry_count,
            )
            return

        if message == "t0":
            self._transmitter_active = False
            self._transmitter_idle.set()
            self._busy_latched = False
            source = self._pending_transmit_source or "internal"
            level = (
                logging.WARNING
                if source in DIAGNOSTIC_TRANSMIT_SOURCES
                else logging.INFO
            )
            if self._retry_task is not None and not self._retry_task.done():
                _LOGGER.log(
                    level,
                    "Serial transmitter idle response=t0 source=%s; queued retry "
                    "may proceed payload=%s",
                    source,
                    self._pending_retry_command,
                )
            else:
                _LOGGER.log(
                    level,
                    "Serial transmit ACK complete response=t0 source=%s mode=%s "
                    "payload=%s retries=%d result=completed stick_ack_only=True "
                    "motor_result=unknown",
                    source,
                    self._device_mode,
                    self._pending_retry_command,
                    self._transmit_retry_count,
                )
                self._clear_pending_transmit(cancel_retry=False)
            return

        if message == "tE":
            command = self._pending_retry_command
            source = self._pending_transmit_source or "internal"
            self._transmitter_active = True
            self._transmitter_idle.clear()
            if command is None:
                _LOGGER.warning(
                    "Serial stick reported busy with no pending transmit payload "
                    "source=%s",
                    source,
                )
                return

            # A burst of duplicate tE responses must not continually cancel and
            # postpone the retry that is already waiting to run.
            if self._retry_task is not None and not self._retry_task.done():
                _LOGGER.debug(
                    "Serial stick still busy; retry already scheduled source=%s "
                    "payload=%s retry=%d/%d",
                    source,
                    command,
                    self._transmit_retry_count,
                    TRANSMIT_MAX_RETRIES,
                )
                return

            if self._transmit_retry_count >= TRANSMIT_MAX_RETRIES:
                self._busy_latched = True
                _LOGGER.error(
                    "Serial transmit abandoned after %d attempts because the stick "
                    "remained busy source=%s mode=%s payload=%s; "
                    "reset/reconnect required",
                    TRANSMIT_MAX_RETRIES + 1,
                    source,
                    self._device_mode,
                    command,
                )
                self._clear_pending_transmit(cancel_retry=False)
                return

            self._transmit_retry_count += 1
            _LOGGER.warning(
                "Serial stick busy; retry %d/%d will wait up to %.1fs for idle "
                "source=%s mode=%s payload=%s",
                self._transmit_retry_count,
                TRANSMIT_MAX_RETRIES,
                TRANSMIT_IDLE_TIMEOUT,
                source,
                self._device_mode,
                command,
            )
            self._retry_task = asyncio.create_task(
                self._retry_command_after_delay(command, source)
            )
            return
        # Handle device ID response (format: sr5D3E7C where 5D3E7C is the device ID)
        if message.startswith("sr") and len(message) >= 8:
            device_id = message[2:8]
            _LOGGER.debug("Received device ID response: %s", device_id)
            if self._device_id_future and not self._device_id_future.done():
                self._device_id_future.set_result(device_id)
            return

        # Handle pairing/list responses (format: sl00BEXXXXXX...)
        # sl = list/pairing response prefix
        # 00BE = 2 bytes to ignore (address prefix)
        # XXXXXX = 3 bytes device ID (the actual device ID we want)
        # Rest = can be ignored
        if message.startswith("sl") and len(message) >= 8:
            # Extract the device ID: skip "sl" (2 chars) + "00BE" (4 chars) = 6 chars
            # Then take the next 6 characters (3 bytes as hex) = 6 chars
            device_id = message[6:12]
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
                    if (
                        device_id not in self._registered_devices
                        and not known_status_id
                    ):
                        _LOGGER.info(
                            "Pairing candidate detected device_id=%s", device_id
                        )
                        self._pairing_future.set_result(device_id)
                        # Don't send dispatcher signal here - let the caller handle persistence
                        return

                if registration is None:
                    level = (
                        logging.DEBUG
                        if self._registered_entity_keys
                        else logging.WARNING
                    )
                    _LOGGER.log(
                        level,
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
            except (IndexError, ValueError) as err:
                _LOGGER.debug("Failed to parse message %s: %s", message, err)

    async def send_command(self, command: str, *, source: str = "internal") -> bool:
        """Queue a raw command on the serial transport."""
        return await self._write_command(command, is_retry=False, source=source)

    async def _write_command(
        self, command: str, *, is_retry: bool, source: str
    ) -> bool:
        """Write one command while serializing access to the transport."""
        async with self._transmit_lock:
            self._transmit_busy = True
            try:
                if is_retry and self._pending_retry_command != command:
                    _LOGGER.debug(
                        "Skipping stale serial retry source=%s payload=%s pending=%s",
                        source,
                        command,
                        self._pending_retry_command,
                    )
                    return False

                if self._transport is None or self._transport.is_closing():
                    _LOGGER.error(
                        "Serial write blocked reason=transport_unavailable source=%s "
                        "payload=%s connected=%s mode=%s",
                        source,
                        command,
                        self._is_connected,
                        self._device_mode,
                    )
                    if is_retry or command.startswith(CMD_TRANSMIT):
                        self._clear_pending_transmit(cancel_retry=False)
                    return False

                is_transmit = command.startswith(CMD_TRANSMIT)
                diagnostic = is_transmit and source in DIAGNOSTIC_TRANSMIT_SOURCES
                visible_level = logging.WARNING if diagnostic else logging.DEBUG
                if is_transmit:
                    if self._busy_latched:
                        _LOGGER.error(
                            "Serial transmit blocked reason=busy_latched source=%s "
                            "mode=%s payload=%s; reset/reconnect required",
                            source,
                            self._device_mode,
                            command,
                        )
                        return False

                    if not is_retry and not self._transmitter_idle.is_set():
                        _LOGGER.log(
                            logging.WARNING if diagnostic else logging.INFO,
                            "Waiting for active serial transmit to finish source=%s "
                            "mode=%s pending_payload=%s new_payload=%s",
                            source,
                            self._device_mode,
                            self._pending_retry_command,
                            command,
                        )
                        if not await self._wait_for_transmitter_idle(
                            "before a new transmit"
                        ):
                            self._busy_latched = True
                            return False

                    if not is_retry:
                        if self._pending_retry_command is not None:
                            _LOGGER.warning(
                                "Replacing completed serial transmit old_payload=%s "
                                "new_payload=%s source=%s",
                                self._pending_retry_command,
                                command,
                                source,
                            )
                        self._clear_pending_transmit()
                        self._pending_retry_command = command
                        self._pending_transmit_source = source
                        self._transmit_retry_count = 0

                    # Close the idle window before writing. This prevents another
                    # coroutine from queuing a command before the stick reports t1.
                    self._transmitter_active = True
                    self._transmitter_idle.clear()

                full_command = f"{command}\r\n".encode("ascii")
                attempt = self._transmit_retry_count + 1 if is_retry else 1
                max_attempts = TRANSMIT_MAX_RETRIES + 1
                _LOGGER.log(
                    visible_level,
                    "Serial write attempt source=%s mode=%s connected=%s pairing=%s "
                    "transmitter_active=%s payload=%s bytes=%d attempt=%d/%d retry=%s",
                    source,
                    self._device_mode,
                    self._is_connected,
                    self._pairing_active,
                    self._transmitter_active,
                    command,
                    len(full_command),
                    attempt,
                    max_attempts if is_transmit else 1,
                    is_retry,
                )
                try:
                    self._transport.write(full_command)
                except (OSError, RuntimeError):
                    _LOGGER.exception(
                        "Serial write failed source=%s payload=%s attempt=%d retry=%s",
                        source,
                        command,
                        attempt,
                        is_retry,
                    )
                    if self._pending_retry_command == command:
                        self._clear_pending_transmit(cancel_retry=False)
                    if is_transmit:
                        self._transmitter_active = False
                        self._transmitter_idle.set()
                    return False

                _LOGGER.log(
                    visible_level,
                    "Serial write succeeded source=%s payload=%s attempt=%d "
                    "retry=%s result=written",
                    source,
                    command,
                    attempt,
                    is_retry,
                )
                return True
            finally:
                # Never leave local transmit state busy after an exception,
                # cancellation, disconnected transport, or stale retry.
                self._transmit_busy = False

    async def _wait_for_transmitter_idle(self, reason: str) -> bool:
        """Wait for the stick's t0 completion marker with a hard timeout."""
        if self._transmitter_idle.is_set():
            return True
        try:
            await asyncio.wait_for(
                self._transmitter_idle.wait(), timeout=TRANSMIT_IDLE_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.error(
                "Serial transmitter did not return to idle within %.1fs (%s) "
                "source=%s mode=%s pending_payload=%s",
                TRANSMIT_IDLE_TIMEOUT,
                reason,
                self._pending_transmit_source or "internal",
                self._device_mode,
                self._pending_retry_command,
            )
            return False
        return True

    async def _retry_command_after_delay(self, command: str, source: str) -> None:
        """Retry a busy transmit only after the stick reports idle."""
        retry_task = asyncio.current_task()
        diagnostic = source in DIAGNOSTIC_TRANSMIT_SOURCES
        try:
            if not await self._wait_for_transmitter_idle("after a busy response"):
                self._busy_latched = True
                _LOGGER.error(
                    "Serial transmit abandoned because the stick never returned "
                    "to idle source=%s payload=%s; reset/reconnect required",
                    source,
                    command,
                )
                self._clear_pending_transmit(cancel_retry=False)
                return

            await asyncio.sleep(TRANSMIT_RETRY_DELAY)
            if self._pending_retry_command != command:
                _LOGGER.debug(
                    "Busy retry no longer pending source=%s payload=%s", source, command
                )
                return

            # Clear the task reference before writing so a tE response generated
            # by this attempt can schedule the next bounded retry.
            if self._retry_task is retry_task:
                self._retry_task = None
            _LOGGER.log(
                logging.WARNING if diagnostic else logging.INFO,
                "Retrying serial transmit after idle source=%s retry=%d/%d "
                "mode=%s payload=%s",
                source,
                self._transmit_retry_count,
                TRANSMIT_MAX_RETRIES,
                self._device_mode,
                command,
            )
            await self._write_command(command, is_retry=True, source=source)
        except asyncio.CancelledError:
            _LOGGER.debug(
                "Serial transmit retry cancelled source=%s payload=%s", source, command
            )
            raise
        finally:
            if self._retry_task is retry_task:
                self._retry_task = None

    def _clear_pending_transmit(self, *, cancel_retry: bool = True) -> None:
        """Clear retry state and optionally cancel the scheduled retry task."""
        retry_task = self._retry_task
        self._retry_task = None
        if (
            cancel_retry
            and retry_task is not None
            and retry_task is not asyncio.current_task()
            and not retry_task.done()
        ):
            retry_task.cancel()
        self._pending_retry_command = None
        self._pending_transmit_source = None
        self._transmit_retry_count = 0

    async def pair_device_and_wait(self) -> tuple[str, str] | None:
        """Put the stick into pairing mode and wait for a device to pair."""
        if self._pairing_future and not self._pairing_future.done():
            _LOGGER.warning("Pairing already in progress")
            return None
        if not self._is_transmit_capable():
            _LOGGER.error(
                "Pairing blocked because stick is not ready connected=%s mode=%s "
                "busy_latched=%s",
                self._is_connected,
                self._device_mode,
                self._busy_latched,
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
            for phase, payload in (
                ("teach_60", pair_command),
                ("finish_40", finish_pair_command),
            ):
                if not await self.send_command(payload, source="pairing"):
                    raise ConnectionError(f"could not send pairing phase {phase}")
                if not await self._wait_for_transmitter_idle(
                    f"finishing pairing phase {phase}"
                ):
                    self._busy_latched = True
                    return None
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
            stop_task = asyncio.create_task(
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
        """Toggle pairing off and restore the normal listening-mode state."""
        stopped = False
        try:
            if delay:
                await asyncio.sleep(2)
            _LOGGER.debug("Stopping pairing mode with command: sp")
            stopped = await self.send_command(CMD_GET_PARAM_P)
            if stopped:
                _LOGGER.info("Pairing mode stopped; stick returned to listening mode")
            else:
                _LOGGER.warning("Could not send pairing stop command")
        except OSError as err:
            _LOGGER.debug("Error stopping pairing mode (communication error): %s", err)
        finally:
            self._pairing_active = False
            if self._is_connected:
                self._device_mode = "listening" if stopped else "unknown"
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
                self._busy_latched = True
                _LOGGER.error(
                    "Motor teach ACK timeout source=%s device_id=%s enum=%s "
                    "phase=%s payload=%s",
                    source,
                    device_id or "unknown",
                    normalized_enum,
                    phase,
                    payload,
                )
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

        acknowledged = await self._wait_for_transmitter_idle(
            "finishing raw RF transmit"
        )
        if not acknowledged:
            self._busy_latched = True
        _LOGGER.log(
            logging.WARNING if acknowledged else logging.ERROR,
            "Raw RF transmit result source=%s payload=%s result=%s "
            "stick_ack_only=True motor_result=unknown",
            source,
            normalized,
            "stick_ack_completed" if acknowledged else "stick_ack_timeout",
        )
        return acknowledged

    async def control_blind(
        self,
        device_enum: str,
        action: str,
        *,
        device_id: str | None = None,
        source: str = "cover",
    ) -> bool:
        """Send a control command to a specific blind."""
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
            "connected=%s mode=%s ready=%s pairing=%s transmitter_active=%s "
            "busy_latched=%s",
            source,
            action_name,
            device_id or "unknown",
            normalized_enum,
            self._is_connected,
            self._device_mode,
            self.transmit_ready,
            self._pairing_active,
            self._transmitter_active,
            self._busy_latched,
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
        sent = await self.send_command(raw_payload, source=source)
        if sent:
            _LOGGER.log(
                visible_level,
                "Blind transmit write result source=%s command=%s payload=%s "
                "result=written awaiting_ack=t1/t0",
                source,
                action_name,
                raw_payload,
            )
        else:
            _LOGGER.error(
                "Blind transmit write result source=%s command=%s payload=%s "
                "result=failed",
                source,
                action_name,
                raw_payload,
            )
        return sent

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
        """Register existing devices and all persisted status identities."""
        for device in devices:
            command_device_id = (
                device.get(CONF_COMMAND_DEVICE_ID)
                or device.get("id")
                or device.get(CONF_STATUS_DEVICE_ID)
            )
            command_enum = (
                device.get(CONF_COMMAND_ENUM)
                or device.get("enum")
                or device.get(CONF_STATUS_ENUM)
            )
            status_device_id = (
                device.get(CONF_STATUS_DEVICE_ID)
                or device.get("id")
                or command_device_id
            )
            status_enum = (
                device.get(CONF_STATUS_ENUM) or device.get("enum") or command_enum
            )
            entity_name = str(device.get("name") or status_device_id)
            if command_device_id and command_enum:
                self._registered_devices[str(command_device_id)] = str(command_enum)
            primary_identity = normalize_status_identity(status_device_id, status_enum)
            if primary_identity is not None and command_device_id:
                self._register_status_identity(
                    primary_identity,
                    entity_name=entity_name,
                    command_device_id=str(command_device_id),
                    primary=True,
                )
            secondary_identities = normalize_status_identities(
                device.get(CONF_SECONDARY_STATUS_IDENTITIES)
            )
            for identity in secondary_identities:
                if identity != primary_identity and command_device_id:
                    self._register_status_identity(
                        identity,
                        entity_name=entity_name,
                        command_device_id=str(command_device_id),
                        primary=False,
                    )
            _LOGGER.debug(
                "Registered existing entity=%s command=%s/%s primary_status=%s/%s "
                "secondary_statuses=%s",
                entity_name,
                command_device_id,
                command_enum,
                status_device_id,
                status_enum,
                secondary_identities,
            )

    def remove_known_device(self, device_id: str) -> None:
        """Remove a command device and all of its registered status identities."""
        self._registered_devices.pop(device_id, None)
        normalized_id = device_id.upper()
        self._registered_entity_keys = {
            key: registration
            for key, registration in self._registered_entity_keys.items()
            if registration.command_device_id != normalized_id
        }
        _LOGGER.debug("Removed device %s from registered entities", device_id)

    def register_entity(
        self,
        status_device_id: str,
        status_enum: str,
        entity_name: str | None = None,
        *,
        command_device_id: str | None = None,
        command_enum: str | None = None,
        secondary_status_identities: object = None,
    ) -> None:
        """Register command, primary status, and diagnostic secondary identities."""
        command_id = command_device_id or status_device_id
        command_slot = command_enum or status_enum
        display_name = entity_name or status_device_id
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
    ) -> None:
        """Store the latest cover position calculation for diagnostics."""
        self._last_position_updates[command_device_id.upper()] = {
            "source": source,
            "direction": direction,
            "previous_position": previous_position,
            "new_position": new_position,
            "status": status,
            "time": dt_util.now().strftime("%H:%M:%S"),
        }

    @callback
    def get_last_position_update(self, command_device_id: str) -> dict[str, Any] | None:
        """Return the latest position calculation for a command identity."""
        update = self._last_position_updates.get(command_device_id.upper())
        return dict(update) if update is not None else None

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

    def update_connection_status(self, connected: bool) -> None:
        """Update connection status for compatibility with existing callers."""
        self._is_connected = connected
        self._update_status()

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
        self._clear_pending_transmit()
        self._transmit_busy = False
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._busy_latched = False

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
        """Return the exact reason RF transmission is currently unavailable."""
        if not self._is_connected:
            return "serial stick is disconnected"
        if self._transport is None:
            return "serial transport is unavailable"
        if self._transport.is_closing():
            return "serial transport is closing"
        if self._pairing_active:
            return "pairing is active"
        if self._device_mode != "listening":
            return f"stick mode is {self._device_mode or 'unknown'}, expected listening"
        if self._busy_latched:
            return "stick busy state is latched; reset/reconnect required"
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
        """Return whether a busy timeout requires reset/reconnect."""
        return self._busy_latched

    @property
    def transmitter_active(self) -> bool:
        """Return whether t1 was seen without a subsequent t0."""
        return self._transmitter_active

    @property
    def transmit_block_reason(self) -> str | None:
        """Return why a new diagnostic command cannot be sent immediately."""
        if reason := self._transmit_capability_block_reason():
            return reason
        if self._transmitter_active or not self._transmitter_idle.is_set():
            return "transmitter is active; waiting for t0"
        if self._pending_retry_command is not None:
            return f"transmit is pending for payload {self._pending_retry_command}"
        if self._transmit_busy:
            return "serial write is in progress"
        if self._transmit_lock.locked():
            return "serial transmit lock is held"
        return None

    @property
    def transmit_ready(self) -> bool:
        """Return whether a new Developer Tools command can be sent immediately."""
        return self.transmit_block_reason is None

    # LED Control Methods
    async def led_on(self) -> None:
        """Turn the USB stick LED on."""
        _LOGGER.debug("Turning LED on")
        await self.send_command(CMD_LED_ON)

    async def led_off(self) -> None:
        """Turn the USB stick LED off."""
        _LOGGER.debug("Turning LED off")
        await self.send_command(CMD_LED_OFF)

    async def led_blink(self, count: int = 5) -> None:
        """Blink the USB stick LED a specific number of times.

        Args:
            count: Number of times to blink (1-9)

        """
        blink_commands = {
            1: CMD_LED_BLINK_1,
            2: CMD_LED_BLINK_2,
            3: CMD_LED_BLINK_3,
            4: CMD_LED_BLINK_4,
            5: CMD_LED_BLINK_5,
            6: CMD_LED_BLINK_6,
            7: CMD_LED_BLINK_7,
            8: CMD_LED_BLINK_8,
            9: CMD_LED_BLINK_9,
        }

        if count not in blink_commands:
            _LOGGER.error("Invalid blink count %d. Must be 1-9", count)
            return

        _LOGGER.debug("Blinking LED %d times", count)
        await self.send_command(blink_commands[count])

    # Device Calibration Methods
    async def set_upper_endpoint(self, device_enum: str) -> None:
        """Set the upper endpoint for a blind device.

        Args:
            device_enum: The device enumerator (hex string like "10")

        """
        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{CMD_SET_UPPER_ENDPOINT}0000"
        _LOGGER.debug("Setting upper endpoint for device %s: %s", device_enum, command)
        await self.send_command(command)

    async def set_lower_endpoint(self, device_enum: str) -> None:
        """Set the lower endpoint for a blind device.

        Args:
            device_enum: The device enumerator (hex string like "10")

        """
        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{CMD_SET_LOWER_ENDPOINT}0000"
        _LOGGER.debug("Setting lower endpoint for device %s: %s", device_enum, command)
        await self.send_command(command)

    async def allow_pairing_on_device(self, device_enum: str) -> None:
        """Make a device listen to a new remote's ID.

        Args:
            device_enum: The device enumerator (hex string like "10")

        """
        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{CMD_ALLOW_PAIRING}0000"
        _LOGGER.debug("Allowing pairing on device %s: %s", device_enum, command)
        await self.send_command(command)

    async def manual_up(self, device_enum: str) -> None:
        """Manually move blind up (simulates holding button).

        Args:
            device_enum: The device enumerator (hex string like "10")

        """
        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{CMD_MANUAL_UP}0000"
        _LOGGER.debug("Manual up for device %s: %s", device_enum, command)
        await self.send_command(command)

    async def manual_down(self, device_enum: str) -> None:
        """Manually move blind down (simulates holding button).

        Args:
            device_enum: The device enumerator (hex string like "10")

        """
        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{CMD_MANUAL_DOWN}0000"
        _LOGGER.debug("Manual down for device %s: %s", device_enum, command)
        await self.send_command(command)

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

    async def echo_on(self) -> None:
        """Enable local echo on the USB stick."""
        _LOGGER.debug("Enabling local echo")
        await self.send_command(CMD_ECHO_ON)

    async def echo_off(self) -> None:
        """Disable local echo on the USB stick."""
        _LOGGER.debug("Disabling local echo")
        await self.send_command(CMD_ECHO_OFF)

    async def enter_bootloader_mode(self) -> None:
        """Enter bootloader mode (B:0)."""
        _LOGGER.debug("Entering bootloader mode")
        await self.send_command(CMD_ENTER_BOOTLOADER)

    async def enter_initial_mode(self) -> None:
        """Enter initial mode (B:1)."""
        _LOGGER.debug("Entering initial mode")
        await self.send_command(CMD_ENTER_INITIAL)

    async def reboot_stick(self) -> None:
        """Reboot the USB stick (only available in bootloader mode)."""
        _LOGGER.debug("Rebooting USB stick")
        await self.send_command(CMD_REBOOT)

    async def disconnect(self) -> None:
        """Disconnect intentionally and cancel all pending serial work."""
        self._disconnect_requested = True
        self._cancel_scheduled_reconnect()

        self._clear_pending_transmit()
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
        self._transmit_busy = False
        self._transmitter_active = False
        self._transmitter_idle.set()
        self._busy_latched = False
        if transport is not None:
            transport.close()
        self._update_status()
        _LOGGER.info("Disconnected from Schellenberg USB stick")

    async def reset_and_reconnect(self) -> bool:
        """Close and reopen the serial port, restoring listening mode."""
        _LOGGER.warning(
            "Resetting Schellenberg stick serial state connected=%s mode=%s "
            "pairing=%s transmitter_active=%s busy_latched=%s",
            self._is_connected,
            self._device_mode,
            self._pairing_active,
            self._transmitter_active,
            self._busy_latched,
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
