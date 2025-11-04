"""Options flow for Schellenberg USB hub.

Hub options allow changing the USB serial port path. Calibration is handled
exclusively during blind subentry pairing and not exposed here.
"""

from __future__ import annotations

import logging
from typing import Any

import serial
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_SERIAL_PORT

_LOGGER = logging.getLogger(__name__)


class SchellenbergOptionsFlowHandler(OptionsFlow):
    """Handle hub options (edit serial port)."""

    def __init__(self) -> None:
        """Initialize hub options flow state."""
        self._errors: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the USB serial port."""
        self._errors = {}
        current_port = self.config_entry.data.get(CONF_SERIAL_PORT, "/dev/ttyUSB0")
        if user_input is not None:
            new_port = user_input[CONF_SERIAL_PORT]
            if new_port != current_port:
                try:
                    serial_conn = serial.Serial(new_port)
                    serial_conn.close()
                except serial.SerialException:
                    _LOGGER.error(
                        "Failed to open serial port %s during options save", new_port
                    )
                    self._errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error validating port %s", new_port)
                    self._errors["base"] = "unknown"
                else:
                    # Update entry data and reload if changed
                    updated = {**self.config_entry.data, CONF_SERIAL_PORT: new_port}
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=updated
                    )
                    # Schedule reload for new port usage
                    self.hass.config_entries.async_schedule_reload(
                        self.config_entry.entry_id
                    )
                    return self.async_create_entry(title="", data={})
            else:
                # No change
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SERIAL_PORT, default=current_port
                    ): selector.TextSelector(),
                }
            ),
            errors=self._errors,
        )

    @callback
    def async_get_options_flow(self):
        """Return self (options flow factory compatibility)."""
        return self
