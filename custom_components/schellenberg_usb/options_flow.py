"""Config flow for Schellenberg USB options.

This module provides the main options flow handler which delegates
to specialized handlers for calibration functionality.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow

from .options_flow_calibration import CalibrationFlowHandler


class SchellenbergOptionsFlowHandler(OptionsFlow):
    """Handle options for Schellenberg USB.

    This class serves as the main entry point and delegates specific
    functionality to the calibration handler.
    """

    def __init__(self) -> None:
        """Initialize the options flow."""
        self.calibration_handler = CalibrationFlowHandler(self)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show device calibration form directly."""
        return await self.calibration_handler.async_step_calibration(user_input)

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
