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

from .blind_id import generate_blind_id
from .const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_MANUAL,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
    TEST_COMMAND_DELAY,
)
from .identities import (
    format_status_identities,
    normalize_status_identities,
    normalize_status_identity,
    parse_status_identities_text,
    serialize_status_identities,
)
from .options_flow import SchellenbergOptionsFlowHandler
from .options_flow_calibration import CalibrationFlowHandler

_LOGGER = logging.getLogger(__name__)

DEVELOPER_TOOLS_MENU_OPTIONS = {
    "test_open": "Test Open",
    "test_close": "Test Close",
    "test_stop": "Test Stop",
    "discover_status": "Discover status from original remote",
    "set_position_open": "Set position fully open",
    "set_position_closed": "Set position fully closed",
    "set_position_manual": "Set position manually",
    "teach_motor": "Teach motor / activate USB transmitter",
    "send_raw_command": "Send raw RF payload",
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
        self._pending_blind_id = generate_blind_id()
        self._pending_device_id: str | None = None
        self._pending_device_enum: str | None = None
        self._pending_device_name: str | None = None
        self._pending_status_device_id: str | None = None
        self._pending_status_enum: str | None = None
        self._pending_secondary_status_identities: list[dict[str, str]] = []
        self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        self._status_discovery_result: dict[str, Any] | None = None
        self._status_discovery_updates_existing = False
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
        self._pending_status_device_id = None
        self._pending_status_enum = None
        self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        self._pending_secondary_status_identities = []
        self._pending_device_name = None
        return await self.async_step_name_device()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect manual command and primary/secondary status identities."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_name = str(user_input[CONF_DEVICE_NAME]).strip()
            command_device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
            command_enum = str(user_input[CONF_DEVICE_ENUM]).strip().upper()
            status_device_id = (
                str(user_input.get(CONF_STATUS_DEVICE_ID, "")).strip().upper()
            )
            status_enum = str(user_input.get(CONF_STATUS_ENUM, "")).strip().upper()
            try:
                secondary_identities = parse_status_identities_text(
                    user_input.get(CONF_SECONDARY_STATUS_IDENTITIES, "")
                )
            except ValueError:
                secondary_identities = ()
                errors[CONF_SECONDARY_STATUS_IDENTITIES] = "invalid_status_identities"
            primary_identity = (
                (status_device_id, status_enum)
                if status_device_id and status_enum
                else None
            )
            secondary_identities = tuple(
                identity
                for identity in secondary_identities
                if primary_identity is None or identity != primary_identity
            )
            open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"
            if not self._is_hex_value(command_device_id, 6):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(command_enum, 2):
                errors[CONF_DEVICE_ENUM] = "invalid_device_enum"
            if bool(status_device_id) != bool(status_enum):
                errors[
                    CONF_STATUS_ENUM if status_device_id else CONF_STATUS_DEVICE_ID
                ] = "status_identity_incomplete"
            if status_device_id and not self._is_hex_value(status_device_id, 6):
                errors[CONF_STATUS_DEVICE_ID] = "invalid_device_id"
            if status_enum and not self._is_hex_value(status_enum, 2):
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
                self._pending_status_enum = status_enum or None
                self._pending_status_device_id = status_device_id or None
                self._pending_status_identity_source = (
                    STATUS_IDENTITY_SOURCE_MANUAL
                    if status_device_id and status_enum
                    else STATUS_IDENTITY_SOURCE_UNKNOWN
                )
                self._pending_secondary_status_identities = serialize_status_identities(
                    secondary_identities
                )
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
                vol.Optional(
                    CONF_SECONDARY_STATUS_IDENTITIES,
                    default=format_status_identities(
                        self._pending_secondary_status_identities
                    ),
                ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
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

        assert self._pending_open_time is not None
        assert self._pending_close_time is not None
        data: dict[str, Any] = {
            CONF_BLIND_ID: self._pending_blind_id,
            # Legacy command keys stay populated for backward compatibility.
            CONF_DEVICE_ID: self._pending_device_id,
            CONF_DEVICE_ENUM: self._pending_device_enum,
            CONF_COMMAND_DEVICE_ID: self._pending_device_id,
            CONF_COMMAND_ENUM: self._pending_device_enum,
            CONF_STATUS_IDENTITY_SOURCE: self._pending_status_identity_source,
            CONF_SECONDARY_STATUS_IDENTITIES: list(
                self._pending_secondary_status_identities
            ),
            CONF_OPEN_TIME: self._pending_open_time,
            CONF_CLOSE_TIME: self._pending_close_time,
            CONF_INVERT_DIRECTION: self._pending_invert_direction,
        }
        if (
            self._pending_status_device_id is not None
            and self._pending_status_enum is not None
        ):
            data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
            data[CONF_STATUS_ENUM] = self._pending_status_enum
        return data

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
            return await self.async_step_discover_status()

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
                # Pairing identity is used transiently to preserve the existing
                # calibration listener; it is not persisted as primary status.
                "id": device_id,
                "entity_id": device_id,
                "name": device_name,
                "enum": device_enum,
            }
        )
        handler.enable_subentry_creation(
            blind_id=self._pending_blind_id,
            device_id=device_id,
            device_enum=device_enum,
            device_name=device_name,
            status_device_id=self._pending_status_device_id,
            status_enum=self._pending_status_enum,
            secondary_status_identities=(self._pending_secondary_status_identities),
            status_identity_source=self._pending_status_identity_source,
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
        configured_source = str(data.get(CONF_STATUS_IDENTITY_SOURCE) or "legacy")
        if configured_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
            primary_identity = None
        else:
            primary_identity = normalize_status_identity(
                data.get(CONF_STATUS_DEVICE_ID) or command_device_id,
                data.get(CONF_STATUS_ENUM) or command_enum,
            )
        primary_status_device_id = (
            primary_identity[0] if primary_identity is not None else "Unknown"
        )
        primary_status_enum = (
            primary_identity[1] if primary_identity is not None else "--"
        )
        secondary_status_identities = normalize_status_identities(
            data.get(CONF_SECONDARY_STATUS_IDENTITIES)
        )
        status_identities = (
            *((primary_identity,) if primary_identity is not None else ()),
            *secondary_status_identities,
        )
        source_label = {
            STATUS_IDENTITY_SOURCE_MANUAL: "manually entered",
            STATUS_IDENTITY_SOURCE_CALIBRATION: (
                "automatically discovered during calibration"
            ),
            STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY: (
                "automatically discovered from original remote"
            ),
            STATUS_IDENTITY_SOURCE_UNKNOWN: "unknown / not discovered",
        }.get(configured_source, "legacy configuration / unverified")
        last_calibration_value = data.get(CONF_LAST_CALIBRATION)
        last_calibration = (
            last_calibration_value if isinstance(last_calibration_value, dict) else {}
        )
        calibration_frames = last_calibration.get("frames", [])
        calibration_groups = last_calibration.get("groups", [])
        calibration_frames_text = (
            "\n".join(
                f"{frame.get('time', '--')} "
                f"{frame.get('device_id', 'Unknown')}/{frame.get('enum', '--')} "
                f"cmd={frame.get('command', '--')} phase={frame.get('phase', 'unknown')}"
                for frame in calibration_frames
                if isinstance(frame, dict)
            )
            or "None recorded"
        )
        calibration_candidates_text = (
            "\n".join(
                f"{group.get('device_id', 'Unknown')}/{group.get('enum', '--')}: "
                f"{','.join(group.get('commands', []))}"
                for group in calibration_groups
                if isinstance(group, dict)
            )
            or "None"
        )
        return {
            "name": subentry.title,
            "command_device_id": command_device_id,
            "command_enum": command_enum,
            # Backward-compatible names used in existing command logs/tests.
            "status_device_id": primary_status_device_id,
            "status_enum": primary_status_enum,
            "primary_status_device_id": primary_status_device_id,
            "primary_status_enum": primary_status_enum,
            "status_identity_source": source_label,
            "secondary_status_identities": secondary_status_identities,
            "secondary_status_identities_text": (
                format_status_identities(secondary_status_identities) or "None"
            ),
            "status_identities": status_identities,
            "invert_direction": bool(data.get(CONF_INVERT_DIRECTION, False)),
            "open_time": float(data.get(CONF_OPEN_TIME, 60.0)),
            "close_time": float(data.get(CONF_CLOSE_TIME, 60.0)),
            "last_calibration_time": str(last_calibration.get("completed_at", "Never")),
            "calibration_end_reason": str(
                last_calibration.get("end_reason", "Not recorded")
            ),
            "calibration_frames_text": calibration_frames_text,
            "calibration_candidates_text": calibration_candidates_text,
        }

    def _developer_snapshot(
        self,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        """Return separated frame and position diagnostics for one blind."""
        details = self._developer_details()
        api = self._get_entry().runtime_data
        empty_frame = {
            "device_id": "No matching frame received",
            "enum": "--",
            "command": "--",
            "time": "--",
            "identity_role": "none",
            "interpreted_command": "unknown",
            "position_tracking": False,
        }
        last_matched = api.get_last_received_for_identities(
            details["status_identities"]
        ) or dict(empty_frame)
        last_primary = api.get_last_primary_tracking_frame(
            details["primary_status_device_id"],
            details["primary_status_enum"],
        ) or dict(empty_frame)
        last_secondary = api.get_last_secondary_frame(
            details["secondary_status_identities"]
        ) or dict(empty_frame)
        last_position = api.get_last_position_update(details["command_device_id"]) or {
            "source": "No position update recorded",
            "direction": "--",
            "previous_position": None,
            "new_position": None,
            "status": "--",
            "time": "--",
        }
        last_manual_sync = api.get_last_manual_position_sync(
            details["command_device_id"]
        )
        if not isinstance(last_manual_sync, dict):
            last_manual_sync = {
                "new_position": None,
                "time": "Never",
            }
        return (
            details,
            last_matched,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        )

    async def async_step_developer_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show live protocol diagnostics and direct test actions."""
        (
            details,
            last_received,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        ) = self._developer_snapshot()
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
                "primary_status_device_id": details["primary_status_device_id"],
                "primary_status_enum": details["primary_status_enum"],
                "status_identity_source": details["status_identity_source"],
                "last_calibration_time": details["last_calibration_time"],
                "calibration_end_reason": details["calibration_end_reason"],
                "calibration_frames": details["calibration_frames_text"],
                "calibration_candidates": details["calibration_candidates_text"],
                "secondary_status_identities": details[
                    "secondary_status_identities_text"
                ],
                "last_identity_role": last_received["identity_role"],
                "last_interpretation": last_received["interpreted_command"],
                "last_position_tracking": str(last_received["position_tracking"]),
                "primary_last_device_id": last_primary["device_id"],
                "primary_last_enum": last_primary["enum"],
                "primary_last_command": last_primary["command"],
                "primary_last_interpretation": last_primary["interpreted_command"],
                "primary_last_time": last_primary["time"],
                "secondary_last_device_id": last_secondary["device_id"],
                "secondary_last_enum": last_secondary["enum"],
                "secondary_last_command": last_secondary["command"],
                "secondary_last_interpretation": last_secondary["interpreted_command"],
                "secondary_last_time": last_secondary["time"],
                "position_source": last_position["source"],
                "position_direction": last_position["direction"],
                "position_previous": (
                    "--"
                    if last_position["previous_position"] is None
                    else f"{last_position['previous_position']}%"
                ),
                "position_new": (
                    "--"
                    if last_position["new_position"] is None
                    else f"{last_position['new_position']}%"
                ),
                "position_status": last_position["status"],
                "position_time": last_position["time"],
                "current_position": (
                    "--"
                    if last_position["new_position"] is None
                    else f"{last_position['new_position']}%"
                ),
                "last_manual_sync_time": last_manual_sync["time"],
                "position_confidence": (
                    "manually confirmed"
                    if last_position["status"] == "confirmed/manual"
                    else (
                        "estimated"
                        if last_position["new_position"] is not None
                        else "unknown"
                    )
                ),
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
        details = self._developer_details()
        _LOGGER.warning(
            "Developer Tools command clicked selected_blind=%s "
            "command_requested=%s command_device_id=%s command_enum=%s "
            "status_device_id=%s status_enum=%s secondary_statuses=%s "
            "stick_connected=%s stick_mode=%s stick_ready=%s pairing=%s "
            "transmitter_active=%s "
            "busy_latched=%s",
            details["name"],
            command,
            details["command_device_id"],
            details["command_enum"],
            details["status_device_id"],
            details["status_enum"],
            details["secondary_status_identities_text"],
            api.is_connected,
            api.device_mode or "unknown",
            api.transmit_ready,
            api.pairing_active,
            api.transmitter_active,
            api.busy_latched,
        )

        if reason := api.transmit_block_reason:
            _LOGGER.error(
                "Developer Tools command blocked selected_blind=%s "
                "command_requested=%s reason=%s",
                details["name"],
                command,
                reason,
            )
            self._developer_notice = (
                f"{command.title()} command blocked: {reason}. "
                "Use Reset stick / reconnect serial if the condition does not clear."
            )
            return await self.async_step_developer_tools()

        invert_direction = details["invert_direction"]
        action = {
            "open": CMD_DOWN if invert_direction else CMD_UP,
            "close": CMD_UP if invert_direction else CMD_DOWN,
            "stop": CMD_STOP,
        }[command]
        _LOGGER.warning(
            "Developer Tools command dispatching selected_blind=%s "
            "command_requested=%s protocol_action=%s command_device_id=%s "
            "command_enum=%s",
            details["name"],
            command,
            action,
            details["command_device_id"],
            details["command_enum"],
        )
        try:
            sent = await api.control_blind(
                details["command_enum"],
                action,
                device_id=details["command_device_id"],
                source="developer_tools",
            )
        except Exception:
            _LOGGER.exception(
                "Developer Tools command raised an exception selected_blind=%s "
                "command_requested=%s",
                details["name"],
                command,
            )
            sent = False

        if sent:
            _LOGGER.warning(
                "Developer Tools command result selected_blind=%s "
                "command_requested=%s result=written_awaiting_ack",
                details["name"],
                command,
            )
            self._developer_notice = f"{command.title()} command written successfully."
        else:
            _LOGGER.error(
                "Developer Tools command result selected_blind=%s "
                "command_requested=%s result=failed",
                details["name"],
                command,
            )
            self._developer_notice = (
                f"{command.title()} command failed; check the integration logs."
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

    async def _async_manual_position_sync(self, position: int) -> SubentryFlowResult:
        """Apply one Developer Tools position correction to the live cover."""
        api = self._get_entry().runtime_data
        details = self._developer_details()
        _LOGGER.warning(
            "Developer Tools manual position sync clicked selected_blind=%s "
            "command_device_id=%s position=%d",
            details["name"],
            details["command_device_id"],
            position,
        )
        try:
            synced = api.manual_sync_position(details["command_device_id"], position)
        except (TypeError, ValueError):
            _LOGGER.exception(
                "Developer Tools manual position sync rejected "
                "selected_blind=%s position=%s",
                details["name"],
                position,
            )
            synced = False

        if synced:
            self._developer_notice = (
                f"Position manually confirmed at {position}%. No RF command was sent."
            )
        else:
            self._developer_notice = (
                "Manual position sync failed because the live cover entity is not "
                "registered. Reload the integration and try again."
            )
        return await self.async_step_developer_tools()

    async def async_step_set_position_open(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manually confirm that the blind is fully open."""
        return await self._async_manual_position_sync(100)

    async def async_step_set_position_closed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manually confirm that the blind is fully closed."""
        return await self._async_manual_position_sync(0)

    async def async_step_set_position_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Accept and apply an exact manual blind position."""
        if user_input is not None:
            return await self._async_manual_position_sync(int(user_input["position"]))

        *_, last_position, _last_manual_sync = self._developer_snapshot()
        default_position = last_position["new_position"]
        if not isinstance(default_position, int):
            default_position = 50
        return self.async_show_form(
            step_id="set_position_manual",
            data_schema=vol.Schema(
                {
                    vol.Required("position", default=default_position): (
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0,
                                max=100,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        )
                    )
                }
            ),
            description_placeholders={
                "selected_blind": str(self._developer_details()["name"]),
                "current_position": f"{default_position}%",
            },
        )

    def _prepare_existing_status_discovery(self) -> None:
        """Load the selected persisted blind as a discovery update target."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        self._pending_device_name = subentry.title
        self._pending_device_id = str(
            data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
        ).upper()
        self._pending_device_enum = (
            str(data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, ""))
            .upper()
            .zfill(2)
        )
        self._status_discovery_updates_existing = True

    def _apply_remote_status_discovery(self, result: dict[str, Any]) -> None:
        """Apply one guided capture result without aliasing transmit identity."""
        self._status_discovery_result = result
        primary = result.get("primary")
        if primary is None:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        else:
            self._pending_status_device_id = str(primary["device_id"])
            self._pending_status_enum = str(primary["enum"])
            self._pending_status_identity_source = (
                STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
            )
        self._pending_secondary_status_identities = [
            {"device_id": str(group["device_id"]), "enum": str(group["enum"])}
            for group in result.get("secondary", [])
        ]

    def _status_discovery_placeholders(self) -> dict[str, str]:
        """Format a complete confirmation summary without requiring log access."""
        result = self._status_discovery_result or {}
        primary = result.get("primary")
        secondary = result.get("secondary", [])
        unknown = result.get("unknown_commands", [])
        return {
            "command_identity": (
                f"{self._pending_device_id or 'Unknown'}/"
                f"{self._pending_device_enum or '--'}"
            ),
            "primary_identity": (
                f"{primary['device_id']}/{primary['enum']}"
                if primary is not None
                else "Not discovered"
            ),
            "primary_commands": (
                ", ".join(primary.get("commands", []))
                if primary is not None
                else "None"
            ),
            "primary_timestamps": (
                ", ".join(primary.get("timestamps", []))
                if primary is not None
                else "None"
            ),
            "secondary_identities": (
                "\n".join(
                    f"{group['device_id']}/{group['enum']}: "
                    f"{','.join(group.get('commands', []))}"
                    for group in secondary
                )
                or "None"
            ),
            "unknown_commands": (
                "\n".join(
                    f"{group['device_id']}/{group['enum']}: "
                    f"{','.join(group.get('commands', []))}"
                    for group in unknown
                )
                or "None"
            ),
            "position_tracking": (
                "Available from received 00/01/02 status frames"
                if primary is not None
                else (
                    "No remote/status tracking identity was discovered. The blind "
                    "can still be controlled, but position tracking will use Home "
                    "Assistant commands only."
                )
            ),
            "frame_count": str(len(result.get("frames", []))),
        }

    async def async_step_discover_status(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Guide an original-remote sequence and capture all received identities."""
        if not self._pending_device_id:
            self._prepare_existing_status_discovery()
        if user_input is None:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "selected_blind": self._pending_device_name or "Blind",
                    "command_identity": (
                        f"{self._pending_device_id or 'Unknown'}/"
                        f"{self._pending_device_enum or '--'}"
                    ),
                },
            )

        api = self._get_entry().runtime_data
        try:
            result = await api.async_discover_status_identities()
        except ConnectionError:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                errors={"base": "status_discovery_unavailable"},
                description_placeholders={
                    "selected_blind": self._pending_device_name or "Blind",
                    "command_identity": (
                        f"{self._pending_device_id or 'Unknown'}/"
                        f"{self._pending_device_enum or '--'}"
                    ),
                },
            )
        except RuntimeError:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                errors={"base": "status_discovery_busy"},
                description_placeholders={
                    "selected_blind": self._pending_device_name or "Blind",
                    "command_identity": (
                        f"{self._pending_device_id or 'Unknown'}/"
                        f"{self._pending_device_enum or '--'}"
                    ),
                },
            )
        self._apply_remote_status_discovery(result)
        return await self.async_step_confirm_status_discovery()

    async def async_step_confirm_status_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm detected primary/secondary identities before persistence."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm_status_discovery",
                data_schema=vol.Schema({}),
                description_placeholders=self._status_discovery_placeholders(),
            )

        if self._status_discovery_updates_existing:
            entry = self._get_entry()
            subentry = self._get_reconfigure_subentry()
            data = dict(subentry.data)
            data[CONF_STATUS_IDENTITY_SOURCE] = self._pending_status_identity_source
            data[CONF_SECONDARY_STATUS_IDENTITIES] = list(
                self._pending_secondary_status_identities
            )
            if (
                self._pending_status_device_id is not None
                and self._pending_status_enum is not None
            ):
                data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
                data[CONF_STATUS_ENUM] = self._pending_status_enum
            else:
                data.pop(CONF_STATUS_DEVICE_ID, None)
                data.pop(CONF_STATUS_ENUM, None)
            return self.async_update_and_abort(entry, subentry, data=data)

        if (
            not self._pending_device_name
            or self._pending_open_time is None
            or self._pending_close_time is None
        ):
            return self.async_abort(reason="device_not_found")
        return self.async_create_entry(
            title=self._pending_device_name,
            data=self._pending_data(),
            unique_id=self._pending_device_id,
        )

    async def async_step_teach_motor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Teach the USB transmitter to a motor, then send Open and Stop."""
        details = self._developer_details()
        if user_input is None:
            return self.async_show_form(
                step_id="teach_motor",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "selected_blind": str(details["name"]),
                    "command_device_id": details["command_device_id"],
                    "command_enum": details["command_enum"],
                },
            )

        api = self._get_entry().runtime_data
        if reason := api.transmit_block_reason:
            _LOGGER.error(
                "Motor teach blocked selected_blind=%s reason=%s",
                details["name"],
                reason,
            )
            self._developer_notice = f"Motor teach blocked: {reason}."
            return await self.async_step_developer_tools()

        _LOGGER.warning(
            "Motor teach requested selected_blind=%s command_device_id=%s "
            "command_enum=%s status_device_id=%s status_enum=%s",
            details["name"],
            details["command_device_id"],
            details["command_enum"],
            details["status_device_id"],
            details["status_enum"],
        )
        try:
            taught = await api.teach_motor(
                details["command_enum"],
                device_id=details["command_device_id"],
                source="developer_tools",
            )
            opened = False
            stopped = False
            if taught:
                open_action = CMD_DOWN if details["invert_direction"] else CMD_UP
                opened = await api.control_blind(
                    details["command_enum"],
                    open_action,
                    device_id=details["command_device_id"],
                    source="developer_tools",
                )
                if opened:
                    await asyncio.sleep(TEST_COMMAND_DELAY)
                    stopped = await api.control_blind(
                        details["command_enum"],
                        CMD_STOP,
                        device_id=details["command_device_id"],
                        source="developer_tools",
                    )
        except Exception:
            _LOGGER.exception(
                "Motor teach/test raised an exception selected_blind=%s",
                details["name"],
            )
            taught = opened = stopped = False

        if taught and opened and stopped:
            self._developer_notice = (
                "Teach, Open, and Stop were transmitted. Stick ACKs confirm only "
                "radio transmission; verify that the motor reacted."
            )
        else:
            self._developer_notice = (
                "Teach/test transmission failed; inspect the integration logs."
            )
        return await self.async_step_developer_tools()

    async def async_step_send_raw_command(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Validate and send an exact Schellenberg RF command payload."""
        details = self._developer_details()
        default_payload = f"ss{details['command_enum']}9{CMD_UP}0000"
        errors: dict[str, str] = {}
        if user_input is not None:
            payload = str(user_input.get("payload", "")).strip()
            _LOGGER.warning(
                "Developer Tools raw RF requested selected_blind=%s payload=%s",
                details["name"],
                payload,
            )
            try:
                sent = await self._get_entry().runtime_data.send_raw_transmit(
                    payload, source="developer_tools"
                )
            except ValueError:
                sent = False
                errors["payload"] = "invalid_raw_payload"
            if sent:
                self._developer_notice = (
                    f"Raw payload {payload} was written. Stick ACKs do not confirm "
                    "motor movement."
                )
                return await self.async_step_developer_tools()
            if not errors:
                errors["base"] = "transmit_failed"

        return self.async_show_form(
            step_id="send_raw_command",
            data_schema=vol.Schema(
                {
                    vol.Required("payload", default=default_payload): (
                        selector.TextSelector()
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "selected_blind": str(details["name"]),
                "command_enum": details["command_enum"],
            },
        )

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

        (
            details,
            last_received,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        ) = self._developer_snapshot()
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
                "ACK semantics:",
                "t1/t0 confirm only that the USB stick transmitter turned on/off.",
                "Motor reception and movement remain unverified (unidirectional RF).",
                "",
                "Last matched frame:",
                f"Device ID: {last_received['device_id']}",
                f"Enum: {last_received['enum']}",
                f"Identity role: {last_received['identity_role']}",
                f"Command: {last_received['command']}",
                f"Interpretation: {last_received['interpreted_command']}",
                f"Position tracking: {last_received['position_tracking']}",
                f"Time: {last_received['time']}",
                "",
                "Last primary tracking frame:",
                f"Device ID: {last_primary['device_id']}",
                f"Enum: {last_primary['enum']}",
                f"Command: {last_primary['command']}",
                f"Interpretation: {last_primary['interpreted_command']}",
                f"Time: {last_primary['time']}",
                "",
                "Last secondary frame:",
                f"Device ID: {last_secondary['device_id']}",
                f"Enum: {last_secondary['enum']}",
                f"Command: {last_secondary['command']}",
                f"Interpretation: {last_secondary['interpreted_command']}",
                f"Time: {last_secondary['time']}",
                "",
                "Last position update:",
                f"Source: {last_position['source']}",
                f"Direction: {last_position['direction']}",
                f"Previous position: {last_position['previous_position']}",
                f"New position: {last_position['new_position']}",
                f"Status: {last_position['status']}",
                f"Time: {last_position['time']}",
                "",
                "Position confidence:",
                f"Current estimated position: {last_position['new_position']}",
                f"Last manual sync time: {last_manual_sync['time']}",
                "Estimated or manually confirmed: "
                + (
                    "manually confirmed"
                    if last_position["status"] == "confirmed/manual"
                    else (
                        "estimated"
                        if last_position["new_position"] is not None
                        else "unknown"
                    )
                ),
                "",
                "Current transmit target:",
                f"Device ID: {details['command_device_id']}",
                f"Enum: {details['command_enum']}",
                "",
                "Configured primary status identity:",
                f"Device ID: {details['primary_status_device_id']}",
                f"Enum: {details['primary_status_enum']}",
                f"Source: {details['status_identity_source']}",
                "Configured secondary status identities:",
                details["secondary_status_identities_text"],
                "",
                "Last calibration run:",
                f"Completed: {details['last_calibration_time']}",
                f"End reason: {details['calibration_end_reason']}",
                "Frames observed during calibration:",
                details["calibration_frames_text"],
                "Candidate status identities:",
                details["calibration_candidates_text"],
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
        self._pending_secondary_status_identities = serialize_status_identities(
            normalize_status_identities(data.get(CONF_SECONDARY_STATUS_IDENTITIES))
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
        current_status_source = current_data.get(CONF_STATUS_IDENTITY_SOURCE)
        if current_status_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
            status_device_id = str(current_data.get(CONF_STATUS_DEVICE_ID) or "")
            status_enum = str(current_data.get(CONF_STATUS_ENUM) or "")
        else:
            status_device_id = str(
                current_data.get(CONF_STATUS_DEVICE_ID) or command_device_id
            )
            status_enum = str(current_data.get(CONF_STATUS_ENUM) or command_enum)
        secondary_status_text = format_status_identities(
            current_data.get(CONF_SECONDARY_STATUS_IDENTITIES)
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            device_name = str(user_input[CONF_DEVICE_NAME]).strip()
            command_device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
            command_enum = str(user_input[CONF_DEVICE_ENUM]).strip().upper()
            status_device_id = (
                str(user_input.get(CONF_STATUS_DEVICE_ID, "")).strip().upper()
            )
            status_enum = str(user_input.get(CONF_STATUS_ENUM, "")).strip().upper()
            secondary_status_text = str(
                user_input.get(CONF_SECONDARY_STATUS_IDENTITIES, "")
            )
            try:
                secondary_identities = parse_status_identities_text(
                    secondary_status_text
                )
            except ValueError:
                secondary_identities = ()
                errors[CONF_SECONDARY_STATUS_IDENTITIES] = "invalid_status_identities"
            primary_identity = (
                (status_device_id, status_enum)
                if status_device_id and status_enum
                else None
            )
            secondary_identities = tuple(
                identity
                for identity in secondary_identities
                if primary_identity is None or identity != primary_identity
            )
            open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"
            if not self._is_hex_value(command_device_id, 6):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(command_enum, 2):
                errors[CONF_DEVICE_ENUM] = "invalid_device_enum"
            if bool(status_device_id) != bool(status_enum):
                errors[
                    CONF_STATUS_ENUM if status_device_id else CONF_STATUS_DEVICE_ID
                ] = "status_identity_incomplete"
            if status_device_id and not self._is_hex_value(status_device_id, 6):
                errors[CONF_STATUS_DEVICE_ID] = "invalid_device_id"
            if status_enum and not self._is_hex_value(status_enum, 2):
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
                        CONF_STATUS_IDENTITY_SOURCE: (
                            STATUS_IDENTITY_SOURCE_MANUAL
                            if status_device_id and status_enum
                            else STATUS_IDENTITY_SOURCE_UNKNOWN
                        ),
                        CONF_SECONDARY_STATUS_IDENTITIES: (
                            serialize_status_identities(secondary_identities)
                        ),
                        CONF_OPEN_TIME: open_time,
                        CONF_CLOSE_TIME: close_time,
                        CONF_INVERT_DIRECTION: bool(
                            user_input.get(CONF_INVERT_DIRECTION, False)
                        ),
                    }
                )
                if status_device_id and status_enum:
                    current_data[CONF_STATUS_DEVICE_ID] = status_device_id
                    current_data[CONF_STATUS_ENUM] = status_enum
                else:
                    current_data.pop(CONF_STATUS_DEVICE_ID, None)
                    current_data.pop(CONF_STATUS_ENUM, None)
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
                    vol.Optional(
                        CONF_SECONDARY_STATUS_IDENTITIES,
                        default=secondary_status_text,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
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
