"""Pairing options flow handlers for Schellenberg USB."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers import config_validation as cv

from .const import CONF_DEVICE_NAME


class PairingFlowHandler:
    """Handle pairing options flow steps."""

    def __init__(self, flow: OptionsFlow) -> None:
        """Initialize the pairing flow handler."""
        self.flow = flow
        self._device_id: str | None = None
        self._device_enum: str | None = None

    def get_last_paired_device_id(self) -> str | None:
        """Return the last paired device ID for calibration flow."""
        return self._device_id

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Navigate to pairing interface."""
        if user_input is not None:
            # User clicked "Pair" button, initiate pairing
            return await self.async_step_pair_device()

        return self.flow.async_show_form(
            step_id="pairing",
            data_schema=vol.Schema({}),
        )

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair a new device and wait for response."""
        errors = {}

        # Get the API from the config entry runtime data
        api = self.flow.config_entry.runtime_data

        # Initiate pairing and wait for response (up to 10 seconds)
        pairing_result = await api.pair_device_and_wait()

        if pairing_result is None:
            # Pairing timeout - show error and go back to init
            errors["base"] = "pairing_timeout"
            return self.flow.async_show_form(
                step_id="init",
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

            # Call the handle_new_device_no_reload function to save without reloading
            hass = self.flow.hass
            handle_new_device_no_reload = hass.data["schellenberg_usb"].get(
                "handle_new_device_no_reload"
            )
            if handle_new_device_no_reload:
                await handle_new_device_no_reload(
                    self._device_id, device_name, self._device_enum
                )
                # Reload to create the entity (wait for it to complete)
                entry = self.flow.config_entry
                await hass.config_entries.async_reload(entry.entry_id)

            # Pairing complete - end the flow
            return self.flow.async_create_entry(title="", data={})

        return self.flow.async_show_form(
            step_id="name_device",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICE_NAME): cv.string,
                }
            ),
            description_placeholders={
                "device_id": self._device_id,
            },
        )
