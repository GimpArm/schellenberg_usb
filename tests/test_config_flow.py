"""Tests for the complete configuration UI: main config flow and the full
blind-pairing/reconfigure/developer-tools subentry flow.

Driven through the real flow managers (hass.config_entries.flow /
.subentries) rather than calling step methods directly, matching Home
Assistant's own idiomatic flow-testing style. Simple command-dispatch
actions (test_open/close/stop, teach_motor's open+stop) run against the real
SchellenbergUsbApi wired to conftest.py's FakeTransport and are checked via
written(), the same way tests/test_api.py and tests/test_cover.py already
do - api.py's exact wire format is that module's own job to guard, not
this file's. Only calls needing a controlled return value for branching
(pairing, discovery, teach failure) are monkeypatched directly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigSubentry, SubentryFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.schellenberg_usb.api as api_module
import custom_components.schellenberg_usb.config_flow as config_flow_module
import custom_components.schellenberg_usb.options_flow_calibration as calibration_module
from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_CLOSE_TIME,
    CONF_CLOSE_TIME_SECONDS,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_INITIAL_POSITION,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_DEVICE_EVENT,
    STATUS_IDENTITY_SOURCE_MANUAL,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
)
from tests.conftest import written

DEVICE_ID = "ABCDEF"
DEVICE_ENUM = "10"


# ---------------------------------------------------------------------------
# Main config flow: user / usb / usb_confirm
# ---------------------------------------------------------------------------


async def test_user_step_creates_entry_on_successful_connection(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config_flow_module, "check_serial_port", Mock(return_value=None)
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == "/dev/ttyUSB0"


async def test_user_step_shows_cannot_connect_on_oserror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real connection failure (OSError/TimeoutError) shows cannot_connect.

    Per the serialx migration guide, this integration no longer catches a
    broad serialx.SerialException - serialx itself raises plain OSError/
    TimeoutError for connection failures, and that is what api.py's
    connect() (and this same handling in config_flow.py/options_flow.py)
    catches.
    """

    def failing_check(port: str) -> None:
        raise OSError("no such device")

    monkeypatch.setattr(config_flow_module, "check_serial_port", failing_check)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_shows_unknown_on_unexpected_exception(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exploding_check(port: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_flow_module, "check_serial_port", exploding_check)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_step_aborts_if_the_port_is_already_configured(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config_flow_module, "check_serial_port", Mock(return_value=None)
    )
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="/dev/ttyUSB0",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_usb_discovery_shows_a_confirm_form_prefilled_with_the_device_path(
    hass: HomeAssistant,
) -> None:
    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB7",
        vid="16C0",
        pid="05E1",
        serial_number="SN123",
        manufacturer="van ooijen",
        description="FunkStick",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "usb"}, data=discovery_info
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "usb_confirm"
    assert result["data_schema"] is not None
    assert result["data_schema"]({})[CONF_SERIAL_PORT] == "/dev/ttyUSB7"


async def test_usb_confirm_creates_entry_on_successful_connection(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config_flow_module, "check_serial_port", Mock(return_value=None)
    )
    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB7",
        vid="16C0",
        pid="05E1",
        serial_number="SN123",
        manufacturer="van ooijen",
        description="FunkStick",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "usb"}, data=discovery_info
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB7"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "van ooijen FunkStick"


async def test_usb_confirm_shows_cannot_connect_on_oserror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_check(port: str) -> None:
        raise OSError("gone")

    monkeypatch.setattr(config_flow_module, "check_serial_port", failing_check)
    discovery_info = UsbServiceInfo(
        device="/dev/ttyUSB7",
        vid="16C0",
        pid="05E1",
        serial_number="SN123",
        manufacturer="van ooijen",
        description="FunkStick",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "usb"}, data=discovery_info
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB7"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# Shared subentry-flow fixtures
# ---------------------------------------------------------------------------


def _build_hub_entry(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    entry.runtime_data = connected_api
    return entry


def _add_existing_blind(hass: HomeAssistant, entry: MockConfigEntry) -> ConfigSubentry:
    blind = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: DEVICE_ID,
                CONF_DEVICE_ENUM: DEVICE_ENUM,
                CONF_STATUS_DEVICE_ID: DEVICE_ID,
                CONF_STATUS_ENUM: DEVICE_ENUM,
                CONF_OPEN_TIME: 20.0,
                CONF_CLOSE_TIME: 18.0,
                CONF_INVERT_DIRECTION: False,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Living Room Blind",
        unique_id=DEVICE_ID,
    )
    hass.config_entries.async_add_subentry(entry, blind)
    return blind


async def _start_pairing_flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> SubentryFlowResult:
    """Reach the blind subentry's initial pairing form.

    Real Home Assistant starts a *new* subentry flow with
    context={"source": "user"} (see this integration's strings.json:
    config_subentries.blind.initiate_flow.user is the "Add device" button;
    there is no initiate_flow.blind). Subentry-type dispatch happens via the
    (entry_id, subentry_type) handler tuple, not via the context source, so
    async_step_user() is the correct, real entry point regardless of type.

    Since the Issue #78 pairing-flow unification (2026-11-07), this *is*
    the pairing step itself (step_id "user") - no menu precedes it any
    more; see async_step_user()'s own docstring in config_flow.py. A
    caller submits an empty user_input against the returned flow_id to
    actually pair (api.pair_device_and_wait()) and reach name_device.
    """
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _start_reconfigure_flow(
    hass: HomeAssistant, entry: MockConfigEntry, blind: ConfigSubentry
) -> SubentryFlowResult:
    """Reach an existing blind's reconfigure menu."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={"source": "reconfigure", "subentry_id": blind.subentry_id},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "edit",
        "test_existing",
        "developer_tools",
        "calibrate",
    }
    return result


# ---------------------------------------------------------------------------
# Subentry flow: pairing a new blind
# ---------------------------------------------------------------------------


async def test_pairing_workflow_names_device_then_starts_calibration(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    result = await _start_pairing_flow(hass, entry)

    # Since the Issue #78 pairing-flow unification (2026-11-07), the
    # pairing form itself *is* step_id "user" - no intermediate
    # "pair_device" step precedes it any more (see async_step_user()'s
    # own docstring in config_flow.py).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Kitchen Blind"}
    )

    # Every fresh pairing offers this same choice after naming (Issue
    # #77, 2026-11-07) rather than going straight into calibration the
    # way the old "legacy" workflow used to - see async_step_name_
    # device()'s own comment on this branch.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_method_choice"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "test_motor",
        "manual_times",
    }

    # Choosing to calibrate now also runs the motor-test confirmation
    # that, before the unification, only the old "Add device from
    # remote" workflow required (Issue #78 discussion) - see
    # test_choosing_test_motor_runs_sequence_before_calibration_initial_
    # state below for the rest of that same sequence through to a real
    # calibration_initial_state form.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"


async def test_pairing_timeout_aborts_with_pairing_timeout(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api, "pair_device_and_wait", AsyncMock(return_value=None)
    )
    result = await _start_pairing_flow(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"


async def test_disconnect_during_pairing_wait_aborts_with_distinct_reason(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub-wide reload (triggered by a *different* blind's own subentry
    add/edit), the hub unloading, a Core stop/restart, or an explicit
    stick reset cutting this wait short must not be reported as the same
    generic "no device responded" message a genuine 2-minute timeout
    gets - see pairing_interrupted_by_disconnect's own docstring in
    api.py. pair_device_and_wait() is mocked here exactly like the
    pairing_timeout test above (both return None); only the new property
    it would have set for real differs, set directly since the mock
    bypasses the method that would otherwise set it.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api, "pair_device_and_wait", AsyncMock(return_value=None)
    )
    connected_api._pairing_interrupted_by_disconnect = True
    result = await _start_pairing_flow(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_interrupted_by_disconnect"


def _spy_on_dispatcher_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Awaitable[None]]:
    """Make async_dispatcher_connect() calls awaitable, one at a time.

    Same deterministic-registration spy as
    tests/test_options_flow_calibration.py's helper of the same name (see
    that module's own docstring for the full "why" - a fixed sleep/settle
    time to bridge the gap between starting a calibration step and it
    actually registering its movement-wait listener proved unreliable).
    Duplicated here rather than imported, matching this suite's existing
    one-file-per-module convention (no test module imports from another).
    """
    real_connect = calibration_module.async_dispatcher_connect
    registered = asyncio.Event()

    def spy(*args: Any, **kwargs: Any) -> Any:
        connection = real_connect(*args, **kwargs)
        registered.set()
        return connection

    monkeypatch.setattr(calibration_module, "async_dispatcher_connect", spy)

    async def wait_for_next_registration() -> None:
        try:
            await asyncio.wait_for(registered.wait(), timeout=5)
        except TimeoutError:
            pytest.fail(
                "async_dispatcher_connect() was not called within 5s - the "
                "calibration flow task never reached its next "
                "_wait_for_movement_start()/_wait_for_stop_event() listener "
                "registration."
            )
        registered.clear()

    return wait_for_next_registration


async def _drive_calibration_leg(
    hass: HomeAssistant,
    flow_id: str,
    wait_for_registration: Callable[[], Awaitable[None]],
    *,
    start_event: str,
) -> SubentryFlowResult:
    """Submit the current instruction form and drive it to completion.

    Shared by both pairing-calibration tests below, for whichever leg
    (open or close) is currently showing its instruction form - the
    EVENT_STARTED_MOVING_UP/DOWN constant passed in as start_event decides
    which.
    """
    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(flow_id, user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    signal = f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}"
    async_dispatcher_send(hass, signal, start_event)
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, signal, EVENT_STOPPED)
    return await task


async def _drive_pairing_to_calibration_initial_state(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> SubentryFlowResult:
    """Drive pairing through naming and reach the calibration_initial_state
    form.

    Since Issue #77 (2026-11-07), naming no longer leads straight into
    calibration_initial_state - it shows calibration_method_choice first
    (see async_step_name_device()'s own comment) - so this helper submits
    that choice too. Since the Issue #78 pairing-flow unification
    (2026-11-07), choosing to calibrate ("test_motor") also runs the same
    motor-test confirmation (test_motor/did_motor_move/test_success) every
    fresh pairing now gets before reaching a real calibration_initial_state
    - this helper drives through that too, with its own asyncio.sleep/
    MOVE_ACK_TIMEOUT mocks (see test_choosing_test_motor_runs_sequence_
    before_calibration_initial_state's own comment on why both are
    needed), so every test built on top of it (which only care about
    what happens from calibration_initial_state onward) stays unaffected.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "name_device"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Kitchen Blind"}
    )
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    assert result["step_id"] == "test_motor"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )
    assert result["step_id"] == "test_success"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "calibration_initial_state"
    return result


async def test_pairing_calibration_choosing_closed_sets_initial_position_closed(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live calibration driven all the way to CREATE_ENTRY.

    Choosing initial_state="closed" runs the open leg first and the close
    leg last, so the close leg's own end stop - 0%, fully closed - is the
    position the fresh subentry's data should carry under
    CONF_INITIAL_POSITION. Neither this path nor CONF_INITIAL_POSITION
    being present in the created entry's data were previously exercised
    anywhere in this file - every existing pairing test stops at
    reaching the calibration_initial_state form without submitting it, see
    test_pairing_workflow_names_device_then_starts_calibration above.

    Since the fix routing live calibration through a real status-identity
    discovery attempt (see async_step_calibration_complete's own comment,
    options_flow_calibration.py), calibration_complete no longer finishes
    pairing directly - it now reaches discover_status/confirm_status_
    discovery first, the same chain test_manual_times_choice_creates_
    entry_directly already drives for the manual-entry pairing route. Here
    discovery finds nothing, proving that path still reaches CREATE_ENTRY
    with everything this test already checked; the positive-discovery case
    is covered separately by test_live_calibration_pairing_captures_
    status_identity_via_discovery below.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": None,
                "secondary": [],
                "unknown_commands": [],
                "frames": [],
            }
        ),
    )
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    assert result["step_id"] == "calibration_close_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_DOWN,
    )
    assert result["step_id"] == "calibration_complete"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert result["data"][CONF_SECONDARY_STATUS_IDENTITIES] == []


async def test_pairing_calibration_choosing_open_sets_initial_position_open(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the test above: initial_state="open" runs close then open.

    The open leg now runs last, so the fresh subentry's data should carry
    CONF_INITIAL_POSITION == 100 (fully open) instead of the "closed"
    choice's 0. Also drives through the new discover_status/confirm_
    status_discovery detour - see the sibling test's own comment on why
    that is now part of this path.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": None,
                "secondary": [],
                "unknown_commands": [],
                "frames": [],
            }
        ),
    )
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "open"}
    )
    assert result["step_id"] == "calibration_close_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_DOWN,
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    assert result["step_id"] == "calibration_complete"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 100


async def test_live_calibration_pairing_captures_status_identity_via_discovery(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a live-calibration pairing must get a real chance at a
    primary/secondary status identity, same as the manual-entry route
    already does.

    Before this fix, async_step_calibration_complete() (options_flow_
    calibration.py) finished a fresh pairing immediately on reaching this
    step, never once calling async_discover_status_identities() - so every
    blind paired through live calibration (the "test_motor" -> calibrate
    route, the one calibration_method_choice's own default/first option
    leads to) ended up with CONF_SECONDARY_STATUS_IDENTITIES permanently
    empty and no CONF_STATUS_DEVICE_ID/CONF_STATUS_ENUM override at all -
    reported live, comparing a freshly-paired blind's empty "Zusätzliche
    Statusidentitäten" field against an older blind's populated one. The
    two tests above already prove the "discovery finds nothing" case still
    reaches CREATE_ENTRY correctly; this one is the actual positive case -
    a real primary and secondary identity, captured and persisted.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": "E3D0A5",
                    "enum": "08",
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [{"device_id": "7953A7", "enum": "53"}],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_DOWN,
    )
    assert result["step_id"] == "calibration_complete"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The command identity (used to send move/stop commands) is unaffected -
    # still the device/enum pair pair_device_and_wait() returned.
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_DEVICE_ENUM] == DEVICE_ENUM
    # The actual point of this fix: a real primary/secondary status
    # identity, discovered and persisted, instead of staying unset.
    assert result["data"][CONF_STATUS_DEVICE_ID] == "E3D0A5"
    assert result["data"][CONF_STATUS_ENUM] == "08"
    assert (
        result["data"][CONF_STATUS_IDENTITY_SOURCE]
        == STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
    )
    assert result["data"][CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "7953A7", "enum": "53"}
    ]
    # CONF_LAST_CALIBRATION and the travel times calibration_complete()
    # itself assembled into the stashed data survive the discover_status
    # detour untouched.
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert CONF_LAST_CALIBRATION in result["data"]


async def test_live_calibration_skip_status_discovery_creates_entry_directly(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing "Skip" on status_discovery_choice must finish pairing at once.

    Companion to test_live_calibration_pairing_captures_status_identity_via_
    discovery above, for the new skip option (2026-11-08, project owner's
    own request): a person without their original remote to hand must be
    able to get past this choice without ever reaching async_step_discover_
    status()'s own "Start listening" screen at all. Proves the live-
    calibration route's own _pending_finish_data stash survives this path
    too, not just the "ran discovery, found nothing" one the first test in
    this section already covers - async_discover_status_identities() is
    deliberately never mocked to return anything here, so a regression that
    accidentally still called it would surface as an un-mocked-call error,
    not a silently-passing assertion.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_DOWN,
    )
    assert result["step_id"] == "calibration_complete"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "skip_status_discovery"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_DEVICE_ENUM] == DEVICE_ENUM
    assert CONF_STATUS_DEVICE_ID not in result["data"]
    assert CONF_STATUS_ENUM not in result["data"]
    assert result["data"][CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_UNKNOWN
    assert result["data"][CONF_SECONDARY_STATUS_IDENTITIES] == []
    # The live-calibration route's own already-assembled data (travel
    # times, CONF_LAST_CALIBRATION, the initial position this leg
    # sequence confirmed) must still come through untouched, exactly as
    # it does on the "ran discovery" path above.
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert CONF_LAST_CALIBRATION in result["data"]


# ---------------------------------------------------------------------------
# Subentry flow: pairing's manual-entry and timeout escape hatches
# (Issue #77, 2026-11-07 - see async_step_name_device()'s and async_step_
# calibration_open_instruction()'s own comments)
# ---------------------------------------------------------------------------


async def test_manual_times_choice_creates_entry_directly(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual entry straight from calibration_method_choice, with no motor
    test involved at all. Mirrors test_manual_times_to_discover_status_
    creates_entry_directly, proving the same manual_times/discover_status/
    confirm_status_discovery chain reaches a real CREATE_ENTRY from this
    entry point too.
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": DEVICE_ID,
                    "enum": DEVICE_ENUM,
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    entry = _build_hub_entry(hass, connected_api)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Manual Blind"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_method_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    assert result["step_id"] == "manual_times"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 15.0,
            CONF_CLOSE_TIME_SECONDS: 14.0,
            "initial_state": "closed",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Manual Blind"
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert result["data"][CONF_OPEN_TIME] == 15.0
    assert result["data"][CONF_CLOSE_TIME] == 14.0


async def test_calibration_timeout_retry_targets_the_pending_leg(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh pairing's own calibration timeout offers an explicit choice
    (calibration_timeout_choice) instead of only an on-screen error - see
    async_step_calibration_open_instruction's own comment on its first
    timeout branch in options_flow_calibration.py. Contrast with
    test_open_instruction_movement_start_timeout_shows_error_and_reshows_
    form in test_options_flow_calibration.py, which proves recalibrating an
    *existing* blind still gets the old, unchanged error-and-resubmit
    behavior - the two are deliberately different, scoped by
    CalibrationFlowHandler._create_subentry_after_calibration.

    Exercises "retry" for both legs in one run, proving async_step_
    calibration_retry() targets whichever leg actually timed out rather
    than a hardcoded one: the open leg's own start-timeout is retried
    first (trivial on its own, since self._open_time is still None at that
    point regardless), then that leg is driven to a real completion, and
    the close leg's own start-timeout is retried too - only meaningful if
    the dispatch is state-aware, since a hardcoded "always open_instruction"
    would wrongly pass the first retry and only fail the second.
    """
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_timeout_choice"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "calibration_retry",
        "manual_times",
    }

    # "Try again" re-shows the same, still-pending open leg's own
    # instruction form - fresh, not the live wait itself (same as
    # submitting this form with user_input=None always does; the person
    # still has to press Start again).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_retry"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert not result.get("errors")

    # Drive the open leg to a real completion before exercising the close
    # leg's own timeout - restoring a real budget first so this genuine
    # start-to-stop run does not itself get cut short by the shrunk one.
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 5.0)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    assert result["step_id"] == "calibration_close_instruction"

    # Close leg's own start-timeout - retry must now land on the close leg,
    # not back on the already-finished open one.
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_timeout_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_retry"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close_instruction"


async def test_calibration_timeout_manual_choice_creates_entry(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of calibration_timeout_choice: choosing manual entry
    instead of retrying must still reach a real CREATE_ENTRY, proving the
    hand-off to manual_times works from mid-calibration (abandoning a
    timed-out live attempt), not just from calibration_method_choice before
    any live wait ever started (already covered by
    test_manual_times_choice_creates_entry_directly above).
    """
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": DEVICE_ID,
                    "enum": DEVICE_ENUM,
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "calibration_timeout_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    assert result["step_id"] == "manual_times"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
            "initial_state": "open",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 100
    assert result["data"][CONF_OPEN_TIME] == 20.0
    assert result["data"][CONF_CLOSE_TIME] == 18.0


async def test_calibration_timeout_manual_choice_prefills_the_leg_already_measured(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike test_calibration_timeout_manual_choice_creates_entry above
    (a start-timeout, before either leg ever measures anything),
    this drives the open leg to a real completion first, then times out
    the close leg's own start and escapes to manual entry - the scenario
    CalibrationFlowHandler.recorded_leg_times() (options_flow_
    calibration.py) exists for: async_step_manual_times() (config_flow.py)
    must pre-fill the open leg's own already-measured time instead of
    asking the person to re-enter or guess a number Home Assistant
    already knows precisely. Proven here by omitting CONF_OPEN_TIME_
    SECONDS from the submitted user_input entirely - exactly how a real
    flow manager receives a numeric field nobody touched - and relying on
    the schema's own default to fill it in: before this fix, this exact
    omission would have been rejected (that field had no default at all,
    so voluptuous would re-show this same form with a validation error
    instead of proceeding).
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": DEVICE_ID,
                    "enum": DEVICE_ENUM,
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    entry = _build_hub_entry(hass, connected_api)
    result = await _drive_pairing_to_calibration_initial_state(hass, entry, monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    # Drive the open leg to a real, measured completion - see
    # _drive_calibration_leg's own docstring. _open_time ends up floored
    # at 0.1s (CalibrationFlowHandler's own max(elapsed, 0.1), mirroring
    # test_full_open_and_close_legs_reach_the_summary_with_measured_times'
    # own reasoning for not asserting a tighter bound than that floor on
    # a real, unmocked elapsed-time measurement).
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    assert result["step_id"] == "calibration_close_instruction"

    # Now the close leg's own start-timeout - escape to manual entry
    # instead of retrying.
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_timeout_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    assert result["step_id"] == "manual_times"

    # CONF_OPEN_TIME_SECONDS deliberately omitted - the whole point being
    # tested - alongside a required field that was never auto-filled
    # (CONF_CLOSE_TIME_SECONDS, since the close leg never ran) and the
    # blind's current position (now open, having just finished the open
    # leg - not the "closed" answer given at calibration_initial_state
    # above, which only ever described the state *before* this run
    # started).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_CLOSE_TIME_SECONDS: 12.0,
            "initial_state": "open",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 100
    assert result["data"][CONF_CLOSE_TIME] == 12.0
    # The pre-filled default, not a missing-field error and not some
    # unrelated placeholder - the real, measured open leg's own time,
    # floored at 0.1s like any other real elapsed-time measurement.
    assert result["data"][CONF_OPEN_TIME] >= 0.1
    assert result["data"][CONF_OPEN_TIME] < 1.0


async def test_choosing_test_motor_runs_sequence_before_calibration_initial_state(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    # No stick reply is ever fed to either write below, so without this,
    # control_blind_exclusive()'s own ack-wait would block for the real
    # MOVE_ACK_TIMEOUT on each one - mocking
    # asyncio.sleep above does not cover this, since asyncio.wait_for's own
    # timeout path needs genuinely elapsed time regardless (see
    # test_control_blind_with_retry_treats_a_silent_ack_timeout_like_busy
    # in test_api.py for the same caveat).
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    result = await _start_pairing_flow(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "name_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    # Since the Issue #78 pairing-flow unification (2026-11-07), every
    # fresh pairing is offered this choice, and "test_motor" is how it
    # reaches the motor test that only the old "Add device from remote"
    # workflow used to require (see async_step_name_device()'s own
    # comment).
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    # This is the confirmation gate: the form is shown and nothing has
    # been sent to the stick yet - the person must choose a direction
    # explicitly, there is no default and no position-based suggestion.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert written(connected_api) == []

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    # Choosing "open" and confirming sends open then stop, and
    # did_motor_move's own placeholder reports back the direction that
    # was actually chosen (read from self._pending_test_direction, not
    # recomputed).
    assert (
        result["description_placeholders"]["test_action"]  # type: ignore[index]
        == "open"
    )
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )
    # test_success is a plain confirmation now, not a choice (Issue #78
    # pairing-flow unification) - "Enter travel times manually" moved
    # earlier, to calibration_method_choice, before the motor test ever
    # runs.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_success"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_initial_state"


async def test_test_motor_rejects_submitting_without_a_real_choice(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant's list-mode selector highlights its first option (the
    "unselected" placeholder, see config_flow.py's _test_direction_schema
    comment) before anything has actually been chosen. Pressing "Weiter" on
    that highlighted-but-unchosen state must not be silently treated as a
    real direction or raise - it must reshow this same form with an
    explicit error and send nothing to the stick, and a genuine choice
    afterwards must still work normally.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "unselected"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["errors"] == {"base": "test_direction_required"}
    assert written(connected_api) == []

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]


async def test_did_motor_move_no_shows_retry_notice_and_lets_person_choose_again(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant used to decide the retry direction itself (silently
    alternating open/close). It no longer decides anything: "no movement"
    just reshows test_motor's own required, no-default select so the
    person chooses a direction again - the same one, or the other one -
    with a retry_notice explaining why the form is back. There is no
    retry limit and no fallback to a manual form, confirmed here across
    three retries in a row. See
    test_did_motor_move_false_then_true_on_retry_reaches_test_success below
    for a retry actually succeeding.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    assert not result["description_placeholders"]["retry_notice"]  # type: ignore[index]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]

    # First "no movement": test_motor reshows with no direction pre-chosen -
    # nothing sent yet, the retry's own form still needs its own choice and
    # confirmation. retry_notice now explains why the form is back.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]

    # The person chooses "close" this time - their own decision, not
    # anything the software picked.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "close"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9020000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]

    # Second "no movement": still no limit, still no fallback - the form
    # keeps coming back for as long as the person keeps answering "no".
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]

    # This time the person chooses "open" again - proving the choice is
    # genuinely free each time, not forced to alternate.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9020000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]

    # Third "no movement": proof this is genuinely unlimited, not just one
    # extra retry.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]


async def test_did_motor_move_false_then_true_on_retry_reaches_test_success(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite-direction retry succeeding (common case: the blind was
    simply already fully open) reaches test_success exactly like an
    ordinary first-try success - see
    test_choosing_test_motor_runs_sequence_before_calibration_initial_
    state.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )
    assert result["step_id"] == "test_motor"
    # The retry is answered with a different choice than the first
    # attempt - allowed and expected, since nothing forces alternation
    # or repetition anymore. It is the person's own free choice each time.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "close"}
    )
    assert result["step_id"] == "did_motor_move"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )

    # test_success is a plain confirmation now, not a choice (Issue #78
    # pairing-flow unification) - see
    # test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state above.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_success"


async def _drive_pairing_to_did_motor_move(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> SubentryFlowResult:
    """Drive a fresh pairing through naming and the motor test's own
    direction choice, reaching did_motor_move's own form.

    Shared prefix for the wrong-motor-retry tests below, which all need to
    reach this exact point before diverging on what they submit to did_
    motor_move itself. Mirrors _drive_pairing_to_calibration_initial_state's own
    convention: asyncio.sleep/MOVE_ACK_TIMEOUT are mocked here since every
    caller needs the same inert values, but pair_device_and_wait() is left
    to each caller, since what it should return (and how many times)
    differs per test.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "name_device"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_motor"}
    )
    assert result["step_id"] == "test_motor"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"
    return result


async def test_did_motor_move_rejects_submitting_without_a_real_choice(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reasoning as test_test_motor_rejects_submitting_without_a_real_
    choice above, for did_motor_move's own three-way select
    (_motor_moved_result_schema()), only offered for a fresh pairing:
    Home Assistant's list-mode selector highlights the "unselected"
    placeholder before anything is chosen, so submitting that highlighted-
    but-unchosen state must reshow this same form with an explicit error -
    not be silently treated as any of the three real answers - and a
    genuine choice afterwards must still work normally.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    result = await _drive_pairing_to_did_motor_move(hass, entry, monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "unselected"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    assert result["errors"] == {"base": "motor_moved_result_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_success"


async def test_did_motor_move_wrong_motor_reopens_test_motor_with_a_fresh_identity(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing "wrong_motor" first shows the wrong_motor_reset_notice form
    (a confirmation gate, same reasoning as test_choosing_test_motor_runs_
    motor_test_sequence_before_calibration_initial_state's own comment:
    nothing paired yet from the choice alone). Confirming it runs a
    brand-new pair_device_and_wait() - the mocked side_effect below only
    has a second item for this call to consume, so the flow reaching
    test_motor at all already proves exactly one retry call happened, no
    more - and loops back to test_motor with that fresh identity in its
    own placeholders, not the original one.

    Also proves _test_motor_retry_attempted genuinely resets rather than
    merely having stayed at whatever it already was: a plain "no" is
    answered first (setting it True, same as test_did_motor_move_no_shows_
    retry_notice_and_lets_person_choose_again above), so the retry_notice
    reappearing empty after the wrong-motor retry cannot be a coincidence
    of it never having been set in the first place.
    """
    new_device_id, new_device_enum = "FEDCBA", "20"
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(
            side_effect=[(DEVICE_ID, DEVICE_ENUM), (new_device_id, new_device_enum)]
        ),
    )
    result = await _drive_pairing_to_did_motor_move(hass, entry, monkeypatch)

    # Force a plain "no" first, to set _test_motor_retry_attempted True.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "wrong_motor"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wrong_motor_reset_notice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test_motor"
    # Fresh identity shown, not the original one.
    assert (
        result["description_placeholders"]["device_id"]  # type: ignore[index]
        == new_device_id
    )
    assert (
        result["description_placeholders"]["device_enum"]  # type: ignore[index]
        == new_device_enum
    )
    # And the retry_notice from the earlier plain "no" is gone - a brand
    # new address deserves a clean slate, not a notice about the old one.
    assert not result["description_placeholders"]["retry_notice"]  # type: ignore[index]


async def test_did_motor_move_wrong_motor_retry_completes_pairing_with_new_identity(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrong-motor retry's own fresh identity is not just shown in
    test_motor's placeholders (the test above) - it is the identity that
    actually ends up in the finished subentry's data, proving
    _apply_paired_device_identity() really did keep CalibrationFlowHandler's
    own parallel copy (set_selected_device()/enable_subentry_creation()) in
    step with it, not just this flow's own _pending_device_id/
    _pending_device_enum - a real risk, since calibration's own status-
    identity capture reads the handler's copy, not the flow's.

    Reuses the same physical device_id on the retry and only bumps the
    enum, so _drive_calibration_leg()'s own fixed, DEVICE_ID-keyed
    dispatcher signal still reaches this entity's listeners after the
    retry - a different device_id here would need its own dispatcher
    signal plumbing this test is not set up to provide. This also matches
    the to-do's own fix (only the enum is ever bumped) and reality (the
    hardware-reported device_id is not under this integration's control to
    begin with).
    """
    bumped_enum = "11"
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(side_effect=[(DEVICE_ID, DEVICE_ENUM), (DEVICE_ID, bumped_enum)]),
    )
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": None,
                "secondary": [],
                "unknown_commands": [],
                "frames": [],
            }
        ),
    )
    result = await _drive_pairing_to_did_motor_move(hass, entry, monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "wrong_motor"}
    )
    assert result["step_id"] == "wrong_motor_reset_notice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "test_motor"
    assert result["description_placeholders"]["device_enum"] == bumped_enum  # type: ignore[index]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )
    assert result["step_id"] == "test_success"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "calibration_initial_state"

    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_UP,
    )
    assert result["step_id"] == "calibration_close_instruction"

    result = await _drive_calibration_leg(
        hass,
        result["flow_id"],
        wait_for_registration,
        start_event=EVENT_STARTED_MOVING_DOWN,
    )
    assert result["step_id"] == "calibration_complete"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "status_discovery_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_DEVICE_ENUM] == bumped_enum


async def test_wrong_motor_reset_notice_aborts_on_pairing_timeout(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry handshake is a real pair_device_and_wait() call like any
    other - if it times out, this aborts exactly like the original pairing
    attempt does (test_pairing_timeout_aborts_with_pairing_timeout further
    above), not with some different, wrong_motor-specific reason.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(side_effect=[(DEVICE_ID, DEVICE_ENUM), None]),
    )
    result = await _drive_pairing_to_did_motor_move(hass, entry, monkeypatch)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "wrong_motor"}
    )
    assert result["step_id"] == "wrong_motor_reset_notice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"


async def test_wrong_motor_reset_notice_aborts_on_pairing_interrupted_by_disconnect(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same distinction as test_disconnect_during_pairing_wait_aborts_with_
    distinct_reason further above, for the retry handshake specifically.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(side_effect=[(DEVICE_ID, DEVICE_ENUM), None]),
    )
    result = await _drive_pairing_to_did_motor_move(hass, entry, monkeypatch)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "wrong_motor"}
    )
    assert result["step_id"] == "wrong_motor_reset_notice"
    connected_api._pairing_interrupted_by_disconnect = True

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_interrupted_by_disconnect"


# ---------------------------------------------------------------------------
# Subentry flow: status discovery (guided remote capture)
# ---------------------------------------------------------------------------


async def test_manual_times_leads_into_discover_status(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    # Since the Issue #78 pairing-flow unification (2026-11-07),
    # "manual_times" is offered by calibration_method_choice, right
    # after naming - no motor test needed first (see async_step_name_
    # device()'s own comment).
    assert result["step_id"] == "calibration_method_choice"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    assert result["step_id"] == "manual_times"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "open",
        },
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_status"


async def test_manual_times_to_discover_status_creates_entry_directly(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "manual_times" branch must reach a real CREATE_ENTRY, the same
    way its "calibration_initial_state" sibling does.

    test_manual_times_leads_into_discover_status above only checks that the
    discover_status form is reached; it never confirms that submitting it
    actually finishes pairing a *brand-new* device
    (confirm_status_discovery's async_create_entry() branch) rather than
    updating an existing one, which
    test_discover_status_success_confirms_and_updates_existing_blind already
    covers via the reconfigure/Developer Tools entry point instead.
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": DEVICE_ID,
                    "enum": DEVICE_ENUM,
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    entry = _build_hub_entry(hass, connected_api)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "New Blind"}
    )
    # Since the Issue #78 pairing-flow unification (2026-11-07),
    # "manual_times" is offered by calibration_method_choice, right
    # after naming - no motor test needed first (see async_step_name_
    # device()'s own comment).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "closed",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The hybrid workflow's manual_times branch had no way at all to reach
    # CONF_INITIAL_POSITION before this fix - unlike its
    # calibration_initial_state sibling
    # (test_pairing_calibration_choosing_closed_sets_initial_position_closed
    # and its "open" counterpart), nothing here ever drove
    # the blind through a real leg to confirm an end stop, so this is the
    # person's own direct "closed" answer, not a measured one.
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert result["title"] == "New Blind"
    assert result["data"][CONF_OPEN_TIME] == 30.0
    assert result["data"][CONF_CLOSE_TIME] == 28.0
    assert result["data"][CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_STATUS_ENUM] == DEVICE_ENUM
    assert len(entry.subentries) == 1
    created = next(iter(entry.subentries.values()))
    assert created.unique_id == DEVICE_ID


async def test_manual_times_to_discover_status_creates_entry_with_open_initial_position(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of test_manual_times_to_discover_status_creates_entry_directly
    above, which only ever drove the "closed" -> CONF_INITIAL_POSITION == 0
    side of async_step_manual_times()'s initial_state mapping through to a
    real CREATE_ENTRY. test_manual_times_leads_into_discover_status submits
    "open" too, but only ever asserted the step transition, never the
    resulting entry data - so a regression that mis-mapped only the "open"
    branch (to anything other than 100, while leaving "closed" -> 0 intact)
    would have passed every other manual_times test undetected. This test
    closes that gap, mirroring test_pairing_calibration_choosing_open_
    sets_initial_position_open's equivalent coverage for the
    calibration_initial_state sibling.
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(
            return_value={
                "primary": {
                    "device_id": DEVICE_ID,
                    "enum": DEVICE_ENUM,
                    "commands": ["00", "01"],
                    "timestamps": ["10:00:00"],
                },
                "secondary": [],
                "unknown_commands": [],
                "frames": [{}],
            }
        ),
    )
    entry = _build_hub_entry(hass, connected_api)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "New Blind"}
    )
    # Since the Issue #78 pairing-flow unification (2026-11-07),
    # "manual_times" is offered by calibration_method_choice, right
    # after naming - no motor test needed first (see async_step_name_
    # device()'s own comment).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "open",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 100


async def test_manual_times_skip_status_discovery_creates_entry_directly(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of test_live_calibration_skip_status_discovery_creates_entry_
    directly above, for the manual-entry pairing route's own _pending_data()
    finish branch instead of the live-calibration route's _pending_
    finish_data one - both branches in async_step_confirm_status_discovery()
    need their own coverage for the skip path, the same way both already
    have it for the "ran discovery" path above. async_discover_status_
    identities() is deliberately never mocked here either, for the same
    un-mocked-call-error reasoning as that test.
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "New Blind"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "closed",
        },
    )
    assert result["step_id"] == "status_discovery_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "skip_status_discovery"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "New Blind"
    assert result["data"][CONF_OPEN_TIME] == 30.0
    assert result["data"][CONF_CLOSE_TIME] == 28.0
    assert result["data"][CONF_INITIAL_POSITION] == 0
    assert CONF_STATUS_DEVICE_ID not in result["data"]
    assert CONF_STATUS_ENUM not in result["data"]
    assert result["data"][CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_UNKNOWN
    assert result["data"][CONF_SECONDARY_STATUS_IDENTITIES] == []
    assert len(entry.subentries) == 1


async def test_manual_times_without_a_real_initial_state_choice_shows_an_error(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of options_flow_calibration.py's own test for
    calibration_initial_state's identical "unselected" placeholder guard
    (test_submitting_without_a_real_choice_shows_an_error_and_stays_on_the_form
    in test_options_flow_calibration.py) - manual_times's own initial_state
    field reuses the same selector/translation_key and must reject the same
    highlighted-but-unchosen submission the same way, since a silent
    "closed" default here would be exactly the kind of unverified position
    this whole fallback exists to avoid.
    """
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "New Blind"}
    )
    # Since the Issue #78 pairing-flow unification (2026-11-07),
    # "manual_times" is offered by calibration_method_choice, right
    # after naming - no motor test needed first (see async_step_name_
    # device()'s own comment).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "unselected",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_times"
    assert result["errors"] == {"base": "initial_state_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPEN_TIME_SECONDS: 30.0,
            CONF_CLOSE_TIME_SECONDS: 28.0,
            "initial_state": "closed",
        },
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "status_discovery_choice"


async def test_discover_status_unavailable_shows_an_error(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(side_effect=ConnectionError),
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "status_discovery_unavailable"}


async def test_discover_status_busy_shows_an_error(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(side_effect=RuntimeError),
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "status_discovery_busy"}


async def test_discover_status_success_confirms_and_updates_existing_blind(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful discovery run gives a blind its first confirmed status
    identity.

    Builds its own blind rather than using the shared _add_existing_blind()
    fixture directly (2026-11-15): that fixture's own CONF_STATUS_DEVICE_ID/
    CONF_STATUS_ENUM (matching DEVICE_ID/DEVICE_ENUM) now count as an
    already-confirmed identity under today's conflict-protection fix (its
    own CONF_STATUS_IDENTITY_SOURCE is unset, which - like
    resolve_blind_identities()'s own "legacy" fallback elsewhere - this
    flow treats as a real, confirmed identity, not an unknown one), so
    reusing it here would collide with discovery's different "112233"/"01"
    result instead of cleanly adopting it. That collision case is
    test_discover_status_conflicting_primary_keeps_existing_and_adds_
    secondary above; this test is about the ordinary case this one was
    always meant to cover - a blind that has simply never had a status
    identity confirmed before, explicit via STATUS_IDENTITY_SOURCE_UNKNOWN.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: DEVICE_ID,
                CONF_DEVICE_ENUM: DEVICE_ENUM,
                CONF_OPEN_TIME: 20.0,
                CONF_CLOSE_TIME: 18.0,
                CONF_INVERT_DIRECTION: False,
                CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Living Room Blind",
        unique_id=DEVICE_ID,
    )
    hass.config_entries.async_add_subentry(entry, blind)
    discovery_result = {
        "primary": {
            "device_id": "112233",
            "enum": "01",
            "commands": ["00", "01"],
            "timestamps": ["10:00:00"],
        },
        "secondary": [],
        "unknown_commands": [],
        "frames": [{}, {}],
    }
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(return_value=discovery_result),
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    assert updated.data[CONF_STATUS_DEVICE_ID] == "112233"
    assert updated.data[CONF_STATUS_ENUM] == "01"


async def test_discover_status_conflicting_primary_keeps_existing_and_adds_secondary(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *different* device discovered via the remote must not replace an
    already-working status identity - it is folded into the secondary
    list instead, never used to replace anything.

    Regression for a real report (2026-11-15): Developer Tools' own
    "Discover status" dialog, run against an already-paired blind whose
    status identity an electronic Gurtwickler's own motor echo had
    already confirmed, found the *original remote*'s identity instead
    (the same stick also hears it) and - before this fix - the branch
    below ran unconditionally whenever discovery found anything,
    replacing both the primary identity and the whole secondary list
    with just this one run's result. The Gurtwickler's identity did not
    even survive as a secondary, it simply vanished, leaving it producing
    no reaction in Home Assistant afterward. Mirrors the identical
    protection test_confirm_status_discovery_keeps_calibration_primary_
    on_conflict (tests/test_pairing_subentry_flow.py) already has for the
    sibling, fresh-pairing branch.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    discovery_result = {
        "primary": {
            "device_id": "112233",
            "enum": "01",
            "commands": ["00", "01"],
            "timestamps": ["10:00:00"],
        },
        "secondary": [{"device_id": "445566", "enum": "03"}],
        "unknown_commands": [],
        "frames": [{}, {}, {}],
    }
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(return_value=discovery_result),
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    # The Gurtwickler's own, already-working primary survives untouched...
    assert updated.data[CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert updated.data[CONF_STATUS_ENUM] == DEVICE_ENUM
    # ...and both the remote's own primary and its own secondary are
    # folded into the secondary list instead of replacing anything.
    assert updated.data[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "445566", "enum": "03"},
        {"device_id": "112233", "enum": "01"},
    ]


async def test_discover_status_finding_nothing_keeps_the_existing_identity(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: an inconclusive status discovery must not clear
    the existing identity.

    An inconclusive discovery attempt (wrong button, pressed too fast, RF
    interference - no primary frame captured) used to unconditionally pop
    the existing CONF_STATUS_DEVICE_ID/CONF_STATUS_ENUM on "Save" anyway,
    silently erasing a previously-working status identity with no
    warning. _add_existing_blind()'s blind already has one (matching
    DEVICE_ID/DEVICE_ENUM); it must survive an empty discovery result
    untouched, unlike test_discover_status_success_confirms_and_updates_
    existing_blind above, where discovery genuinely finds something.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    # Explicit annotation: unlike the otherwise-identical discovery_result
    # above (which has a concrete device_id/enum pair for mypy to infer
    # from), every value here is None or an empty list, giving mypy
    # nothing concrete to infer the dict's value type from on its own.
    discovery_result: dict[str, Any] = {
        "primary": None,
        "secondary": [],
        "unknown_commands": [],
        "frames": [],
    }
    monkeypatch.setattr(
        connected_api,
        "async_discover_status_identities",
        AsyncMock(return_value=discovery_result),
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "discover_status"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    assert updated.data[CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert updated.data[CONF_STATUS_ENUM] == DEVICE_ENUM


# ---------------------------------------------------------------------------
# Subentry flow: reconfigure an existing blind (edit / test_existing)
# ---------------------------------------------------------------------------


async def test_edit_updates_an_existing_blind(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )
    assert result["step_id"] == "edit"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Renamed Blind",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_DEVICE_ENUM: DEVICE_ENUM,
            CONF_OPEN_TIME_SECONDS: 33.0,
            CONF_CLOSE_TIME_SECONDS: 31.0,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    assert updated.title == "Renamed Blind"
    assert updated.data[CONF_OPEN_TIME] == 33.0


async def test_edit_rejects_a_device_id_already_used_by_another_blind(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    other = ConfigSubentry(
        data=MappingProxyType({CONF_DEVICE_ID: "112233", CONF_DEVICE_ENUM: "20"}),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Other Blind",
        unique_id="112233",
    )
    hass.config_entries.async_add_subentry(entry, other)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Living Room Blind",
            CONF_DEVICE_ID: "112233",
            CONF_DEVICE_ENUM: DEVICE_ENUM,
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE_ID: "already_configured"}


async def test_edit_rejects_a_status_identity_already_used_by_another_blind(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """A manually-entered primary status identity must not silently collide
    with another subentry's - see the comment above this check in
    async_step_edit(). Unlike test_edit_rejects_a_device_id_already_used_by_
    another_blind above, the two blinds here have different *command*
    device IDs, so only the new status-identity check (not the pre-existing
    command_device_id one) can be responsible for the rejection.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    other = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: "445566",
                CONF_DEVICE_ENUM: "30",
                CONF_STATUS_DEVICE_ID: "998877",
                CONF_STATUS_ENUM: "40",
                CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_MANUAL,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Other Blind",
        unique_id="445566",
    )
    hass.config_entries.async_add_subentry(entry, other)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Living Room Blind",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_DEVICE_ENUM: DEVICE_ENUM,
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
            CONF_STATUS_DEVICE_ID: "998877",
            CONF_STATUS_ENUM: "40",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_STATUS_DEVICE_ID: "already_configured"}


async def test_edit_allows_a_status_identity_matching_an_unconfirmed_candidate(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """The new duplicate-status-identity check above must not fire against a
    candidate whose own status source is STATUS_IDENTITY_SOURCE_UNKNOWN:
    that candidate has no real, confirmed status identity of its own, so its
    command identity showing up as a fallback display value elsewhere is
    not a genuine collision with a blind that legitimately uses the same
    value as its own, separately confirmed, status identity.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    other = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: "998877",
                CONF_DEVICE_ENUM: "40",
                CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Other Blind",
        unique_id="998877",
    )
    hass.config_entries.async_add_subentry(entry, other)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Living Room Blind",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_DEVICE_ENUM: DEVICE_ENUM,
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
            CONF_STATUS_DEVICE_ID: "998877",
            CONF_STATUS_ENUM: "40",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_edit_rejects_an_invalid_device_enum(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Living Room Blind",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_DEVICE_ENUM: "ZZ",
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE_ENUM: "invalid_device_enum"}


async def test_edit_clears_status_identity_when_left_blank(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Leaving both primary status fields blank while editing must drop any
    previously stored status identity instead of silently keeping the stale
    one (_add_existing_blind's fixture blind starts with one set).
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "edit"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEVICE_NAME: "Living Room Blind",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_DEVICE_ENUM: DEVICE_ENUM,
            CONF_OPEN_TIME_SECONDS: 20.0,
            CONF_CLOSE_TIME_SECONDS: 18.0,
            # Explicitly cleared, not omitted: the form's own schema default
            # for this optional field is the *current* status_device_id
            # (see async_step_edit's vol.Optional(..., default=status_device_id)),
            # so a real flow manager fills in that old value for a key simply
            # missing from user_input - exactly as it would if the person had
            # never touched the field in the UI. An empty string is what the
            # flow manager actually receives when the person selects the
            # field's text and deletes it.
            CONF_STATUS_DEVICE_ID: "",
            CONF_STATUS_ENUM: "",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    updated = entry.subentries[blind.subentry_id]
    assert CONF_STATUS_DEVICE_ID not in updated.data
    assert CONF_STATUS_ENUM not in updated.data


async def test_test_existing_leads_into_the_short_command_test(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person must choose a direction explicitly - there is no default
    and no position-based suggestion, confirmed here for a blind with no
    recorded position at all. See
    test_test_existing_honors_chosen_direction_when_recorded_closed
    and its mirror test for a recorded-open blind below, which prove a
    tracked position doesn't override the choice either.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    # See test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state's own comment on this same patch: without it,
    # each write below blocks for the real MOVE_ACK_TIMEOUT waiting for a
    # stick reply that is never fed.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"
    assert written(connected_api) == []

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )

    assert result["step_id"] == "did_motor_move"
    assert (
        result["description_placeholders"]["test_action"]  # type: ignore[index]
        == "open"
    )
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),
    ]


async def test_test_existing_honors_chosen_direction_when_recorded_closed(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracked position used to pick the test direction automatically
    (toward "open" for a closed blind). It no longer does - the person's
    own choice is what counts, confirmed here by choosing "close" for a
    blind recorded as closed, the opposite of what the old auto-logic
    would have picked, and getting exactly that sent.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    # See test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state's own comment on this same patch: without it,
    # each write below blocks for the real MOVE_ACK_TIMEOUT waiting for a
    # stick reply that is never fed.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    connected_api.record_position_update(
        DEVICE_ID,
        source="calibration",
        direction="closing",
        previous_position=100,
        new_position=0,
        status="confirmed",
    )
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "close"}
    )
    assert result["step_id"] == "did_motor_move"
    # The chosen direction, not the tracked ("closing") one, is what gets
    # reported back.
    assert (
        result["description_placeholders"]["test_action"]  # type: ignore[index]
        == "close"
    )
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9020000\r\n".encode("ascii"),  # close
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),  # stop
    ]


async def test_test_existing_honors_chosen_direction_when_recorded_open(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same proof as above in the other direction: a blind recorded as
    open, tested by explicitly choosing "open" anyway - the opposite of
    what the old auto-logic would have picked for an open blind ("close")
    - still sends exactly the chosen direction.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    # See test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state's own comment on this same patch: without it,
    # each write below blocks for the real MOVE_ACK_TIMEOUT waiting for a
    # stick reply that is never fed.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    connected_api.record_position_update(
        DEVICE_ID,
        source="calibration",
        direction="opening",
        previous_position=0,
        new_position=100,
        status="confirmed",
    )
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"
    # The chosen direction, not the tracked ("opening") one, is what gets
    # reported back.
    assert (
        result["description_placeholders"]["test_action"]  # type: ignore[index]
        == "open"
    )
    assert written(connected_api) == [
        f"ss{DEVICE_ENUM}9010000\r\n".encode("ascii"),  # open
        f"ss{DEVICE_ENUM}9000000\r\n".encode("ascii"),  # stop
    ]


async def test_test_motor_shows_command_failed_when_the_rf_command_does_not_send(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the stick itself reports the test command could not be sent (as
    opposed to the person saying the motor didn't move), the form must stay
    on "test_motor" and show "command_failed" instead of advancing to
    did_motor_move - and since the direction command never went out, the
    stop command must not be sent either.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(connected_api, "control_blind", AsyncMock(return_value=False))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )

    assert result["step_id"] == "test_motor"
    assert result["errors"] == {"base": "command_failed"}
    assert written(connected_api) == []


async def test_test_motor_command_failed_shows_retry_notice(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command_failed result is just as inconclusive as a sent-but-
    unmoving test: the RF command never went out, so nothing about that
    direction was actually tested. It gets the same retry_notice and the
    same free, unlimited choice that "no movement" does, with no cap and
    no fallback - demonstrated here across two retries in a row.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(connected_api, "control_blind", AsyncMock(return_value=False))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    assert result["step_id"] == "test_motor"
    assert not result["description_placeholders"]["retry_notice"]  # type: ignore[index]

    # First command_failed: reshows test_motor with the retry_notice, and
    # no direction pre-chosen.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "test_motor"
    assert result["errors"] == {"base": "command_failed"}
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]

    # Second command_failed: the person chooses "open" again, proving the
    # choice is genuinely free each time rather than forced to alternate.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "test_motor"
    assert result["errors"] == {"base": "command_failed"}
    assert result["description_placeholders"]["retry_notice"]  # type: ignore[index]
    assert written(connected_api) == []


async def test_did_motor_move_true_for_an_existing_blind_aborts_successfully(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "command_test_successful"


async def test_did_motor_move_false_for_an_existing_blind_returns_to_edit(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-test of an *already-configured* blind sends the person to
    its "edit" form on the very first "no movement" - already having a
    cover entity and known settings to fix makes that a more useful next
    step than an open-ended retry loop.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "no"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "edit"


async def test_did_motor_move_rejects_unselected_for_an_existing_blind(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reasoning as test_did_motor_move_rejects_submitting_without_a_
    real_choice above, now for the "existing" workflow's own re-test form -
    unified onto the same required, no-default select on 2026-11-14 (see
    _motor_moved_result_schema()'s own docstring), it must reject the
    highlighted-but-unchosen "unselected" placeholder the same way the
    "pairing" form does, rather than ever silently treating it as "no".
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "unselected"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "did_motor_move"
    assert result["errors"] == {"base": "motor_moved_result_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved_result": "yes"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "command_test_successful"


async def test_did_motor_move_rejects_wrong_motor_for_an_existing_blind(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "wrong_motor" only ever makes sense while a fresh device_enum is
    still up for grabs during a "pairing" - an already-paired blind being
    re-tested has no fresh address to retry at (see
    _motor_moved_result_schema()'s own docstring), so the "existing"
    workflow's own form must never offer it at all - confirmed here at a
    stronger level than test_did_motor_move_rejects_unselected_for_an_
    existing_blind just above: "unselected" is a genuine, declared option
    of that same selector, so it passes Home Assistant's own schema
    validation and only then gets rejected by async_step_did_motor_move()'s
    own manual check. "wrong_motor" is not a declared option of the
    schema shown to "existing" at all (allow_wrong_motor=False drops it
    from the SelectSelector's own options=[...] list), so submitting it
    anyway never even reaches that manual check or this step's handler:
    Home Assistant's own schema validation (a plain vol.In(options) under
    the hood) rejects it first, raising InvalidData instead of returning
    a form. The schema itself, not just this step's own code, is what
    makes the option unreachable here.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "test_existing"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    assert result["step_id"] == "did_motor_move"

    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={"motor_moved_result": "wrong_motor"}
        )


# ---------------------------------------------------------------------------
# Subentry flow: developer tools
# ---------------------------------------------------------------------------


async def test_developer_tools_shows_the_full_diagnostics_menu(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "developer_tools"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "test_open",
        "test_close",
        "test_stop",
        "discover_status",
        "set_position_open",
        "set_position_closed",
        "set_position_manual",
        "reset_stick",
        "copy_diagnostics",
        "teach_motor",
        "send_raw_command",
    }
    assert result["description_placeholders"] is not None
    assert result["description_placeholders"]["selected_blind"] == "Living Room Blind"
    assert result["description_placeholders"]["command_device_id"] == DEVICE_ID


async def test_developer_tools_test_open_close_stop_each_send_one_command(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No stick reply is ever fed for any of the three writes below, so
    # without this, control_blind_exclusive()'s own ack-wait would block
    # for the real MOVE_ACK_TIMEOUT on each one - see
    # test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state's own comment on this same patch.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)

    for action in ("test_open", "test_close", "test_stop"):
        result = await _start_reconfigure_flow(hass, entry, blind)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={"next_step_id": "developer_tools"}
        )
        before = len(written(connected_api))

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={"next_step_id": action}
        )

        assert result["step_id"] == "developer_tools"
        assert len(written(connected_api)) == before + 1
        assert result["description_placeholders"] is not None
        # "notice_command_sent" in runtime_translation_text.py: "{command}
        # command sent successfully." - the command name itself varies
        # (Open/Close/Stop), so only the stable tail is asserted here.
        assert (
            "command sent successfully" in result["description_placeholders"]["result"]
        )


async def test_developer_tools_set_position_open_and_closed_sync_without_sending_rf(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    # manual_sync_position() only succeeds for a "live" cover, i.e. a device
    # id the cover platform has registered on the api; these tests drive the
    # subentry flow directly without setting up that platform, so register
    # it by hand the way cover.py's async_added_to_hass() normally would
    # (register_entity() with no status identity - this fixture only needs
    # the command identity "live").
    connected_api.register_entity(
        None, None, command_device_id=DEVICE_ID, command_enum=DEVICE_ENUM
    )

    for action, expected_position in (
        ("set_position_open", 100),
        ("set_position_closed", 0),
    ):
        result = await _start_reconfigure_flow(hass, entry, blind)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={"next_step_id": "developer_tools"}
        )
        before = len(written(connected_api))

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={"next_step_id": action}
        )

        assert result["step_id"] == "developer_tools"
        # Manual position confirmation never transmits an RF command.
        assert len(written(connected_api)) == before
        assert result["description_placeholders"] is not None
        assert str(expected_position) in result["description_placeholders"]["result"]
        # Also persisted as this blind's durable startup fallback (not
        # just applied to the live entity, which this test never even sets
        # up) - re-read from entry.subentries rather than the original
        # `blind` reference, which an update replaces rather than mutates.
        updated_subentry = entry.subentries[blind.subentry_id]
        assert updated_subentry.data[CONF_INITIAL_POSITION] == expected_position


async def test_developer_tools_set_position_manual_accepts_an_exact_value(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    # See test_developer_tools_set_position_open_and_closed_sync_without_
    # sending_rf: manual_sync_position() requires the device to be
    # registered as a "live" cover, which this direct flow-driven test
    # never does on its own.
    connected_api.register_entity(
        None, None, command_device_id=DEVICE_ID, command_enum=DEVICE_ENUM
    )
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "set_position_manual"}
    )
    assert result["step_id"] == "set_position_manual"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"position": 42}
    )

    assert result["step_id"] == "developer_tools"
    assert result["description_placeholders"] is not None
    assert "42" in result["description_placeholders"]["result"]
    updated_subentry = entry.subentries[blind.subentry_id]
    assert updated_subentry.data[CONF_INITIAL_POSITION] == 42


async def test_developer_tools_teach_motor_success_sends_teach_open_and_stop(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    # The trailing open+stop test (after a successful teach) never gets a
    # stick reply fed either, so without this, control_blind_exclusive()'s
    # own ack-wait would block for the real MOVE_ACK_TIMEOUT on each of
    # those two writes - see
    # test_choosing_test_motor_runs_motor_test_sequence_before_
    # calibration_initial_state's own comment on this same patch.
    monkeypatch.setattr(api_module, "MOVE_ACK_TIMEOUT", 0.0)
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "teach_motor"}
    )
    assert result["step_id"] == "teach_motor"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["step_id"] == "developer_tools"
    assert result["description_placeholders"] is not None
    assert (
        "Teach, Open, and Stop were transmitted"
        in (result["description_placeholders"]["result"])
    )
    # teach_motor() itself is a two-phase transmit (teach_60, finish_40),
    # followed by open and stop: 4 writes in total.
    assert len(written(connected_api)) == 4  # teach_60, finish_40, open, stop


async def test_developer_tools_teach_motor_failure_shows_failure_notice(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connected_api, "teach_motor", AsyncMock(return_value=False))
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "teach_motor"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["step_id"] == "developer_tools"
    assert result["description_placeholders"] is not None
    assert "failed" in result["description_placeholders"]["result"]


async def test_developer_tools_send_raw_command_success(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "send_raw_command"}
    )
    assert result["step_id"] == "send_raw_command"
    assert result["data_schema"] is not None
    payload = result["data_schema"]({})["payload"]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"payload": payload}
    )

    assert result["step_id"] == "developer_tools"
    assert len(written(connected_api)) == 1


async def test_developer_tools_send_raw_command_rejects_an_invalid_payload(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "send_raw_command"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"payload": "not a valid payload!"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "send_raw_command"
    assert result["errors"] == {"payload": "invalid_raw_payload"}


async def test_developer_tools_reset_stick(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "reset_stick"}
    )

    assert result["step_id"] == "developer_tools"
    assert result["description_placeholders"] is not None
    assert "reset" in result["description_placeholders"]["result"].lower()
    # reset_and_reconnect() tried a real connect() against the fake port
    # above, which fails (no such device) and schedules a 5s retry via
    # async_call_later - exactly what a real failed reconnect should do, but
    # it would otherwise outlive this test as a "lingering timer". Clean it
    # up the same way a normal integration unload would.
    await connected_api.disconnect()


async def test_developer_tools_copy_diagnostics_shows_a_text_snapshot_then_returns(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "copy_diagnostics"}
    )
    assert result["step_id"] == "copy_diagnostics"
    assert result["data_schema"] is not None
    diagnostics_text = result["data_schema"]({})["diagnostics"]
    assert "Living Room Blind" in diagnostics_text

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"diagnostics": diagnostics_text}
    )

    assert result["step_id"] == "developer_tools"


async def test_copy_diagnostics_shows_dashes_not_none_with_no_position_history(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Regression: the copy-diagnostics text export must show dashes,
    not the literal word "None", when a blind has no position history.

    _add_existing_blind()'s device never has a position update recorded,
    so api.get_last_position_update() falls back to a dict with
    previous_position/new_position both None - exactly a freshly set up
    or just-restarted blind. The live developer_tools panel's own
    description (via _developer_snapshot()'s position_previous/
    position_new placeholders) already renders that as "--"; this text
    export used to pass those same None values straight into
    _t(..., value=...) instead, rendering the literal word "None" for
    all three position lines below.
    """
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    result = await _start_reconfigure_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "developer_tools"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "copy_diagnostics"}
    )
    assert result["data_schema"] is not None

    diagnostics_text = result["data_schema"]({})["diagnostics"]

    assert "Previous position: --" in diagnostics_text
    assert "New position: --" in diagnostics_text
    assert "Current estimated position: --" in diagnostics_text
    # Deliberately scoped to the three position lines finding 21 actually
    # covers, not a blanket "None" ban: a never-calibrated blind's
    # entities/candidate-status-identities lines legitimately show "None"
    # too (and "Frames observed during calibration" legitimately shows
    # "None recorded"), none of which this finding ever claimed was wrong.
    assert "Previous position: None" not in diagnostics_text
    assert "New position: None" not in diagnostics_text
    assert "Current estimated position: None" not in diagnostics_text


def _load_step(language: str, step_id: str) -> dict[str, Any]:
    integration_dir = (
        Path(__file__).parent.parent / "custom_components" / "schellenberg_usb"
    )
    path = (
        integration_dir / "strings.json"
        if language == "strings"
        else integration_dir / "translations" / f"{language}.json"
    )
    with path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    step: dict[str, Any] = doc["config_subentries"]["blind"]["step"][step_id]
    return step


def _load_selector_translations(language: str) -> dict[str, Any]:
    integration_dir = (
        Path(__file__).parent.parent / "custom_components" / "schellenberg_usb"
    )
    path = (
        integration_dir / "strings.json"
        if language == "strings"
        else integration_dir / "translations" / f"{language}.json"
    )
    with path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    return cast(dict[str, Any], doc["selector"])


def test_calibration_initial_state_forces_an_explicit_choice() -> None:
    """The old currently_open checkbox defaulted to "closed" (unchecked)
    whenever the person left it alone, which looked identical to an actual,
    considered answer - and its own label only ever stated what *checking*
    it meant, with nothing on the form saying what leaving it unchecked
    meant instead (see the now-removed data_description this test used to
    check for). First replaced by a required "initial_state" select with no
    default and exactly two options ("open"/"closed"), on the assumption
    that Home Assistant's frontend could not submit this form with neither
    chosen - confirmed live to be false: its "list" (radio) rendering
    visibly highlights the first configured option before anything is
    picked, indistinguishable from a deliberate answer, reintroducing the
    original problem one level up. A third, clearly-not-a-real-answer
    "unselected" option (see options_flow_calibration.py's
    async_step_calibration_initial_state) now occupies that highlighted
    first slot instead of "open"; submitting it - or anything other than
    an explicit
    "open"/"closed" - is rejected with an on-screen error instead of being
    silently treated as "closed" or relied on to be unsubmittable. "open"
    and "closed" still get their own visible, translated label via the
    top-level "selector" translation key instead of an implied opposite.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        step = _load_step(language, "calibration_initial_state")
        assert "initial_state" in step["data"], language
        assert "currently_open" not in step["data"], language
        assert "data_description" not in step, language

        options = _load_selector_translations(language)["initial_state"]["options"]
        assert set(options) == {"unselected", "open", "closed"}, language

    # Reuses the exact pre-existing wording the removed checkbox/
    # data_description pair already had - "open" was that checkbox's own
    # label, "closed" was its data_description - rather than inventing new,
    # shorter labels for either state. "unselected" is new.
    de_options = _load_selector_translations("de")["initial_state"]["options"]
    assert de_options == {
        "unselected": "Bitte auswählen",
        "open": "Der Rollladen ist aktuell vollständig geöffnet",
        "closed": "Der Rollladen ist aktuell vollständig geschlossen",
    }


def test_did_motor_move_unified_field_replaces_the_old_boolean_everywhere() -> None:
    """The "existing" workflow's own re-test used to have its own separate
    "motor_moved" plain-boolean field and label here, dropped when it was
    unified (2026-11-14) onto the same "motor_moved_result" required select
    _motor_moved_result_schema() already used for "pairing" (see that
    schema's own docstring for why). Confirmed across every language, the
    same way test_calibration_initial_state_forces_an_explicit_choice just
    above confirms its own, analogous boolean-to-select replacement: the
    old field and its label are gone, not just unreferenced from Python.
    The full four-label option set backing the shared selector still
    carries all three real outcomes plus the placeholder in every
    language - which caller actually offers "wrong_motor" is a runtime
    subset chosen in config_flow.py (see allow_wrong_motor there), not a
    reason to drop its label from strings.json.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        step = _load_step(language, "did_motor_move")
        assert "motor_moved_result" in step["data"], language
        assert "motor_moved" not in step["data"], language

        options = _load_selector_translations(language)["motor_moved_result"]["options"]
        assert set(options) == {"unselected", "yes", "no", "wrong_motor"}, language


def test_calibration_method_choice_menu_options_capitalization() -> None:
    """ "test_motor" keeps this step's own original, deliberate
    lowercase-initial convention - confirmed in an earlier session as
    unconventional but intentional (a plural noun is normally
    capitalised in German regardless of sentence position).

    "manual_times" no longer follows that same lowercase convention,
    though: reworded (2026-11-14, see test_calibration_method_choice_
    title_names_the_menu_not_just_manual_entry below for the wording
    itself), then, the same day, explicitly asked to be capitalised
    again at its first letter in every language - overriding its own
    earlier lowercase treatment for this one option specifically. The
    two options on this same step are therefore now deliberately cased
    differently from each other, not a lapse to "fix": that asymmetry
    is exactly what was asked for, the second time around.
    """
    menu_options = _load_step("de", "calibration_method_choice")["menu_options"]
    assert menu_options == {
        "test_motor": "durch Bewegen des Rollladens kalibrieren",
        "manual_times": "Rollladenmotorlaufzeiten manuell festlegen",
    }


def test_calibration_timeout_choice_manual_times_matches_the_other_two() -> None:
    """calibration_timeout_choice's own "manual_times" option used to
    read "Laufzeiten manuell eingeben" - deliberately kept distinct from
    calibration_method_choice's sibling option (a test guarded against
    an accidental blanket find-and-replace ever touching it, since
    nobody had actually asked for it to change).

    2026-11-14, same day, explicitly asked to change after all: both
    menu options AND the manual_times step's own title (previously a
    fourth, generic wording, "Laufzeiten eingeben"/"Enter travel
    times") now read identically, in every language - the person's own
    follow-up once this leftover three-way mismatch was pointed out
    while renaming the step's title to match calibration_method_
    choice's option. This supersedes the old guard above: that one
    protected against an *unintentional* propagation, not against a
    deliberate, explicit request to unify all three.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        choice_option = _load_step(language, "calibration_method_choice")[
            "menu_options"
        ]["manual_times"]
        timeout_option = _load_step(language, "calibration_timeout_choice")[
            "menu_options"
        ]["manual_times"]
        step_title = _load_step(language, "manual_times")["title"]
        assert timeout_option == choice_option == step_title, language


def test_calibration_method_choice_title_names_the_menu_not_just_manual_entry() -> None:
    """Companion to test_calibration_method_choice_menu_options_
    capitalization above. A same-day first pass at this rename
    (2026-11-14) made the title and the manual_times option share one
    wording - wrong, because this step's own page *is* the two-option
    menu (confirmed against a screenshot of it): it offers a choice
    between calibrating by moving the shutter (test_motor) and entering
    times by hand (manual_times), so a title that already says
    "manually"/"manuell" would misdescribe the test_motor path too.

    Corrected the same day: the title now names only what both options
    are *for* - the shutter motor's travel times - with no "manually"
    anywhere in it, while the manual_times option alone keeps that
    word, since it is the one path it actually describes. The two must
    therefore differ in every language, not match.

    German briefly dropped "lauf" from the option only ("Rollladenmotorzeiten
    manuell festlegen" vs. the title's "Rollladenmotorlaufzeiten festlegen") -
    the person's own exact dictated wording for the option at the time, kept
    verbatim rather than "corrected" to match the title's stem. Later the
    same day, after seeing the two side by side in the running app, the
    person asked for "Rollladenmotorlaufzeiten" everywhere instead, in every
    menu it appears in - reversing that specific earlier dictation. German's
    option and title now share that same stem, differing only by "manuell",
    matching how EN/ES/FR already worked all along.
    """
    expected = {
        "strings": (
            "Set the shutter motor's travel times",
            "Manually set the shutter motor's travel times",
        ),
        "en": (
            "Set the shutter motor's travel times",
            "Manually set the shutter motor's travel times",
        ),
        "de": (
            "Rollladenmotorlaufzeiten festlegen",
            "Rollladenmotorlaufzeiten manuell festlegen",
        ),
        "es": (
            "Establecer los tiempos de recorrido del motor de la persiana",
            "Establecer manualmente los tiempos de recorrido del motor de la persiana",
        ),
        "fr": (
            "Définir les durées de course du moteur du volet",
            "Définir manuellement les durées de course du moteur du volet",
        ),
    }
    for language, (title, manual_times) in expected.items():
        step = _load_step(language, "calibration_method_choice")
        assert step["title"] == title, language
        assert step["menu_options"]["manual_times"] == manual_times, language
        # The two must differ now - see this test's own docstring for why
        # a shared wording was wrong in the first place.
        assert step["title"] != step["menu_options"]["manual_times"], language


def test_teach_motor_step_title_matches_its_own_menu_label() -> None:
    """An earlier fix (see this same test's own prior history) split the
    menu_options label under developer_tools from this step's own title,
    because the label used to carry a long, two-part explanation
    (teaching a new motor AND activating the USB transmitter, plus a
    data-loss caution) that the person would otherwise read twice in a
    row - once to choose the menu entry, then again as the page heading.

    2026-11-14: the person asked for the two to match again, this time
    the other way around - both now share this short, more specific
    phrase naming what the step actually does (pairing a *different*
    shutter at this same RF identity, overwriting the one currently
    taught) - and for the caution that used to live in the long menu
    label to move into the step's own description instead, as a
    standalone sentence, rather than staying a parenthetical bolted onto
    the menu entry. That caution must still reach the person before they
    commit to clicking the menu entry, and a short title/label alone
    cannot fit it - so the step's own description, which the menu entry
    cannot show in advance, needs to carry it instead; this test also
    guards that it is still present somewhere in the step, not quietly
    dropped in the handoff.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        step = _load_step(language, "teach_motor")
        menu_label = _load_step(language, "developer_tools")["menu_options"][
            "teach_motor"
        ]
        assert step["title"] == menu_label, language

    assert _load_step("de", "teach_motor")["title"] == (
        "Einen neuen Rollladen anlernen, um ihn mit diesem Rollladen zu überschreiben"
    )


def test_teach_motor_description_still_carries_the_overwrite_caution() -> None:
    """Companion to test_teach_motor_step_title_matches_its_own_menu_label
    above: the caution moved out of the long developer_tools menu label
    and into this step's own description (2026-11-14) must actually have
    landed there, in every language, rather than having been dropped
    while the menu label was shortened.
    """
    caution_substring = {
        "strings": "Caution: existing motor data will be overwritten",
        "en": "Caution: existing motor data will be overwritten",
        "de": "Achtung: Die bestehenden Motordaten werden",
        "es": "Atención: los datos de motor existentes se sobrescribirán",
        "fr": "Attention : les données moteur existantes seront écrasées",
    }
    for language, substring in caution_substring.items():
        description = _load_step(language, "teach_motor")["description"]
        assert substring in description, language

        # The caution now lives solely in the description - the menu
        # label (now identical to the step's own short title) must not
        # carry a second copy of it.
        menu_label = _load_step(language, "developer_tools")["menu_options"][
            "teach_motor"
        ]
        assert substring not in menu_label, language


def test_status_discovery_choice_matches_discover_status_capture_wording() -> None:
    """status_discovery_choice is the menu shown at the very end of the
    "Add device" pairing flow, right before a person leaves it; picking
    its own "discover_status" option leads into the very same
    async_step_discover_status() the developer_tools menu entry also
    leads to. All three used to say "anlernen"/"train"/"entrenar"/
    "apprendre" here, which is inaccurate for what actually happens -
    nothing is taught, Home Assistant only listens to and records the
    remote's radio signals for 45 seconds (see discover_status's own
    description). developer_tools and discover_status itself were
    already corrected to "erfassen"/"capture"/"captar"/"capter"
    wording; this guards the pairing-flow entry point against falling
    back out of step with that fix, since status_discovery_choice.title
    and .menu_options.discover_status are their own translation keys
    (scoped to status_discovery_choice's own step_id) that a find-and-
    replace over the "discover_status" identifier alone would not reach.
    """
    capture_verb = {
        "strings": "capture",
        "en": "capture",
        "de": "erfassen",
        "es": "captar",
        "fr": "capter",
    }
    old_verb = {
        "strings": "train",
        "en": "train",
        "de": "anlernen",
        "es": "entrenar",
        "fr": "apprendre",
    }
    for language, verb in capture_verb.items():
        choice_step = _load_step(language, "status_discovery_choice")
        title = choice_step["title"].lower()
        menu_label = choice_step["menu_options"]["discover_status"].lower()
        assert verb in title, language
        assert verb in menu_label, language
        assert old_verb[language] not in title, language
        assert old_verb[language] not in menu_label, language

    assert _load_step("de", "status_discovery_choice")["title"] == (
        "Funkbefehle von einer Fernbedienung erfassen?"
    )
    assert (
        _load_step("de", "status_discovery_choice")["menu_options"]["discover_status"]
        == "von der Original-Fernbedienung erfassen (empfohlen)"
    )


def _load_create_entry(language: str) -> dict[str, Any]:
    integration_dir = (
        Path(__file__).parent.parent / "custom_components" / "schellenberg_usb"
    )
    path = (
        integration_dir / "strings.json"
        if language == "strings"
        else integration_dir / "translations" / f"{language}.json"
    )
    with path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    return cast(dict[str, Any], doc["config_subentries"]["blind"]["create_entry"])


def test_blind_subentry_create_entry_has_its_own_confirmation_text() -> None:
    """Previously absent entirely, so Home Assistant fell back to its own
    generic create-entry text after a blind subentry was created - with
    nothing on screen naming what had actually just happened, which is
    what made this confusing after a first calibration. Covers the
    English strings.json source and all 4 translations; the German
    wording is the person's own exact words, not a paraphrase.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        assert _load_create_entry(language)["default"], language

    assert _load_create_entry("de")["default"] == "Die Konfiguration wurde erstellt."


def _load_calibration_complete_description(language: str) -> str:
    integration_dir = (
        Path(__file__).parent.parent / "custom_components" / "schellenberg_usb"
    )
    path = (
        integration_dir / "strings.json"
        if language == "strings"
        else integration_dir / "translations" / f"{language}.json"
    )
    with path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    return cast(
        str,
        doc["config_subentries"]["blind"]["step"]["calibration_complete"][
            "description"
        ],
    )


def test_calibration_complete_description_opens_with_a_grammatical_sentence() -> None:
    """Reported directly against a live screenshot, by comparison with the
    blind subentry's own confirmation text just above (create_entry.default,
    now "Die Konfiguration wurde erstellt." / "The configuration has been
    created."): this step's own first line was the same kind of bare noun
    phrase with no article or verb - "Kalibrierung für {device_name}
    abgeschlossen!" / "Calibration for {device_name} complete!" - unlike
    every other step's description in these same translation files. Covers
    only the opening line; the rest of the description (measured times,
    status detection, position-tracking note) is unchanged. The German
    wording is the person's own exact words, not a paraphrase.
    """
    expected_openings = {
        "strings": "The calibration for {device_name} is complete!\n\n",
        "en": "The calibration for {device_name} is complete!\n\n",
        "de": "Die Kalibrierung für {device_name} ist abgeschlossen!\n\n",
        "es": "¡La calibración de {device_name} se ha completado!\n\n",
        "fr": "La calibration de {device_name} est terminée !\n\n",
    }
    for language, expected_opening in expected_openings.items():
        assert _load_calibration_complete_description(language).startswith(
            expected_opening
        ), language


def test_set_position_manual_names_the_selected_blind_by_the_field_too() -> None:
    """Reported from a screenshot: on a hub with several blinds, the
    position field's own pre-filled value gave no hint by itself which
    blind it belonged to - only the step's top-level description said so,
    and that line is easy to have already scrolled past. Three fixes, all
    from the same day, settled this string's final shape:

    First, a data_description was added under the "position" field,
    repeating that same {selected_blind} name right next to the field
    itself, in every language, worded "The position shown below is for
    {selected_blind}." (and each language's own equivalent) - reusing the
    placeholder async_step_set_position_manual() already fills via its
    existing description_placeholders.

    Second, a follow-up screenshot showed that wording had the direction
    backwards in every language: Home Assistant renders a field's own
    data_description as a caption *underneath* that field, not above it,
    so "shown below" should have read "shown above" from the start.
    Corrected to "shown above"/"oben angezeigte"/"mostrada arriba"/
    "ci-dessus" - briefly, before the third fix below reverted it again.

    Third, rather than keep fighting the above/below wording against a
    caption position neither of the first two fixes actually controlled,
    the sentence itself was moved to *before* the field instead - the one
    place in this form that already renders above it, the step's own
    top-level `description`, which already uses this same {selected_blind}
    placeholder successfully for its own first sentence. The sentence was
    re-reverted to its original "below"/"unten"/"a continuación"/
    "ci-dessous" wording (correct again now that it precedes the field)
    and appended as `description`'s own final sentence; `data_description`
    was removed for this step entirely, rather than left behind holding an
    empty object once "position" was its only key. Being the very last
    thing read before the field itself, immediately adjacent to it exactly
    as the original real-world report asked for, no caption below the
    field is needed any more either - and no config_flow.py change was
    needed for any of the three fixes, since description_placeholders
    already reaches both `description` and `data_description` alike (the
    second screenshot incidentally confirmed this for `data_description`
    too, rendering {selected_blind} as the real device name
    "Badezimmerrolladen" rather than a literal, unsubstituted placeholder -
    moot for this field's own final shape, which no longer uses
    data_description at all, but settled for any future use of it
    elsewhere in this integration).

    The German description text separately dropped its leading article on
    the second sentence ("Aktuell erfasste Position" instead of "Die
    aktuell erfasste Position") - also from the first screenshot, and also
    fixed here; "en"/"es"/"fr" already read naturally without one.

    Fourth (2026-11-08, a later real-world report, same final sentence):
    with a long enough {selected_blind} name and/or a narrow enough dialog,
    the German final sentence's own leading "Die unten" wrapped across two
    lines on its own - "Die" ending one line, "unten angezeigte Position
    ist für <name>." starting the next - reading as if "Die" belonged to
    the sentence above it rather than to "unten" right after it. Joined
    with U+00A0 (a non-breaking space) between exactly those two words, so
    a browser can still wrap the rest of the sentence normally but can no
    longer split this specific pair - the same narrow, surgical fix the
    report's own wording asked for ("die zwei Wörter 'Die unten' sollen
    nur eine Zeile runter zu den anderen Wörtern"), without reopening the
    sentence-boundary/caption-position questions the first three fixes
    above already settled. "en"/"es"/"fr" start their own final sentence
    differently ("The position"/"La posición"/"La position", not a bare
    two-word adverbial lead-in) and were never reported to have this
    problem, so none of the three were touched.
    """
    expected_final_sentence = {
        "strings": "The position shown below is for {selected_blind}.",
        "en": "The position shown below is for {selected_blind}.",
        # The space between "Die" and "unten" below is U+00A0 (a
        # non-breaking space), matching the production string exactly -
        # see the fourth fix in this test's own docstring above. RUF001
        # flags it as visually ambiguous against a plain space, which is
        # exactly why it needs this comment, but it is not a mistake.
        "de": "Die unten angezeigte Position ist für {selected_blind}.",  # noqa: RUF001
        "es": "La posición mostrada a continuación es para {selected_blind}.",
        "fr": "La position indiquée ci-dessous concerne {selected_blind}.",
    }
    for language in ("strings", "en", "de", "es", "fr"):
        step = _load_step(language, "set_position_manual")
        assert step["description"].endswith(expected_final_sentence[language]), language
        assert "data_description" not in step, language

    assert (
        "Die aktuell erfasste Position"
        in _load_step("de", "set_position_manual")["description"]
    )
