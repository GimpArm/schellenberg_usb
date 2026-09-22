"""Calibration options flow handlers for Schellenberg USB."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .api import SchellenbergUsbApi
from .blind_id import generate_blind_id, normalize_blind_id
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

# Type alias for the subentry flow step results this handler returns.
type FlowResult = SubentryFlowResult


class CalibrationFlowHandler:
    """Handle calibration steps shared by the blind config-subentry flow.

    This is only ever constructed by SchellenbergPairingSubentryFlow
    (config_flow.py), never by SchellenbergOptionsFlowHandler
    (options_flow.py) - hub options only edit the serial port and do not
    calibrate. `flow` is therefore always a ConfigSubentryFlow, not a plain
    OptionsFlow, and code here can rely on ConfigSubentryFlow-only methods
    such as `_get_entry()`/`_get_reconfigure_subentry()`.
    """

    def __init__(self, flow: ConfigSubentryFlow) -> None:
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
        """Return the loaded hub API for this subentry flow's parent entry."""
        entry = self.flow._get_entry()
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
            "open_time": (
                round(self._open_time, 2) if self._open_time is not None else None
            ),
            "close_time": (
                round(self._close_time, 2) if self._close_time is not None else None
            ),
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

    def set_selected_device(self, device: dict[str, Any]) -> None:
        """Public setter to assign selected device without storage lookup."""
        self._selected_device = device

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

            # Start timing the open movement. Monotonic, not wall-clock: an
            # NTP correction or manual clock change during the up-to-5-minute
            # calibration window must not corrupt the measured travel time.
            self._calibration_start_time = time.monotonic()

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

            # Record the open time. Floor it well above zero: on a very fast
            # motor (or if the start/stop events land in the same clock
            # tick) this could otherwise measure as 0.0, which would later
            # divide-by-zero when cover.py uses it to compute position
            # during movement.
            self._open_time = max(
                time.monotonic() - self._calibration_start_time, 0.1
            )
            _LOGGER.debug("Calibration open_time: %s seconds", self._open_time)
            self._set_calibration_capture_phase("idle_between_legs")

            # Move to close instruction step
            return await self.async_step_calibration_close_instruction()

        except asyncio.CancelledError:
            # The flow itself was cancelled (e.g. HA shutting down, or the
            # user abandoning the wizard) while awaiting movement/stop
            # events. Still close out the capture window before propagating
            # the cancellation, otherwise it is left "active" forever and
            # the next status-discovery attempt fails with a busy error
            # until something else happens to supersede it.
            self._finish_calibration_capture("opening_cancelled")
            raise
        except Exception:
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

            # Start timing the close movement (see the open-instruction step
            # for why this uses monotonic rather than wall-clock time).
            self._calibration_start_time = time.monotonic()

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

            # Record the close time (see the open_time floor above for why).
            self._close_time = max(
                time.monotonic() - self._calibration_start_time, 0.1
            )
            _LOGGER.debug("Calibration close_time: %s seconds", self._close_time)
            self._finish_calibration_capture("completed")
            self._apply_calibration_status_candidates()

            # Move to completion step
            return await self.async_step_calibration_complete()

        except asyncio.CancelledError:
            # See the matching comment in the open-instruction step: always
            # close out the capture window before the cancellation
            # propagates.
            self._finish_calibration_capture("closing_cancelled")
            raise
        except Exception:
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
            # User confirmed completion - notify the live entity. The actual
            # open/close times are persisted below via async_create_entry /
            # async_update_and_abort (the config subentry, not ad-hoc storage).
            await self._notify_calibration_completed(self._open_time, self._close_time)

            # If pairing flow requested creation after calibration,
            # create subentry entry now.
            if (
                self._create_subentry_after_calibration
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
                return self.flow.async_create_entry(
                    title=self._pending_device_name,
                    data=data,
                    unique_id=self._pending_device_id,
                )

            # Otherwise this is a recalibration of an already-existing blind
            # subentry (self.flow is always a ConfigSubentryFlow; see the
            # class docstring).
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
            event_type: The event type to wait for
                (EVENT_STARTED_MOVING_UP or EVENT_STARTED_MOVING_DOWN)

        Returns:
            True if movement start event received, False if timeout.
        """
        if self._selected_device is None:
            return False
        device_id = self._selected_device["id"]
        self._start_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Set up listener for movement start events
        def handle_device_event(command: str) -> None:
            """Handle device event."""
            if command == event_type and self._start_event:
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
        loop = asyncio.get_running_loop()

        # Set up listener for stop events
        def handle_device_event(command: str) -> None:
            """Handle device event."""
            if command == EVENT_STOPPED and self._stop_event:
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

    async def _notify_calibration_completed(
        self, open_time: float, close_time: float
    ) -> None:
        """Notify the live cover entity that new calibration times are available.

        The open/close times themselves are persisted as part of the config
        subentry (see async_create_entry / async_update_and_abort in
        async_step_calibration_complete); this just signals the already-running
        cover entity so it can pick up the new times without waiting for a reload.
        """
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
