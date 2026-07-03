"""Tests for Schellenberg USB blind subentry flows."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryFlow, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.schellenberg_usb.config_flow import (
    SchellenbergPairingSubentryFlow,
)
from custom_components.schellenberg_usb.const import (
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
)
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
    """Test that users can choose pairing or manual setup."""
    flow = _create_flow()

    result = await flow.async_step_user()

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["pair_device", "manual"]


@pytest.mark.asyncio
async def test_pairing_path_is_unchanged() -> None:
    """Test that selecting pairing still starts the existing pairing flow."""
    flow = _create_flow()
    api = MagicMock()
    api.pair_device_and_wait = AsyncMock(return_value=("3720B8", "08"))
    hub_entry = MagicMock(runtime_data=api)

    form = await flow.async_step_pair_device()
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "pair_device"

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_pair_device({})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name_device"
    assert result["description_placeholders"] == {"device_id": "3720B8"}


@pytest.mark.asyncio
async def test_manual_setup_creates_normalized_blind_subentry() -> None:
    """Test creating a calibrated blind without running calibration."""
    flow = _create_flow()
    hub_entry = MagicMock(subentries=MappingProxyType({}))

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_manual(
            {
                CONF_DEVICE_NAME: "Living room",
                CONF_DEVICE_ID: "3720b8",
                CONF_DEVICE_ENUM: "08",
                CONF_OPEN_TIME_SECONDS: 24.5,
                CONF_CLOSE_TIME_SECONDS: 22,
            }
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    assert result["data"] == {
        CONF_DEVICE_ID: "3720B8",
        CONF_DEVICE_ENUM: "08",
        CONF_OPEN_TIME: 24.5,
        CONF_CLOSE_TIME: 22.0,
    }
    assert result["unique_id"] == "3720B8"


@pytest.mark.asyncio
async def test_manual_setup_validates_protocol_values() -> None:
    """Test validation of IDs and positive travel times."""
    flow = _create_flow()
    hub_entry = MagicMock(subentries=MappingProxyType({}))

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_manual(
            {
                CONF_DEVICE_NAME: " ",
                CONF_DEVICE_ID: "not-hex",
                CONF_DEVICE_ENUM: "123",
                CONF_OPEN_TIME_SECONDS: 0,
                CONF_CLOSE_TIME_SECONDS: -1,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_DEVICE_NAME: "required",
        CONF_DEVICE_ID: "invalid_device_id",
        CONF_DEVICE_ENUM: "invalid_device_enum",
        CONF_OPEN_TIME_SECONDS: "invalid_travel_time",
        CONF_CLOSE_TIME_SECONDS: "invalid_travel_time",
    }


@pytest.mark.asyncio
async def test_manual_setup_rejects_existing_device_id() -> None:
    """Test that manual setup cannot duplicate a blind subentry."""
    flow = _create_flow()
    existing_subentry = MagicMock(data={CONF_DEVICE_ID: "3720B8"})
    hub_entry = MagicMock(subentries=MappingProxyType({"existing": existing_subentry}))

    with patch.object(flow, "_get_entry", return_value=hub_entry):
        result = await flow.async_step_manual(
            {
                CONF_DEVICE_NAME: "Duplicate",
                CONF_DEVICE_ID: "3720b8",
                CONF_DEVICE_ENUM: "08",
                CONF_OPEN_TIME_SECONDS: 24,
                CONF_CLOSE_TIME_SECONDS: 22,
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE_ID: "already_configured"}


@pytest.mark.asyncio
async def test_pairing_persists_calibrated_travel_times() -> None:
    """Test that paired subentries retain measured times across restarts."""
    flow = MagicMock(spec=ConfigSubentryFlow)
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device(
        {
            "id": "3720B8",
            "name": "Living room",
            "enum": "08",
        }
    )
    handler.enable_subentry_creation(
        device_id="3720B8",
        device_enum="08",
        device_name="Living room",
    )
    handler._open_time = 24.567
    handler._close_time = 22.345

    with patch.object(handler, "_save_calibration_data", new=AsyncMock()):
        await handler.async_step_calibration_complete({})

    flow.async_create_entry.assert_called_once_with(
        title="Living room",
        data={
            CONF_DEVICE_ID: "3720B8",
            CONF_DEVICE_ENUM: "08",
            CONF_OPEN_TIME: 24.57,
            CONF_CLOSE_TIME: 22.34,
        },
        unique_id="3720B8",
    )


@pytest.mark.asyncio
async def test_reconfigure_persists_calibrated_travel_times() -> None:
    """Test that recalibration updates the existing blind subentry."""
    flow = MagicMock(spec=ConfigSubentryFlow)
    entry = MagicMock()
    subentry = MagicMock()
    flow._get_entry.return_value = entry
    flow._get_reconfigure_subentry.return_value = subentry
    expected_result = {"type": FlowResultType.ABORT}
    flow.async_update_and_abort.return_value = expected_result
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device(
        {
            "id": "3720B8",
            "name": "Living room",
            "enum": "08",
        }
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
