"""Config flow for Schellenberg USB integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, cast

import serial  # NOTE: blocking open used only to sanity-check connectivity
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_INVERT_DIRECTION,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    DOMAIN,
    SUBENTRY_TYPE_BLIND,
    TEST_COMMAND_DELAY,
)
from .options_flow import SchellenbergOptionsFlowHandler
from .options_flow_calibration import CalibrationFlowHandler

_LOGGER = logging.getLogger(__name__)

DEVELOPER_TOOLS_MENU_OPTIONS = {
    "test_open": "Test Open",
    "test_close": "Test Close",
    "test_stop": "Test Stop",
    "reset_stick": "Reset stick / reconnect serial",
    "copy_diagnostics": "Copy diagnostics",
}


class SchellenbergUsbConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Schellenberg USB."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return SchellenbergOptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        # Use constant for subentry type so strings/json and code stay in sync
        return {SUBENTRY_TYPE_BLIND: SchellenbergPairingSubentryFlow}

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_port: str | None = None
        self._discovered_title: str | None = None
        self._discovered_unique: str | None = None

    # -------------------------
    # MENU FLOW (Hub only)
    # -------------------------
    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show menu to set up hub."""
        # For now, only allow setting up the hub through the user flow
        # Device pairing is handled through the subentry flow
        return await self.async_step_user()

    # -------------------------
    # USER-INITIATED FLOW
    # -------------------------
    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the initial step started by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            try:
                # Quick, blocking sanity check that the port is reachable.
                serial_conn = serial.Serial(port)

                serial_conn.close()

                # Use the port path as the unique ID when set up manually.
                await self.async_set_unique_id(port, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Schellenberg USB ({port})", data=user_input
                )
            except serial.SerialException:
                errors["base"] = "cannot_connect"
                _LOGGER.error("Failed to connect to serial port %s", port)
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("An unexpected error occurred")

        return self._form_schema(errors, default_port="/dev/ttyUSB0")

    # -------------------------
    # USB DISCOVERY FLOW
    # -------------------------
    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        """Handle discovery from the USB subsystem."""
        # Try to get the most stable unique identifier we can (serial number if present).
        unique = getattr(discovery_info, "serial_number", None) or (
            f"{getattr(discovery_info, 'vid', 'unknown')}:"
            f"{getattr(discovery_info, 'pid', 'unknown')}:"
            f"{getattr(discovery_info, 'device', 'unknown')}"
        )

        # Prefer the OS device path for the default value in the confirmation form.
        port = getattr(discovery_info, "device", None)
        manufacturer = getattr(discovery_info, "manufacturer", None) or "Schellenberg"
        description = getattr(discovery_info, "description", None) or "USB device"

        # Save for the confirm step
        self._discovered_port = port
        self._discovered_unique = unique
        self._discovered_title = f"{manufacturer} {description}".strip()

        # Deduplicate if already configured; update the stored port if it changed.
        await self.async_set_unique_id(unique, raise_on_progress=False)
        self._abort_if_unique_id_configured(
            updates={CONF_SERIAL_PORT: port} if port else None
        )

        # Ask for confirmation (and allow editing the port if the host maps it differently)
        return await self.async_step_usb_confirm()

    async def async_step_usb_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Confirm USB-discovered device and create the entry."""
        errors: dict[str, str] = {}

        # If we don’t have a port path, let the user supply one.
        default_port = self._discovered_port or "/dev/ttyUSB0"

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            try:
                serial_conn = serial.Serial(port)
                serial_conn.close()

                # unique_id was already set in async_step_usb(), re-assert and create the entry
                await self.async_set_unique_id(
                    self._discovered_unique, raise_on_progress=False
                )
                self._abort_if_unique_id_configured()

                title = self._discovered_title or f"Schellenberg USB ({port})"
                return self.async_create_entry(
                    title=title, data={CONF_SERIAL_PORT: port}
                )
            except serial.SerialException:
                errors["base"] = "cannot_connect"
                _LOGGER.error("Failed to connect to serial port %s", port)
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("An unexpected error occurred during USB confirm")

        # Mark as confirm-only so the UI shows a simple confirmation experience
        self._set_confirm_only()
        return self._form_schema(
            errors, default_port=default_port, step_id="usb_confirm"
        )

    # -------------------------
    # Helpers
    # -------------------------
    @callback
    def _form_schema(
        self, errors: dict[str, str], default_port: str, step_id: str = "user"
    ) -> ConfigFlowResult:
        """Return a form with a (prefilled) serial port field."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SERIAL_PORT, default=default_port
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )


class SchellenbergPairingSubentryFlow(ConfigSubentryFlow):
    """Flow for adding new blind devices as subentries."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self.calibration_handler: CalibrationFlowHandler | None = None
        self._pending_device_id: str | None = None
        self._pending_device_enum: str | None = None
        self._pending_device_name: str | None = None
        self._pending_status_device_id: str | None = None
        self._pending_status_enum: str | None = None
        self._pending_open_time: float | None = None
        self._pending_close_time: float | None = None
        self._pending_invert_direction = False
        self._pairing_workflow = "legacy"
        self._developer_notice = "No test command sent in this session."

    def _get_calibration_handler(self) -> CalibrationFlowHandler:
        """Return (and lazily create) the calibration flow handler."""
        if self.calibration_handler is None:
            self.calibration_handler = CalibrationFlowHandler(self)
        return self.calibration_handler

    async def _await_subentry_result(
        self,
        step_coro: Awaitable[ConfigFlowResult | SubentryFlowResult],
    ) -> SubentryFlowResult:
        """Await a calibration step and cast to SubentryFlowResult for mypy."""
        return cast(SubentryFlowResult, await step_coro)

    async def async_step_blind(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point when the user clicks the 'Add blind' button.

        Home Assistant calls async_step_{subentry_type}() where subentry_type is
        the key returned by async_get_supported_subentry_types. Since our type is
        'blind', we implement async_step_blind(). Previously this was named
        async_step_pairing, which caused the flow to fall back and the
        translation key for the initiate button to be missing.
        """
        _LOGGER.debug("Subentry blind flow initiated")
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose between pairing/calibration and manual setup."""
        return self.async_show_menu(
            step_id="user", menu_options=["pair_test", "pair_device", "manual"]
        )

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Run the original pair-then-calibrate workflow."""
        self._pairing_workflow = "legacy"
        return await self._async_pair_device("pair_device", user_input)

    async def async_step_pair_test(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pair a blind and verify outgoing control before calibration."""
        self._pairing_workflow = "hybrid"
        return await self._async_pair_device("pair_test", user_input)

    async def _async_pair_device(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Pair a device for either supported pairing workflow."""
        _LOGGER.debug("Pairing step user input: %s", user_input)
        if user_input is None:
            _LOGGER.info("Showing pairing form")
            return self.async_show_form(step_id=step_id, data_schema=vol.Schema({}))

        # Get the hub entry (parent config entry)
        hub_entry = self._get_entry()
        api = hub_entry.runtime_data

        # Initiate pairing and wait for response (up to 10 seconds)
        pairing_result = await api.pair_device_and_wait()

        if pairing_result is None:
            # Pairing timeout
            return self.async_abort(reason="pairing_timeout")

        # Pairing successful! Store device_id and device_enum in context
        device_id, device_enum = pairing_result
        self._pending_device_id = device_id
        self._pending_device_enum = device_enum
        self._pending_status_device_id = device_id
        self._pending_status_enum = device_enum
        self._pending_device_name = None
        return await self.async_step_name_device()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect manual command/status identities and travel times."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_name = str(user_input[CONF_DEVICE_NAME]).strip()
            command_device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
            command_enum = str(user_input[CONF_DEVICE_ENUM]).strip().upper()
            status_device_id = (
                str(user_input.get(CONF_STATUS_DEVICE_ID, "")).strip().upper()
                or command_device_id
            )
            status_enum = (
                str(user_input.get(CONF_STATUS_ENUM, "")).strip().upper()
                or command_enum
            )
            open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"
            if not self._is_hex_value(command_device_id, 6):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(command_enum, 2):
                errors[CONF_DEVICE_ENUM] = "invalid_device_enum"
            if not self._is_hex_value(status_device_id, 6):
                errors[CONF_STATUS_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(status_enum, 2):
                errors[CONF_STATUS_ENUM] = "invalid_device_enum"
            if open_time <= 0:
                errors[CONF_OPEN_TIME_SECONDS] = "invalid_travel_time"
            if close_time <= 0:
                errors[CONF_CLOSE_TIME_SECONDS] = "invalid_travel_time"

            hub_entry = self._get_entry()
            if any(
                str(
                    subentry.data.get(CONF_COMMAND_DEVICE_ID)
                    or subentry.data.get(CONF_DEVICE_ID, "")
                ).upper()
                == command_device_id
                for subentry in hub_entry.subentries.values()
            ):
                errors[CONF_DEVICE_ID] = "already_configured"

            if not errors:
                self._pending_device_name = device_name
                self._pending_device_id = command_device_id
                self._pending_device_enum = command_enum
                self._pending_status_device_id = status_device_id
                self._pending_status_enum = status_enum
                self._pending_open_time = open_time
                self._pending_close_time = close_time
                self._pending_invert_direction = bool(
                    user_input.get(CONF_INVERT_DIRECTION, False)
                )
                self._pairing_workflow = "manual"
                return await self.async_step_manual_next()

        return self.async_show_form(
            step_id="manual",
            data_schema=self._manual_schema(),
            errors=errors,
        )

    @staticmethod
    def _is_hex_value(value: str, length: int) -> bool:
        """Return whether value is an exact-length hexadecimal string."""
        return len(value) == length and all(
            character in "0123456789ABCDEF" for character in value
        )

    def _manual_schema(self) -> vol.Schema:
        """Build the manual form with pending values as defaults."""
        open_time_key = (
            vol.Required(
                CONF_OPEN_TIME_SECONDS,
                default=self._pending_open_time,
            )
            if self._pending_open_time is not None
            else vol.Required(CONF_OPEN_TIME_SECONDS)
        )
        close_time_key = (
            vol.Required(
                CONF_CLOSE_TIME_SECONDS,
                default=self._pending_close_time,
            )
            if self._pending_close_time is not None
            else vol.Required(CONF_CLOSE_TIME_SECONDS)
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_NAME,
                    default=self._pending_device_name or "",
                ): selector.TextSelector(),
                vol.Required(
                    CONF_DEVICE_ID,
                    default=self._pending_device_id or "",
                ): selector.TextSelector(),
                vol.Required(
                    CONF_DEVICE_ENUM,
                    default=self._pending_device_enum or "",
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_STATUS_DEVICE_ID,
                    default=self._pending_status_device_id or "",
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_STATUS_ENUM,
                    default=self._pending_status_enum or "",
                ): selector.TextSelector(),
                open_time_key: selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        step=0.1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                close_time_key: selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        step=0.1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_INVERT_DIRECTION,
                    default=self._pending_invert_direction,
                ): selector.BooleanSelector(),
            }
        )

    def _pending_data(self) -> dict[str, Any]:
        """Return config-subentry data for the pending blind."""
        assert self._pending_device_id is not None
        assert self._pending_device_enum is not None
        assert self._pending_status_device_id is not None
        assert self._pending_status_enum is not None
        assert self._pending_open_time is not None
        assert self._pending_close_time is not None
        return {
            # Legacy keys stay populated for backward compatibility.
            CONF_DEVICE_ID: self._pending_device_id,
            CONF_DEVICE_ENUM: self._pending_device_enum,
            CONF_COMMAND_DEVICE_ID: self._pending_device_id,
            CONF_COMMAND_ENUM: self._pending_device_enum,
            CONF_STATUS_DEVICE_ID: self._pending_status_device_id,
            CONF_STATUS_ENUM: self._pending_status_enum,
            CONF_OPEN_TIME: self._pending_open_time,
            CONF_CLOSE_TIME: self._pending_close_time,
            CONF_INVERT_DIRECTION: self._pending_invert_direction,
        }

    async def async_step_manual_next(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Offer command testing before saving a manually configured blind."""
        return self.async_show_menu(
            step_id="manual_next", menu_options=["test_motor", "save_manual"]
        )

    async def async_step_save_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Save the pending blind without calibration."""
        if (
            not self._pending_device_name
            or not self._pending_device_id
            or self._pending_open_time is None
            or self._pending_close_time is None
        ):
            return self.async_abort(reason="device_not_found")
        return self.async_create_entry(
            title=self._pending_device_name,
            data=self._pending_data(),
            unique_id=self._pending_device_id,
        )

    async def async_step_test_motor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a short logical-open command followed by stop."""
        if not self._pending_device_id or not self._pending_device_enum:
            return self.async_abort(reason="device_not_found")

        placeholders = {
            "device_id": self._pending_device_id,
            "device_enum": self._pending_device_enum,
        }
        if user_input is None:
            return self.async_show_form(
                step_id="test_motor",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
            )

        api = self._get_entry().runtime_data
        action = CMD_DOWN if self._pending_invert_direction else CMD_UP
        if not await api.control_blind(
            self._pending_device_enum,
            action,
            device_id=self._pending_device_id,
        ):
            return self.async_show_form(
                step_id="test_motor",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
                errors={"base": "command_failed"},
            )
        try:
            await asyncio.sleep(TEST_COMMAND_DELAY)
        finally:
            stopped = await api.control_blind(
                self._pending_device_enum,
                CMD_STOP,
                device_id=self._pending_device_id,
            )
        if not stopped:
            return self.async_show_form(
                step_id="test_motor",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
                errors={"base": "command_failed"},
            )
        return await self.async_step_did_motor_move()

    async def async_step_did_motor_move(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask whether the short command moved the motor."""
        if user_input is None:
            return self.async_show_form(
                step_id="did_motor_move",
                data_schema=vol.Schema(
                    {
                        vol.Required("motor_moved", default=True): (
                            selector.BooleanSelector()
                        )
                    }
                ),
                description_placeholders={
                    "device_id": self._pending_device_id or "unknown",
                    "device_enum": self._pending_device_enum or "unknown",
                },
            )

        if not user_input["motor_moved"]:
            if self._pairing_workflow == "existing":
                return await self.async_step_edit()
            # Reuse the manual form with the detected/current values prefilled.
            return await self.async_step_manual()

        if self._pairing_workflow == "existing":
            return self.async_abort(reason="command_test_successful")
        if self._pairing_workflow == "hybrid":
            return self.async_show_menu(
                step_id="test_success",
                menu_options=["calibration_close", "manual_times"],
            )
        return await self.async_step_save_manual()

    async def async_step_manual_times(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect travel times after a successful paired command test."""
        if user_input is not None:
            self._pending_open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            self._pending_close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])
            return await self.async_step_save_manual()

        return self.async_show_form(
            step_id="manual_times",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OPEN_TIME_SECONDS): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_CLOSE_TIME_SECONDS): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def async_step_name_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask user to provide a friendly name for the paired device."""
        device_id = self._pending_device_id
        device_enum = self._pending_device_enum

        if user_input is None:
            # Initial call - show form
            if not device_id:
                return self.async_abort(reason="pairing_failed")

            return self.async_show_form(
                step_id="name_device",
                data_schema=vol.Schema(
                    {
                        vol.Optional("device_name"): selector.TextSelector(),
                    }
                ),
                description_placeholders={
                    "device_id": device_id,
                },
            )

        # User provided a name; configure calibration state for either workflow.
        if not device_id or not device_enum:
            return self.async_abort(reason="pairing_failed")

        device_name = user_input.get("device_name") or f"Blind {device_id}"
        self._pending_device_name = device_name

        handler = self._get_calibration_handler()

        # Provide minimal device to handler
        handler.set_selected_device(
            {
                "id": self._pending_status_device_id or device_id,
                "entity_id": device_id,
                "name": device_name,
                "enum": self._pending_status_enum or device_enum,
            }
        )
        handler.enable_subentry_creation(
            device_id=device_id,
            device_enum=device_enum,
            device_name=device_name,
            status_device_id=self._pending_status_device_id or device_id,
            status_enum=self._pending_status_enum or device_enum,
            invert_direction=self._pending_invert_direction,
        )
        if self._pairing_workflow == "hybrid":
            return await self.async_step_test_motor()

        _LOGGER.debug(
            "Starting calibration for paired device %s (%s) before creating subentry",
            device_id,
            device_name,
        )
        return await self._await_subentry_result(
            handler.async_step_calibration_close(None)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose whether to edit settings or recalibrate a blind."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["edit", "test_existing", "developer_tools", "calibrate"],
        )

    def _developer_details(self) -> dict[str, Any]:
        """Return normalized protocol details for the selected blind."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        command_device_id = str(
            data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
        ).upper()
        command_enum = str(
            data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, "")
        ).upper()
        return {
            "name": subentry.title,
            "command_device_id": command_device_id,
            "command_enum": command_enum,
            "status_device_id": str(
                data.get(CONF_STATUS_DEVICE_ID) or command_device_id
            ).upper(),
            "status_enum": str(data.get(CONF_STATUS_ENUM) or command_enum).upper(),
            "invert_direction": bool(data.get(CONF_INVERT_DIRECTION, False)),
            "open_time": float(data.get(CONF_OPEN_TIME, 60.0)),
            "close_time": float(data.get(CONF_CLOSE_TIME, 60.0)),
        }

    def _developer_snapshot(
        self,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return current blind details and its latest received frame."""
        details = self._developer_details()
        api = self._get_entry().runtime_data
        last_received = api.get_last_received(
            details["status_device_id"], details["status_enum"]
        ) or {
            "device_id": "No matching frame received",
            "enum": "--",
            "command": "--",
            "time": "--",
        }
        return details, last_received

    async def async_step_developer_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show live protocol diagnostics and direct test actions."""
        details, last_received = self._developer_snapshot()
        api = self._get_entry().runtime_data
        return self.async_show_menu(
            step_id="developer_tools",
            menu_options=DEVELOPER_TOOLS_MENU_OPTIONS,
            description_placeholders={
                "selected_blind": str(details["name"]),
                "last_device_id": last_received["device_id"],
                "last_enum": last_received["enum"],
                "last_command": last_received["command"],
                "last_time": last_received["time"],
                "command_device_id": details["command_device_id"],
                "command_enum": details["command_enum"],
                "stick_connected": str(api.is_connected),
                "stick_mode": str(api.device_mode or "unknown"),
                "stick_ready": str(api.transmit_ready),
                "stick_busy": str(api.busy_latched),
                "result": self._developer_notice,
            },
        )

    async def _async_developer_command(self, command: str) -> SubentryFlowResult:
        """Send one logical command from the developer tools menu."""
        api = self._get_entry().runtime_data
        if not api.transmit_ready:
            self._developer_notice = (
                f"{command.title()} command blocked: stick is not ready "
                f"(connected={api.is_connected}, mode={api.device_mode or 'unknown'}, "
                f"busy={api.busy_latched}). Use Reset stick / reconnect serial."
            )
            return await self.async_step_developer_tools()

        details = self._developer_details()
        invert_direction = details["invert_direction"]
        action = {
            "open": CMD_DOWN if invert_direction else CMD_UP,
            "close": CMD_UP if invert_direction else CMD_DOWN,
            "stop": CMD_STOP,
        }[command]
        sent = await api.control_blind(
            details["command_enum"],
            action,
            device_id=details["command_device_id"],
        )
        self._developer_notice = (
            f"{command.title()} command queued successfully."
            if sent
            else f"{command.title()} command failed; check the integration logs."
        )
        return await self.async_step_developer_tools()

    async def async_step_test_open(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct logical-open test command."""
        return await self._async_developer_command("open")

    async def async_step_test_close(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct logical-close test command."""
        return await self._async_developer_command("close")

    async def async_step_test_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct stop test command."""
        return await self._async_developer_command("stop")

    async def async_step_reset_stick(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reset local stick state and reopen the serial connection."""
        api = self._get_entry().runtime_data
        ready = await api.reset_and_reconnect()
        self._developer_notice = (
            "Stick reset and serial reconnect completed; ready for transmit."
            if ready
            else (
                "Stick reset/reconnect did not become ready "
                f"(connected={api.is_connected}, mode={api.device_mode or 'unknown'}). "
                "Check the integration logs and USB connection."
            )
        )
        return await self.async_step_developer_tools()

    async def async_step_copy_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show a copyable text snapshot for troubleshooting."""
        if user_input is not None:
            return await self.async_step_developer_tools()

        details, last_received = self._developer_snapshot()
        api = self._get_entry().runtime_data
        diagnostics = "\n".join(
            (
                "Schellenberg USB blind diagnostics",
                f"Selected blind: {details['name']}",
                "",
                "Stick state:",
                f"Connected: {api.is_connected}",
                f"Mode: {api.device_mode or 'unknown'}",
                f"Ready: {api.transmit_ready}",
                f"Pairing active: {api.pairing_active}",
                f"Transmitter active: {api.transmitter_active}",
                f"Busy latched: {api.busy_latched}",
                "",
                "Last received:",
                f"Device ID: {last_received['device_id']}",
                f"Enum: {last_received['enum']}",
                f"Command: {last_received['command']}",
                f"Time: {last_received['time']}",
                "",
                "Current transmit target:",
                f"Device ID: {details['command_device_id']}",
                f"Enum: {details['command_enum']}",
                "",
                "Configured status identity:",
                f"Device ID: {details['status_device_id']}",
                f"Enum: {details['status_enum']}",
                f"Open time: {details['open_time']:.2f} seconds",
                f"Close time: {details['close_time']:.2f} seconds",
                f"Invert direction: {details['invert_direction']}",
            )
        )
        return self.async_show_form(
            step_id="copy_diagnostics",
            data_schema=vol.Schema(
                {
                    vol.Required("diagnostics", default=diagnostics): (
                        selector.TextSelector(
                            selector.TextSelectorConfig(multiline=True)
                        )
                    )
                }
            ),
        )

    async def async_step_test_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Load an existing blind into the short command test."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        self._pending_device_name = subentry.title
        self._pending_device_id = str(
            data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
        )
        self._pending_device_enum = str(
            data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, "")
        )
        self._pending_status_device_id = str(
            data.get(CONF_STATUS_DEVICE_ID) or self._pending_device_id
        )
        self._pending_status_enum = str(
            data.get(CONF_STATUS_ENUM) or self._pending_device_enum
        )
        self._pending_open_time = float(data.get(CONF_OPEN_TIME, 60.0))
        self._pending_close_time = float(data.get(CONF_CLOSE_TIME, 60.0))
        self._pending_invert_direction = bool(data.get(CONF_INVERT_DIRECTION, False))
        self._pairing_workflow = "existing"
        return await self.async_step_test_motor(user_input)

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a blind while preserving its subentry and entity unique IDs."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        current_data = dict(subentry.data)
        command_device_id = str(
            current_data.get(CONF_COMMAND_DEVICE_ID)
            or current_data.get(CONF_DEVICE_ID, "")
        )
        command_enum = str(
            current_data.get(CONF_COMMAND_ENUM)
            or current_data.get(CONF_DEVICE_ENUM, "")
        )
        status_device_id = str(
            current_data.get(CONF_STATUS_DEVICE_ID) or command_device_id
        )
        status_enum = str(current_data.get(CONF_STATUS_ENUM) or command_enum)
        errors: dict[str, str] = {}

        if user_input is not None:
            device_name = str(user_input[CONF_DEVICE_NAME]).strip()
            command_device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
            command_enum = str(user_input[CONF_DEVICE_ENUM]).strip().upper()
            status_device_id = (
                str(user_input.get(CONF_STATUS_DEVICE_ID, "")).strip().upper()
                or command_device_id
            )
            status_enum = (
                str(user_input.get(CONF_STATUS_ENUM, "")).strip().upper()
                or command_enum
            )
            open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"
            if not self._is_hex_value(command_device_id, 6):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(command_enum, 2):
                errors[CONF_DEVICE_ENUM] = "invalid_device_enum"
            if not self._is_hex_value(status_device_id, 6):
                errors[CONF_STATUS_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(status_enum, 2):
                errors[CONF_STATUS_ENUM] = "invalid_device_enum"
            if open_time <= 0:
                errors[CONF_OPEN_TIME_SECONDS] = "invalid_travel_time"
            if close_time <= 0:
                errors[CONF_CLOSE_TIME_SECONDS] = "invalid_travel_time"

            if any(
                candidate.subentry_id != subentry.subentry_id
                and str(
                    candidate.data.get(CONF_COMMAND_DEVICE_ID)
                    or candidate.data.get(CONF_DEVICE_ID, "")
                ).upper()
                == command_device_id
                for candidate in entry.subentries.values()
            ):
                errors[CONF_DEVICE_ID] = "already_configured"

            if not errors:
                current_data.update(
                    {
                        CONF_DEVICE_ID: command_device_id,
                        CONF_DEVICE_ENUM: command_enum,
                        CONF_COMMAND_DEVICE_ID: command_device_id,
                        CONF_COMMAND_ENUM: command_enum,
                        CONF_STATUS_DEVICE_ID: status_device_id,
                        CONF_STATUS_ENUM: status_enum,
                        CONF_OPEN_TIME: open_time,
                        CONF_CLOSE_TIME: close_time,
                        CONF_INVERT_DIRECTION: bool(
                            user_input.get(CONF_INVERT_DIRECTION, False)
                        ),
                    }
                )
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=device_name,
                    data=current_data,
                )

        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=subentry.title,
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=command_device_id,
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_DEVICE_ENUM,
                        default=command_enum,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_STATUS_DEVICE_ID,
                        default=status_device_id,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_STATUS_ENUM,
                        default=status_enum,
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_OPEN_TIME_SECONDS,
                        default=current_data.get(CONF_OPEN_TIME, 60.0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_CLOSE_TIME_SECONDS,
                        default=current_data.get(CONF_CLOSE_TIME, 60.0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_INVERT_DIRECTION,
                        default=current_data.get(CONF_INVERT_DIRECTION, False),
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_calibrate(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Run calibration for the selected blind subentry."""
        handler = self._get_calibration_handler()
        handler.disable_subentry_creation()

        subentry = self._get_reconfigure_subentry()
        command_device_id = subentry.data.get(
            CONF_COMMAND_DEVICE_ID, subentry.data.get(CONF_DEVICE_ID)
        )
        command_enum = subentry.data.get(
            CONF_COMMAND_ENUM, subentry.data.get(CONF_DEVICE_ENUM)
        )
        status_device_id = subentry.data.get(CONF_STATUS_DEVICE_ID, command_device_id)
        status_enum = subentry.data.get(CONF_STATUS_ENUM, command_enum)
        if not command_device_id or not status_device_id:
            return self.async_abort(reason="device_not_found")

        stable_id = (
            subentry.unique_id
            if isinstance(subentry.unique_id, str) and subentry.unique_id
            else command_device_id
        )
        device_name = subentry.title or f"Blind {stable_id}"
        handler.set_selected_device(
            {
                "id": status_device_id,
                "entity_id": stable_id,
                "name": device_name,
                CONF_OPEN_TIME: subentry.data.get(CONF_OPEN_TIME),
                CONF_CLOSE_TIME: subentry.data.get(CONF_CLOSE_TIME),
                CONF_INVERT_DIRECTION: subentry.data.get(CONF_INVERT_DIRECTION, False),
                "enum": status_enum,
            }
        )

        return await self._await_subentry_result(
            handler.async_step_calibration_close(user_input)
        )

    # Delegate all calibration steps to the handler
    async def async_step_calibration_close(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_close(user_input)
        )

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_open_instruction(user_input)
        )

    async def async_step_calibration_close_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_close_instruction(user_input)
        )

    async def async_step_calibration_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler (handler now creates entry)."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_complete(user_input)
        )
