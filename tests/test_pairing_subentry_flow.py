"""Direct method-level tests for SchellenbergPairingSubentryFlow's Developer
Tools commands (test_open/close/stop, reset_stick).

These exercise the flow class's own methods directly (make_flow()/
make_subentry() below), rather than driving it through Home Assistant's
real flow manager the way test_config_flow.py's
test_developer_tools_test_open_close_stop_each_send_one_command and
test_developer_tools_reset_stick already do. The two styles are
intentionally complementary - this one calls async_step_test_open() etc.
in isolation, so a failure here points straight at the flow class's own
logic without the full subentry-flow machinery in between.

Recreated after both this file and tests/test_runtime_translation_text.py were lost to
an earlier container reset and never carried forward; see CHANGELOG.md's
v1.1.0 entry for why the wording asserted below reads the way it does.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.schellenberg_usb.config_flow import (
    SchellenbergPairingSubentryFlow,
)
from custom_components.schellenberg_usb.const import (
    CMD_DOWN,
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_INVERT_DIRECTION,
    CONF_OPEN_TIME,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    SUBENTRY_TYPE_BLIND,
)

DEVICE_ID = "5D3E7C"
DEVICE_ENUM = "01"


def make_subentry(**data_overrides: Any) -> ConfigSubentry:
    """Build a real ConfigSubentry with sane defaults for a paired blind."""
    data: dict[str, Any] = {
        CONF_COMMAND_DEVICE_ID: DEVICE_ID,
        CONF_COMMAND_ENUM: DEVICE_ENUM,
        CONF_STATUS_DEVICE_ID: DEVICE_ID,
        CONF_STATUS_ENUM: DEVICE_ENUM,
        CONF_OPEN_TIME: 20.0,
        CONF_CLOSE_TIME: 18.0,
        CONF_INVERT_DIRECTION: False,
    }
    data.update(data_overrides)
    return ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Living room",
        unique_id=DEVICE_ID,
    )


def make_flow(hass: HomeAssistant) -> Any:
    """Build a flow instance with a MagicMock api wired as its runtime_data.

    flow._api is this test module's own spy, never read by production code
    directly - _get_entry() is mocked to hand back an object whose
    runtime_data is this same api, matching how _async_developer_command()
    and async_step_reset_stick() actually fetch it
    (self._get_entry().runtime_data). subentries is an empty dict for the
    same reason: a real ConfigEntry always carries that attribute (every
    other module reads entry.subentries unconditionally - cover.py,
    sensor.py, switch.py, __init__.py, diagnostics.py - none of them
    guard against it being absent), and async_step_calibrate()'s
    recalibration path now reads it too, via
    _foreign_capture_device_ids(self._get_entry(), ...), to learn which
    device ids belong to this hub's *other* paired blinds. None of this
    module's tests exercise more than one blind, so the empty default -
    "no other blinds" - is correct for all of them.
    """
    flow: Any = SchellenbergPairingSubentryFlow()
    flow.hass = hass
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    # control_blind_exclusive() is what the developer-tools test-open/
    # close/stop steps and the post-teach open+stop test actually call
    # (audit finding 7) - control_blind itself stays mocked too, since
    # this same api spy backs tests of other flow steps that still call
    # it directly.
    api.control_blind_exclusive = AsyncMock(return_value=True)
    api.reset_and_reconnect = AsyncMock(return_value=True)
    api.manual_sync_position = MagicMock(return_value=True)
    api.teach_motor = AsyncMock(return_value=True)
    api.wait_for_transmitter_idle = AsyncMock(return_value=True)
    api.send_raw_transmit = AsyncMock(return_value=True)
    api.async_discover_status_identities = AsyncMock(
        return_value={
            "primary": None,
            "secondary": [],
            "unknown_commands": [],
            "frames": [],
        }
    )
    # _developer_snapshot()/_developer_details() fall back to an empty-frame
    # dict whenever these return None, so leaving them as bare MagicMocks
    # would feed non-dict garbage into string formatting further down.
    api.get_last_received_for_identities = MagicMock(return_value=None)
    api.get_last_primary_tracking_frame = MagicMock(return_value=None)
    api.get_last_secondary_frame = MagicMock(return_value=None)
    api.get_last_position_update = MagicMock(return_value=None)
    api.get_last_manual_position_sync = MagicMock(return_value=None)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    api.transmit_block_reason = None
    flow._api = api
    flow._get_entry = MagicMock(
        return_value=SimpleNamespace(runtime_data=api, subentries={})
    )
    return flow


async def test_developer_command_open_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_test_open()

    flow._api.control_blind_exclusive.assert_awaited_once_with(
        DEVICE_ENUM, CMD_UP, device_id=DEVICE_ID, source="developer_tools"
    )
    assert result["type"] is FlowResultType.MENU
    assert "command sent successfully" in flow._developer_notice


async def test_developer_command_close_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_test_close()

    flow._api.control_blind_exclusive.assert_awaited_once_with(
        DEVICE_ENUM, CMD_DOWN, device_id=DEVICE_ID, source="developer_tools"
    )
    assert result["type"] is FlowResultType.MENU
    assert "command sent successfully" in flow._developer_notice


async def test_developer_command_stop_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_test_stop()

    flow._api.control_blind_exclusive.assert_awaited_once_with(
        DEVICE_ENUM, CMD_STOP, device_id=DEVICE_ID, source="developer_tools"
    )
    assert result["type"] is FlowResultType.MENU
    assert "command sent successfully" in flow._developer_notice


async def test_developer_command_respects_invert_direction(
    hass: HomeAssistant,
) -> None:
    """An inverted blind must swap open<->close at the protocol level."""
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(
        return_value=make_subentry(**{CONF_INVERT_DIRECTION: True})
    )

    await flow.async_step_test_open()

    flow._api.control_blind_exclusive.assert_awaited_once_with(
        DEVICE_ENUM, CMD_DOWN, device_id=DEVICE_ID, source="developer_tools"
    )


async def test_developer_command_blocked_when_transmit_blocked(
    hass: HomeAssistant,
) -> None:
    """A stick that cannot transmit right now must not even attempt a write."""
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.transmit_block_reason = "disconnected"

    result = await flow.async_step_test_open()

    flow._api.control_blind.assert_not_called()
    assert result["type"] is FlowResultType.MENU
    assert "command blocked" in flow._developer_notice


async def test_developer_command_failed_when_control_blind_returns_false(
    hass: HomeAssistant,
) -> None:
    """control_blind() refusing the write must be reported, not silently dropped."""
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.control_blind = AsyncMock(return_value=False)

    result = await flow.async_step_test_open()

    assert result["type"] is FlowResultType.MENU
    assert "command failed" in flow._developer_notice


async def test_reset_stick_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.reset_and_reconnect = AsyncMock(return_value=True)

    result = await flow.async_step_reset_stick()

    flow._api.reset_and_reconnect.assert_awaited_once()
    assert result["type"] is FlowResultType.MENU
    assert "ready to transmit" in flow._developer_notice


async def test_reset_stick_not_ready_reports_status(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.reset_and_reconnect = AsyncMock(return_value=False)
    flow._api.is_connected = False
    flow._api.device_mode = None

    result = await flow.async_step_reset_stick()

    assert result["type"] is FlowResultType.MENU
    assert "did not become ready" in flow._developer_notice
    assert "connected=No" in flow._developer_notice
    assert "mode=unknown" in flow._developer_notice


# --- Entering calibration from the reconfigure menu (async_step_calibrate) -


async def test_calibrate_aborts_when_subentry_has_no_enum(hass: HomeAssistant) -> None:
    """Regression: the guard used to only check the two device_id halves,
    so a subentry with valid device_ids but no enum data anywhere (neither
    CONF_COMMAND_ENUM/CONF_DEVICE_ENUM nor CONF_STATUS_ENUM) sailed straight
    through it. status_enum ended up None and reached
    handler.set_selected_device() with "enum": None, from there into the
    serial payload itself unchecked, instead of aborting here with a clear
    reason. See the matching comment on async_step_calibrate()'s guard in
    config_flow.py.
    """
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(
        return_value=make_subentry(**{CONF_COMMAND_ENUM: "", CONF_STATUS_ENUM: ""})
    )

    result = await flow.async_step_calibrate()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_calibrate_proceeds_when_subentry_is_well_formed(
    hass: HomeAssistant,
) -> None:
    """Sanity check for the guard above: a subentry with every device_id
    and enum field present must not be rejected by it.
    """
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_calibrate()

    assert result["type"] != FlowResultType.ABORT


# --- Manual position sync (set_position_open/closed/manual) ---------------


async def test_set_position_open_confirms_position(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_set_position_open()

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 100)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 100%" in flow._developer_notice


async def test_set_position_closed_confirms_position(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_set_position_closed()

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 0)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 0%" in flow._developer_notice


async def test_set_position_manual_shows_form_with_last_known_default(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.get_last_position_update = MagicMock(return_value={"new_position": 42})

    result = await flow.async_step_set_position_manual()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "set_position_manual"
    assert result["description_placeholders"]["current_position"] == "42%"


async def test_set_position_manual_applies_given_position(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_set_position_manual(user_input={"position": 37})

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 37)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 37%" in flow._developer_notice


async def test_manual_position_sync_reports_unregistered_when_sync_fails(
    hass: HomeAssistant,
) -> None:
    """An unregistered live cover entity must be reported, not silently dropped."""
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.manual_sync_position = MagicMock(return_value=False)

    await flow.async_step_set_position_open()

    assert "live cover entity is not registered" in flow._developer_notice


# --- Teach motor / activate USB transmitter --------------------------------


async def test_teach_motor_shows_form_first(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_teach_motor()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "teach_motor"
    flow._api.teach_motor.assert_not_called()


async def test_teach_motor_blocked_when_transmit_blocked(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.transmit_block_reason = "disconnected"

    result = await flow.async_step_teach_motor(user_input={})

    flow._api.teach_motor.assert_not_called()
    assert result["type"] is FlowResultType.MENU
    assert "Motor teach blocked" in flow._developer_notice


async def test_teach_motor_success_sends_teach_open_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def instant_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_teach_motor(user_input={})

    flow._api.teach_motor.assert_awaited_once_with(
        DEVICE_ENUM, device_id=DEVICE_ID, source="developer_tools"
    )
    assert flow._api.control_blind_exclusive.await_count == 2
    assert result["type"] is FlowResultType.MENU
    assert "please verify that the motor actually reacted" in flow._developer_notice


async def test_teach_motor_failed_when_teach_fails(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.teach_motor = AsyncMock(return_value=False)

    result = await flow.async_step_teach_motor(user_input={})

    flow._api.control_blind.assert_not_called()
    assert result["type"] is FlowResultType.MENU
    assert "Teach/test transmission failed" in flow._developer_notice


# --- Send raw RF payload ----------------------------------------------------


async def test_send_raw_command_shows_form_first(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_send_raw_command()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "send_raw_command"


async def test_send_raw_command_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_send_raw_command(
        user_input={"payload": "ss109010000"}
    )

    flow._api.send_raw_transmit.assert_awaited_once_with(
        "ss109010000", source="developer_tools"
    )
    assert result["type"] is FlowResultType.MENU
    assert "was written" in flow._developer_notice


async def test_send_raw_command_invalid_payload(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.send_raw_transmit = AsyncMock(side_effect=ValueError)

    result = await flow.async_step_send_raw_command(user_input={"payload": "bad"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"payload": "invalid_raw_payload"}


async def test_send_raw_command_transmit_failed(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.send_raw_transmit = AsyncMock(return_value=False)

    result = await flow.async_step_send_raw_command(
        user_input={"payload": "ss109010000"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "transmit_failed"}


# --- Discover status from the original remote ------------------------------


async def test_discover_status_shows_form_first(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_discover_status()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_status"
    # The first call primes the pending-identity state from the selected
    # subentry, the same way _prepare_existing_status_discovery() does for
    # every Developer Tools entry point into the shared capture flow.
    assert flow._pending_device_id == DEVICE_ID


async def test_discover_status_success_moves_to_confirmation(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.async_discover_status_identities = AsyncMock(
        return_value={
            "primary": {
                "device_id": DEVICE_ID,
                "enum": DEVICE_ENUM,
                "commands": ["up"],
                "timestamps": ["12:00:00"],
            },
            "secondary": [],
            "unknown_commands": [],
            "frames": [{}],
        }
    )

    result = await flow.async_step_discover_status(user_input={})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_status_discovery"


async def test_discover_status_unavailable_when_disconnected(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.async_discover_status_identities = AsyncMock(side_effect=ConnectionError)

    result = await flow.async_step_discover_status(user_input={})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_status"
    assert result["errors"] == {"base": "status_discovery_unavailable"}


async def test_discover_status_busy_when_capture_in_progress(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.async_discover_status_identities = AsyncMock(side_effect=RuntimeError)

    result = await flow.async_step_discover_status(user_input={})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_status"
    assert result["errors"] == {"base": "status_discovery_busy"}


async def test_confirm_status_discovery_updates_existing_subentry(
    hass: HomeAssistant,
) -> None:
    """Confirming a capture started from Developer Tools updates that subentry.

    This is the _status_discovery_updates_existing=True branch - the one
    every Developer Tools/reconfigure entry point into this shared capture
    flow takes, as opposed to the brand-new-pairing branch that instead
    calls async_create_entry().
    """
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow.async_update_and_abort = MagicMock(
        return_value={"type": FlowResultType.ABORT, "reason": "reconfigure_successful"}
    )
    await flow.async_step_discover_status()  # primes _pending_device_id etc.
    flow._apply_remote_status_discovery(
        {
            "primary": {"device_id": DEVICE_ID, "enum": DEVICE_ENUM},
            "secondary": [],
        }
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    flow.async_update_and_abort.assert_called_once()
    assert result["reason"] == "reconfigure_successful"


# --- Copy diagnostics --------------------------------------------------------


async def test_copy_diagnostics_shows_form_with_snapshot(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_copy_diagnostics()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "copy_diagnostics"
    diagnostics_text = result["data_schema"]({})["diagnostics"]
    assert "Living room" in diagnostics_text


async def test_copy_diagnostics_returns_to_menu_on_submit(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_copy_diagnostics(user_input={})

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "developer_tools"
