"""LED command options flow handlers for Schellenberg USB."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow


class LedCommandsFlowHandler:
    """Handle LED command options flow steps."""

    def __init__(self, flow: OptionsFlow) -> None:
        """Initialize the LED commands flow handler."""
        self.flow = flow

    async def async_step_led_commands(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show LED command submenu."""
        return self.flow.async_show_menu(
            step_id="led_commands",
            menu_options={"led_blink": "Blink LED"},
        )

    async def async_step_led_blink(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Blink LED 5 times."""
        if user_input is not None:
            api = self.flow.config_entry.runtime_data
            # Send blink command (5 blinks)
            await api.led_blink(5)
            # Wait for blink cycle to complete (5 blinks × 400ms = 2 seconds)
            await asyncio.sleep(2)
            # Turn off LED to stop continuous blinking
            await api.led_off()
            return self.flow.async_create_entry(title="", data={})

        return self.flow.async_show_form(
            step_id="led_blink",
            data_schema=vol.Schema({vol.Optional("confirm", default=True): bool}),
        )
