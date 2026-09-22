"""Tests for switch.py's SchellenbergLedSwitch entity (the USB stick LED),
against the real Home Assistant test harness rather than hand-written fakes.

Focus: async_turn_on/async_turn_off must raise HomeAssistantError and leave
the entity's on/off state unchanged when the underlying LED command fails,
instead of silently reporting success; the background-task restore path
(_restore_hardware_state) must log rather than raise on the same failure,
since nothing synchronous is waiting on it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.schellenberg_usb.switch import SchellenbergLedSwitch


def make_switch(hass: HomeAssistant) -> SchellenbergLedSwitch:
    api = MagicMock()
    api.led_on = AsyncMock(return_value=True)
    api.led_off = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_version = "1.0"

    entry = MagicMock()
    entry.entry_id = "test_entry"

    switch = SchellenbergLedSwitch(api, entry)
    switch.hass = hass
    switch.entity_id = "switch.test_led"
    # async_write_ha_state() (called by async_turn_on/off) resolves the
    # entity's translated name via self.platform_data, which is only
    # populated when an entity is added through a real EntityPlatform via
    # async_add_entities() - not the case here, where the entity is built
    # directly. That's a test-setup gap, not a production bug: real setup
    # (switch.py's async_setup_entry) always goes through async_add_entities
    # first. Clearing translation_key sidesteps that name resolution the
    # same way cover.py's own entities do (they use a plain _attr_name
    # instead of a translation_key).
    switch._attr_translation_key = None
    return switch


async def test_turn_on_success(hass: HomeAssistant) -> None:
    switch = make_switch(hass)

    await switch.async_turn_on()

    switch.api.led_on.assert_awaited_once()
    assert switch.is_on is True


async def test_turn_on_raises_and_leaves_state_off_when_command_fails(
    hass: HomeAssistant,
) -> None:
    switch = make_switch(hass)
    switch.api.led_on = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="turn on"):
        await switch.async_turn_on()

    assert switch.is_on is False


async def test_turn_off_success(hass: HomeAssistant) -> None:
    switch = make_switch(hass)
    switch._is_on = True

    await switch.async_turn_off()

    switch.api.led_off.assert_awaited_once()
    assert switch.is_on is False


async def test_turn_off_raises_and_leaves_state_on_when_command_fails(
    hass: HomeAssistant,
) -> None:
    switch = make_switch(hass)
    switch._is_on = True
    switch.api.led_off = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="turn off"):
        await switch.async_turn_off()

    assert switch.is_on is True


async def test_restore_hardware_state_logs_but_does_not_raise_on_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    switch = make_switch(hass)
    switch._is_on = True
    switch.api.led_on = AsyncMock(return_value=False)

    await switch._restore_hardware_state()  # must not raise

    assert "Failed to restore LED hardware state" in caplog.text
