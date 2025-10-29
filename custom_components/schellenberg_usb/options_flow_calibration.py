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
    CONF_CLOSE_TIME,
    CONF_DEVICE_ID,
    CONF_OPEN_TIME,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
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
        self._start_event: asyncio.Event | None = None
        self._stop_event: asyncio.Event | None = None
        self._event_listener_unsub: Any | None = None
        self._open_time: float | None = None
        self._close_time: float | None = None

    async def set_device_by_id(self, device_id: str) -> None:
        """Set the device to calibrate by its ID.

        Used by reconfigure flow to directly set the device without selection.
        """
        storage: Store = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}
        devices = stored_data.get("devices", [])

        self._selected_device = next((d for d in devices if d["id"] == device_id), None)

    async def async_step_calibration_after_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start calibration for a newly paired device.

        This step bypasses device selection and goes straight to calibration
        confirmation for the device that was just paired.
        """
        # Get the device ID from the pairing handler
        device_id = self.flow.pairing_handler.get_last_paired_device_id()  # type: ignore[attr-defined]

        if device_id is None:
            # Fallback to regular calibration if no device ID available
            return await self.async_step_calibration()

        # Load paired devices from storage to get device details
        storage: Store = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}
        devices = stored_data.get("devices", [])

        # Find the newly paired device
        self._selected_device = next((d for d in devices if d["id"] == device_id), None)

        if self._selected_device is None:
            # Device not found, abort
            return self.flow.async_abort(reason="device_not_found")

        # Proceed directly to calibration close step
        return await self.async_step_calibration_close()

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a device to calibrate."""
        # Load paired devices from storage
        storage: Store = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
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
            return await self.async_step_calibration_close()

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

    async def async_step_calibration_close(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Instruct user to close the blinds and press next."""
        if user_input is not None:
            # User has closed the blinds and is ready to proceed
            return await self.async_step_calibration_open_instruction()

        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        return self.flow.async_show_form(
            step_id="calibration_close",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._selected_device["name"],
            },
            last_step=False,
        )

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Instruct user to open the blinds and wait for movement."""
        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        errors = {}

        # Show instruction form first time
        if user_input is None:
            return self.flow.async_show_form(
                step_id="calibration_open_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                last_step=False,
            )

        # User clicked Next - wait for movement start and measure timing
        try:
            # Wait for user to manually open the blinds
            # This will trigger EVENT_STARTED_MOVING_UP from the device
            start_ok = await self._wait_for_movement_start(EVENT_STARTED_MOVING_UP)
            if not start_ok:
                errors["base"] = "calibration_start_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_open_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            # Start timing the open movement
            self._calibration_start_time = time.time()

            # Wait for device to stop moving
            stop_ok = await self._wait_for_stop_event()
            if not stop_ok:
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_open_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            # Record the open time
            self._open_time = time.time() - self._calibration_start_time
            _LOGGER.debug("Calibration open_time: %s seconds", self._open_time)

            # Move to close instruction step
            return await self.async_step_calibration_close_instruction()

        except Exception:  # noqa: BLE001
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_open_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )

    async def async_step_calibration_close_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Instruct user to close the blinds and wait for movement."""
        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        errors = {}

        # Show instruction form first time
        if user_input is None:
            return self.flow.async_show_form(
                step_id="calibration_close_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                last_step=False,
            )

        # User clicked Next - wait for movement start and measure timing
        try:
            # Wait for user to manually close the blinds
            # This will trigger EVENT_STARTED_MOVING_DOWN from the device
            start_ok = await self._wait_for_movement_start(EVENT_STARTED_MOVING_DOWN)
            if not start_ok:
                errors["base"] = "calibration_start_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_close_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            # Start timing the close movement
            self._calibration_start_time = time.time()

            # Wait for device to stop moving
            stop_ok = await self._wait_for_stop_event()
            if not stop_ok:
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_close_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            # Record the close time
            self._close_time = time.time() - self._calibration_start_time
            _LOGGER.debug("Calibration close_time: %s seconds", self._close_time)

            # Move to completion step
            return await self.async_step_calibration_complete()

        except Exception:  # noqa: BLE001
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_close_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )

    async def async_step_calibration_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display calibration complete with recorded times."""
        if (
            self._selected_device is None
            or self._open_time is None
            or self._close_time is None
        ):
            return self.flow.async_abort(reason="device_not_found")

        if user_input is not None:
            # User confirmed completion - save calibration data and set cover position
            await self._save_calibration_data(self._open_time, self._close_time)
            return self.flow.async_create_entry(title="", data={})

        return self.flow.async_show_form(
            step_id="calibration_complete",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._selected_device["name"],
                "open_time": f"{self._open_time:.2f}",
                "close_time": f"{self._close_time:.2f}",
            },
            last_step=True,
        )

    async def _wait_for_movement_start(self, event_type: str) -> bool:
        """Wait for the device to start moving.

        Args:
            event_type: The event type to wait for (EVENT_STARTED_MOVING_UP or EVENT_STARTED_MOVING_DOWN)

        Returns:
            True if movement start event received, False if timeout.
        """
        if self._selected_device is None:
            return False
        device_id = self._selected_device["id"]
        self._start_event = asyncio.Event()
        loop = asyncio.get_event_loop()

        # Set up listener for movement start events
        def handle_device_event(command: str) -> None:
            """Handle device event."""
            if command == event_type:
                if self._start_event:
                    loop.call_soon_threadsafe(self._start_event.set)

        # Subscribe to device events
        self._event_listener_unsub = async_dispatcher_connect(
            self.flow.hass,
            f"{SIGNAL_DEVICE_EVENT}_{device_id}",
            handle_device_event,
        )

        try:
            # Wait for movement start event with timeout
            await asyncio.wait_for(
                self._start_event.wait(), timeout=CALIBRATION_TIMEOUT
            )
        except TimeoutError:
            return False
        else:
            return True
        finally:
            # Clean up listener
            if self._event_listener_unsub is not None:
                self._event_listener_unsub()
                self._event_listener_unsub = None
            self._start_event = None

    async def _wait_for_stop_event(self) -> bool:
        """Wait for the device to send a stop event.

        Returns:
            True if stop event received, False if timeout.
        """
        if self._selected_device is None:
            return False
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
            if self._event_listener_unsub is not None:
                self._event_listener_unsub()
                self._event_listener_unsub = None
            self._stop_event = None

    async def _save_calibration_data(self, open_time: float, close_time: float) -> None:
        """Save calibration times to device storage and set cover position.

        After calibration completes, the device is in fully closed position,
        so we update the cover entity position to 0.
        """
        storage: Store = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}

        # Find and update the device
        if self._selected_device is not None:
            for device in stored_data.get("devices", []):
                if device["id"] == self._selected_device["id"]:
                    device[CONF_OPEN_TIME] = round(open_time, 2)
                    device[CONF_CLOSE_TIME] = round(close_time, 2)
                    break

        await storage.async_save(stored_data)

        # Send signal to notify entities that calibration has been completed
        if self._selected_device is not None:
            async_dispatcher_send(
                self.flow.hass,
                SIGNAL_CALIBRATION_COMPLETED,
                self._selected_device["id"],
                round(open_time, 2),
                round(close_time, 2),
            )
