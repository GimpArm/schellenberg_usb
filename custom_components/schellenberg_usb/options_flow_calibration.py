"""Calibration options flow handlers for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.storage import Store

from .const import (
    CALIBRATION_TIMEOUT,
    CMD_DOWN,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_DEVICE_ID,
    CONF_OPEN_TIME,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "schellenberg_usb_devices"  # Must match __init__.py


class CalibrationFlowHandler:
    """Handle calibration options flow steps."""

    def __init__(self, flow: OptionsFlow) -> None:
        """Initialize the calibration flow handler."""
        self.flow = flow
        self._selected_device: dict[str, Any] | None = None
        self._calibration_start_time: float | None = None
        self._stop_event: asyncio.Event | None = None
        self._event_listener_unsub: Any | None = None

    async def async_step_calibration_after_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start calibration for a newly paired device.

        This step bypasses device selection and goes straight to calibration
        confirmation for the device that was just paired.
        """
        # Get the device ID from the pairing handler
        device_id = self.flow.pairing_handler.get_last_paired_device_id()

        if device_id is None:
            # Fallback to regular calibration if no device ID available
            return await self.async_step_calibration()

        # Load paired devices from storage to get device details
        storage = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}
        devices = stored_data.get("devices", [])

        # Find the newly paired device
        self._selected_device = next((d for d in devices if d["id"] == device_id), None)

        if self._selected_device is None:
            # Device not found, abort
            return self.flow.async_abort(reason="device_not_found")

        # Proceed directly to calibration confirmation
        return await self.async_step_calibration_confirm()

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a device to calibrate."""
        # Load paired devices from storage
        storage = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}
        devices = stored_data.get("devices", [])

        if not devices:
            return self.flow.async_abort(reason="no_devices")

        if user_input is not None:
            # User selected a device
            device_id = user_input[CONF_DEVICE_ID]
            self._selected_device = next(
                (d for d in devices if d["id"] == device_id), None
            )
            if self._selected_device is None:
                return self.flow.async_abort(reason="device_not_found")
            return await self.async_step_calibration_confirm()

        # Show device selection form
        device_options = {device["id"]: device["name"] for device in devices}
        return self.flow.async_show_form(
            step_id="calibration",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(device_options),
                }
            ),
        )

    async def async_step_calibration_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm calibration process."""
        if user_input is not None:
            # User clicked "Start" - begin calibration
            return await self.async_step_calibration_run()

        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        return self.flow.async_show_form(
            step_id="calibration_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._selected_device["name"],
            },
            last_step=False,
        )

    async def async_step_calibration_run(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the calibration process."""
        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        errors = {}
        api = self.flow.config_entry.runtime_data
        device_enum = self._selected_device["enum"]

        try:
            # Step 1: Close the blind completely
            await api.control_blind(device_enum, CMD_DOWN)

            # Wait for stop event
            if not await self._wait_for_stop_event():
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_run",
                    data_schema=vol.Schema(
                        {vol.Optional("confirm", default=True): bool}
                    ),
                    errors=errors,
                )

            # Small delay between operations
            await asyncio.sleep(1)

            # Step 2: Measure UP (open) time
            self._calibration_start_time = time.time()
            await api.control_blind(device_enum, CMD_UP)

            if not await self._wait_for_stop_event():
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_run",
                    data_schema=vol.Schema(
                        {vol.Optional("confirm", default=True): bool}
                    ),
                    errors=errors,
                )

            open_time = time.time() - self._calibration_start_time
            _LOGGER.debug("Calibration open_time: %s seconds", open_time)

            # Small delay between operations
            await asyncio.sleep(1)

            # Step 3: Measure DOWN (close) time
            self._calibration_start_time = time.time()
            await api.control_blind(device_enum, CMD_DOWN)

            if not await self._wait_for_stop_event():
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_run",
                    data_schema=vol.Schema(
                        {vol.Optional("confirm", default=True): bool}
                    ),
                    errors=errors,
                )

            close_time = time.time() - self._calibration_start_time
            _LOGGER.debug("Calibration close_time: %s seconds", close_time)

            # Save calibration data
            await self._save_calibration_data(open_time, close_time)

            return self.flow.async_create_entry(title="", data={})

        except Exception:  # noqa: BLE001
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_run",
                data_schema=vol.Schema({vol.Optional("confirm", default=True): bool}),
                errors=errors,
            )

    async def _wait_for_stop_event(self) -> bool:
        """Wait for the device to send a stop event.

        Returns True if stop event received, False if timeout.
        """
        device_id = self._selected_device["id"]
        self._stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()

        # Set up listener for stop events
        def handle_device_event(command: str) -> None:
            """Handle device event."""
            if command == EVENT_STOPPED:
                if self._stop_event:
                    loop.call_soon_threadsafe(self._stop_event.set)

        # Subscribe to device events
        self._event_listener_unsub = async_dispatcher_connect(
            self.flow.hass,
            f"{SIGNAL_DEVICE_EVENT}_{device_id}",
            handle_device_event,
        )

        try:
            # Wait for stop event with timeout
            await asyncio.wait_for(self._stop_event.wait(), timeout=CALIBRATION_TIMEOUT)
        except TimeoutError:
            return False
        else:
            return True
        finally:
            # Clean up listener
            if self._event_listener_unsub:
                self._event_listener_unsub()
                self._event_listener_unsub = None
            self._stop_event = None

    async def _save_calibration_data(self, open_time: float, close_time: float) -> None:
        """Save calibration times to device storage."""
        storage = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}

        # Find and update the device
        for device in stored_data.get("devices", []):
            if device["id"] == self._selected_device["id"]:
                device[CONF_OPEN_TIME] = round(open_time, 2)
                device[CONF_CLOSE_TIME] = round(close_time, 2)
                break

        await storage.async_save(stored_data)

        # Send signal to notify entities that calibration has been completed
        async_dispatcher_send(
            self.flow.hass,
            SIGNAL_CALIBRATION_COMPLETED,
            self._selected_device["id"],
            round(open_time, 2),
            round(close_time, 2),
        )
