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
from uuid import UUID

import pytest
import serialx
from homeassistant.config_entries import ConfigSubentry, SubentryFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    CONF_OPEN_TIME,
    CONF_OPEN_TIME_SECONDS,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_DEVICE_EVENT,
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


async def test_user_step_shows_cannot_connect_on_serial_exception(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_check(port: str) -> None:
        raise serialx.SerialException("no such device")

    monkeypatch.setattr(config_flow_module, "check_serial_port", failing_check)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SERIAL_PORT: "/dev/ttyUSB0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_shows_cannot_connect_on_oserror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """serialx-specific: OSError/TimeoutError are also connect failures here.

    v1.0.4 only caught serial.SerialException; the serialx migration
    widened this to also catch plain OSError/TimeoutError (see api.py's
    connect() and this same widening in options_flow.py), so both branches
    need coverage, not just serialx.SerialException itself.
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


async def test_usb_confirm_shows_cannot_connect_on_serial_exception(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_check(port: str) -> None:
        raise serialx.SerialException("gone")

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
    """Reach the blind subentry's initial pairing-choice menu.

    Real Home Assistant starts a *new* subentry flow with
    context={"source": "user"} (see this integration's strings.json:
    config_subentries.blind.initiate_flow.user is the "Add device" button;
    there is no initiate_flow.blind). Subentry-type dispatch happens via the
    (entry_id, subentry_type) handler tuple, not via the context source, so
    async_step_user() is the correct, real entry point regardless of type.

    "add_shutter_from_remote" is the recommended entry point: it pairs a
    *new* blind from an unknown remote channel (api.pair_device_and_wait())
    and, after a successful test, offers to pair another blind sharing
    that same channel (see _finish_pairing_or_offer_repeat() in
    config_flow.py).
    """
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "pair_device",
        "add_shutter_from_remote",
    }
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


async def test_pair_device_workflow_names_device_then_starts_calibration(
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

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "pair_device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Kitchen Blind"}
    )

    # pair_device's own workflow goes straight into calibration after naming.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close"


async def test_pair_device_timeout_aborts_with_pairing_timeout(
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
        result["flow_id"], user_input={"next_step_id": "pair_device"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"


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

    Shared by both pair_device calibration tests below, for whichever leg
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


async def _pair_device_up_to_calibration_close(
    hass: HomeAssistant, entry: MockConfigEntry
) -> SubentryFlowResult:
    """Drive pair_device through naming and reach the calibration_close form."""
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "pair_device"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "name_device"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Kitchen Blind"}
    )
    assert result["step_id"] == "calibration_close"
    return result


async def test_pair_device_calibration_choosing_closed_sets_initial_position_closed(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair_device's own calibration, driven all the way to CREATE_ENTRY.

    Choosing initial_state="closed" runs the open leg first and the close
    leg last, so the close leg's own end stop - 0%, fully closed - is the
    position the fresh subentry's data should carry under
    CONF_INITIAL_POSITION. Neither this path nor CONF_INITIAL_POSITION
    being present in the created entry's data were previously exercised
    anywhere in this file - every existing pair_device test stops at
    reaching the calibration_close form without submitting it, see
    test_pair_device_workflow_names_device_then_starts_calibration above.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    result = await _pair_device_up_to_calibration_close(hass, entry)
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 0


async def test_pair_device_calibration_choosing_open_sets_initial_position_open(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the test above: initial_state="open" runs close then open.

    The open leg now runs last, so the fresh subentry's data should carry
    CONF_INITIAL_POSITION == 100 (fully open) instead of the "closed"
    choice's 0.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    result = await _pair_device_up_to_calibration_close(hass, entry)
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INITIAL_POSITION] == 100


async def test_add_shutter_from_remote_runs_test_motor_before_calibration_choice(
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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "name_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    # Hybrid workflow tests the motor before offering the calibration choice.
    # This is the confirmation gate: the form is shown and nothing has been
    # sent to the stick yet - the person must choose a direction explicitly,
    # there is no default and no position-based suggestion.
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
        result["flow_id"], user_input={"motor_moved": True}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "test_success"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "calibration_close",
        "manual_times",
    }


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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
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
        result["flow_id"], user_input={"motor_moved": False}
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
        result["flow_id"], user_input={"motor_moved": False}
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
        result["flow_id"], user_input={"motor_moved": False}
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
    test_add_shutter_from_remote_runs_test_motor_before_calibration_choice.
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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved": False}
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
        result["flow_id"], user_input={"motor_moved": True}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "test_success"
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "calibration_close",
        "manual_times",
    }


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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved": True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    assert result["step_id"] == "manual_times"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_OPEN_TIME_SECONDS: 30.0, CONF_CLOSE_TIME_SECONDS: 28.0},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_status"


async def test_manual_times_to_discover_status_offers_repeat_then_finish_creates_entry(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hybrid workflow's "manual_times" branch must reach a real
    CREATE_ENTRY, the same way its "calibration_close" sibling does - but
    only after the add_shutter_from_remote repeat-offer gate
    (_finish_pairing_or_offer_repeat() in config_flow.py), since
    add_shutter_from_remote sets _offer_repeat_pairing: a real remote
    channel can control more than one motor, so confirm_status_discovery's
    success no longer creates the subentry directly, it shows
    "pairing_repeat_choice" first.

    test_manual_times_leads_into_discover_status above only checks that the
    discover_status form is reached; it never confirms that submitting it
    actually finishes pairing a *brand-new* device
    (confirm_status_discovery's async_create_entry() branch) rather than
    updating an existing one, which
    test_discover_status_success_confirms_and_updates_existing_blind already
    covers via the reconfigure/Developer Tools entry point instead. See
    test_add_shutter_from_remote_pair_another_on_channel_adds_a_second_blind
    below for the sibling "pair_another_on_channel" branch of this same
    menu.
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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "New Blind"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved": True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_OPEN_TIME_SECONDS: 30.0, CONF_CLOSE_TIME_SECONDS: 28.0},
    )
    assert result["step_id"] == "discover_status"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pairing_repeat_choice"
    assert result["description_placeholders"] == {"device_name": "New Blind"}
    assert set(cast(Iterable[str], result["menu_options"])) == {
        "pair_another_on_channel",
        "finish_pairing",
    }
    # Nothing is persisted yet while this choice is pending.
    assert entry.subentries == {}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "finish_pairing"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "New Blind"
    assert result["data"][CONF_OPEN_TIME] == 30.0
    assert result["data"][CONF_CLOSE_TIME] == 28.0
    assert result["data"][CONF_STATUS_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_STATUS_ENUM] == DEVICE_ENUM
    # add_shutter_from_remote always sets _offer_repeat_pairing (see
    # async_step_add_shutter_from_remote), so even this single,
    # never-repeated blind gets a generated per-blind unique_id
    # (_pending_blind_id, a UUID) rather than the raw, potentially-shared
    # command_device_id - see _pairing_unique_id().
    assert len(entry.subentries) == 1
    created = next(iter(entry.subentries.values()))
    assert created.unique_id is not None
    assert UUID(created.unique_id)
    assert created.unique_id != DEVICE_ID


async def test_add_shutter_from_remote_pair_another_on_channel_adds_a_second_blind(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sibling branch of pairing_repeat_choice: choosing to pair another
    blind on the same channel must persist the first blind immediately
    (async_add_subentry(), since create_entry/abort can only answer once -
    see async_step_pair_another_on_channel()'s own docstring), loop back to
    name_device for a second blind sharing the exact same command identity,
    and give each blind its own unique_id (_pending_blind_id, not the
    shared command_device_id - see _pairing_unique_id()) so the second one
    does not collide with the first.
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
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Living Room Left"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved": True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_OPEN_TIME_SECONDS: 30.0, CONF_CLOSE_TIME_SECONDS: 28.0},
    )
    assert result["step_id"] == "discover_status"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "pairing_repeat_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "pair_another_on_channel"}
    )

    # The first blind is already a real subentry - persisted directly,
    # since this flow is still running and cannot create_entry() twice.
    assert len(entry.subentries) == 1
    first = next(iter(entry.subentries.values()))
    assert first.title == "Living Room Left"
    assert first.data[CONF_DEVICE_ID] == DEVICE_ID
    assert first.unique_id != DEVICE_ID  # see _pairing_unique_id()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name_device"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Living Room Right"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"test_direction": "open"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"motor_moved": True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual_times"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_OPEN_TIME_SECONDS: 30.0, CONF_CLOSE_TIME_SECONDS: 28.0},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "confirm_status_discovery"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "pairing_repeat_choice"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "finish_pairing"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room Right"
    # Same shared remote channel on both blinds...
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID
    assert result["data"][CONF_DEVICE_ENUM] == DEVICE_ENUM

    # ...but never the same unique_id, and never the first blind's own
    # unique_id either - each blind gets its own via _pending_blind_id
    # (see _pairing_unique_id()), so the second never collides with the
    # first despite sharing a command_device_id/command_enum.
    assert len(entry.subentries) == 2
    second = next(
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_id != first.subentry_id
    )
    assert second.title == "Living Room Right"
    assert second.unique_id is not None
    assert UUID(second.unique_id)
    assert second.unique_id != DEVICE_ID
    assert second.unique_id != first.unique_id


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
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
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
    """Unlike the "existing"/reconfigure case above, a fresh pairing's
    command_failed is just as inconclusive as a sent-but-unmoving test: the
    RF command never went out, so nothing about that direction was actually
    tested. It gets the same retry_notice and the same free, unlimited
    choice that "no movement" does (see
    test_did_motor_move_no_shows_retry_notice_and_lets_person_choose_again),
    with no cap and no fallback - demonstrated here across two retries in
    a row.
    """
    entry = _build_hub_entry(hass, connected_api)
    monkeypatch.setattr(
        connected_api,
        "pair_device_and_wait",
        AsyncMock(return_value=(DEVICE_ID, DEVICE_ENUM)),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(connected_api, "control_blind", AsyncMock(return_value=False))
    result = await _start_pairing_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "add_shutter_from_remote"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    # Naming the device chains straight into showing the test_motor form
    # (the hybrid branch of async_step_name_device() calls
    # async_step_test_motor() with no user_input): nothing has been
    # attempted yet, so there is no retry notice yet either.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_NAME: "Hybrid Blind"}
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
        result["flow_id"], user_input={"motor_moved": True}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "command_test_successful"


async def test_did_motor_move_false_for_an_existing_blind_returns_to_edit(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the hybrid-pairing path (which keeps reshowing the choice
    indefinitely, see
    test_did_motor_move_no_shows_retry_notice_and_lets_person_choose_again),
    a failed re-test of an *already-configured* blind sends the person to
    its "edit" form instead, on the very first "no movement" - already
    having a cover entity and known settings to fix makes that a more
    useful next step than an open-ended retry loop.
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
        result["flow_id"], user_input={"motor_moved": False}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "edit"


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
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
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

        # A move registers itself as the pending transmission - that is what
        # makes a tE busy reply retryable - and transmit_block_reason() keeps
        # reporting pending_retry until the stick's t0 completes the cycle.
        # The Developer Tools step deliberately refuses to start another
        # command while one is still outstanding, since that is how frames
        # got dropped, so this models the ACK real hardware sends once the RF
        # frame is done.
        connected_api._handle_message("t0")


async def test_developer_tools_set_position_open_and_closed_sync_without_sending_rf(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    # manual_sync_position() only succeeds for a "live" cover, i.e. a device
    # id the cover platform has registered on the api; these tests drive the
    # subentry flow directly without setting up that platform, so register
    # it by hand the way cover.py's async_added_to_hass() normally would.
    connected_api.register_existing_devices([{"id": DEVICE_ID, "enum": DEVICE_ENUM}])

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


async def test_developer_tools_set_position_manual_accepts_an_exact_value(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry = _build_hub_entry(hass, connected_api)
    blind = _add_existing_blind(hass, entry)
    # See test_developer_tools_set_position_open_and_closed_sync_without_
    # sending_rf: manual_sync_position() requires the device to be
    # registered as a "live" cover, which this direct flow-driven test
    # never does on its own.
    connected_api.register_existing_devices([{"id": DEVICE_ID, "enum": DEVICE_ENUM}])
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


async def test_developer_tools_teach_motor_success_sends_teach_open_and_stop(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
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


def _load_calibration_close_step(language: str) -> dict[str, Any]:
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
    step: dict[str, Any] = doc["config_subentries"]["blind"]["step"][
        "calibration_close"
    ]
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


def test_calibration_close_forces_an_explicit_initial_state_choice() -> None:
    """The old currently_open checkbox defaulted to "closed" (unchecked)
    whenever the person left it alone, which looked identical to an actual,
    considered answer - and its own label only ever stated what *checking*
    it meant, with nothing on the form saying what leaving it unchecked
    meant instead (see the now-removed data_description this test used to
    check for). Replaced by a required "initial_state" select with no
    default and exactly two options ("open"/"closed", see
    options_flow_calibration.py's async_step_calibration_close): Home
    Assistant cannot submit this form with neither chosen, and both states
    get their own visible, translated label via the top-level "selector"
    translation key instead of an implied opposite.
    """
    for language in ("strings", "en", "de", "es", "fr"):
        step = _load_calibration_close_step(language)
        assert "initial_state" in step["data"], language
        assert "currently_open" not in step["data"], language
        assert "data_description" not in step, language

        options = _load_selector_translations(language)["initial_state"]["options"]
        assert set(options) == {"open", "closed"}, language

    # Reuses the exact pre-existing wording the removed checkbox/
    # data_description pair already had - "open" was that checkbox's own
    # label, "closed" was its data_description - rather than inventing new,
    # shorter labels for either state.
    de_options = _load_selector_translations("de")["initial_state"]["options"]
    assert de_options == {
        "open": "Der Rollladen ist aktuell vollständig geöffnet",
        "closed": "Der Rollladen ist aktuell vollständig geschlossen",
    }
