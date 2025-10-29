"""API for Schellenberg USB Stick."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

import serial_asyncio

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

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
    PAIRING_DEVICE_ENUM_START,
    PAIRING_TIMEOUT,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_STICK_STATUS_UPDATED,
    VERIFY_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


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
        self._is_connecting = False
        self._pairing_future: asyncio.Future[str] | None = None
        self._stop_pairing_task: asyncio.Task[None] | None = (
            None  # Track task to stop pairing
        )

        # USB stick status
        self._is_connected = False
        self._device_version: str | None = None
        self._device_mode: str | None = None  # boot, initial, or listening
        self._verify_future: asyncio.Future[bool] | None = None
        self._device_id_future: asyncio.Future[str] | None = None
        self._hub_id: str | None = None

        # Retry queue for commands that failed with "stick busy"
        self._pending_retry_command: str | None = None
        self._retry_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Establish a connection to the serial port."""
        if self._is_connecting or (
            self._transport and not self._transport.is_closing()
        ):
            _LOGGER.debug("Connection attempt already in progress or established")
            return

        self._is_connecting = True
        _LOGGER.info("Connecting to Schellenberg USB stick at %s", self.port)
        try:
            (
                self._transport,
                self._protocol,
            ) = await serial_asyncio.create_serial_connection(
                self.hass.loop,
                lambda: SchellenbergProtocol(self._handle_message, self),
                self.port,
                baudrate=112500,
            )
            self._is_connecting = False
            _LOGGER.info("Successfully connected to Schellenberg USB stick")

            # Verify this is a Schellenberg device
            if not await self.verify_device():
                _LOGGER.error(
                    "Device verification failed - not a Schellenberg USB stick"
                )
                if self._transport:
                    self._transport.close()
                self._transport = None
                self._is_connected = False
                return

            self._is_connected = True
            self._update_status()

            # Enter listening mode if not already in it
            if self._device_mode != "listening":
                _LOGGER.info(
                    "Device is in %s mode, entering listening mode", self._device_mode
                )
                # Send any lowercase command to enter listening mode (B:2)
                await self.send_command("hello")
                # Give the device a moment to switch modes
                await asyncio.sleep(0.5)
                # Update the mode to listening after sending the command
                self._device_mode = "listening"
                self._update_status()
                _LOGGER.info("Device now in listening mode")
            else:
                _LOGGER.info("Device already in listening mode")

            # Get the hub device ID after listening mode
            hub_id = await self.get_device_id()
            if hub_id:
                self._hub_id = hub_id
                _LOGGER.info("Hub device ID retrieved: %s", self._hub_id)
            else:
                _LOGGER.warning("Failed to retrieve hub device ID")
        except (serial_asyncio.serial.SerialException, OSError) as err:
            _LOGGER.error(
                "Failed to connect to %s: %s. Retrying in 5 seconds",
                self.port,
                err,
            )
            self._is_connecting = False
            # Always retry after 5 seconds
            self.hass.loop.call_later(5, lambda: self.hass.create_task(self.connect()))

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

        # Handle acknowledgments
        if message in ("t1", "t0"):
            _LOGGER.debug("Transmit ACK: %s", message)
            return

        if message == "tE":
            _LOGGER.warning("Transmit error - stick busy, will retry in 50ms")
            # Schedule a retry if we have a pending command
            if self._pending_retry_command:
                if self._retry_task and not self._retry_task.done():
                    self._retry_task.cancel()
                self._retry_task = asyncio.create_task(
                    self._retry_command_after_delay()
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
                _LOGGER.info("Pairing successful! New device ID: %s", device_id)
                self._pairing_future.set_result(device_id)
                # Stop pairing mode after a 2 second delay to ensure device has fully paired
                self._stop_pairing_task = asyncio.create_task(
                    self._stop_pairing_mode(delay=True)
                )
                self._stop_pairing_task.add_done_callback(
                    lambda _: setattr(self, "_stop_pairing_task", None)
                )
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

                # If we're in pairing mode and this is a new device
                if self._pairing_future and not self._pairing_future.done():
                    if device_id not in self._registered_devices:
                        _LOGGER.info("Pairing successful! New device ID: %s", device_id)
                        self._pairing_future.set_result(device_id)
                        # Stop pairing mode after a 2 second delay to ensure device has fully paired
                        self._stop_pairing_task = asyncio.create_task(
                            self._stop_pairing_mode(delay=True)
                        )
                        self._stop_pairing_task.add_done_callback(
                            lambda _: setattr(self, "_stop_pairing_task", None)
                        )
                        # Don't send dispatcher signal here - let the caller handle persistence
                        return

                # If this is the first time we see this device (auto-discovery mode)
                if device_id not in self._registered_devices:
                    _LOGGER.warning(
                        "Received message for device %s (enum=%s, cmd=%s) but no "
                        "corresponding entity found. The device may need to be added "
                        "to Home Assistant",
                        device_id,
                        device_enum,
                        command,
                    )
                else:
                    # The entity will handle the event via the dispatcher
                    _LOGGER.debug(
                        "Forwarding event to device %s (enum=%s): command=%s",
                        device_id,
                        device_enum,
                        command,
                    )

                # Forward the event to the correct entity (if it exists)
                async_dispatcher_send(
                    self.hass, f"{SIGNAL_DEVICE_EVENT}_{device_id}", command
                )
            except (IndexError, ValueError) as err:
                _LOGGER.debug("Failed to parse message %s: %s", message, err)

    async def send_command(self, command: str) -> None:
        """Send a command to the USB stick."""
        if self._transport is None or self._transport.is_closing():
            _LOGGER.warning("Serial port not connected. Command dropped: %s", command)
            return

        # Store command for potential retry on "stick busy" error
        self._pending_retry_command = command

        full_command = f"{command}\r\n".encode("ascii")
        _LOGGER.debug("Sending to serial device: %s", full_command.strip())
        self._transport.write(full_command)
        _LOGGER.debug("Command sent to serial device: %s", full_command.strip())

    async def _retry_command_after_delay(self) -> None:
        """Retry sending the pending command after a 100ms delay."""
        try:
            await asyncio.sleep(0.1)  # 100 milliseconds
            if self._pending_retry_command:
                command = self._pending_retry_command
                _LOGGER.debug("Retrying command after stick busy: %s", command)
                await self.send_command(command)
        except asyncio.CancelledError:
            _LOGGER.debug("Retry task cancelled")
        finally:
            self._retry_task = None

    async def pair_device_and_wait(self) -> tuple[str, str] | None:
        """Put the stick into pairing mode and wait for a device to pair.

        Returns a tuple of (device_id, device_enum) if successful, None if timeout.
        """
        if self._pairing_future and not self._pairing_future.done():
            _LOGGER.warning("Pairing already in progress")
            return None

        # Get the next available device enumerator
        device_enum = self.initialize_next_device_enum()

        # Format: ssXX9400000
        # XX = device enumerator, 94 = pair command, 00000 = padding
        pair_command = f"{CMD_TRANSMIT}{device_enum}{CMD_PAIR}00000"

        _LOGGER.info(
            "Initiating pairing with device enum %s. Command: %s",
            device_enum,
            pair_command,
        )

        # Create a future to wait for device ID first
        self._pairing_future = self.hass.loop.create_future()

        try:
            # Wait for device to send its ID first (with timeout)
            device_id = await asyncio.wait_for(
                self._pairing_future, timeout=PAIRING_TIMEOUT
            )

            # Once we have the device ID, send the pairing command
            _LOGGER.debug(
                "Received device ID %s, sending pairing command",
                device_id,
            )
            await self.send_command(pair_command)
        except TimeoutError:
            _LOGGER.warning("Pairing timeout - no device responded with ID")
            return None
        else:
            # Pairing successful - return the device ID and enum
            _LOGGER.info(
                "Pairing completed successfully: %s with device enum %s",
                device_id,
                device_enum,
            )
            return (device_id, device_enum)
        finally:
            self._pairing_future = None

    async def _stop_pairing_mode(self, delay: bool = False) -> None:
        """Stop pairing mode by sending a stop command to the stick.

        Args:
            delay: If True, wait 2 seconds before stopping to ensure device has fully paired.
        """
        try:
            if delay:
                # Wait 2 seconds before stopping pairing mode to ensure device has fully paired
                await asyncio.sleep(2)
            _LOGGER.debug("Stopping pairing mode with command: sp")
            await self.send_command(CMD_GET_PARAM_P)
            _LOGGER.info("Pairing mode stopped")
        except OSError as err:
            _LOGGER.debug("Error stopping pairing mode (communication error): %s", err)

    async def control_blind(self, device_enum: str, action: str) -> None:
        """Send a control command to a specific blind.

        Args:
            device_enum: The device enumerator (hex string like "10")
            action: Command (CMD_UP, CMD_DOWN, CMD_STOP)

        """
        if action not in (CMD_UP, CMD_DOWN, CMD_STOP):
            _LOGGER.error("Invalid blind action: %s", action)
            return

        # Format: ssXX9AAZZZ
        # XX = device enum, 9 = number of messages, AA = command, ZZZ = padding
        command = f"{CMD_TRANSMIT}{device_enum}9{action}0000"
        _LOGGER.debug("Sending blind control: %s", command)
        await self.send_command(command)

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

    def register_existing_devices(self, devices: list[dict]) -> None:
        """Register existing devices from storage.

        Args:
            devices: List of device dicts with 'id' and 'enum' keys
        """
        for device in devices:
            device_id = device.get("id")
            device_enum = device.get("enum")
            if device_id and device_enum:
                self._registered_devices[device_id] = device_enum
                _LOGGER.debug(
                    "Registered existing device %s with enum %s", device_id, device_enum
                )

    def remove_known_device(self, device_id: str) -> None:
        """Remove a device from the registered entities.

        After removal, messages from this device will be treated as unknown.
        """
        self._registered_devices.pop(device_id, None)
        _LOGGER.debug("Removed device %s from registered entities", device_id)

    def register_entity(self, device_id: str, device_enum: str) -> None:
        """Register that an entity exists for this device ID with its enum."""
        self._registered_devices[device_id] = device_enum
        _LOGGER.debug(
            "Registered entity for device %s with enum %s", device_id, device_enum
        )

    async def verify_device(self) -> bool:
        """Verify this is a Schellenberg USB stick by sending !? command.

        Returns True if verification succeeds, False otherwise.
        """
        if self._verify_future and not self._verify_future.done():
            _LOGGER.warning("Device verification already in progress")
            return False

        _LOGGER.debug("Verifying Schellenberg USB device")
        self._verify_future = self.hass.loop.create_future()

        try:
            # Send the verification command
            await self.send_command(CMD_VERIFY)

            # Wait for verification response with timeout
            result = await asyncio.wait_for(self._verify_future, timeout=VERIFY_TIMEOUT)
        except TimeoutError:
            _LOGGER.error("Device verification timeout - device did not respond to !?")
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
        """Update connection status (called from protocol)."""
        self._is_connected = connected
        self._update_status()

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
        """Return the device mode (boot, initial, or listening)."""
        return self._device_mode

    @property
    def hub_id(self) -> str | None:
        """Return the hub device ID."""
        return self._hub_id

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
        """Get the USB stick's unique device ID.

        Returns the device ID string or None if request fails.
        """
        if self._device_id_future and not self._device_id_future.done():
            _LOGGER.warning("Device ID request already in progress")
            return None

        _LOGGER.debug("Requesting device ID")
        self._device_id_future = self.hass.loop.create_future()

        try:
            # Send the request command
            await self.send_command(CMD_GET_DEVICE_ID)

            # Wait for device ID response with timeout
            device_id = await asyncio.wait_for(self._device_id_future, timeout=5)
        except TimeoutError:
            _LOGGER.error("Device ID request timeout - device did not respond")
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
        """Disconnect from the serial port."""
        # Cancel any pending retry task
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            self._retry_task = None

        if self._transport:
            self._transport.close()
            self._transport = None
            _LOGGER.info("Disconnected from Schellenberg USB stick")


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
        _LOGGER.warning("Serial port connection lost: %s", exc)
        self.api.update_connection_status(False)
