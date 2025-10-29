"""Config flow for Schellenberg USB integration."""

from __future__ import annotations

import logging
from typing import Any

import serial  # NOTE: blocking open used only to sanity-check connectivity
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .const import CONF_DEVICE_NAME, CONF_SERIAL_PORT, DOMAIN
from .options_flow import SchellenbergOptionsFlowHandler
from .options_flow_calibration import CalibrationFlowHandler

_LOGGER = logging.getLogger(__name__)


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

    @staticmethod
    @callback
    def async_get_reconfigure_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SchellenbergReconfigureFlow:
        """Get the reconfigure flow for device calibration."""
        return SchellenbergReconfigureFlow(config_entry)

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_port: str | None = None
        self._discovered_title: str | None = None
        self._discovered_unique: str | None = None
        self._device_id: str | None = None
        self._device_enum: str | None = None

    # -------------------------
    # MENU FLOW (Hub or Device)
    # -------------------------
    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show menu to set up hub or pair device."""
        # Check if there's already a hub configured
        existing_entries = self._async_current_entries()
        hub_exists = any(
            entry.data.get(CONF_SERIAL_PORT) is not None for entry in existing_entries
        )

        if hub_exists:
            # Hub exists, only show pairing option
            return await self.async_step_pair_device_menu()

        # No hub, show setup hub option
        return self.async_show_menu(
            step_id="menu",
            menu_options=["user", "pair_device_menu"],
        )

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

    # -------------------------
    # PAIRING FLOW
    # -------------------------
    async def async_step_pair_device_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show pairing menu."""
        if user_input is not None:
            return await self.async_step_pair_device()

        return self.async_show_form(
            step_id="pair_device_menu",
            data_schema=vol.Schema({}),
        )

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair a new device and wait for response."""
        errors = {}

        # Find the hub entry to get the API
        existing_entries = self._async_current_entries()
        hub_entry = None
        for entry in existing_entries:
            if entry.data.get(CONF_SERIAL_PORT) is not None:
                hub_entry = entry
                break

        if not hub_entry or not hub_entry.runtime_data:
            return self.async_abort(reason="no_hub")

        # Get the API from the hub entry
        api = hub_entry.runtime_data

        # Initiate pairing and wait for response (up to 10 seconds)
        pairing_result = await api.pair_device_and_wait()

        if pairing_result is None:
            # Pairing timeout - show error
            errors["base"] = "pairing_timeout"
            return self.async_show_form(
                step_id="pair_device_menu",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        # Pairing successful! Store device_id and device_enum, then ask for friendly name
        self._device_id, self._device_enum = pairing_result
        return await self.async_step_name_device()

    async def async_step_name_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask user to provide a friendly name for the paired device."""
        if user_input is not None:
            # User provided a name or left it empty
            device_name = user_input.get(CONF_DEVICE_NAME) or f"Blind {self._device_id}"

            # Find the hub entry
            existing_entries = self._async_current_entries()
            hub_entry = None
            for entry in existing_entries:
                if entry.data.get(CONF_SERIAL_PORT) is not None:
                    hub_entry = entry
                    break

            if not hub_entry:
                return self.async_abort(reason="no_hub")

            # Call the handle_new_device_no_reload function to save without reloading
            handle_new_device_no_reload = self.hass.data.get(
                "schellenberg_usb", {}
            ).get("handle_new_device_no_reload")
            if handle_new_device_no_reload:
                await handle_new_device_no_reload(
                    self._device_id, device_name, self._device_enum
                )
                # Reload hub entry to create the entity
                await self.hass.config_entries.async_reload(hub_entry.entry_id)

            # Create entry for this device
            await self.async_set_unique_id(f"{hub_entry.entry_id}_{self._device_id}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=device_name,
                data={
                    "device_id": self._device_id,
                    "device_enum": self._device_enum,
                    "hub_entry_id": hub_entry.entry_id,
                },
            )

        return self.async_show_form(
            step_id="name_device",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICE_NAME): selector.TextSelector(),
                }
            ),
            description_placeholders={
                "device_id": self._device_id or "unknown",
            },
        )


class SchellenbergReconfigureFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle reconfiguration (calibration) of a paired device."""

    VERSION = 1

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the reconfigure flow."""
        self.config_entry = config_entry
        self.calibration_handler = CalibrationFlowHandler(self)  # type: ignore[arg-type]

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the calibration flow for this device."""
        # Set the device ID from the config entry so calibration knows which device
        device_id = self.config_entry.data.get("device_id")
        if not device_id:
            return self.async_abort(reason="device_not_found")

        # Set the selected device for the calibration handler
        await self.calibration_handler.set_device_by_id(device_id)

        # Start the calibration process
        return await self.calibration_handler.async_step_calibration_close(user_input)

    # Delegate all calibration steps to the handler
    async def async_step_calibration_close(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration_close(user_input)

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration_open_instruction(
            user_input
        )

    async def async_step_calibration_close_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration_close_instruction(
            user_input
        )

    async def async_step_calibration_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration_complete(
            user_input
        )

    def async_create_entry(
        self,
        *,
        title: str | None = None,
        data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Override to use update_reload_and_abort for reconfigure flows."""
        # For reconfigure flow, we don't create a new entry, we just end the flow
        return self.async_abort(reason="reconfigure_successful")
