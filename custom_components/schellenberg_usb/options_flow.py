"""Config flow for Schellenberg USB options.

This module provides the main options flow handler which delegates
to specialized handlers for different functional areas:
- LED commands (options_flow_led.py)
- Calibration (options_flow_calibration.py)
- Pairing (options_flow_pairing.py)
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow

from .options_flow_calibration import CalibrationFlowHandler
from .options_flow_led import LedCommandsFlowHandler
from .options_flow_pairing import PairingFlowHandler


class SchellenbergOptionsFlowHandler(OptionsFlow):
    """Handle options for Schellenberg USB.

    This class serves as the main entry point and delegates specific
    functionality to specialized handler classes.
    """

    def __init__(self) -> None:
        """Initialize the options flow."""
        self.led_handler = LedCommandsFlowHandler(self)
        self.calibration_handler = CalibrationFlowHandler(self)
        self.pairing_handler = PairingFlowHandler(self)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show main menu with options."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "led_commands": "LED commands",
                "calibration": "Device calibration",
                "pairing": "Pair new device",
            },
        )

    # LED Commands delegation
    async def async_step_led_commands(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to LED commands handler."""
        return await self.led_handler.async_step_led_commands(user_input)

    async def async_step_led_on(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to LED commands handler."""
        return await self.led_handler.async_step_led_commands(user_input)

    async def async_step_led_off(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to LED commands handler."""
        return await self.led_handler.async_step_led_commands(user_input)

    async def async_step_led_blink(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to LED commands handler."""
        return await self.led_handler.async_step_led_blink(user_input)

    # Calibration delegation
    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration(user_input)

    async def async_step_calibration_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration(user_input)

    async def async_step_calibration_run(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler."""
        return await self.calibration_handler.async_step_calibration_complete(
            user_input
        )

    async def async_step_calibration_after_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to calibration handler for newly paired device."""
        return await self.calibration_handler.async_step_calibration_after_pairing(
            user_input
        )

    # Pairing delegation
    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to pairing handler."""
        return await self.pairing_handler.async_step_pairing(user_input)

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to pairing handler."""
        return await self.pairing_handler.async_step_pair_device(user_input)

    async def async_step_name_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delegate to pairing handler."""
        return await self.pairing_handler.async_step_name_device(user_input)
