"""Calibration options flow handlers for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.storage import Store

from .blind_id import generate_blind_id, normalize_blind_id
from .api import SchellenbergUsbApi
from .const import (
    CALIBRATION_TIMEOUT,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "schellenberg_usb_devices"  # Must match __init__.py

# Type alias for flow results that work with both OptionsFlow and ConfigSubentryFlow
FlowResult = ConfigFlowResult | SubentryFlowResult


class CalibrationFlowHandler:
    """Handle calibration options flow steps."""

    def __init__(self, flow: OptionsFlow | ConfigSubentryFlow) -> None:
        """Initialize the calibration flow handler."""
        self.flow = flow
        self._selected_device: dict[str, Any] | None = None
        self._calibration_start_time: float | None = None
        self._start_event: asyncio.Event | None = None
        self._stop_event: asyncio.Event | None = None
        self._event_listener_unsub: Any | None = None
        self._open_time: float | None = None
        self._close_time: float | None = None
        self._create_subentry_after_calibration = False
        self._pending_blind_id: str | None = None
        self._pending_device_id: str | None = None
        self._pending_device_enum: str | None = None
        self._pending_device_name: str | None = None
        self._pending_status_device_id: str | None = None
        self._pending_status_enum: str | None = None
        self._pending_secondary_status_identities: list[dict[str, str]] = []
        self._pending_status_identity_source: str | None = None
        self._calibration_discovery_result: dict[str, Any] | None = None
        self._pending_invert_direction = False

    def _runtime_api(self) -> SchellenbergUsbApi | None:
        """Return the loaded hub API when this flow has one."""
        entry = None
        get_entry = getattr(self.flow, "_get_entry", None)
        if callable(get_entry):
            entry = get_entry()
        if entry is None:
            entry = getattr(self.flow, "config_entry", None)
        api = getattr(entry, "runtime_data", None)
        return api if isinstance(api, SchellenbergUsbApi) else None

    def _start_calibration_capture(self) -> None:
        """Begin phase-labelled raw-frame capture for this calibration run."""
        api = self._runtime_api()
        if api is None:
            return
        try:
            api.start_status_frame_capture(phase="opening")
        except RuntimeError:
            # A stale capture should not break calibration; close it explicitly and
            # start the calibration-owned window.
            api.finish_status_frame_capture(end_reason="superseded_by_calibration")
            api.start_status_frame_capture(phase="opening")

    def _set_calibration_capture_phase(self, phase: str) -> None:
        """Label future frames with the current calibration leg."""
        api = self._runtime_api()
        if api is not None:
            api.set_status_frame_capture_phase(phase)

    def _finish_calibration_capture(self, end_reason: str) -> None:
        """Finish capture and retain candidates for persistence and summary."""
        api = self._runtime_api()
        if api is None:
            return
        self._calibration_discovery_result = api.finish_status_frame_capture(
            end_reason=end_reason
        )

    def _apply_calibration_status_candidates(self) -> None:
        """Apply captured identities to pending subentry fields without guessing."""
        result = self._calibration_discovery_result
        if not result:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
            return
        primary = result.get("primary")
        if primary is None:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        else:
            self._pending_status_device_id = str(primary["device_id"])
            self._pending_status_enum = str(primary["enum"])
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_CALIBRATION
        self._pending_secondary_status_identities = [
            {"device_id": str(group["device_id"]), "enum": str(group["enum"])}
            for group in result.get("secondary", [])
        ]

    def _calibration_record(self) -> dict[str, Any] | None:
        """Return JSON-compatible diagnostics for the completed calibration run."""
        if self._calibration_discovery_result is None:
            return None
        return {
            **self._calibration_discovery_result,
            "open_time": round(self._open_time, 2) if self._open_time else None,
            "close_time": round(self._close_time, 2) if self._close_time else None,
        }

    def _calibration_summary_placeholders(self) -> dict[str, str]:
        """Build a user-facing summary of measured times and received streams."""
        result = self._calibration_discovery_result or {}
        primary = result.get("primary")
        secondary = result.get("secondary", [])
        primary_text = (
            f"{primary['device_id']}/{primary['enum']}"
            if primary is not None
            else "Not discovered"
        )
        primary_frames = (
            ", ".join(primary.get("commands", [])) if primary is not None else "None"
        )
        secondary_text = (
            ", ".join(
                f"{group['device_id']}/{group['enum']} "
                f"({','.join(group.get('commands', []))})"
                for group in secondary
            )
            or "None"
        )
        return {
            "primary_status_identity": primary_text,
            "primary_frames": primary_frames,
            "secondary_status_identities": secondary_text,
            "position_tracking": (
                "Available from received status frames"
                if primary is not None
                else (
                    "Unavailable: HA commands can still estimate position, but "
                    "remote/status tracking was not discovered"
                )
            ),
            "calibration_end_reason": str(result.get("end_reason", "completed")),
            "observed_frame_count": str(len(result.get("frames", []))),
        }

    async def set_device_by_id(self, device_id: str) -> None:
        """Set the device to calibrate by its ID.

        Used by reconfigure flow to directly set the device without selection.
        """
        storage: Store = Store(self.flow.hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await storage.async_load() or {"devices": []}
        devices = stored_data.get("devices", [])
        self._selected_device = next((d for d in devices if d["id"] == device_id), None)

        # Fallback: if device not present in storage yet, build minimal record
        if self._selected_device is None:
            # Attempt to derive name from subentry (OptionsFlow context has config_entry)
            # We access the config entry via flow.config_entry and search its subentries.
            try:
                entry = getattr(self.flow, "config_entry", None)
                if entry is not None:
                    subentry = next(
                        (
                            s
                            for s in entry.subentries.values()
                            if s.data.get("device_id") == device_id
                        ),
                        None,
                    )
                    if subentry is not None:
                        self._selected_device = {
                            "id": device_id,
                            "name": subentry.title or f"Blind {device_id}",
                            # Calibration times unknown at this point
                            CONF_OPEN_TIME: None,
                            CONF_CLOSE_TIME: None,
                        }
            except Exception:  # noqa: BLE001
                # Leave _selected_device as None; caller will abort appropriately
                _LOGGER.debug(
                    "Fallback subentry lookup failed for device %s", device_id
                )

    def set_selected_device(self, device: dict[str, Any]) -> None:
        """Public setter to assign selected device without storage lookup."""
        self._selected_device = device

    async def async_step_calibration_after_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start calibration for a newly paired device.

        This step bypasses device selection and goes straight to calibration
        confirmation for the device that was just paired.
        """
        pairing_handler = getattr(self.flow, "pairing_handler", None)
        if pairing_handler is None:
            return await self.async_step_calibration()

        device_id = pairing_handler.get_last_paired_device_id()

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
    ) -> FlowResult:
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
    ) -> FlowResult:
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
    ) -> FlowResult:
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
        self._start_calibration_capture()
        try:
            # Wait for the physical direction that corresponds to logical opening.
            open_event = (
                EVENT_STARTED_MOVING_DOWN
                if self._selected_device.get(CONF_INVERT_DIRECTION, False)
                else EVENT_STARTED_MOVING_UP
            )
            start_ok = await self._wait_for_movement_start(open_event)
            if not start_ok:
                self._finish_calibration_capture("opening_start_timeout")
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
                self._finish_calibration_capture("opening_stop_timeout")
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
            self._set_calibration_capture_phase("idle_between_legs")

            # Move to close instruction step
            return await self.async_step_calibration_close_instruction()

        except Exception:  # noqa: BLE001
            self._finish_calibration_capture("opening_error")
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
    ) -> FlowResult:
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
        self._set_calibration_capture_phase("closing")
        try:
            # Wait for the physical direction that corresponds to logical closing.
            close_event = (
                EVENT_STARTED_MOVING_UP
                if self._selected_device.get(CONF_INVERT_DIRECTION, False)
                else EVENT_STARTED_MOVING_DOWN
            )
            start_ok = await self._wait_for_movement_start(close_event)
            if not start_ok:
                self._finish_calibration_capture("closing_start_timeout")
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
                self._finish_calibration_capture("closing_stop_timeout")
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
            self._finish_calibration_capture("completed")
            self._apply_calibration_status_candidates()

            # Move to completion step
            return await self.async_step_calibration_complete()

        except Exception:  # noqa: BLE001
            self._finish_calibration_capture("closing_error")
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
    ) -> FlowResult:
        """Display calibration complete with recorded times."""
        if (
            self._selected_device is None
            or self._open_time is None
            or self._close_time is None
        ):
            return self.flow.async_abort(reason="device_not_found")

        if user_input is not None:
            # User confirmed completion - save calibration data
            await self._save_calibration_data(self._open_time, self._close_time)

            # If pairing flow requested creation after calibration, create subentry entry now.
            if (
                not isinstance(self.flow, OptionsFlow)
                and self._create_subentry_after_calibration
                and self._pending_device_id
                and self._pending_device_enum
                and self._pending_device_name
            ):
                data: dict[str, Any] = {
                    CONF_BLIND_ID: self._pending_blind_id or generate_blind_id(),
                    CONF_DEVICE_ID: self._pending_device_id,
                    CONF_DEVICE_ENUM: self._pending_device_enum,
                    CONF_COMMAND_DEVICE_ID: self._pending_device_id,
                    CONF_COMMAND_ENUM: self._pending_device_enum,
                    CONF_SECONDARY_STATUS_IDENTITIES: list(
                        self._pending_secondary_status_identities
                    ),
                    CONF_OPEN_TIME: round(self._open_time, 2),
                    CONF_CLOSE_TIME: round(self._close_time, 2),
                    CONF_INVERT_DIRECTION: self._pending_invert_direction,
                }
                if (
                    self._pending_status_device_id is not None
                    and self._pending_status_enum is not None
                ):
                    data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
                    data[CONF_STATUS_ENUM] = self._pending_status_enum
                if self._pending_status_identity_source is not None:
                    data[CONF_STATUS_IDENTITY_SOURCE] = (
                        self._pending_status_identity_source
                    )
                if calibration_record := self._calibration_record():
                    data[CONF_LAST_CALIBRATION] = calibration_record
                return self.flow.async_create_entry(  # type: ignore[attr-defined]
                    title=self._pending_device_name,
                    data=data,
                    unique_id=self._pending_device_id,
                )

            # Options flow: create empty entry to finish
            if isinstance(self.flow, OptionsFlow):
                return self.flow.async_create_entry(title="", data={})

            if isinstance(self.flow, ConfigSubentryFlow):
                data_updates: dict[str, Any] = {
                    CONF_OPEN_TIME: round(self._open_time, 2),
                    CONF_CLOSE_TIME: round(self._close_time, 2),
                }
                if calibration_record := self._calibration_record():
                    data_updates[CONF_LAST_CALIBRATION] = calibration_record
                if (
                    self._pending_status_device_id is not None
                    and self._pending_status_enum is not None
                    and self._pending_status_identity_source
                    == STATUS_IDENTITY_SOURCE_CALIBRATION
                ):
                    data_updates.update(
                        {
                            CONF_STATUS_DEVICE_ID: self._pending_status_device_id,
                            CONF_STATUS_ENUM: self._pending_status_enum,
                            CONF_STATUS_IDENTITY_SOURCE: (
                                STATUS_IDENTITY_SOURCE_CALIBRATION
                            ),
                            CONF_SECONDARY_STATUS_IDENTITIES: list(
                                self._pending_secondary_status_identities
                            ),
                        }
                    )
                return self.flow.async_update_and_abort(
                    self.flow._get_entry(),
                    self.flow._get_reconfigure_subentry(),
                    data_updates=data_updates,
                )

            # Fallback: abort with success if no creation path triggered
            return self.flow.async_abort(reason="reconfigure_successful")

        return self.flow.async_show_form(
            step_id="calibration_complete",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._selected_device["name"],
                "open_time": f"{self._open_time:.2f}",
                "close_time": f"{self._close_time:.2f}",
                **self._calibration_summary_placeholders(),
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
                self._selected_device.get("entity_id", self._selected_device["id"]),
                round(open_time, 2),
                round(close_time, 2),
            )

    def enable_subentry_creation(
        self,
        *,
        blind_id: str | None = None,
        device_id: str,
        device_enum: str,
        device_name: str,
        status_device_id: str | None = None,
        status_enum: str | None = None,
        secondary_status_identities: list[dict[str, str]] | None = None,
        status_identity_source: str | None = None,
        invert_direction: bool = False,
    ) -> None:
        """Enable creating a subentry after calibration completes."""
        self._create_subentry_after_calibration = True
        self._pending_blind_id = normalize_blind_id(blind_id) or generate_blind_id()
        self._pending_device_id = device_id
        self._pending_device_enum = device_enum
        self._pending_device_name = device_name
        self._pending_status_device_id = status_device_id
        self._pending_status_enum = status_enum
        self._pending_status_identity_source = status_identity_source
        self._pending_secondary_status_identities = list(
            secondary_status_identities or []
        )
        self._pending_invert_direction = invert_direction

    def disable_subentry_creation(self) -> None:
        """Disable subentry creation (used for reconfigure flows)."""
        self._create_subentry_after_calibration = False
        self._pending_blind_id = None
        self._pending_device_id = None
        self._pending_device_enum = None
        self._pending_device_name = None
        self._pending_status_device_id = None
        self._pending_status_enum = None
        self._pending_secondary_status_identities = []
        self._pending_status_identity_source = None
        self._calibration_discovery_result = None
        self._pending_invert_direction = False
