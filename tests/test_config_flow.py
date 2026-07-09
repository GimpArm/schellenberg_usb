"""Tests for Schellenberg USB blind subentry flows."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntries,
    ConfigSubentry,
    ConfigSubentryFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.config_flow import (
    DEVELOPER_TOOLS_MENU_OPTIONS,
    SchellenbergPairingSubentryFlow,
)
from custom_components.schellenberg_usb.const import (
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
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_MANUAL,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
)
from custom_components.schellenberg_usb.cover import SchellenbergCover
from custom_components.schellenberg_usb.options_flow_calibration import (
    CalibrationFlowHandler,
)


def _create_flow() -> SchellenbergPairingSubentryFlow:
    """Create a subentry flow with user source context."""
    flow = SchellenbergPairingSubentryFlow()
    flow.context = {"source": SOURCE_USER}
    return flow


@pytest.mark.asyncio
async def test_blind_subentry_flow_shows_setup_method_menu() -> None:
    """Test that legacy, hybrid, and manual setup remain available."""
    result = await _create_flow().async_step_user()

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["pair_test", "pair_device", "manual"]


@pytest.mark.asyncio
async def test_all_blind_subentry_navigation_menus_expose_expected_options() -> None:
    """Test every static subentry navigation menu uses the translated keys."""
    flow = _create_flow()

    manual_next = await flow.async_step_manual_next()
    reconfigure = await flow.async_step_reconfigure()
    flow._pairing_workflow = "hybrid"
    test_success = await flow.async_step_did_motor_move({"motor_moved": True})

    assert manual_next["menu_options"] == ["test_motor", "save_manual"]
    assert reconfigure["menu_options"] == [
        "edit",
        "test_existing",
        "developer_tools",
        "calibrate",
    ]
    assert test_success["menu_options"] == ["calibration_close", "manual_times"]


@pytest.mark.asyncio
async def test_legacy_pairing_path_is_unchanged() -> None:
    """Test selecting legacy pairing still reaches naming and calibration."""
    flow = _create_flow()
    api = MagicMock()
    api.pair_device_and_wait = AsyncMock(return_value=("3720B8", "08"))
    hub_entry = MagicMock(runtime_data=api)

    form = await flow.async_step_pair_device()
    assert form["step_id"] == "pair_device"

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_pair_device({})

    assert result["step_id"] == "name_device"
    assert flow._pairing_workflow == "legacy"
    assert flow._pending_status_device_id is None
    assert flow._pending_status_enum is None
    assert flow._pending_status_identity_source == STATUS_IDENTITY_SOURCE_UNKNOWN


@pytest.mark.asyncio
async def test_hybrid_pairing_reaches_command_test() -> None:
    """Test hybrid pairing names the device before command testing."""
    flow = _create_flow()
    api = MagicMock()
    api.pair_device_and_wait = AsyncMock(return_value=("F2B8D5", "23"))
    hub_entry = MagicMock(runtime_data=api)

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        await flow.async_step_pair_test({})
        result = await flow.async_step_name_device({CONF_DEVICE_NAME: "Sitting room"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"] == {
        "device_id": "F2B8D5",
        "device_enum": "23",
    }


@pytest.mark.asyncio
async def test_manual_setup_stores_separate_command_and_status_identity() -> None:
    """Test manual setup stores split identities while retaining legacy keys."""
    flow = _create_flow()
    hub_entry = MagicMock(subentries=MappingProxyType({}))

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_manual(
            {
                CONF_DEVICE_NAME: "Sitting room",
                CONF_DEVICE_ID: "f2b8d5",
                CONF_DEVICE_ENUM: "23",
                CONF_STATUS_DEVICE_ID: "3720b8",
                CONF_STATUS_ENUM: "08",
                CONF_SECONDARY_STATUS_IDENTITIES: "F2B8D5/23\nABCDEF/0d",
                CONF_OPEN_TIME_SECONDS: 25.06,
                CONF_CLOSE_TIME_SECONDS: 23.05,
                CONF_INVERT_DIRECTION: False,
            }
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "manual_next"

    result = await flow.async_step_save_manual()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sitting room"
    saved_data = dict(result["data"])
    assert str(UUID(saved_data.pop(CONF_BLIND_ID)))
    assert saved_data == {
        CONF_DEVICE_ID: "F2B8D5",
        CONF_DEVICE_ENUM: "23",
        CONF_COMMAND_DEVICE_ID: "F2B8D5",
        CONF_COMMAND_ENUM: "23",
        CONF_STATUS_DEVICE_ID: "3720B8",
        CONF_STATUS_ENUM: "08",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_MANUAL,
        CONF_SECONDARY_STATUS_IDENTITIES: [
            {"device_id": "F2B8D5", "enum": "23"},
            {"device_id": "ABCDEF", "enum": "0D"},
        ],
        CONF_OPEN_TIME: 25.06,
        CONF_CLOSE_TIME: 23.05,
        CONF_INVERT_DIRECTION: False,
    }
    assert result["unique_id"] == "F2B8D5"


@pytest.mark.asyncio
async def test_manual_subentry_persists_through_storage_reload(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test manual add is committed by HA and survives config-entry hydration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"serial_port": "/dev/ttyUSB0"},
        title="Schellenberg USB",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_NAME: "Sitting room door",
            CONF_DEVICE_ID: "F2B8D5",
            CONF_DEVICE_ENUM: "23",
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: "08",
            CONF_SECONDARY_STATUS_IDENTITIES: "F2B8D5/23",
            CONF_OPEN_TIME_SECONDS: 25.06,
            CONF_CLOSE_TIME_SECONDS: 23.05,
            CONF_INVERT_DIRECTION: False,
        },
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "save_manual"}
    )
    saved_subentry_id = next(iter(entry.subentries))

    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    restarted_config_entries = ConfigEntries(hass, {})
    await restarted_config_entries.async_initialize()
    restored = restarted_config_entries.async_get_entry(entry.entry_id)

    assert restored is not None
    assert len(restored.subentries) == 1
    subentry = next(iter(restored.subentries.values()))
    assert subentry.subentry_id == saved_subentry_id
    assert subentry.subentry_type == SUBENTRY_TYPE_BLIND
    assert subentry.title == "Sitting room door"
    assert subentry.unique_id == "F2B8D5"
    assert subentry.data[CONF_STATUS_DEVICE_ID] == "3720B8"
    assert subentry.data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "F2B8D5", "enum": "23"}
    ]
    assert subentry.data[CONF_OPEN_TIME] == 25.06


@pytest.mark.asyncio
async def test_manual_setup_validates_protocol_values() -> None:
    """Test validation of command/status identities and travel times."""
    flow = _create_flow()
    hub_entry = MagicMock(subentries=MappingProxyType({}))

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_manual(
            {
                CONF_DEVICE_NAME: " ",
                CONF_DEVICE_ID: "not-hex",
                CONF_DEVICE_ENUM: "123",
                CONF_STATUS_DEVICE_ID: "also-bad",
                CONF_STATUS_ENUM: "x",
                CONF_SECONDARY_STATUS_IDENTITIES: "not-an-identity",
                CONF_OPEN_TIME_SECONDS: 0,
                CONF_CLOSE_TIME_SECONDS: -1,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_DEVICE_NAME: "required",
        CONF_DEVICE_ID: "invalid_device_id",
        CONF_DEVICE_ENUM: "invalid_device_enum",
        CONF_STATUS_DEVICE_ID: "invalid_device_id",
        CONF_STATUS_ENUM: "invalid_device_enum",
        CONF_SECONDARY_STATUS_IDENTITIES: "invalid_status_identities",
        CONF_OPEN_TIME_SECONDS: "invalid_travel_time",
        CONF_CLOSE_TIME_SECONDS: "invalid_travel_time",
    }


@pytest.mark.asyncio
async def test_short_motor_command_sends_open_then_stop() -> None:
    """Test the hybrid command sequence and its diagnostic context."""
    flow = _create_flow()
    flow._pending_device_id = "F2B8D5"
    flow._pending_device_enum = "23"
    api = MagicMock()
    api.control_blind = AsyncMock(return_value=True)
    hub_entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=hub_entry),
        patch("custom_components.schellenberg_usb.config_flow.asyncio.sleep") as sleep,
    ):
        result = await flow.async_step_test_motor({})

    assert result["step_id"] == "did_motor_move"
    api.control_blind.assert_has_awaits(
        [
            call("23", CMD_UP, device_id="F2B8D5"),
            call("23", CMD_STOP, device_id="F2B8D5"),
        ]
    )
    sleep.assert_awaited_once_with(0.75)


@pytest.mark.asyncio
async def test_edit_updates_protocol_data_without_unique_id_change() -> None:
    """Test editing updates subentry data without replacing the subentry."""
    flow = _create_flow()
    subentry = MagicMock(
        subentry_id="sub1",
        title="Sitting room",
        unique_id="F2B8D5",
        data={
            CONF_DEVICE_ID: "F2B8D5",
            CONF_DEVICE_ENUM: "23",
            CONF_OPEN_TIME: 25.06,
            CONF_CLOSE_TIME: 23.05,
        },
    )
    entry = MagicMock(subentries=MappingProxyType({"sub1": subentry}))
    expected_result = {"type": FlowResultType.ABORT}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
        patch.object(
            flow, "async_update_and_abort", return_value=expected_result
        ) as update,
    ):
        result = await flow.async_step_edit(
            {
                CONF_DEVICE_NAME: "Door",
                CONF_DEVICE_ID: "f2b8d5",
                CONF_DEVICE_ENUM: "13",
                CONF_STATUS_DEVICE_ID: "3720b8",
                CONF_STATUS_ENUM: "08",
                CONF_SECONDARY_STATUS_IDENTITIES: "F2B8D5/23",
                CONF_OPEN_TIME_SECONDS: 25.06,
                CONF_CLOSE_TIME_SECONDS: 23.05,
                CONF_INVERT_DIRECTION: True,
            }
        )

    assert result is expected_result
    _, kwargs = update.call_args
    assert kwargs["title"] == "Door"
    assert kwargs["data"][CONF_COMMAND_DEVICE_ID] == "F2B8D5"
    assert kwargs["data"][CONF_COMMAND_ENUM] == "13"
    assert kwargs["data"][CONF_STATUS_DEVICE_ID] == "3720B8"
    assert kwargs["data"][CONF_STATUS_ENUM] == "08"
    assert kwargs["data"][CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "F2B8D5", "enum": "23"}
    ]
    assert kwargs["data"][CONF_INVERT_DIRECTION] is True
    assert "unique_id" not in kwargs


@pytest.mark.asyncio
async def test_developer_tools_show_last_frame_and_send_selected_target() -> None:
    """Test the native diagnostic view and its direct command actions."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting room door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "23",
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: "08",
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
            CONF_LAST_CALIBRATION: {
                "completed_at": "2026-07-04T12:00:55+02:00",
                "end_reason": "completed",
                "frames": [
                    {
                        "device_id": "3720B8",
                        "enum": "08",
                        "command": "00",
                        "time": "12:00:25",
                        "phase": "opening_endstop",
                    }
                ],
                "groups": [
                    {
                        "device_id": "3720B8",
                        "enum": "08",
                        "commands": ["01", "00", "02"],
                    }
                ],
            },
            CONF_SECONDARY_STATUS_IDENTITIES: [{"device_id": "F2B8D5", "enum": "23"}],
            CONF_OPEN_TIME: 25.06,
            CONF_CLOSE_TIME: 23.05,
            CONF_INVERT_DIRECTION: True,
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = {
        "device_id": "F2B8D5",
        "enum": "23",
        "command": "C1",
        "time": "17:32:14",
        "identity_role": "secondary",
        "interpreted_command": "unknown",
        "position_tracking": False,
    }
    api.get_last_primary_tracking_frame.return_value = {
        "device_id": "3720B8",
        "enum": "08",
        "command": "01",
        "time": "17:31:50",
        "identity_role": "primary",
        "interpreted_command": "open",
        "position_tracking": True,
    }
    api.get_last_secondary_frame.return_value = {
        "device_id": "F2B8D5",
        "enum": "23",
        "command": "C1",
        "time": "17:32:14",
        "identity_role": "secondary",
        "interpreted_command": "unknown",
        "position_tracking": False,
    }
    api.get_last_position_update.return_value = {
        "source": "primary status 3720B8/08 command 01",
        "position_source": "primary status",
        "confirmed_since_restart": True,
        "direction": "opening",
        "previous_position": 40,
        "new_position": 44,
        "status": "estimated",
        "time": "17:32:15",
    }
    api.get_last_manual_position_sync.return_value = {
        "source": "Developer Tools manual position sync",
        "direction": "manual",
        "previous_position": 0,
        "new_position": 40,
        "status": "confirmed/manual",
        "time": "17:30:00",
    }
    api.control_blind = AsyncMock(return_value=True)
    api.reset_and_reconnect = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.transmit_block_reason = None
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        result = await flow.async_step_developer_tools()
        command_result = await flow.async_step_test_open()
        reset_result = await flow.async_step_reset_stick()
        copy_result = await flow.async_step_copy_diagnostics()

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["selected_blind"] == "Sitting room door"
    assert placeholders["last_device_id"] == "F2B8D5"
    assert placeholders["last_identity_role"] == "secondary"
    assert placeholders["last_interpretation"] == "unknown"
    assert placeholders["last_position_tracking"] == "False"
    assert placeholders["primary_last_device_id"] == "3720B8"
    assert placeholders["primary_last_command"] == "01"
    assert placeholders["primary_last_interpretation"] == "open"
    assert placeholders["secondary_last_device_id"] == "F2B8D5"
    assert placeholders["secondary_last_command"] == "C1"
    assert placeholders["position_source"] == "primary status"
    assert placeholders["position_direction"] == "opening"
    assert placeholders["position_previous"] == "40%"
    assert placeholders["position_new"] == "44%"
    assert placeholders["position_status"] == "estimated"
    assert placeholders["current_position"] == "44%"
    assert placeholders["last_manual_sync_time"] == "17:30:00"
    assert placeholders["position_confidence"] == "estimated"
    assert placeholders["position_confirmed_since_restart"] == "Yes"
    assert placeholders["primary_status_device_id"] == "3720B8"
    assert placeholders["primary_status_enum"] == "08"
    assert placeholders["status_identity_source"] == (
        "automatically discovered during calibration"
    )
    assert placeholders["last_calibration_time"] == ("2026-07-04T12:00:55+02:00")
    assert placeholders["calibration_end_reason"] == "completed"
    assert "opening_endstop" in placeholders["calibration_frames"]
    assert "3720B8/08: 01,00,02" in placeholders["calibration_candidates"]
    assert placeholders["secondary_status_identities"] == "F2B8D5/23"
    assert placeholders["command_device_id"] == "F2B8D5"
    assert placeholders["command_enum"] == "23"
    assert result["menu_options"] == DEVELOPER_TOOLS_MENU_OPTIONS
    api.control_blind.assert_awaited_once_with(
        "23", CMD_DOWN, device_id="F2B8D5", source="developer_tools"
    )
    api.reset_and_reconnect.assert_awaited_once_with()
    command_placeholders = command_result["description_placeholders"]
    assert command_placeholders is not None
    assert "written successfully" in command_placeholders["result"]
    reset_placeholders = reset_result["description_placeholders"]
    assert reset_placeholders is not None
    assert "ready for transmit" in reset_placeholders["result"]
    schema = copy_result["data_schema"]
    assert schema is not None
    diagnostics = schema({})["diagnostics"]
    assert "Selected blind: Sitting room door" in diagnostics
    assert "Configured primary status identity:" in diagnostics
    assert "Source: automatically discovered during calibration" in diagnostics
    assert "Last calibration run:" in diagnostics
    assert "End reason: completed" in diagnostics
    assert "phase=opening_endstop" in diagnostics
    assert "Device ID: 3720B8" in diagnostics
    assert "Configured secondary status identities:" in diagnostics
    assert "F2B8D5/23" in diagnostics
    assert "Identity role: secondary" in diagnostics
    assert "Interpretation: unknown" in diagnostics
    assert "Last primary tracking frame:" in diagnostics
    assert "Last secondary frame:" in diagnostics
    assert "Last position update:" in diagnostics
    assert "Previous position: 40" in diagnostics
    assert "New position: 44" in diagnostics
    assert "Status: estimated" in diagnostics
    assert "Current estimated position: 44" in diagnostics
    assert "Last manual sync time: 17:30:00" in diagnostics
    assert "Source: primary status" in diagnostics
    assert "Details: primary status 3720B8/08 command 01" in diagnostics
    assert "Confidence: estimated" in diagnostics
    assert "Confirmed since restart: Yes" in diagnostics
    assert "Mode: listening" in diagnostics
    assert "Ready: True" in diagnostics
    assert "t1/t0 confirm only" in diagnostics
    assert "Motor reception and movement remain unverified" in diagnostics


@pytest.mark.asyncio
async def test_developer_tools_show_restored_startup_confidence() -> None:
    """Test restored HA position is visibly unconfirmed after restart."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Garden",
        data={
            CONF_COMMAND_DEVICE_ID: "ABC123",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = {
        "source": "restored HA state",
        "position_source": "restored HA state",
        "confirmed_since_restart": False,
        "direction": "idle",
        "previous_position": None,
        "new_position": 75,
        "status": "restored / estimated / not confirmed since restart",
        "time": "08:00:00",
    }
    api.get_last_manual_position_sync.return_value = None
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        result = await flow.async_step_developer_tools()
        copy_result = await flow.async_step_copy_diagnostics()

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["current_position"] == "75%"
    assert placeholders["position_source"] == "restored HA state"
    assert placeholders["position_confidence"] == (
        "restored / estimated / not confirmed since restart"
    )
    assert placeholders["position_confirmed_since_restart"] == "No"

    data_schema = copy_result["data_schema"]
    assert data_schema is not None
    diagnostics = data_schema({})["diagnostics"]
    assert "Current estimated position: 75" in diagnostics
    assert "Source: restored HA state" in diagnostics
    assert (
        "Confidence: restored / estimated / not confirmed since restart" in diagnostics
    )
    assert "Confirmed since restart: No" in diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_method", "expected_position"),
    [
        ("async_step_set_position_open", 100),
        ("async_step_set_position_closed", 0),
    ],
)
async def test_developer_position_endpoint_actions_update_live_cover(
    step_method: str,
    expected_position: int,
) -> None:
    """Test fully-open and fully-closed actions dispatch exact positions."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting room door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: "08",
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.get_last_manual_position_sync.return_value = None
    api.manual_sync_position.return_value = True
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        result = await getattr(flow, step_method)()

    api.manual_sync_position.assert_called_once_with("F2B8D5", expected_position)
    assert result["step_id"] == "developer_tools"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert f"confirmed at {expected_position}%" in placeholders["result"]


@pytest.mark.asyncio
async def test_developer_manual_position_form_uses_current_position_and_submits() -> (
    None
):
    """Test the numeric manual-sync form defaults to and applies tracked position."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting room door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: "08",
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = {
        "source": "restored Home Assistant state",
        "direction": "idle",
        "previous_position": None,
        "new_position": 37,
        "status": "estimated",
        "time": "18:00:00",
    }
    api.get_last_manual_position_sync.return_value = None
    api.manual_sync_position.return_value = True
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        form = await flow.async_step_set_position_manual()
        result = await flow.async_step_set_position_manual({"position": 63})

    assert form["step_id"] == "set_position_manual"
    assert form["data_schema"] is not None
    assert form["data_schema"]({})["position"] == 37
    assert form["description_placeholders"] == {
        "selected_blind": "Sitting room door",
        "current_position": "37%",
    }
    api.manual_sync_position.assert_called_once_with("F2B8D5", 63)
    assert result["step_id"] == "developer_tools"
    assert result["description_placeholders"] is not None
    assert "confirmed at 63%" in result["description_placeholders"]["result"]


@pytest.mark.asyncio
async def test_original_remote_discovery_saves_primary_secondary_and_provenance() -> (
    None
):
    """Test guided discovery updates an existing blind without transmit fallback."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Garden",
        data={
            CONF_COMMAND_DEVICE_ID: "06C5C0",
            CONF_COMMAND_ENUM: "11",
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
            CONF_OPEN_TIME: 24.86,
            CONF_CLOSE_TIME: 22.59,
        },
    )
    entry = MagicMock()
    result_data = {
        "primary": {
            "device_id": "3720B8",
            "enum": "08",
            "commands": ["01", "00", "02"],
            "timestamps": ["12:00:01", "12:00:02", "12:00:03"],
        },
        "secondary": [
            {
                "device_id": "06C5C0",
                "enum": "23",
                "commands": ["E1", "E2"],
                "timestamps": ["12:00:01", "12:00:03"],
            }
        ],
        "unknown_commands": [
            {"device_id": "06C5C0", "enum": "23", "commands": ["E1", "E2"]}
        ],
        "frames": [{"device_id": "3720B8", "enum": "08", "command": "01"}],
        "position_tracking_available": True,
    }
    api = MagicMock()
    api.async_discover_status_identities = AsyncMock(return_value=result_data)
    entry.runtime_data = api
    expected = {"type": FlowResultType.ABORT}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
        patch.object(flow, "async_update_and_abort", return_value=expected) as update,
    ):
        form = await flow.async_step_discover_status()
        confirmation = await flow.async_step_discover_status({})
        result = await flow.async_step_confirm_status_discovery({})

    assert form["step_id"] == "discover_status"
    assert confirmation["step_id"] == "confirm_status_discovery"
    placeholders = confirmation["description_placeholders"]
    assert placeholders is not None
    assert placeholders["command_identity"] == "06C5C0/11"
    assert placeholders["primary_identity"] == "3720B8/08"
    assert placeholders["primary_commands"] == "01, 00, 02"
    assert "06C5C0/23" in placeholders["secondary_identities"]
    assert result is expected
    data = update.call_args.kwargs["data"]
    assert data[CONF_STATUS_DEVICE_ID] == "3720B8"
    assert data[CONF_STATUS_ENUM] == "08"
    assert data[CONF_STATUS_IDENTITY_SOURCE] == (
        STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    )
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "06C5C0", "enum": "23"}
    ]


@pytest.mark.asyncio
async def test_original_remote_discovery_keeps_status_unknown_when_unrecognized() -> (
    None
):
    """Test unknown-only frames never turn command identity into primary status."""
    flow = _create_flow()
    flow._pending_device_name = "Garden"
    flow._pending_device_id = "06C5C0"
    flow._pending_device_enum = "11"
    flow._pending_open_time = 24.86
    flow._pending_close_time = 22.59
    api = MagicMock()
    api.async_discover_status_identities = AsyncMock(
        return_value={
            "primary": None,
            "secondary": [
                {
                    "device_id": "06C5C0",
                    "enum": "23",
                    "commands": ["E1"],
                    "timestamps": ["12:00:01"],
                }
            ],
            "unknown_commands": [
                {"device_id": "06C5C0", "enum": "23", "commands": ["E1"]}
            ],
            "frames": [{"device_id": "06C5C0", "enum": "23", "command": "E1"}],
            "position_tracking_available": False,
        }
    )
    entry = MagicMock(runtime_data=api)

    with patch.object(flow, "_get_entry", return_value=entry):
        confirmation = await flow.async_step_discover_status({})
        result = await flow.async_step_confirm_status_discovery({})

    placeholders = confirmation["description_placeholders"]
    assert placeholders is not None
    assert "No remote/status tracking identity" in placeholders["position_tracking"]
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_STATUS_DEVICE_ID not in result["data"]
    assert CONF_STATUS_ENUM not in result["data"]
    assert result["data"][CONF_STATUS_IDENTITY_SOURCE] == (
        STATUS_IDENTITY_SOURCE_UNKNOWN
    )
    assert result["data"][CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "06C5C0", "enum": "23"}
    ]


@pytest.mark.asyncio
async def test_developer_teach_motor_sends_60_then_open_and_stop() -> None:
    """Test guided teach-in uses the API and follows with a movement test."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting room door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "0D",
            CONF_STATUS_DEVICE_ID: "F2B8D5",
            CONF_STATUS_ENUM: "23",
            CONF_INVERT_DIRECTION: False,
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.teach_motor = AsyncMock(return_value=True)
    api.control_blind = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.transmit_block_reason = None
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
        patch(
            "custom_components.schellenberg_usb.config_flow.asyncio.sleep",
            new_callable=AsyncMock,
        ) as delay,
    ):
        form = await flow.async_step_teach_motor()
        result = await flow.async_step_teach_motor({})

    assert form["step_id"] == "teach_motor"
    api.teach_motor.assert_awaited_once_with(
        "0D", device_id="F2B8D5", source="developer_tools"
    )
    api.control_blind.assert_has_awaits(
        [
            call(
                "0D",
                CMD_UP,
                device_id="F2B8D5",
                source="developer_tools",
            ),
            call(
                "0D",
                CMD_STOP,
                device_id="F2B8D5",
                source="developer_tools",
            ),
        ]
    )
    delay.assert_awaited_once()
    assert result["step_id"] == "developer_tools"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "Stick ACKs confirm only" in placeholders["result"]


@pytest.mark.asyncio
async def test_developer_raw_command_validates_and_uses_api() -> None:
    """Test the raw RF form supplies an exact packet and invokes the API."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting room door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "F2B8D5",
            CONF_STATUS_ENUM: "23",
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.send_raw_transmit = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        form = await flow.async_step_send_raw_command()
        result = await flow.async_step_send_raw_command({"payload": "ss109010000"})

    assert form["step_id"] == "send_raw_command"
    assert form["data_schema"] is not None
    assert form["data_schema"]({})["payload"] == "ss109010000"
    api.send_raw_transmit.assert_awaited_once_with(
        "ss109010000", source="developer_tools"
    )
    assert result["step_id"] == "developer_tools"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "do not confirm motor movement" in placeholders["result"]


@pytest.mark.asyncio
async def test_developer_raw_command_shows_validation_error() -> None:
    """Test malformed raw packets remain on the form with a field error."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Blind",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "08",
        },
    )
    api = MagicMock()
    api.send_raw_transmit = AsyncMock(side_effect=ValueError("invalid"))
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        result = await flow.async_step_send_raw_command({"payload": "ss08901000"})

    assert result["step_id"] == "send_raw_command"
    assert result["errors"] == {"payload": "invalid_raw_payload"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_id", "requested_command", "protocol_action"),
    [
        ("test_open", "open", CMD_UP),
        ("test_close", "close", CMD_DOWN),
        ("test_stop", "stop", CMD_STOP),
    ],
)
async def test_developer_menu_navigation_dispatches_every_command(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    caplog: pytest.LogCaptureFixture,
    step_id: str,
    requested_command: str,
    protocol_action: str,
) -> None:
    """Test actual HA menu navigation invokes every Developer Tools action."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"serial_port": "/dev/ttyUSB0"},
        title="Schellenberg USB",
    )
    entry.add_to_hass(hass)
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_COMMAND_DEVICE_ID: "F2B8D5",
                CONF_COMMAND_ENUM: "10",
                CONF_STATUS_DEVICE_ID: "F2B8D5",
                CONF_STATUS_ENUM: "23",
                CONF_INVERT_DIRECTION: False,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Sitting_room_door",
        unique_id="F2B8D5",
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.control_blind = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.transmit_block_reason = None
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry.runtime_data = api

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={
            "source": SOURCE_RECONFIGURE,
            "subentry_id": subentry.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "developer_tools"}
    )
    assert result["step_id"] == "developer_tools"

    with caplog.at_level("WARNING"):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": step_id}
        )

    assert result["step_id"] == "developer_tools"
    api.control_blind.assert_awaited_once_with(
        "10",
        protocol_action,
        device_id="F2B8D5",
        source="developer_tools",
    )
    assert "Developer Tools command clicked" in caplog.text
    assert "selected_blind=Sitting_room_door" in caplog.text
    assert f"command_requested={requested_command}" in caplog.text
    assert "command_device_id=F2B8D5" in caplog.text
    assert "command_enum=10" in caplog.text
    assert "status_device_id=F2B8D5" in caplog.text
    assert "status_enum=23" in caplog.text
    assert "stick_connected=True" in caplog.text
    assert "stick_mode=listening" in caplog.text
    assert "stick_ready=True" in caplog.text
    assert "pairing=False" in caplog.text
    assert "transmitter_active=False" in caplog.text
    assert "busy_latched=False" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_method", "cover_method", "protocol_action"),
    [
        ("async_step_test_open", "async_open_cover", CMD_UP),
        ("async_step_test_close", "async_close_cover", CMD_DOWN),
        ("async_step_test_stop", "async_stop_cover", CMD_STOP),
    ],
)
async def test_developer_and_cover_actions_share_control_blind_path(
    step_method: str, cover_method: str, protocol_action: str
) -> None:
    """Test Developer Tools and cover actions invoke the same API command path."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Sitting_room_door",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "10",
            CONF_STATUS_DEVICE_ID: "F2B8D5",
            CONF_STATUS_ENUM: "23",
            CONF_INVERT_DIRECTION: False,
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.control_blind = AsyncMock(return_value=True)
    api.is_connected = True
    api.device_mode = "listening"
    api.transmit_ready = True
    api.transmit_block_reason = None
    api.pairing_active = False
    api.transmitter_active = False
    api.busy_latched = False
    entry = MagicMock(runtime_data=api)
    cover = SchellenbergCover(
        api=api,
        device_id="F2B8D5",
        device_enum="10",
        device_name="Sitting_room_door",
        command_device_id="F2B8D5",
        status_device_id="F2B8D5",
        status_enum="23",
    )
    cover._attr_current_cover_position = 50

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        await getattr(flow, step_method)()
    developer_call = api.control_blind.await_args
    api.control_blind.reset_mock()

    with (
        patch.object(cover, "_start_position_tracking"),
        patch.object(cover, "_stop_position_tracking"),
        patch.object(cover, "async_write_ha_state"),
    ):
        await getattr(cover, cover_method)()
    cover_call = api.control_blind.await_args

    assert developer_call.args == cover_call.args == ("10", protocol_action)
    assert developer_call.kwargs == {
        "device_id": "F2B8D5",
        "source": "developer_tools",
    }
    assert cover_call.kwargs == {"device_id": "F2B8D5"}


@pytest.mark.asyncio
async def test_developer_command_is_blocked_when_stick_is_not_ready() -> None:
    """Test Developer Tools does not queue commands in a non-ready state."""
    flow = _create_flow()
    subentry = MagicMock(
        title="Blind",
        data={
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: "08",
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: "0D",
        },
    )
    api = MagicMock()
    api.get_last_received_for_identities.return_value = None
    api.get_last_primary_tracking_frame.return_value = None
    api.get_last_secondary_frame.return_value = None
    api.get_last_position_update.return_value = None
    api.control_blind = AsyncMock()
    api.is_connected = True
    api.device_mode = "pairing"
    api.transmit_ready = False
    api.transmit_block_reason = "pairing is active"
    api.pairing_active = True
    api.transmitter_active = False
    api.busy_latched = True
    entry = MagicMock(runtime_data=api)

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
    ):
        result = await flow.async_step_test_stop()

    api.control_blind.assert_not_awaited()
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "command blocked" in placeholders["result"]
    assert "Reset stick / reconnect serial" in placeholders["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_enum", "status_enum"),
    [("23", "08"), ("08", "0D"), ("0D", "08")],
)
async def test_pairing_persists_calibrated_protocol_data(
    command_enum: str, status_enum: str
) -> None:
    """Test paired subentries retain identities and measured times."""
    flow = MagicMock(spec=ConfigSubentryFlow)
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device(
        {
            "id": "3720B8",
            "entity_id": "F2B8D5",
            "name": "Sitting room",
            "enum": status_enum,
        }
    )
    handler.enable_subentry_creation(
        device_id="F2B8D5",
        blind_id="11111111-1111-4111-8111-111111111111",
        device_enum=command_enum,
        device_name="Sitting room",
        status_device_id="3720B8",
        status_enum=status_enum,
    )
    handler._open_time = 25.064
    handler._close_time = 23.054

    with patch.object(handler, "_save_calibration_data", new=AsyncMock()):
        await handler.async_step_calibration_complete({})

    flow.async_create_entry.assert_called_once_with(
        title="Sitting room",
        data={
            CONF_DEVICE_ID: "F2B8D5",
            CONF_BLIND_ID: "11111111-1111-4111-8111-111111111111",
            CONF_DEVICE_ENUM: command_enum,
            CONF_COMMAND_DEVICE_ID: "F2B8D5",
            CONF_COMMAND_ENUM: command_enum,
            CONF_STATUS_DEVICE_ID: "3720B8",
            CONF_STATUS_ENUM: status_enum,
            CONF_SECONDARY_STATUS_IDENTITIES: [],
            CONF_OPEN_TIME: 25.06,
            CONF_CLOSE_TIME: 23.05,
            CONF_INVERT_DIRECTION: False,
        },
        unique_id="F2B8D5",
    )


@pytest.mark.asyncio
async def test_calibration_candidates_are_persisted_with_frame_diagnostics() -> None:
    """Test calibration-derived primary, secondary, phases, and provenance persist."""
    flow = MagicMock(spec=ConfigSubentryFlow)
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device(
        {"id": "06C5C0", "entity_id": "06C5C0", "name": "Garden", "enum": "11"}
    )
    handler.enable_subentry_creation(
        device_id="06C5C0",
        device_enum="11",
        device_name="Garden",
        status_identity_source=STATUS_IDENTITY_SOURCE_UNKNOWN,
    )
    handler._open_time = 24.864
    handler._close_time = 22.594
    handler._calibration_discovery_result = {
        "primary": {
            "device_id": "3720B8",
            "enum": "08",
            "commands": ["01", "00", "02"],
            "timestamps": ["12:00:01", "12:00:25", "12:00:30"],
        },
        "secondary": [
            {
                "device_id": "06C5C0",
                "enum": "23",
                "commands": ["E1", "E2"],
                "timestamps": ["12:00:01", "12:00:30"],
            }
        ],
        "groups": [],
        "unknown_commands": [
            {"device_id": "06C5C0", "enum": "23", "commands": ["E1", "E2"]}
        ],
        "position_tracking_available": True,
        "frames": [
            {
                "device_id": "3720B8",
                "enum": "08",
                "command": "00",
                "time": "12:00:25",
                "phase": "opening_endstop",
            }
        ],
        "started_at": "2026-07-04T12:00:00+02:00",
        "completed_at": "2026-07-04T12:00:55+02:00",
        "end_reason": "completed",
    }
    handler._apply_calibration_status_candidates()

    with patch.object(handler, "_save_calibration_data", new=AsyncMock()):
        await handler.async_step_calibration_complete({})

    data = flow.async_create_entry.call_args.kwargs["data"]
    assert data[CONF_STATUS_DEVICE_ID] == "3720B8"
    assert data[CONF_STATUS_ENUM] == "08"
    assert data[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_CALIBRATION
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "06C5C0", "enum": "23"}
    ]
    assert data[CONF_LAST_CALIBRATION]["end_reason"] == "completed"
    assert data[CONF_LAST_CALIBRATION]["frames"][0]["phase"] == ("opening_endstop")
    assert data[CONF_OPEN_TIME] == 24.86
    assert data[CONF_CLOSE_TIME] == 22.59


@pytest.mark.asyncio
async def test_reconfigure_persists_calibrated_travel_times() -> None:
    """Test recalibration updates the existing blind subentry."""
    flow = MagicMock(spec=ConfigSubentryFlow)
    entry = MagicMock()
    subentry = MagicMock()
    flow._get_entry.return_value = entry
    flow._get_reconfigure_subentry.return_value = subentry
    expected_result = {"type": FlowResultType.ABORT}
    flow.async_update_and_abort.return_value = expected_result
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device(
        {"id": "3720B8", "entity_id": "F2B8D5", "name": "Door", "enum": "08"}
    )
    handler._open_time = 25.678
    handler._close_time = 23.456

    with patch.object(handler, "_save_calibration_data", new=AsyncMock()):
        result = await handler.async_step_calibration_complete({})

    assert result is expected_result
    flow.async_update_and_abort.assert_called_once_with(
        entry,
        subentry,
        data_updates={
            CONF_OPEN_TIME: 25.68,
            CONF_CLOSE_TIME: 23.46,
        },
    )
