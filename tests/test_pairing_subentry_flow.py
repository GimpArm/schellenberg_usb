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

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
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
    CONF_INITIAL_POSITION,
    CONF_INVERT_DIRECTION,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
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
    # close/stop steps and the post-teach open+stop test actually call -
    # control_blind itself stays mocked too, since this same api spy
    # backs tests of other flow steps that still call it directly.
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
    """control_blind_exclusive() refusing the write must be reported, not
    silently dropped - this step actually calls control_blind_exclusive(),
    not control_blind() directly; overriding the latter here was dead as
    of that fix, since make_flow()'s own default
    control_blind_exclusive (return_value=True) was what this step's write
    still actually resolved against.
    """
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.control_blind_exclusive = AsyncMock(return_value=False)

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
    """Regression: a subentry with valid device_ids but no enum data
    anywhere (neither CONF_COMMAND_ENUM/CONF_DEVICE_ENUM nor
    CONF_STATUS_ENUM) must not sail through the guard with status_enum
    ending up None and reaching handler.set_selected_device() with
    "enum": None, from there into the serial payload itself unchecked -
    it must abort here with a clear reason instead. See the matching
    comment on async_step_calibrate()'s guard in config_flow.py.
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


async def test_set_position_open_confirms_position(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    update_subentry = MagicMock()
    monkeypatch.setattr(
        flow.hass.config_entries, "async_update_subentry", update_subentry
    )

    result = await flow.async_step_set_position_open()

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 100)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 100%" in flow._developer_notice
    # As of v1.6.0, deliberately NOT also persisted to the subentry (see
    # CHANGELOG.md and _async_manual_position_sync()'s own comment in
    # config_flow.py): an unverified manual correction used to durably
    # override the blind's own calibration-confirmed startup fallback.
    # The live entity update above is the only effect now.
    update_subentry.assert_not_called()


async def test_set_position_closed_confirms_position(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    update_subentry = MagicMock()
    monkeypatch.setattr(
        flow.hass.config_entries, "async_update_subentry", update_subentry
    )

    result = await flow.async_step_set_position_closed()

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 0)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 0%" in flow._developer_notice
    # As of v1.6.0, deliberately NOT also persisted - see the sibling
    # set_position_open test above for why.
    update_subentry.assert_not_called()


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


async def test_set_position_manual_applies_given_position(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    update_subentry = MagicMock()
    monkeypatch.setattr(
        flow.hass.config_entries, "async_update_subentry", update_subentry
    )

    result = await flow.async_step_set_position_manual(user_input={"position": 37})

    flow._api.manual_sync_position.assert_called_once_with(DEVICE_ID, 37)
    assert result["type"] is FlowResultType.MENU
    assert "manually confirmed at 37%" in flow._developer_notice
    # As of v1.6.0, deliberately NOT also persisted - see
    # test_set_position_open_confirms_position above for why.
    update_subentry.assert_not_called()


async def test_manual_position_sync_reports_unregistered_when_sync_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered live cover entity must be reported, not silently dropped.

    update_subentry.assert_not_called() here is now trivially true (no call
    site in this method writes to the subentry at all any more, see the
    three tests above) but still worth keeping: it would catch a regression
    that reintroduces a write gated on success, same as any of its siblings.
    """
    flow = make_flow(hass)
    flow._get_reconfigure_subentry = MagicMock(return_value=make_subentry())
    flow._api.manual_sync_position = MagicMock(return_value=False)
    update_subentry = MagicMock()
    monkeypatch.setattr(
        flow.hass.config_entries, "async_update_subentry", update_subentry
    )

    await flow.async_step_set_position_open()

    assert "live cover entity is not registered" in flow._developer_notice
    update_subentry.assert_not_called()


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


# --- Status discovery choice (skip option) ----------------------------------


async def test_status_discovery_choice_shows_a_menu(hass: HomeAssistant) -> None:
    """This choice step is a plain two-option menu, not a form - so a
    person without their original remote to hand, or who doesn't want to
    run discovery right now, is never forced through
    async_step_discover_status()'s own "Start listening" screen just to
    get past it.
    """
    flow = make_flow(hass)

    result = await flow.async_step_status_discovery_choice()

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "status_discovery_choice"
    assert result["menu_options"] == ["discover_status", "skip_status_discovery"]


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


async def test_confirm_status_discovery_updates_existing_keeps_primary_on_conflict(
    hass: HomeAssistant,
) -> None:
    """A *different* device discovered via the remote must not replace an
    already-confirmed status identity on an already-paired blind - it is
    folded into the secondary list instead, never used to replace
    anything.

    This is the _status_discovery_updates_existing=True branch's own
    counterpart to test_confirm_status_discovery_keeps_calibration_
    primary_on_conflict below, for the sibling, Developer-Tools/
    reconfigure path. Without this protection, an electronic
    Gurtwickler's already-working status identity could vanish (and the
    device would produce no reaction in Home Assistant afterward) the
    moment Developer Tools' "Discover status" was run with the original
    remote instead.
    """
    flow = make_flow(hass)
    existing_secondary = [{"device_id": "778899", "enum": "05"}]
    flow._get_reconfigure_subentry = MagicMock(
        return_value=make_subentry(
            **{CONF_SECONDARY_STATUS_IDENTITIES: existing_secondary}
        )
    )
    flow.async_update_and_abort = MagicMock(
        return_value={"type": FlowResultType.ABORT, "reason": "reconfigure_successful"}
    )
    await flow.async_step_discover_status()  # primes _pending_device_id etc.
    flow._apply_remote_status_discovery(
        {
            "primary": {"device_id": "112233", "enum": "01"},
            "secondary": [],
        }
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    flow.async_update_and_abort.assert_called_once()
    data = flow.async_update_and_abort.call_args.kwargs["data"]
    # The already-confirmed primary survives untouched...
    assert data[CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert data[CONF_STATUS_ENUM] == DEVICE_ENUM
    # ...and the newly discovered device is folded into the secondary
    # list alongside what was already there, instead of replacing it.
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "778899", "enum": "05"},
        {"device_id": "112233", "enum": "01"},
    ]
    assert result["reason"] == "reconfigure_successful"


async def test_confirm_status_discovery_preserves_calibration_identity_when_skipped(
    hass: HomeAssistant,
) -> None:
    """A fresh pairing's own live-calibration drive already found a usable
    status identity; skipping (or never running) remote discovery must not
    erase it.

    This is the _pending_finish_data branch's counterpart to
    test_confirm_status_discovery_updates_existing_subentry above - the
    "brand-new-pairing... calls async_create_entry()" branch. Without
    this protection, self._pending_status_* (this *flow's* own copies,
    blank unless async_step_discover_status() ran) would unconditionally
    overwrite whatever data CalibrationFlowHandler.async_step_
    calibration_complete() had already stashed in _pending_finish_data -
    wiping out the calibration drive's own findings even in the common
    case of skipping discovery entirely (see
    async_step_skip_status_discovery()). self._status_discovery_result
    is None here because no discovery attempt ever ran this round - the
    actual signal this logic keys off.
    """
    flow = make_flow(hass)
    flow._pending_device_id = DEVICE_ID
    flow._pending_device_name = "Kitchen Blind"
    flow._pending_finish_data = {
        CONF_OPEN_TIME: 20.0,
        CONF_CLOSE_TIME: 18.0,
        CONF_INITIAL_POSITION: 0,
        CONF_STATUS_DEVICE_ID: "112233",
        CONF_STATUS_ENUM: "02",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
        CONF_SECONDARY_STATUS_IDENTITIES: [{"device_id": "445566", "enum": "03"}],
    }
    # The parent flow's own copies were never updated this round (no
    # guided discovery ran) - still at their post-reset defaults.
    flow._status_discovery_result = None
    flow._pending_status_device_id = None
    flow._pending_status_enum = None
    flow._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
    flow._pending_secondary_status_identities = []
    flow.async_create_entry = MagicMock(
        return_value={"type": FlowResultType.CREATE_ENTRY}
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    flow.async_create_entry.assert_called_once()
    data = flow.async_create_entry.call_args.kwargs["data"]
    assert data[CONF_STATUS_DEVICE_ID] == "112233"
    assert data[CONF_STATUS_ENUM] == "02"
    assert data[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_CALIBRATION
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "445566", "enum": "03"}
    ]
    # Untouched fields from the calibration drive's own data pass through.
    assert data[CONF_INITIAL_POSITION] == 0
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow._pending_finish_data is None


async def test_confirm_status_discovery_keeps_calibration_primary_on_conflict(
    hass: HomeAssistant,
) -> None:
    """A guided remote-discovery attempt that finds a *different*, real
    primary identity must not discard the calibration drive's own,
    already self-echo-confirmed one - it is folded into the secondary
    list instead, never used to replace it.

    Without this protection, both the primary identity and the whole
    secondary list could be overwritten in one go whenever discovery
    found *anything*, so the calibration-confirmed identity would not
    even survive as a secondary. This can genuinely happen with hardware
    like an electronic Gurtwickler, whose motor echoes its own commanded
    moves under one identity while the original remote driving the same
    physical blind is heard under a different one entirely - both real,
    neither one wrong.
    """
    flow = make_flow(hass)
    flow._pending_device_id = DEVICE_ID
    flow._pending_device_name = "Kitchen Blind"
    flow._pending_finish_data = {
        CONF_OPEN_TIME: 20.0,
        CONF_CLOSE_TIME: 18.0,
        CONF_STATUS_DEVICE_ID: "112233",
        CONF_STATUS_ENUM: "02",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
        CONF_SECONDARY_STATUS_IDENTITIES: [],
    }
    flow._apply_remote_status_discovery(
        {
            "primary": {"device_id": DEVICE_ID, "enum": DEVICE_ENUM},
            "secondary": [{"device_id": "778899", "enum": "05"}],
        }
    )
    flow.async_create_entry = MagicMock(
        return_value={"type": FlowResultType.CREATE_ENTRY}
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    data = flow.async_create_entry.call_args.kwargs["data"]
    # The calibration drive's own, already-confirmed primary is untouched...
    assert data[CONF_STATUS_DEVICE_ID] == "112233"
    assert data[CONF_STATUS_ENUM] == "02"
    assert data[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_CALIBRATION
    # ...and both discovery's own primary and its own secondary are folded
    # into the secondary list instead of replacing anything.
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "778899", "enum": "05"},
        {"device_id": DEVICE_ID, "enum": DEVICE_ENUM},
    ]
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_confirm_status_discovery_adopts_discovery_primary_if_none_confirmed(
    hass: HomeAssistant,
) -> None:
    """When the calibration drive itself found no confirmed primary (its
    own self-echo capture came up empty), a guided discovery result is
    the only candidate there is, and must still be adopted - the "keep
    the existing one" protection above only ever applies when there
    actually is an existing, confirmed primary to protect in the first
    place.
    """
    flow = make_flow(hass)
    flow._pending_device_id = DEVICE_ID
    flow._pending_device_name = "Kitchen Blind"
    flow._pending_finish_data = {
        CONF_OPEN_TIME: 20.0,
        CONF_CLOSE_TIME: 18.0,
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
        CONF_SECONDARY_STATUS_IDENTITIES: [],
    }
    flow._apply_remote_status_discovery(
        {
            "primary": {"device_id": DEVICE_ID, "enum": DEVICE_ENUM},
            "secondary": [{"device_id": "778899", "enum": "05"}],
        }
    )
    flow.async_create_entry = MagicMock(
        return_value={"type": FlowResultType.CREATE_ENTRY}
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    data = flow.async_create_entry.call_args.kwargs["data"]
    assert data[CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert data[CONF_STATUS_ENUM] == DEVICE_ENUM
    assert data[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "778899", "enum": "05"}
    ]
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_confirm_status_discovery_matching_primary_is_not_duplicated(
    hass: HomeAssistant,
) -> None:
    """Discovery confirming the *same* identity calibration already found
    is not also appended to the secondary list - the "differs from the
    existing primary" guard must actually exclude a match, not just
    happen to never see one in practice.
    """
    flow = make_flow(hass)
    flow._pending_device_id = DEVICE_ID
    flow._pending_device_name = "Kitchen Blind"
    flow._pending_finish_data = {
        CONF_OPEN_TIME: 20.0,
        CONF_CLOSE_TIME: 18.0,
        CONF_STATUS_DEVICE_ID: DEVICE_ID,
        CONF_STATUS_ENUM: DEVICE_ENUM,
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
        CONF_SECONDARY_STATUS_IDENTITIES: [],
    }
    flow._apply_remote_status_discovery(
        {
            "primary": {"device_id": DEVICE_ID, "enum": DEVICE_ENUM},
            "secondary": [],
        }
    )
    flow.async_create_entry = MagicMock(
        return_value={"type": FlowResultType.CREATE_ENTRY}
    )

    result = await flow.async_step_confirm_status_discovery(user_input={})

    data = flow.async_create_entry.call_args.kwargs["data"]
    assert data[CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert data[CONF_STATUS_ENUM] == DEVICE_ENUM
    assert data[CONF_SECONDARY_STATUS_IDENTITIES] == []
    assert result["type"] is FlowResultType.CREATE_ENTRY


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


# --- async_remove() - releasing a still-open calibration capture the
# instant this flow goes away (e.g. the person closes the dialog mid-form,
# between the two calibration drives), instead of leaving it to time out on
# its own via api.py's STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS. ---------


async def test_async_remove_is_a_safe_noop_when_calibration_was_never_started(
    hass: HomeAssistant,
) -> None:
    """A flow removed before ever reaching a calibration step - cancelled
    straight from the reconfigure/pairing menu, say - has never called
    _get_calibration_handler(), so calibration_handler is still None. This
    must not raise; _finish_calibration_capture() is only ever reached
    through a real handler instance.
    """
    flow = make_flow(hass)
    assert flow.calibration_handler is None

    flow.async_remove()

    assert flow.calibration_handler is None


async def test_async_remove_releases_an_active_calibration_capture(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
) -> None:
    """The common real case: a capture is genuinely open (the person left
    the dialog standing on calibration_open_instruction/_close_instruction,
    between the two drives) when the flow is removed.

    Drives CalibrationFlowHandler's own real _start_calibration_capture()/
    _finish_calibration_capture() against a genuine SchellenbergUsbApi
    (connected_api, from conftest.py) rather than make_flow()'s usual bare
    MagicMock: _runtime_api() (options_flow_calibration.py) gates on
    isinstance(api, SchellenbergUsbApi), which a plain, spec-less
    MagicMock() never satisfies - with one, _start_calibration_capture()
    would return False before ever reaching async_remove(), the method
    this test exists to exercise. Reassigning flow._api/_get_entry() right
    after make_flow() mirrors exactly what that helper itself just wired,
    only with a real instance in place of its usual mock; every other
    test in this file is unaffected, since they never call
    _get_calibration_handler()/_runtime_api() at all.
    """
    flow = make_flow(hass)
    flow._api = connected_api
    flow._get_entry = MagicMock(
        return_value=SimpleNamespace(runtime_data=connected_api, subentries={})
    )
    handler = flow._get_calibration_handler()
    assert handler._start_calibration_capture(phase="opening") is True
    assert handler._capture_active is True
    assert handler._capture_api is connected_api
    connected_api.finish_status_frame_capture = MagicMock(
        return_value={"end_reason": "flow_removed", "primary": None}
    )

    flow.async_remove()

    connected_api.finish_status_frame_capture.assert_called_once_with(
        end_reason="flow_removed", exclude_device_ids=frozenset()
    )
    # The capture is actually released - not merely asked to be - so a
    # different blind's own calibration/status-discovery flow could claim
    # the hub-wide buffer right away instead of waiting out the staleness
    # timeout.
    assert handler._capture_active is False
    assert handler._capture_api is None
    assert handler._calibration_discovery_result == {
        "end_reason": "flow_removed",
        "primary": None,
    }
