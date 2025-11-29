"""API wrapper for Schellenberg USB Stick integration with Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from pyschellenberg import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    DeviceEvent,
    SchellenbergStick,
    StickStatus,
)

from .const import (
    SIGNAL_DEVICE_EVENT,
    SIGNAL_STICK_STATUS_UPDATED,
)

_LOGGER = logging.getLogger(__name__)


class SchellenbergUsbApi:
    """Manages all communication with the Schellenberg USB stick.

    This is a Home Assistant wrapper around the pyschellenberg library.
    """

    def __init__(self, hass: HomeAssistant, port: str) -> None:
        """Initialize the Schellenberg USB API."""
        self.hass = hass
        self.port = port

        # Create the low-level stick interface
        self._stick = SchellenbergStick(
            port=port,
            loop=hass.loop,
            on_device_event=self._on_device_event,
            on_status_change=self._on_status_change,
        )

        # Connection retry handling
        self._is_connecting = False
        self._retry_task: asyncio.Task[None] | None = None

    def _on_device_event(self, event: DeviceEvent) -> None:
        """Handle device events from the stick."""
        _LOGGER.debug(
            "Device event: id=%s, enum=%s, cmd=%s",
            event.device_id,
            event.device_enum,
            event.command,
        )

        # Check if this is a known device
        if event.device_id not in self._stick._registered_devices:
            _LOGGER.warning(
                "Received message for device %s (enum=%s, cmd=%s) but no "
                "corresponding entity found. The device may need to be added "
                "to Home Assistant",
                event.device_id,
                event.device_enum,
                event.command,
            )
        else:
            _LOGGER.debug(
                "Forwarding event to device %s (enum=%s): command=%s",
                event.device_id,
                event.device_enum,
                event.command,
            )

        # Forward the event to the correct entity via dispatcher
        async_dispatcher_send(
            self.hass, f"{SIGNAL_DEVICE_EVENT}_{event.device_id}", event.command
        )

    @callback
    def _on_status_change(self, status: StickStatus) -> None:
        """Handle status changes from the stick."""
        _LOGGER.debug(
            "Status change: connected=%s, version=%s, mode=%s, hub_id=%s",
            status.is_connected,
            status.device_version,
            status.device_mode,
            status.hub_id,
        )
        async_dispatcher_send(self.hass, SIGNAL_STICK_STATUS_UPDATED)

    async def connect(self) -> None:
        """Establish a connection to the serial port."""
        if self._is_connecting:
            _LOGGER.debug("Connection attempt already in progress")
            return

        self._is_connecting = True
        _LOGGER.info("Connecting to Schellenberg USB stick at %s", self.port)

        try:
            success = await self._stick.connect()
            if not success:
                _LOGGER.error("Failed to connect to Schellenberg USB stick")
                # Retry after 5 seconds
                self._schedule_reconnect()
        except Exception as err:
            _LOGGER.error(
                "Failed to connect to %s: %s. Retrying in 5 seconds",
                self.port,
                err,
            )
            self._schedule_reconnect()
        finally:
            self._is_connecting = False

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt after 5 seconds."""
        if self._retry_task and not self._retry_task.done():
            return
        self._retry_task = self.hass.async_create_task(self._reconnect_after_delay())

    async def _reconnect_after_delay(self) -> None:
        """Reconnect after a delay."""
        await asyncio.sleep(5)
        await self.connect()

    async def pair_device_and_wait(self) -> tuple[str, str] | None:
        """Put the stick into pairing mode and wait for a device to pair.

        Returns a tuple of (device_id, device_enum) if successful, None if timeout.
        """
        return await self._stick.pair_device_and_wait()

    async def control_blind(self, device_enum: str, action: str) -> None:
        """Send a control command to a specific blind.

        Args:
            device_enum: The device enumerator (hex string like "10")
            action: Command (CMD_UP, CMD_DOWN, CMD_STOP)
        """
        if action not in (CMD_UP, CMD_DOWN, CMD_STOP):
            _LOGGER.error("Invalid blind action: %s", action)
            return
        await self._stick.control_blind(device_enum, action)

    def initialize_next_device_enum(self) -> str:
        """Get the next available device enum based on registered devices."""
        return self._stick.initialize_next_device_enum()

    def register_existing_devices(self, devices: list[dict[str, Any]]) -> None:
        """Register existing devices from storage."""
        self._stick.register_existing_devices(devices)

    def remove_known_device(self, device_id: str) -> None:
        """Remove a device from the registered entities."""
        self._stick.remove_known_device(device_id)

    def register_entity(self, device_id: str, device_enum: str) -> None:
        """Register that an entity exists for this device ID with its enum."""
        self._stick.register_entity(device_id, device_enum)

    async def verify_device(self) -> bool:
        """Verify this is a Schellenberg USB stick."""
        return await self._stick.verify_device()

    @property
    def is_connected(self) -> bool:
        """Return whether the USB stick is connected."""
        return self._stick.is_connected

    @property
    def device_version(self) -> str | None:
        """Return the device firmware version."""
        return self._stick.device_version

    @property
    def device_mode(self) -> str | None:
        """Return the device mode (boot, initial, or listening)."""
        return self._stick.device_mode

    @property
    def hub_id(self) -> str | None:
        """Return the hub device ID."""
        return self._stick.hub_id

    # LED Control Methods
    async def led_on(self) -> None:
        """Turn the USB stick LED on."""
        await self._stick.led_on()

    async def led_off(self) -> None:
        """Turn the USB stick LED off."""
        await self._stick.led_off()

    async def led_blink(self, count: int = 5) -> None:
        """Blink the USB stick LED a specific number of times."""
        await self._stick.led_blink(count)

    # Device Calibration Methods
    async def set_upper_endpoint(self, device_enum: str) -> None:
        """Set the upper endpoint for a blind device."""
        await self._stick.set_upper_endpoint(device_enum)

    async def set_lower_endpoint(self, device_enum: str) -> None:
        """Set the lower endpoint for a blind device."""
        await self._stick.set_lower_endpoint(device_enum)

    async def allow_pairing_on_device(self, device_enum: str) -> None:
        """Make a device listen to a new remote's ID."""
        await self._stick.allow_pairing_on_device(device_enum)

    async def manual_up(self, device_enum: str) -> None:
        """Manually move blind up (simulates holding button)."""
        await self._stick.manual_up(device_enum)

    async def manual_down(self, device_enum: str) -> None:
        """Manually move blind down (simulates holding button)."""
        await self._stick.manual_down(device_enum)

    # USB Stick System Commands
    async def get_device_id(self) -> str | None:
        """Get the USB stick's unique device ID."""
        return await self._stick.get_device_id()

    async def echo_on(self) -> None:
        """Enable local echo on the USB stick."""
        await self._stick.echo_on()

    async def echo_off(self) -> None:
        """Disable local echo on the USB stick."""
        await self._stick.echo_off()

    async def enter_bootloader_mode(self) -> None:
        """Enter bootloader mode (B:0)."""
        await self._stick.enter_bootloader_mode()

    async def enter_initial_mode(self) -> None:
        """Enter initial mode (B:1)."""
        await self._stick.enter_initial_mode()

    async def reboot_stick(self) -> None:
        """Reboot the USB stick (only available in bootloader mode)."""
        await self._stick.reboot_stick()

    async def disconnect(self) -> None:
        """Disconnect from the serial port."""
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            self._retry_task = None
        await self._stick.disconnect()
