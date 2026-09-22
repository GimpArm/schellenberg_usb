"""Tests for options_flow_calibration.py's CalibrationFlowHandler.

CalibrationFlowHandler is a plain helper class (not a FlowHandler itself) -
config_flow.py's SchellenbergPairingSubentryFlow constructs one and delegates
several of its own async_step_calibration_* methods straight to it. It is
exercised here with a MagicMock `flow` collaborator (its async_show_form /
async_abort / async_create_entry / async_update_and_abort are given
side_effect lambdas that turn kwargs into plain, inspectable result dicts,
matching how the real FlowHandler methods build result dicts), but a REAL
`hass` - so the dispatcher-based movement-wait logic
(_wait_for_movement_start / _wait_for_stop_event) runs for real against a
real event bus, only the flow-result plumbing is faked.

The phase-labelled raw-frame capture that happens *through the live API*
during calibration (_start_calibration_capture / _set_calibration_capture_phase /
_finish_calibration_capture, all going through self._runtime_api()) is
covered too, in the "Frame capture, via the live API" section near the
bottom: make_handler_with_live_api() gives _get_entry() a
MagicMock(spec=SchellenbergUsbApi) instead of make_handler()'s plain
MagicMock, so _runtime_api()'s isinstance() check succeeds and the capture
calls actually fire, instead of silently no-op'ing as they do for every
other test in this file. The pure data-transformation half of that
discovery (_apply_calibration_status_candidates, _calibration_record) is
covered separately, by setting _calibration_discovery_result directly,
without needing a live API at all. The real 300-second CALIBRATION_TIMEOUT
itself is also covered (see the "Real timeout" section): CALIBRATION_TIMEOUT
is patched down to a fraction of a second for those specific tests, so the
actual asyncio.wait_for(..., timeout=CALIBRATION_TIMEOUT) genuinely elapses
and raises TimeoutError for real, without the suite having to wait out 300
real seconds. That's a different thing from the "Timeout / error branches"
tests above, which mock _wait_for_movement_start/_wait_for_stop_event
themselves to return False directly - those cover what happens *after* a
timeout is assumed; the "Real timeout" ones cover the timeout actually
happening.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_CLOSE_TIME,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_STATUS_DEVICE_ID,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
)
from custom_components.schellenberg_usb.options_flow_calibration import (
    CalibrationFlowHandler,
)


def make_handler(hass: HomeAssistant) -> CalibrationFlowHandler:
    """Build a CalibrationFlowHandler with a MagicMock ConfigSubentryFlow.

    _get_entry() returns a plain MagicMock (not a SchellenbergUsbApi
    instance), so _runtime_api() deliberately resolves to None - see the
    module docstring for what that excludes.
    """
    flow = MagicMock()
    flow.hass = hass
    flow.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": FlowResultType.FORM, **kwargs}
    )
    flow.async_abort = MagicMock(
        side_effect=lambda **kwargs: {"type": FlowResultType.ABORT, **kwargs}
    )
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": FlowResultType.CREATE_ENTRY, **kwargs}
    )
    flow.async_update_and_abort = MagicMock(
        side_effect=lambda *args, **kwargs: {
            "type": FlowResultType.ABORT,
            "reason": "reconfigure_successful",
            **kwargs,
        }
    )
    flow._get_entry = MagicMock(return_value=MagicMock())
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock())
    return CalibrationFlowHandler(flow)


def make_handler_with_live_api(
    hass: HomeAssistant, api: MagicMock | None = None
) -> tuple[CalibrationFlowHandler, MagicMock]:
    """Build a handler whose _runtime_api() resolves to a real-ish API.

    make_handler() above deliberately gives _get_entry() a plain MagicMock(),
    so _runtime_api()'s isinstance(api, SchellenbergUsbApi) check fails and
    the phase-labelled frame-capture calls are silent no-ops (see the module
    docstring). MagicMock(spec=SchellenbergUsbApi) passes that isinstance()
    check instead - a standard unittest.mock feature - so the real
    capture-call wiring in _start_calibration_capture/
    _set_calibration_capture_phase/_finish_calibration_capture actually runs.
    """
    if api is None:
        api = MagicMock(spec=SchellenbergUsbApi)
    handler = make_handler(hass)
    fake_entry = MagicMock()
    fake_entry.runtime_data = api
    handler.flow._get_entry = MagicMock(return_value=fake_entry)
    return handler, api


async def _send_device_event(hass: HomeAssistant, device_id: str, command: str) -> None:
    """Dispatch a device event and give the loop time to process it.

    async_dispatcher_send() may invoke a plain (non-@callback) listener via
    HA's executor rather than inline, and the listener itself only schedules
    the asyncio.Event via loop.call_soon_threadsafe() (see
    _wait_for_movement_start / _wait_for_stop_event) rather than setting it
    directly - so the waiting coroutine needs a real moment to wake up. A
    short real sleep is used, rather than a bare asyncio.sleep(0), so this
    isn't sensitive to exactly how many internal hops asyncio/HA need; the
    outer asyncio.wait_for(task, timeout=5) in each test is the real safety
    net if that assumption is ever too tight.
    """
    async_dispatcher_send(hass, f"{SIGNAL_DEVICE_EVENT}_{device_id}", command)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)


# --- calibration_close / calibration_open_instruction: form-showing and the
# two-hop structure (calibration_close's real input cascades straight into
# calibration_open_instruction() with NO input, which itself just shows a
# form on that first call rather than waiting for movement) ----------------


async def test_close_step_aborts_without_selected_device(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    result = await handler.async_step_calibration_close()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_close_step_shows_form_when_device_selected(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    result = await handler.async_step_calibration_close()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_close"


async def test_close_step_with_input_advances_to_open_instruction_form(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    result = await handler.async_step_calibration_close({})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"


async def test_close_instruction_aborts_without_selected_device(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    result = await handler.async_step_calibration_close_instruction()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_close_instruction_shows_form_before_waiting(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    result = await handler.async_step_calibration_close_instruction()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_close_instruction"


# --- Real dispatcher-driven waiting ----------------------------------------


async def test_open_instruction_waits_for_movement_then_measures_open_time(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    task = asyncio.ensure_future(handler.async_step_calibration_open_instruction({}))
    await asyncio.sleep(0.05)  # let it register the dispatcher listener

    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_UP)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)

    result = await asyncio.wait_for(task, timeout=5)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_close_instruction"
    assert handler._open_time is not None
    assert handler._open_time >= 0.1


async def test_open_instruction_respects_invert_direction(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device(
        {"id": "5D3E7C", "name": "Living room", CONF_INVERT_DIRECTION: True}
    )

    task = asyncio.ensure_future(handler.async_step_calibration_open_instruction({}))
    await asyncio.sleep(0.05)

    # For an inverted device, logical "opening" is a physical downward move,
    # so the plain "up" event must NOT satisfy the wait.
    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_UP)
    assert not task.done()

    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_DOWN)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)

    result = await asyncio.wait_for(task, timeout=5)
    assert result["step_id"] == "calibration_close_instruction"


async def test_full_open_then_close_cycle_reaches_complete_step(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    open_task = asyncio.ensure_future(
        handler.async_step_calibration_open_instruction({})
    )
    await asyncio.sleep(0.05)
    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_UP)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)
    open_result = await asyncio.wait_for(open_task, timeout=5)
    assert open_result["step_id"] == "calibration_close_instruction"

    close_task = asyncio.ensure_future(
        handler.async_step_calibration_close_instruction({})
    )
    await asyncio.sleep(0.05)
    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_DOWN)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)
    close_result = await asyncio.wait_for(close_task, timeout=5)

    assert close_result["step_id"] == "calibration_complete"
    assert handler._close_time is not None
    assert handler._close_time >= 0.1


# --- Timeout / error branches, exercised by mocking the private wait
# helpers directly rather than waiting out the real 300-second timeout ------


async def test_open_instruction_start_timeout_shows_error(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._wait_for_movement_start = AsyncMock(return_value=False)

    result = await handler.async_step_calibration_open_instruction({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"]["base"] == "calibration_start_timeout"


async def test_open_instruction_stop_timeout_shows_error(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._wait_for_movement_start = AsyncMock(return_value=True)
    handler._wait_for_stop_event = AsyncMock(return_value=False)

    result = await handler.async_step_calibration_open_instruction({})

    assert result["errors"]["base"] == "calibration_timeout"


async def test_open_instruction_generic_exception_shows_unknown_error(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._wait_for_movement_start = AsyncMock(side_effect=RuntimeError("boom"))

    result = await handler.async_step_calibration_open_instruction({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"]["base"] == "unknown"


async def test_open_instruction_cancelled_error_reraises(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._wait_for_movement_start = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await handler.async_step_calibration_open_instruction({})


# --- calibration_complete --------------------------------------------------


async def test_complete_aborts_when_device_or_times_missing(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    result = await handler.async_step_calibration_complete()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_found"


async def test_complete_creates_subentry_when_enabled(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._open_time = 12.34
    handler._close_time = 11.11
    handler.enable_subentry_creation(
        device_id="5D3E7C", device_enum="01", device_name="Living room"
    )

    result = await handler.async_step_calibration_complete({})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    assert result["unique_id"] == "5D3E7C"
    assert result["data"][CONF_OPEN_TIME] == 12.34
    assert result["data"][CONF_CLOSE_TIME] == 11.11
    assert result["data"][CONF_DEVICE_ID] == "5D3E7C"
    assert result["data"][CONF_DEVICE_ENUM] == "01"
    assert isinstance(result["data"]["blind_id"], str) and result["data"]["blind_id"]
    # No status identity was supplied to enable_subentry_creation(), so these
    # conditional fields must be absent rather than present-but-None.
    assert CONF_STATUS_DEVICE_ID not in result["data"]
    assert CONF_LAST_CALIBRATION not in result["data"]


async def test_complete_updates_existing_subentry_when_recalibrating(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})
    handler._open_time = 20.0
    handler._close_time = 18.5
    # enable_subentry_creation() was NOT called - this is a recalibration of
    # an already-existing blind subentry, the "else" branch.

    result = await handler.async_step_calibration_complete({})

    assert result["type"] == FlowResultType.ABORT
    handler.flow.async_update_and_abort.assert_called_once()
    _, kwargs = handler.flow.async_update_and_abort.call_args
    assert kwargs["data_updates"][CONF_OPEN_TIME] == 20.0
    assert kwargs["data_updates"][CONF_CLOSE_TIME] == 18.5


async def test_complete_notifies_live_entity_via_dispatcher(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device(
        {"id": "5D3E7C", "name": "Living room", "entity_id": "cover.living_room"}
    )
    handler._open_time = 15.0
    handler._close_time = 14.0
    handler.enable_subentry_creation(
        device_id="5D3E7C", device_enum="01", device_name="Living room"
    )

    received = []
    async_dispatcher_connect(
        hass, SIGNAL_CALIBRATION_COMPLETED, lambda *args: received.append(args)
    )

    await handler.async_step_calibration_complete({})
    await hass.async_block_till_done()

    assert received == [("cover.living_room", 15.0, 14.0)]


# --- Pure data-transformation helpers (no dispatcher/flow involved) --------


def test_apply_calibration_status_candidates_with_primary_and_secondary(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler._calibration_discovery_result = {
        "primary": {"device_id": "5D3E7C", "enum": "01", "commands": ["00"]},
        "secondary": [{"device_id": "AA11BB", "enum": "02", "commands": ["00"]}],
    }
    handler._apply_calibration_status_candidates()
    assert handler._pending_status_device_id == "5D3E7C"
    assert handler._pending_status_enum == "01"
    assert handler._pending_status_identity_source == STATUS_IDENTITY_SOURCE_CALIBRATION
    assert handler._pending_secondary_status_identities == [
        {"device_id": "AA11BB", "enum": "02"}
    ]


def test_apply_calibration_status_candidates_with_no_result(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler._calibration_discovery_result = None
    handler._apply_calibration_status_candidates()
    assert handler._pending_status_device_id is None
    assert handler._pending_status_identity_source == STATUS_IDENTITY_SOURCE_UNKNOWN


def test_apply_calibration_status_candidates_with_no_primary(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler._calibration_discovery_result = {"primary": None, "secondary": []}
    handler._apply_calibration_status_candidates()
    assert handler._pending_status_device_id is None
    assert handler._pending_status_identity_source == STATUS_IDENTITY_SOURCE_UNKNOWN


def test_calibration_record_includes_rounded_times(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler._calibration_discovery_result = {"end_reason": "completed", "frames": []}
    handler._open_time = 12.3456
    handler._close_time = 11.111
    record = handler._calibration_record()
    assert record["open_time"] == 12.35
    assert record["close_time"] == 11.11


def test_calibration_record_is_none_without_discovery_result(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler._calibration_discovery_result = None
    assert handler._calibration_record() is None


def test_enable_then_disable_subentry_creation_resets_pending_fields(
    hass: HomeAssistant,
) -> None:
    handler = make_handler(hass)
    handler.enable_subentry_creation(
        device_id="5D3E7C",
        device_enum="01",
        device_name="Living room",
        status_device_id="AA11BB",
        status_enum="02",
    )
    assert handler._create_subentry_after_calibration is True
    assert handler._pending_device_id == "5D3E7C"
    assert handler._pending_status_device_id == "AA11BB"

    handler.disable_subentry_creation()
    assert handler._create_subentry_after_calibration is False
    assert handler._pending_device_id is None
    assert handler._pending_status_device_id is None


# --- Real timeout: the actual CALIBRATION_TIMEOUT-bounded asyncio.wait_for()
# elapsing for real, rather than mocking _wait_for_movement_start /
# _wait_for_stop_event to just return False (see "Timeout / error branches"
# above). CALIBRATION_TIMEOUT is patched down to a fraction of a second so
# the suite doesn't have to wait out the real 300-second value - the code
# path itself (a real asyncio.Event that is never set, a real
# asyncio.wait_for, a real internally-caught TimeoutError) is unchanged. ----


async def test_wait_for_movement_start_really_times_out(hass: HomeAssistant) -> None:
    """Exercise the real asyncio.wait_for(..., timeout=CALIBRATION_TIMEOUT) path
    instead of only testing the branch that runs *after* a timeout is assumed
    (see the "Timeout / error branches" tests above, which mock
    _wait_for_movement_start itself). CALIBRATION_TIMEOUT is patched down to
    a fraction of a second - actually waiting out the real 300-second value
    isn't practical for a test suite - but the code path is otherwise
    completely real: a real asyncio.Event that is never set, a real
    asyncio.wait_for, and a real TimeoutError caught internally.
    """
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    with patch(
        "custom_components.schellenberg_usb.options_flow_calibration.CALIBRATION_TIMEOUT",
        0.05,
    ):
        result = await handler._wait_for_movement_start(EVENT_STARTED_MOVING_UP)

    assert result is False
    assert handler._event_listener_unsub is None
    assert handler._start_event is None


async def test_wait_for_stop_event_really_times_out(hass: HomeAssistant) -> None:
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    with patch(
        "custom_components.schellenberg_usb.options_flow_calibration.CALIBRATION_TIMEOUT",
        0.05,
    ):
        result = await handler._wait_for_stop_event()

    assert result is False
    assert handler._event_listener_unsub is None
    assert handler._stop_event is None


async def test_open_instruction_real_timeout_shows_error(hass: HomeAssistant) -> None:
    """End-to-end version of test_open_instruction_start_timeout_shows_error:
    lets the real CALIBRATION_TIMEOUT-bounded wait run (patched short) instead
    of mocking _wait_for_movement_start's return value directly.
    """
    handler = make_handler(hass)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    with patch(
        "custom_components.schellenberg_usb.options_flow_calibration.CALIBRATION_TIMEOUT",
        0.05,
    ):
        result = await handler.async_step_calibration_open_instruction({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"]["base"] == "calibration_start_timeout"


# --- Frame capture, via the live API ----------------------------------------
#
# Every test above gives make_handler() a plain MagicMock flow, so
# _runtime_api() resolves to None and these calls are silent no-ops (see the
# module docstring). These tests use make_handler_with_live_api() instead, to
# exercise the real capture-call wiring.


def test_start_calibration_capture_calls_api_with_opening_phase(
    hass: HomeAssistant,
) -> None:
    handler, api = make_handler_with_live_api(hass)
    handler._start_calibration_capture()
    api.start_status_frame_capture.assert_called_once_with(phase="opening")


def test_start_calibration_capture_recovers_from_stale_capture(
    hass: HomeAssistant,
) -> None:
    """A stale capture left over from a previous run must not break a new
    calibration: it is closed out explicitly, then a fresh window is opened.
    """
    handler, api = make_handler_with_live_api(hass)
    api.start_status_frame_capture = MagicMock(
        side_effect=[RuntimeError("stale capture"), None]
    )

    handler._start_calibration_capture()

    api.finish_status_frame_capture.assert_called_once_with(
        end_reason="superseded_by_calibration"
    )
    assert api.start_status_frame_capture.call_count == 2


def test_set_calibration_capture_phase_labels_frames(hass: HomeAssistant) -> None:
    handler, api = make_handler_with_live_api(hass)
    handler._set_calibration_capture_phase("closing")
    api.set_status_frame_capture_phase.assert_called_once_with("closing")


def test_finish_calibration_capture_stores_discovery_result(
    hass: HomeAssistant,
) -> None:
    handler, api = make_handler_with_live_api(hass)
    discovery_result = {
        "primary": {"device_id": "5D3E7C", "enum": "01", "commands": ["00"]},
        "secondary": [],
    }
    api.finish_status_frame_capture = MagicMock(return_value=discovery_result)

    handler._finish_calibration_capture("completed")

    api.finish_status_frame_capture.assert_called_once_with(end_reason="completed")
    assert handler._calibration_discovery_result == discovery_result


async def test_full_calibration_cycle_with_live_api_captures_frames_in_order(
    hass: HomeAssistant,
) -> None:
    """The same open-then-close cycle as
    test_full_open_then_close_cycle_reaches_complete_step above, but with a
    live-ish API so the phase-labelled capture calls actually fire in
    sequence, and the resulting discovery result flows into
    _apply_calibration_status_candidates() for real instead of being skipped.
    """
    handler, api = make_handler_with_live_api(hass)
    discovery_result = {
        "primary": {"device_id": "5D3E7C", "enum": "01", "commands": ["00"]},
        "secondary": [],
    }
    api.finish_status_frame_capture = MagicMock(return_value=discovery_result)
    handler.set_selected_device({"id": "5D3E7C", "name": "Living room"})

    open_task = asyncio.ensure_future(
        handler.async_step_calibration_open_instruction({})
    )
    await asyncio.sleep(0.05)
    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_UP)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)
    open_result = await asyncio.wait_for(open_task, timeout=5)
    assert open_result["step_id"] == "calibration_close_instruction"

    close_task = asyncio.ensure_future(
        handler.async_step_calibration_close_instruction({})
    )
    await asyncio.sleep(0.05)
    await _send_device_event(hass, "5D3E7C", EVENT_STARTED_MOVING_DOWN)
    await _send_device_event(hass, "5D3E7C", EVENT_STOPPED)
    close_result = await asyncio.wait_for(close_task, timeout=5)
    assert close_result["step_id"] == "calibration_complete"

    api.start_status_frame_capture.assert_called_once_with(phase="opening")
    api.set_status_frame_capture_phase.assert_any_call("idle_between_legs")
    api.set_status_frame_capture_phase.assert_any_call("closing")
    api.finish_status_frame_capture.assert_called_once_with(end_reason="completed")
    assert handler._calibration_discovery_result == discovery_result
    assert handler._pending_status_device_id == "5D3E7C"
