"""Tests for config_flow.py's SchellenbergPairingSubentryFlow.

Instantiated directly rather than through the full subentry-flow manager:
_get_entry() and _get_reconfigure_subentry() (normally wired up by Home
Assistant's flow manager once a flow is properly initiated against a real,
already-loaded parent config entry) are stubbed directly on the instance
here instead. That keeps the actual business logic under test - field
validation, the manual-entry data shaping, the radio-pairing/naming steps,
the Developer Tools diagnostics dispatch (teach_motor, send_raw_command,
reset_stick, copy_diagnostics), and status-identity discovery - while
avoiding the much larger setup a full
hass.config_entries.subentries.async_init() flow would need (a real loaded
hub entry with a real, connected serial API). async_show_form/
async_show_menu/async_abort/async_create_entry are all real Home Assistant
FlowHandler methods, which build result dicts without needing manager
registration - including when called indirectly through a real (not mocked)
CalibrationFlowHandler, as happens at the end of the legacy/hybrid pairing
workflows below.

test_motor / did_motor_move / manual_times (the paired "quick command test
before saving/calibrating" sub-flow, reachable from the hybrid-pairing,
manual-entry, and "test existing blind" reconfigure workflows) are covered
the same lightweight way. async_step_edit's successful-save path and
confirm_status_discovery's existing-blind branch are different: both call
async_update_and_abort(), which - unlike async_create_entry()/async_abort()/
async_show_form()/async_show_menu() - internally calls the real
hass.config_entries.async_update_subentry() and so needs a subentry that is
genuinely registered on a genuinely-added config entry, not a MagicMock. For
just those two tests, make_registered_subentry() below builds exactly that
(reusing MockConfigEntry/ConfigSubentry/async_add_subentry, the same real
building blocks test_init.py's full-lifecycle test uses) instead of
make_subentry()'s MagicMock - see its docstring for why this is the one
place in the file that needs it.

Not covered here (see the test suite's README): the frame-capture/status-
auto-discovery that happens *through the live API* during calibration - that
lives in options_flow_calibration.py and is covered in test_calibration_flow.py
instead, not here.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schellenberg_usb.config_flow import (
    SchellenbergPairingSubentryFlow,
)
from custom_components.schellenberg_usb.const import (
    CMD_STOP,
    CMD_UP,
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    SUBENTRY_TYPE_BLIND,
)


def make_flow(hass: HomeAssistant, *, api: MagicMock | None = None, subentries=()):
    """Build a subentry flow with _get_entry() stubbed to a fake hub entry."""
    flow = SchellenbergPairingSubentryFlow()
    flow.hass = hass
    # The real subentry-flow manager sets this from the context it's
    # initiated with; async_create_entry() (used by the save-manual success
    # path) checks it and rejects anything but "user" as the source of a
    # *new* subentry. Direct instantiation skips the manager entirely, so it
    # has to be stubbed here too, alongside _get_entry() below. `source` is
    # a read-only property backed by `context["source"]` (no setter), so
    # the context dict itself has to carry it.
    flow.context = {"source": SOURCE_USER}

    if api is None:
        api = MagicMock()
        api.control_blind = AsyncMock(return_value=True)
        api.transmit_block_reason = None
        api.is_connected = True
        api.device_mode = "listening"
        api.transmit_ready = True
        api.busy_latched = False
        api.pairing_active = False
        api.transmitter_active = False
        # These all feed the Developer Tools diagnostics snapshot via
        # `... or dict(empty_frame)` fallbacks in config_flow.py - a bare
        # MagicMock() would be truthy and break subscripting, so they must
        # explicitly return None to exercise the real fallback path.
        api.get_last_received_for_identities = MagicMock(return_value=None)
        api.get_last_primary_tracking_frame = MagicMock(return_value=None)
        api.get_last_secondary_frame = MagicMock(return_value=None)
        api.get_last_position_update = MagicMock(return_value=None)
        api.get_last_manual_position_sync = MagicMock(return_value=None)

    fake_entry = MagicMock()
    fake_entry.runtime_data = api
    fake_entry.subentries = {s.subentry_id: s for s in subentries}
    flow._get_entry = MagicMock(return_value=fake_entry)
    flow._api = api  # convenience handle for test assertions
    return flow


def make_subentry(device_id: str = "5D3E7C", device_enum: str = "01") -> MagicMock:
    subentry = MagicMock()
    subentry.title = "Living room"
    subentry.data = {CONF_DEVICE_ID: device_id, CONF_DEVICE_ENUM: device_enum}
    return subentry


def make_registered_subentry(
    hass: HomeAssistant,
    api: MagicMock | None = None,
    *,
    device_id: str = "5D3E7C",
    device_enum: str = "01",
) -> tuple[MockConfigEntry, ConfigSubentry]:
    """Build a real hub entry with one real, registered blind subentry.

    Only needed for the two async_update_and_abort()-dependent tests below -
    see the module docstring. A real MockConfigEntry + a real ConfigSubentry
    genuinely added via hass.config_entries.async_add_subentry() give
    hass.config_entries.async_update_subentry() (called internally by
    async_update_and_abort()) real backing data to update, unlike
    make_subentry()'s MagicMock.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/ttyUSB0"})
    entry.add_to_hass(hass)
    entry.runtime_data = api if api is not None else MagicMock()
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {CONF_DEVICE_ID: device_id, CONF_DEVICE_ENUM: device_enum}
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Living room",
        unique_id=device_id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return entry, subentry


VALID_MANUAL_INPUT = {
    CONF_DEVICE_NAME: "Living room",
    CONF_DEVICE_ID: "5d3e7c",
    # Must be exactly 2 hex characters after .upper() - config_flow.py's
    # async_step_manual (line ~378) checks _is_hex_value(command_enum, 2)
    # with no zero-padding, so a bare "1" is rejected as invalid_device_enum
    # rather than treated as "01". This was previously "1" here, which made
    # this "valid" input actually invalid and broke every test relying on it.
    CONF_DEVICE_ENUM: "01",
    CONF_OPEN_TIME_SECONDS: 30.0,
    CONF_CLOSE_TIME_SECONDS: 28.0,
}


def test_is_hex_value() -> None:
    assert SchellenbergPairingSubentryFlow._is_hex_value("5D3E7C", 6) is True
    # Lowercase is deliberately not accepted - callers normalize with
    # .upper() before calling this.
    assert SchellenbergPairingSubentryFlow._is_hex_value("5d3e7c", 6) is False
    assert SchellenbergPairingSubentryFlow._is_hex_value("5D3E7", 6) is False
    assert SchellenbergPairingSubentryFlow._is_hex_value("GGGGGG", 6) is False


async def test_manual_step_missing_name_is_rejected(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    bad_input = {**VALID_MANUAL_INPUT, CONF_DEVICE_NAME: "   "}
    result = await flow.async_step_manual(bad_input)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_DEVICE_NAME] == "required"


async def test_manual_step_invalid_device_id_is_rejected(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    bad_input = {**VALID_MANUAL_INPUT, CONF_DEVICE_ID: "not-hex"}
    result = await flow.async_step_manual(bad_input)
    assert result["errors"][CONF_DEVICE_ID] == "invalid_device_id"


async def test_manual_step_invalid_travel_time_is_rejected(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    bad_input = {**VALID_MANUAL_INPUT, CONF_OPEN_TIME_SECONDS: 0}
    result = await flow.async_step_manual(bad_input)
    assert result["errors"][CONF_OPEN_TIME_SECONDS] == "invalid_travel_time"


async def test_manual_step_incomplete_status_identity_is_rejected(
    hass: HomeAssistant,
) -> None:
    from custom_components.schellenberg_usb.const import CONF_STATUS_DEVICE_ID

    flow = make_flow(hass)
    bad_input = {**VALID_MANUAL_INPUT, CONF_STATUS_DEVICE_ID: "5D3E7C"}
    result = await flow.async_step_manual(bad_input)
    assert "status" in "".join(result["errors"].values())


async def test_manual_step_duplicate_device_id_is_rejected(hass: HomeAssistant) -> None:
    existing_subentry = make_subentry(device_id="5D3E7C")
    existing_subentry.subentry_id = "sub1"
    flow = make_flow(hass, subentries=[existing_subentry])

    result = await flow.async_step_manual(dict(VALID_MANUAL_INPUT))
    assert result["errors"][CONF_DEVICE_ID] == "already_configured"


async def test_manual_step_valid_input_advances_to_menu(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_manual(dict(VALID_MANUAL_INPUT))
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "manual_next"
    assert flow._pending_device_id == "5D3E7C"
    assert flow._pending_device_enum == "01"


async def test_save_manual_creates_entry_with_expected_data(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    await flow.async_step_manual(dict(VALID_MANUAL_INPUT))

    result = await flow.async_step_save_manual()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == "5D3E7C"
    assert result["data"][CONF_OPEN_TIME] == 30.0
    assert result["data"][CONF_CLOSE_TIME] == 28.0
    assert result["unique_id"] == "5D3E7C"


async def test_save_manual_aborts_if_called_before_manual_data_collected(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_save_manual()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_developer_command_open_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_test_open()

    flow._api.control_blind.assert_awaited_once_with(
        "01", CMD_UP, device_id="5D3E7C", source="developer_tools"
    )
    assert result["type"] == FlowResultType.MENU
    assert "written successfully" in flow._developer_notice


async def test_developer_command_blocked_by_transmit_reason(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.transmit_block_reason = "stick disconnected"

    await flow.async_step_test_open()

    flow._api.control_blind.assert_not_awaited()
    assert "blocked" in flow._developer_notice
    assert "stick disconnected" in flow._developer_notice


async def test_developer_command_failure_sets_failed_notice(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.control_blind = AsyncMock(return_value=False)

    await flow.async_step_test_stop()

    flow._api.control_blind.assert_awaited_once_with(
        "01", CMD_STOP, device_id="5D3E7C", source="developer_tools"
    )
    assert "failed" in flow._developer_notice


async def test_developer_command_exception_is_caught_and_reported_as_failed(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.control_blind = AsyncMock(side_effect=RuntimeError("serial gone"))

    result = await flow.async_step_test_close()

    assert "failed" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


async def test_manual_position_sync_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.manual_sync_position = MagicMock(return_value=True)

    await flow.async_step_set_position_open()

    flow._api.manual_sync_position.assert_called_once_with("5D3E7C", 100)
    assert "manually confirmed" in flow._developer_notice.lower()


async def test_manual_position_sync_failure_when_entity_missing(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.manual_sync_position = MagicMock(return_value=False)

    await flow.async_step_set_position_closed()

    flow._api.manual_sync_position.assert_called_once_with("5D3E7C", 0)
    assert "failed" in flow._developer_notice.lower()


async def test_edit_step_validation_rejects_invalid_enum(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    bad_input = {**VALID_MANUAL_INPUT, CONF_DEVICE_ENUM: "not-hex"}
    result = await flow.async_step_edit(bad_input)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_DEVICE_ENUM] == "invalid_device_enum"


# --- Radio pairing: pair_device / pair_test / _async_pair_device -----------


async def test_pair_device_shows_form_when_no_input(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_pair_device()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pair_device"
    assert flow._pairing_workflow == "legacy"


async def test_pair_test_shows_form_and_sets_hybrid_workflow(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_pair_test()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pair_test"
    assert flow._pairing_workflow == "hybrid"


async def test_pair_device_timeout_aborts(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._api.pair_device_and_wait = AsyncMock(return_value=None)

    result = await flow.async_step_pair_device({})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"


async def test_pair_device_success_advances_to_name_device_form(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._api.pair_device_and_wait = AsyncMock(return_value=("5D3E7C", "01"))
    # Pre-set stale status-identity state from a hypothetical earlier attempt,
    # to prove a fresh successful pair really clears it rather than carrying
    # it over to the newly-paired device.
    flow._pending_status_device_id = "AAAAAA"
    flow._pending_status_enum = "02"

    result = await flow.async_step_pair_device({})

    assert flow._pending_device_id == "5D3E7C"
    assert flow._pending_device_enum == "01"
    assert flow._pending_status_device_id is None
    assert flow._pending_status_enum is None
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "name_device"
    assert result["description_placeholders"]["device_id"] == "5D3E7C"


# --- name_device -------------------------------------------------------


async def test_name_device_aborts_when_no_pending_device(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_name_device()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "pairing_failed"


async def test_name_device_shows_form_with_device_id_placeholder(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"

    result = await flow.async_step_name_device()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "name_device"
    assert result["description_placeholders"]["device_id"] == "5D3E7C"


async def test_name_device_legacy_workflow_starts_calibration(
    hass: HomeAssistant,
) -> None:
    """The default ("legacy") workflow hands straight off to a real
    CalibrationFlowHandler - async_step_calibration_close(None) is a real
    FlowHandler.async_show_form() call on this same flow instance, not a
    mock, so this also exercises that the bare-instantiated flow is a valid
    collaborator for CalibrationFlowHandler (see the module docstring).
    """
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    # _pairing_workflow defaults to "legacy" (set in __init__).

    result = await flow.async_step_name_device({CONF_DEVICE_NAME: "Living room"})

    assert flow._pending_device_name == "Living room"
    assert flow.calibration_handler is not None
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_close"


async def test_name_device_hybrid_workflow_advances_to_test_motor(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pairing_workflow = "hybrid"

    result = await flow.async_step_name_device({CONF_DEVICE_NAME: "Living room"})

    assert flow.calibration_handler is not None
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "test_motor"


# --- Developer Tools: teach_motor -------------------------------------------


async def test_teach_motor_shows_form(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_teach_motor()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "teach_motor"
    assert result["description_placeholders"]["command_device_id"] == "5D3E7C"


async def test_teach_motor_blocked_by_transmit_reason(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.transmit_block_reason = "stick disconnected"

    result = await flow.async_step_teach_motor({})

    assert "blocked" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


async def test_teach_motor_success_sends_teach_open_stop(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.teach_motor = AsyncMock(return_value=True)

    result = await flow.async_step_teach_motor({})

    flow._api.teach_motor.assert_awaited_once_with(
        "01", device_id="5D3E7C", source="developer_tools"
    )
    assert flow._api.control_blind.await_count == 2
    assert "Teach, Open, and Stop were transmitted" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


async def test_teach_motor_failure_when_teach_returns_false(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.teach_motor = AsyncMock(return_value=False)

    result = await flow.async_step_teach_motor({})

    flow._api.control_blind.assert_not_awaited()
    assert "failed" in flow._developer_notice.lower()
    assert result["type"] == FlowResultType.MENU


async def test_teach_motor_exception_is_caught_and_reported_as_failed(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.teach_motor = AsyncMock(side_effect=RuntimeError("serial gone"))

    result = await flow.async_step_teach_motor({})

    assert "failed" in flow._developer_notice.lower()
    assert result["type"] == FlowResultType.MENU


# --- Developer Tools: send_raw_command --------------------------------------


async def test_send_raw_command_shows_form_with_placeholders(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_send_raw_command()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "send_raw_command"
    assert result["description_placeholders"]["command_enum"] == "01"


async def test_send_raw_command_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.send_raw_transmit = AsyncMock(return_value=True)

    result = await flow.async_step_send_raw_command({"payload": "ss01910000"})

    flow._api.send_raw_transmit.assert_awaited_once_with(
        "ss01910000", source="developer_tools"
    )
    assert "was written" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


async def test_send_raw_command_invalid_payload_shows_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.send_raw_transmit = AsyncMock(side_effect=ValueError("bad payload"))

    result = await flow.async_step_send_raw_command({"payload": "not-valid"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["payload"] == "invalid_raw_payload"


async def test_send_raw_command_transmit_failure_shows_base_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.send_raw_transmit = AsyncMock(return_value=False)

    result = await flow.async_step_send_raw_command({"payload": "ss01910000"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "transmit_failed"


# --- Developer Tools: reset_stick --------------------------------------------


async def test_reset_stick_success(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.reset_and_reconnect = AsyncMock(return_value=True)

    result = await flow.async_step_reset_stick()

    flow._api.reset_and_reconnect.assert_awaited_once()
    assert "ready for transmit" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


async def test_reset_stick_not_ready_reports_status(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.reset_and_reconnect = AsyncMock(return_value=False)
    flow._api.is_connected = False
    flow._api.device_mode = None

    result = await flow.async_step_reset_stick()

    assert "did not become ready" in flow._developer_notice
    assert "connected=False" in flow._developer_notice
    assert result["type"] == FlowResultType.MENU


# --- Developer Tools: copy_diagnostics ---------------------------------------


async def test_copy_diagnostics_shows_form_with_snapshot(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_copy_diagnostics()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "copy_diagnostics"


async def test_copy_diagnostics_user_input_returns_to_developer_tools(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_copy_diagnostics({"diagnostics": "whatever"})

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "developer_tools"


# --- Status-identity discovery: discover_status / confirm_status_discovery -
#
# Only the *new-pairing* path is covered (_status_discovery_updates_existing
# stays False, its __init__ default, because _pending_device_id is already
# set from pairing before discover_status runs) - see the module docstring
# for why the existing-blind async_update_and_abort branch is excluded.


async def test_discover_status_shows_form_for_new_pairing(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pending_device_name = "Living room"

    result = await flow.async_step_discover_status()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "discover_status"
    assert flow._status_discovery_updates_existing is False


async def test_discover_status_success_advances_to_confirm(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pending_device_name = "Living room"
    flow._api.async_discover_status_identities = AsyncMock(
        return_value={
            "primary": {
                "device_id": "5D3E7C",
                "enum": "01",
                "commands": ["00"],
                "timestamps": ["12:00:00"],
            },
            "secondary": [],
            "unknown_commands": [],
            "frames": [],
        }
    )

    result = await flow.async_step_discover_status({})

    assert flow._pending_status_device_id == "5D3E7C"
    assert flow._pending_status_enum == "01"
    assert flow._pending_status_identity_source == STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm_status_discovery"


async def test_discover_status_connection_error_shows_unavailable_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._api.async_discover_status_identities = AsyncMock(
        side_effect=ConnectionError("no ack")
    )

    result = await flow.async_step_discover_status({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "discover_status"
    assert result["errors"]["base"] == "status_discovery_unavailable"


async def test_discover_status_runtime_error_shows_busy_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._api.async_discover_status_identities = AsyncMock(
        side_effect=RuntimeError("busy")
    )

    result = await flow.async_step_discover_status({})

    assert result["errors"]["base"] == "status_discovery_busy"


async def test_confirm_status_discovery_shows_form_with_placeholders(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._status_discovery_result = {
        "primary": {
            "device_id": "5D3E7C",
            "enum": "01",
            "commands": ["00"],
            "timestamps": ["12:00:00"],
        },
        "secondary": [],
        "unknown_commands": [],
        "frames": [
            {
                "time": "12:00:00",
                "device_id": "5D3E7C",
                "enum": "01",
                "command": "00",
                "phase": "open",
            }
        ],
    }

    result = await flow.async_step_confirm_status_discovery()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm_status_discovery"
    assert result["description_placeholders"]["primary_identity"] == "5D3E7C/01"
    assert result["description_placeholders"]["frame_count"] == "1"


async def test_confirm_status_discovery_creates_entry_for_new_pairing(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pending_device_name = "Living room"
    flow._pending_open_time = 30.0
    flow._pending_close_time = 28.0
    flow._pending_status_device_id = "5D3E7C"
    flow._pending_status_enum = "01"
    flow._pending_status_identity_source = STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    # _status_discovery_updates_existing stays False, the __init__ default -
    # this is the new-pairing path, not a reconfigure of an existing blind.

    result = await flow.async_step_confirm_status_discovery({})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "5D3E7C"
    assert result["data"][CONF_STATUS_DEVICE_ID] == "5D3E7C"
    assert result["data"][CONF_STATUS_ENUM] == "01"


async def test_confirm_status_discovery_aborts_when_pending_data_missing(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_confirm_status_discovery({})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


# --- test_motor / did_motor_move / manual_times / test_existing ------------
#
# The paired "quick command test before saving" sub-flow. It is reachable
# from three places: the hybrid-pairing workflow (name_device's hybrid
# branch, tested above), the manual-entry workflow (async_step_manual_next's
# own "test_motor" menu option), and the "test existing blind" reconfigure
# menu option (async_step_test_existing, tested at the bottom of this
# section).


async def test_test_motor_aborts_without_pending_device(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_test_motor()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_test_motor_shows_form(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"

    result = await flow.async_step_test_motor()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["device_id"] == "5D3E7C"


async def test_test_motor_open_command_failure_shows_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._api.control_blind = AsyncMock(return_value=False)

    result = await flow.async_step_test_motor({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["errors"]["base"] == "command_failed"


async def test_test_motor_stop_command_failure_shows_error(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    # First call (open) succeeds, second call (the auto-stop) fails.
    flow._api.control_blind = AsyncMock(side_effect=[True, False])

    result = await flow.async_step_test_motor({})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "command_failed"


async def test_test_motor_success_advances_to_did_motor_move(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"

    result = await flow.async_step_test_motor({})

    assert flow._api.control_blind.await_count == 2  # open, then auto-stop
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"


async def test_did_motor_move_shows_form(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"

    result = await flow.async_step_did_motor_move()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"


async def test_did_motor_move_false_returns_to_manual_form(
    hass: HomeAssistant,
) -> None:
    """Reachable via the manual-entry workflow's own test-before-saving menu
    option. The code treats every non-"existing" workflow identically here,
    but "manual" is the one actually reachable this way in the real app.
    """
    flow = make_flow(hass)
    flow._pairing_workflow = "manual"

    result = await flow.async_step_did_motor_move({"motor_moved": False})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_did_motor_move_false_for_existing_workflow_returns_to_edit_form(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pairing_workflow = "existing"
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_did_motor_move({"motor_moved": False})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit"


async def test_did_motor_move_true_for_existing_workflow_aborts_success(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pairing_workflow = "existing"

    result = await flow.async_step_did_motor_move({"motor_moved": True})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "command_test_successful"


async def test_did_motor_move_true_for_hybrid_workflow_shows_test_success_menu(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pairing_workflow = "hybrid"

    result = await flow.async_step_did_motor_move({"motor_moved": True})

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "test_success"
    assert result["menu_options"] == ["calibration_close", "manual_times"]


async def test_did_motor_move_true_for_manual_workflow_saves(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pairing_workflow = "manual"
    flow._pending_device_name = "Living room"
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pending_open_time = 30.0
    flow._pending_close_time = 28.0

    result = await flow.async_step_did_motor_move({"motor_moved": True})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "5D3E7C"


async def test_manual_times_shows_form(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    result = await flow.async_step_manual_times()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual_times"


async def test_manual_times_success_advances_to_discover_status(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._pending_device_id = "5D3E7C"
    flow._pending_device_enum = "01"
    flow._pending_device_name = "Living room"

    result = await flow.async_step_manual_times(
        {CONF_OPEN_TIME_SECONDS: 25.0, CONF_CLOSE_TIME_SECONDS: 24.0}
    )

    assert flow._pending_open_time == 25.0
    assert flow._pending_close_time == 24.0
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "discover_status"


async def test_test_existing_loads_subentry_and_shows_test_motor_form(
    hass: HomeAssistant,
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())

    result = await flow.async_step_test_existing()

    assert flow._pairing_workflow == "existing"
    assert flow._pending_device_id == "5D3E7C"
    assert flow._pending_device_enum == "01"
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "test_motor"


# --- The two async_update_and_abort()-dependent save paths, via a real,
# registered subentry (see make_registered_subentry() and the module
# docstring for why this is the one place in the file that needs one). ------


async def test_edit_step_success_persists_via_real_subentry_update(
    hass: HomeAssistant,
) -> None:
    entry, subentry = make_registered_subentry(hass)
    flow = make_flow(hass)
    flow._get_entry = MagicMock(return_value=entry)
    flow._get_reconfigure_subentry = MagicMock(return_value=subentry)

    good_input = {**VALID_MANUAL_INPUT, CONF_DEVICE_NAME: "Updated name"}
    result = await flow.async_step_edit(good_input)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Re-fetched from the registry rather than reusing `subentry` - a
    # successful update replaces it with a new, updated ConfigSubentry
    # object rather than mutating the old one in place.
    updated = entry.subentries[subentry.subentry_id]
    assert updated.title == "Updated name"
    assert updated.data[CONF_DEVICE_ID] == "5D3E7C"
    assert updated.data[CONF_OPEN_TIME] == 30.0
    assert updated.data[CONF_CLOSE_TIME] == 28.0


async def test_confirm_status_discovery_updates_existing_blind_via_real_subentry(
    hass: HomeAssistant,
) -> None:
    """Unlike the new-pairing confirm_status_discovery tests above, this
    drives the existing-blind branch, which also calls async_update_and_abort()
    for real - see make_registered_subentry().
    """
    entry, subentry = make_registered_subentry(hass)
    flow = make_flow(hass)
    flow._get_entry = MagicMock(return_value=entry)
    flow._get_reconfigure_subentry = MagicMock(return_value=subentry)
    flow._prepare_existing_status_discovery()
    assert flow._status_discovery_updates_existing is True  # sanity check

    flow._pending_status_device_id = "AA11BB"
    flow._pending_status_enum = "02"
    flow._pending_status_identity_source = STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY

    result = await flow.async_step_confirm_status_discovery({})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[subentry.subentry_id]
    assert updated.data[CONF_STATUS_DEVICE_ID] == "AA11BB"
    assert updated.data[CONF_STATUS_ENUM] == "02"
    assert (
        updated.data[CONF_STATUS_IDENTITY_SOURCE]
        == STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    )
