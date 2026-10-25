"""Tests for the shared calibration step machinery (options_flow_calibration.py).

Driven through the real subentry flow manager (hass.config_entries.subentries)
via the "reconfigure" -> "calibrate" route on an *existing* blind subentry,
since CalibrationFlowHandler is only ever constructed by
SchellenbergPairingSubentryFlow (see its own class docstring) - this also
exercises that delegation in config_flow.py, not just this module in
isolation. The "new pairing -> calibration -> create subentry" path is
covered separately in test_config_flow.py, since setting that path up
requires first driving the pairing wizard itself.

Movement-start/stop waits use a real asyncio.Event under the hood (not
asyncio.sleep), so firing the SIGNAL_DEVICE_EVENT dispatcher signal while a
configure() call is in flight resolves them, with no need to mock asyncio -
the same asyncio.ensure_future() pattern tests/test_cover.py and
tests/test_api.py already use to let a task run up to a blocking point.

The one thing every test below still has to get right by hand is *when* to
fire that signal: too early - before the flow task has actually registered
its listener for it - and the signal is silently missed, since
async_dispatcher_send() does not queue a signal for a listener that
subscribes later. Two earlier approaches tried to bridge that gap with a
fixed amount of "settle time" between creating the flow task and sending
its signal - first a fixed count of bare asyncio.sleep(0) calls, then a flat
real asyncio.sleep(0.5) - and neither reliably worked, because both are
still guesses: the exact number of event-loop iterations (or the exact
wall-clock time) the flow task needs to reach its next
async_dispatcher_connect() call depends on internal implementation details
(such as asyncio.wait_for()'s own task-wrapping) this test has no business
knowing, and that can vary across machines and Python versions.

_spy_on_dispatcher_connect() below removes the guesswork entirely: it wraps
async_dispatcher_connect() (as imported into options_flow_calibration.py) so
that every real call to it - which is exactly what
_wait_for_movement_start()/_wait_for_stop_event() each do, synchronously,
right before they start actually waiting - also flips an asyncio.Event the
test can await. Waiting for *that* event resolves the instant the flow task
is actually listening, however many loop iterations or however much
wall-clock time that took, with its own bounded, clearly labelled
pytest.fail() if a registration never happens at all, rather than relying on
pytest-timeout's blunter, whole-test signal timeout to eventually notice.

None of the above was ever the real problem, as it turns out: the actual bug
was in options_flow_calibration.py itself.
_wait_for_movement_start()/_wait_for_stop_event()'s own dispatcher listener
set its asyncio.Event directly, and Home Assistant's dispatcher can - and,
reproduced live via a thread-based pytest-timeout stack dump, did - run that
listener on a worker thread rather than the event loop even though it is
decorated @callback; @callback is good style for a fast listener but is not
a guarantee of inline execution on this integration's tested Home Assistant
version. asyncio.Event.set() is not thread-safe (only reachable safely
cross-thread via loop.call_soon_threadsafe(), not the plain call_soon() a
Future's own set_result() uses internally), so that cross-thread call
raised RuntimeError - which Home Assistant's background job runner only
logs, never surfaces here - meaning the event was never actually set and
the wait below ran to its own full timeout every time. Now fixed at the
source: the listener schedules the .set() call via
hass.loop.call_soon_threadsafe() instead of calling it directly (see that
module). The deterministic wait above remains, since waiting for the real
event beats guessing regardless - it just isn't covering for a live bug
anymore.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentry, SubentryFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.schellenberg_usb.options_flow_calibration as calibration_module
from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_CLOSE_TIME,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INVERT_DIRECTION,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_EVENT_CAPTURE,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
)
from custom_components.schellenberg_usb.options_flow_calibration import (
    CalibrationFlowHandler,
)

DEVICE_ID = "ABCDEF"
DEVICE_ENUM = "10"


def _build_hub_with_blind(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> tuple[MockConfigEntry, ConfigSubentry]:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SERIAL_PORT: "/dev/fake"})
    entry.add_to_hass(hass)
    entry.runtime_data = connected_api
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
    return entry, blind


async def _start_calibrate_flow(
    hass: HomeAssistant, entry: MockConfigEntry, blind: ConfigSubentry
) -> SubentryFlowResult:
    """Reach the calibration_close form via reconfigure -> calibrate."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BLIND),
        context={"source": "reconfigure", "subentry_id": blind.subentry_id},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibrate"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close"
    return result


def _spy_on_dispatcher_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Awaitable[None]]:
    """Make async_dispatcher_connect() calls awaitable, one at a time.

    _wait_for_movement_start() and _wait_for_stop_event() each create their
    own asyncio.Event and then, with no `await` in between, immediately
    register a dispatcher listener for it - so the moment
    async_dispatcher_connect() is actually called is the precise,
    deterministic point at which the flow task is ready to receive the
    corresponding signal. Nothing about that moment depends on how many
    internal await points the surrounding machinery (asyncio.wait_for()'s
    own task-wrapping, Home Assistant's own flow-manager dispatch, ...) took
    to get there, unlike a fixed sleep(0) count or a flat real sleep - the
    two approaches this module tried before this one that both, in turn,
    still ended up racing that unknown number of hops instead of actually
    waiting for the thing that matters.

    This patches calibration_module.async_dispatcher_connect - the name
    _wait_for_movement_start()/_wait_for_stop_event() actually call, since
    Python resolves it from that module's own namespace at call time - with
    a wrapper that still performs the real registration (so the flow under
    test behaves exactly as it would unpatched) and additionally sets an
    asyncio.Event every time it's called. The coroutine function this
    returns waits for that event and then clears it again, so calling it
    once waits for the *next* registration - however many of them a given
    test needs to step through in turn (one for movement-start, one for
    stop, repeated across the open and close legs).
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


async def _advance_through_open_leg(
    hass: HomeAssistant,
    flow_id: str,
    wait_for_registration: Callable[[], Awaitable[None]],
) -> SubentryFlowResult:
    """From calibration_close's form, drive through the whole open leg."""
    result = await hass.config_entries.subentries.async_configure(
        flow_id, user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(flow_id, user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    signal = f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}"
    async_dispatcher_send(hass, signal, EVENT_STARTED_MOVING_UP)
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, signal, EVENT_STOPPED)
    return await task


async def test_calibrate_menu_choice_shows_the_close_instruction_form(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry, blind = _build_hub_with_blind(hass, connected_api)

    result = await _start_calibrate_flow(hass, entry, blind)

    assert result["description_placeholders"] is not None
    assert result["description_placeholders"]["device_name"] == "Living Room Blind"


async def test_confirming_close_advances_to_the_open_instruction_form(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"


async def test_submitting_without_a_real_choice_shows_an_error_and_stays_on_the_form(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Home Assistant's list-mode selector highlights its first option (the
    new "unselected" placeholder, see options_flow_calibration.py's
    calibration_close schema comment) before anything has actually been
    chosen. Pressing "Weiter" on that highlighted-but-unchosen state must not
    be silently treated as either real answer; it must reshow this same form
    with an explicit error, and a genuine choice afterwards must still work.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "unselected"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close"
    assert result["errors"] == {"base": "initial_state_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"


async def test_full_open_and_close_legs_reach_the_summary_with_measured_times(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await _advance_through_open_leg(
        hass, result["flow_id"], wait_for_registration
    )
    assert result["step_id"] == "calibration_close_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STARTED_MOVING_DOWN
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_complete"
    # Both times are real (small but non-zero) durations, floored at 0.1s.
    assert result["description_placeholders"] is not None
    assert float(result["description_placeholders"]["open_time"]) >= 0.1
    assert float(result["description_placeholders"]["close_time"]) >= 0.1
    # _calibration_summary_placeholders() fills these via the translated
    # runtime text helpers; a translation lookup failure would raise rather
    # than return an empty value, but assert they are actually populated
    # rather than only checking that this step was reached at all.
    assert result["description_placeholders"]["primary_status_identity"]
    assert result["description_placeholders"]["position_tracking"]
    assert result["description_placeholders"]["calibration_end_reason"]


async def test_differing_status_identity_still_completes_via_capture_signal(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A motor whose status frames carry a different id than DEVICE_ID.

    Regression test for the real root cause behind "no RF confirmation
    during calibration": _wait_for_movement_start()/_wait_for_stop_event()
    used to listen only on SIGNAL_DEVICE_EVENT_{DEVICE_ID} - the
    command/pairing identity learned during pairing. A physical device
    whose status (receive) identity differs from that command identity -
    a protocol-documented possibility, see README.md's "Advanced protocol
    notes" - then never satisfied that wait, so calibration always timed
    out even though the motor genuinely moved. Every signal below is sent
    only on the capture-wide SIGNAL_DEVICE_EVENT_CAPTURE channel, tagged
    with a *different* device id ("FEDCBA") than the one being
    calibrated (DEVICE_ID) - exactly what api.py's _handle_message() would
    broadcast for such a device - and calibration must still reach
    calibration_complete rather than time out.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, "FEDCBA", EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, "FEDCBA", EVENT_STOPPED)
    result = await task
    assert result["step_id"] == "calibration_close_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, "FEDCBA", EVENT_STARTED_MOVING_DOWN
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, "FEDCBA", EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_complete"
    assert result["description_placeholders"] is not None
    assert float(result["description_placeholders"]["open_time"]) >= 0.1
    assert float(result["description_placeholders"]["close_time"]) >= 0.1


async def test_capture_signal_from_a_different_blind_does_not_satisfy_the_wait(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second, already-paired blind moving on the same hub while this one
    is being recalibrated must not falsely satisfy this one's movement-
    start wait - found during the independent audit alongside the
    CHANGELOG entries above this one. Before this fix, handle_capture_
    event() (options_flow_calibration.py) matched on command alone, so
    *any* device's frame on the hub-wide SIGNAL_DEVICE_EVENT_CAPTURE
    channel would satisfy it, silently corrupting the open/close time
    being measured for the wrong blind. Contrast with
    test_differing_status_identity_still_completes_via_capture_signal
    above, which proves this fix does not reject a frame from nobody
    else's known identity - only one positively claimed by the *other*
    blind added here.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    other_blind = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: "112233",
                CONF_DEVICE_ENUM: "05",
                CONF_STATUS_DEVICE_ID: "112233",
                CONF_STATUS_ENUM: "05",
                CONF_OPEN_TIME: 15.0,
                CONF_CLOSE_TIME: 14.0,
                CONF_INVERT_DIRECTION: False,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Kitchen Blind",
        unique_id="112233",
    )
    hass.config_entries.async_add_subentry(entry, other_blind)

    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening

    # The *other*, already-paired blind moves - must be ignored.
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, "112233", EVENT_STARTED_MOVING_UP
    )
    await asyncio.sleep(0.2)
    assert not task.done(), (
        "a different, already-paired blind's capture-signal frame "
        "incorrectly satisfied this blind's movement-start wait"
    )

    # This blind's own frame, on the very same hub-wide channel, still
    # works exactly as before.
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close_instruction"


async def test_calibration_excludes_other_blinds_frames_from_identity_result(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign already-paired blind's real status frames must never win
    this blind's calibration identity, even though api.py's hub-wide
    capture buffer physically records both.

    Regression test for audit finding 9. Unlike
    test_capture_signal_from_a_different_blind_does_not_satisfy_the_wait
    above - which only proves a foreign blind's SIGNAL_DEVICE_EVENT_CAPTURE
    *timing* signal cannot fake out the movement-start/stop wait - this
    feeds real "ss" status frames straight through
    connected_api._handle_message(), the exact mechanism api.py itself
    uses to populate _status_discovery_frames, so the foreign device's
    frames are genuinely present in the capture buffer by the time
    finish_status_frame_capture() scores it. It sends three such frames,
    all with a command that matches the "opening" phase, against this
    blind's own single matching frame - enough to outscore it on
    summarize_status_discovery_frames()'s phase_confirmed_frame_count
    tiebreak (identities.py), the first and highest-priority key compared
    - and asserts the foreign device is excluded from the result entirely
    rather than merely deprioritized: it must not become primary, nor
    even appear as a secondary candidate.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    other_blind = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_DEVICE_ID: "112233",
                CONF_DEVICE_ENUM: "05",
                CONF_STATUS_DEVICE_ID: "112233",
                CONF_STATUS_ENUM: "05",
                CONF_OPEN_TIME: 15.0,
                CONF_CLOSE_TIME: 14.0,
                CONF_INVERT_DIRECTION: False,
            }
        ),
        subentry_type=SUBENTRY_TYPE_BLIND,
        title="Kitchen Blind",
        unique_id="112233",
    )
    hass.config_entries.async_add_subentry(entry, other_blind)

    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening

    # The capture's phase is "opening" by this point - set synchronously by
    # async_step_calibration_open_instruction() itself before this very
    # registration - so every frame injected below is captured and
    # phase-labelled exactly as a real motor's would be. Raw wire format
    # from api.py's _handle_message(): ss + enum(2) + device_id(6) +
    # incrementor(4, ignored) + command(2) + padding(2, ignored) +
    # signal(2, ignored). "01" is CMD_UP (const.py).

    # This blind's own motor: one genuine "up" frame.
    connected_api._handle_message("ss10ABCDEF0000010000")

    # A different, already-paired blind on the same hub, moving on its own
    # remote/automation during this capture window - sends more matching
    # frames than the real blind above, which would otherwise win the
    # phase_confirmed_frame_count tiebreak and get stored as *this* blind's
    # status identity.
    connected_api._handle_message("ss051122330000010000")
    connected_api._handle_message("ss051122330001010000")
    connected_api._handle_message("ss051122330002010000")

    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STOPPED)
    result = await task
    assert result["step_id"] == "calibration_close_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STARTED_MOVING_DOWN
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_complete"
    assert result["description_placeholders"] is not None
    # The foreign blind's frames outscored this one's on every per-group
    # count (3 frames to 1), so without finding 9's fix this would read
    # "112233/05" instead.
    assert result["description_placeholders"]["primary_status_identity"] == "ABCDEF/10"
    assert result["description_placeholders"]["primary_frames"] == "01"
    # Excluded from identity *selection* entirely (summarize_status_
    # discovery_frames()'s own exclude_device_ids check), not merely
    # outscored - so the foreign device must not even surface as a
    # secondary candidate.
    assert (
        "112233"
        not in result["description_placeholders"]["secondary_status_identities"]
    )


async def test_calibration_capture_survives_a_reload_mid_flow(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for audit finding 15: every subentry add/remove/edit on
    this hub reloads the whole config entry, replacing entry.runtime_data
    with a brand-new SchellenbergUsbApi - including for a change to some
    other, unrelated blind, not just this one's own. Before this fix,
    _finish_calibration_capture() re-fetched entry.runtime_data on every
    call, so a reload racing in between a capture's start and finish would
    have it read back the new, never-started instance instead (an empty
    result, "not discovered") - silently discarding the real frames the
    old instance had actually buffered, even though nothing about the
    calibration itself went wrong.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening

    # This blind's own motor, received while connected_api is still the
    # live, connected instance the capture actually started on.
    connected_api._handle_message("ss10ABCDEF0000010000")

    # Simulate the hub reloading mid-flow - exactly as __init__.py's
    # async_setup_entry() does on any subentry change, including some
    # other, unrelated blind's: entry.runtime_data now points at a
    # brand-new SchellenbergUsbApi, never connected or started.
    entry.runtime_data = SchellenbergUsbApi(hass, "/dev/fake-reloaded")

    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STOPPED)
    result = await task
    assert result["step_id"] == "calibration_close_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STARTED_MOVING_DOWN
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, SIGNAL_DEVICE_EVENT_CAPTURE, DEVICE_ID, EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_complete"
    assert result["description_placeholders"] is not None
    # Without finding 15's fix, finish_status_frame_capture() would have
    # run on the post-reload instance above instead, which never started a
    # capture at all - this would read "not discovered" instead.
    assert result["description_placeholders"]["primary_status_identity"] == "ABCDEF/10"


async def test_recalibrating_persists_new_times_and_notifies_the_live_entity(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    result = await _advance_through_open_leg(
        hass, result["flow_id"], wait_for_registration
    )

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STARTED_MOVING_DOWN
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STOPPED)
    result = await task
    assert result["step_id"] == "calibration_complete"

    # This listener is a plain function - neither a coroutine nor decorated
    # @callback - so, like every other dispatcher listener this module has
    # run into, Home Assistant is free to invoke it via a worker thread
    # rather than inline; async_configure() below returning is no proof it
    # has actually run yet. A flaky local run caught this directly:
    # len(signals) read as 0 at assert time, while the failure message's own
    # repr of that same list showed the one entry that had, by then, just
    # landed from that other thread - a race, not a data bug. So don't infer
    # delivery from the flow call returning; wait for the listener to
    # actually fire, the same way wait_for_registration() above waits for a
    # registration instead of guessing when it happened.
    signals: list[tuple[Any, ...]] = []
    received = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_calibration_completed(*args: Any) -> None:
        signals.append(args)
        loop.call_soon_threadsafe(received.set)

    unsub = async_dispatcher_connect(
        hass, SIGNAL_CALIBRATION_COMPLETED, _on_calibration_completed
    )
    try:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={}
        )
        try:
            await asyncio.wait_for(received.wait(), timeout=5)
        except TimeoutError:
            pytest.fail(
                "SIGNAL_CALIBRATION_COMPLETED listener was not invoked "
                "within 5s after the recalibration flow finished."
            )
    finally:
        unsub()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    assert updated.data[CONF_OPEN_TIME] >= 0.1
    assert updated.data[CONF_CLOSE_TIME] >= 0.1
    assert len(signals) == 1
    assert signals[0][0] == DEVICE_ID  # entity_id fell back to the device id
    # Choosing initial_state="closed" runs open-leg-first (see
    # _advance_through_open_leg above), so the close leg ran last and the
    # blind ended up closed - args are
    # (entity_id, open_time, close_time, ending_position).
    assert signals[0][3] == 0


async def test_choosing_open_advances_to_the_close_instruction_form(
    hass: HomeAssistant, connected_api: SchellenbergUsbApi
) -> None:
    """Choosing initial_state="open" measures the close leg first instead.

    Mirrors test_confirming_close_advances_to_the_open_instruction_form
    above, which covers the "closed" choice.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "open"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close_instruction"


async def _advance_through_close_leg(
    hass: HomeAssistant,
    flow_id: str,
    wait_for_registration: Callable[[], Awaitable[None]],
) -> SubentryFlowResult:
    """From calibration_close's form, drive through the whole close leg first.

    Mirrors _advance_through_open_leg above, but answers calibration_close
    with initial_state="open", so the close leg (not the open leg) runs
    first - and sends the DOWN/STOPPED pair that leg actually waits for.
    """
    result = await hass.config_entries.subentries.async_configure(
        flow_id, user_input={"initial_state": "open"}
    )
    assert result["step_id"] == "calibration_close_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(flow_id, user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    signal = f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}"
    async_dispatcher_send(hass, signal, EVENT_STARTED_MOVING_DOWN)
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, signal, EVENT_STOPPED)
    return await task


async def test_close_leg_first_then_open_leg_ends_at_open_position(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initial_state="open" runs close then open; open ran last -> ends open.

    The mirror image of test_recalibrating_persists_new_times_and_notifies_
    the_live_entity above (which covers the "closed" open-then-close order
    and asserts ending_position == 0): here the close leg is answered
    first via _advance_through_close_leg, then the open leg runs second
    and reaches its own end stop last, so the blind should end up fully
    open (ending_position == 100) instead.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)

    result = await _advance_through_close_leg(
        hass, result["flow_id"], wait_for_registration
    )
    assert result["step_id"] == "calibration_open_instruction"

    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()  # _wait_for_movement_start is now listening
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()  # _wait_for_stop_event is now listening
    async_dispatcher_send(hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STOPPED)
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_complete"

    signals: list[tuple[Any, ...]] = []
    received = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_calibration_completed(*args: Any) -> None:
        signals.append(args)
        loop.call_soon_threadsafe(received.set)

    unsub = async_dispatcher_connect(
        hass, SIGNAL_CALIBRATION_COMPLETED, _on_calibration_completed
    )
    try:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input={}
        )
        try:
            await asyncio.wait_for(received.wait(), timeout=5)
        except TimeoutError:
            pytest.fail(
                "SIGNAL_CALIBRATION_COMPLETED listener was not invoked "
                "within 5s after the recalibration flow finished."
            )
    finally:
        unsub()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[blind.subentry_id]
    assert updated.data[CONF_OPEN_TIME] >= 0.1
    assert updated.data[CONF_CLOSE_TIME] >= 0.1
    assert len(signals) == 1
    assert signals[0][0] == DEVICE_ID
    assert signals[0][3] == 100


def _build_bare_handler(
    hass: HomeAssistant, *, existing_subentry_data: dict[str, Any]
) -> tuple[CalibrationFlowHandler, MagicMock]:
    """Build a CalibrationFlowHandler with a mocked flow, skipping the wizard.

    The two tests below are only about async_step_calibration_complete()'s
    own recalibration branch - specifically, whether it keeps or overwrites
    an existing status identity - not about the movement-timing wizard that
    normally leads up to it. Driving that wizard for real (as the tests
    above do) would additionally require feeding fake raw serial frames
    through connected_api so finish_status_frame_capture() reports a
    "primary" identity, which exercises the frame-capture module, not the
    persistence decision under test here. Constructing the handler directly
    and setting its _pending_* fields is the same direct-construction
    approach tests/test_cover.py uses for SchellenbergCover, for the same
    reason: it isolates the decision logic from the machinery that normally
    feeds it.

    self.flow only needs to support what async_step_calibration_complete()
    and _notify_calibration_completed() actually call: a real `hass` (for
    the harmless SIGNAL_CALIBRATION_COMPLETED dispatcher send),
    _get_reconfigure_subentry().data returning the existing subentry data
    under test, _get_entry() (opaque, only passed through), and
    async_update_and_abort() (mocked so the test can assert on exactly what
    data_updates it was given).
    """
    flow = MagicMock()
    flow.hass = hass
    flow._get_reconfigure_subentry.return_value = MagicMock(data=existing_subentry_data)
    handler = CalibrationFlowHandler(flow)
    handler._selected_device = {
        "id": DEVICE_ID,
        "entity_id": DEVICE_ID,
        "name": "Living Room Blind",
    }
    handler._open_time = 21.5
    handler._close_time = 19.5
    return handler, flow


async def test_recalibration_keeps_existing_status_identity_when_discovered_differs(
    hass: HomeAssistant,
) -> None:
    """Regression for MEDIUM #2: don't let a recalibration clobber a good ID.

    A recalibration run is normally about remeasuring travel times, not
    re-discovering status identity - but a freshly captured "primary" can be
    wrong (RF noise, a neighboring blind's remote, another blind sharing the
    capture window) in ways the calibration code has no way to tell apart
    from a genuine re-discovery. Before this fix, whatever the new run
    happened to discover was applied unconditionally, silently overwriting
    an already-working status identity with a possibly-wrong guess. Only a
    *different* discovered identity needs protecting against - see the
    sibling test right below for the "confirms the same one" case.
    """
    handler, flow = _build_bare_handler(
        hass,
        existing_subentry_data={
            CONF_STATUS_DEVICE_ID: DEVICE_ID,
            CONF_STATUS_ENUM: DEVICE_ENUM,
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
        },
    )
    # This run "discovers" a different identity than the one already
    # configured above - the case that used to get silently applied.
    handler._pending_status_device_id = "112233"
    handler._pending_status_enum = "99"
    handler._pending_status_identity_source = STATUS_IDENTITY_SOURCE_CALIBRATION
    handler._pending_secondary_status_identities = [
        {"device_id": "445566", "enum": "77"}
    ]

    await handler.async_step_calibration_complete(user_input={})

    flow.async_update_and_abort.assert_called_once()
    data_updates = flow.async_update_and_abort.call_args.kwargs["data_updates"]
    assert data_updates[CONF_OPEN_TIME] == 21.5
    assert data_updates[CONF_CLOSE_TIME] == 19.5
    assert CONF_STATUS_DEVICE_ID not in data_updates
    assert CONF_STATUS_ENUM not in data_updates
    assert CONF_STATUS_IDENTITY_SOURCE not in data_updates
    assert CONF_SECONDARY_STATUS_IDENTITIES not in data_updates


async def test_recalibration_applies_status_identity_when_it_matches_existing(
    hass: HomeAssistant,
) -> None:
    """Confirming the same identity again is not "overwriting" - still applies.

    Refinement of the regression test above: the protection only needs to
    guard against a *different* discovered identity replacing a known-good
    one. When this run's frame capture confirms the exact identity that was
    already configured, applying the update is harmless - it is the same
    identity either way - and still lets CONF_SECONDARY_STATUS_IDENTITIES
    and CONF_STATUS_IDENTITY_SOURCE refresh from this run's own capture,
    instead of being frozen at whatever an earlier run happened to see.
    """
    handler, flow = _build_bare_handler(
        hass,
        existing_subentry_data={
            CONF_STATUS_DEVICE_ID: DEVICE_ID,
            CONF_STATUS_ENUM: DEVICE_ENUM,
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_CALIBRATION,
        },
    )
    # Same identity as already configured, just spelled with different
    # case - normalize_status_identity() must equate the two, the same way
    # every other identity comparison in this integration does, so this
    # also guards against a naive raw-string comparison protecting a
    # "different" identity that is actually the same one.
    handler._pending_status_device_id = DEVICE_ID.lower()
    handler._pending_status_enum = DEVICE_ENUM
    handler._pending_status_identity_source = STATUS_IDENTITY_SOURCE_CALIBRATION
    handler._pending_secondary_status_identities = [
        {"device_id": "445566", "enum": "77"}
    ]

    await handler.async_step_calibration_complete(user_input={})

    flow.async_update_and_abort.assert_called_once()
    data_updates = flow.async_update_and_abort.call_args.kwargs["data_updates"]
    assert data_updates[CONF_STATUS_DEVICE_ID] == DEVICE_ID.lower()
    assert data_updates[CONF_STATUS_ENUM] == DEVICE_ENUM
    assert (
        data_updates[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_CALIBRATION
    )
    assert data_updates[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "445566", "enum": "77"}
    ]


async def test_recalibration_fills_status_identity_when_previously_unknown(
    hass: HomeAssistant,
) -> None:
    """Companion to the regression above: discovery still applies when there

    was nothing to protect. Guards the `if existing_status_known` branch so
    a fix for the "already known" case can't regress into never applying a
    freshly discovered identity at all.
    """
    handler, flow = _build_bare_handler(
        hass,
        existing_subentry_data={
            CONF_STATUS_DEVICE_ID: None,
            CONF_STATUS_ENUM: None,
            CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
        },
    )
    handler._pending_status_device_id = "112233"
    handler._pending_status_enum = "99"
    handler._pending_status_identity_source = STATUS_IDENTITY_SOURCE_CALIBRATION
    handler._pending_secondary_status_identities = [
        {"device_id": "445566", "enum": "77"}
    ]

    await handler.async_step_calibration_complete(user_input={})

    flow.async_update_and_abort.assert_called_once()
    data_updates = flow.async_update_and_abort.call_args.kwargs["data_updates"]
    assert data_updates[CONF_STATUS_DEVICE_ID] == "112233"
    assert data_updates[CONF_STATUS_ENUM] == "99"
    assert (
        data_updates[CONF_STATUS_IDENTITY_SOURCE] == STATUS_IDENTITY_SOURCE_CALIBRATION
    )
    assert data_updates[CONF_SECONDARY_STATUS_IDENTITIES] == [
        {"device_id": "445566", "enum": "77"}
    ]


async def test_open_instruction_movement_start_timeout_shows_error_and_reshows_form(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"] == {"base": "calibration_start_timeout"}


async def test_open_instruction_stop_timeout_shows_error_and_reshows_form(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.05)
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )

    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    # wait_for_registration() resolves the instant _wait_for_movement_start()
    # registers its listener, however long that actually takes - not after a
    # fixed delay - so it's safe to use here too even though
    # CALIBRATION_TIMEOUT is monkeypatched to 0.05s above. What exercises
    # that shrunk timeout is the *second* wait: this test deliberately never
    # sends EVENT_STOPPED below, so _wait_for_stop_event()'s own
    # asyncio.wait_for(..., timeout=CALIBRATION_TIMEOUT) genuinely expires
    # 0.05s after it registers its own listener in turn.
    await wait_for_registration()
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STARTED_MOVING_UP
    )
    # Deliberately never send EVENT_STOPPED - the stop wait must time out.
    result = await task

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"] == {"base": "calibration_timeout"}


async def test_open_instruction_retry_after_timeout_reopens_the_capture(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for audit finding 3.

    Every error exit from a calibration leg finishes the status-frame
    capture (_finish_calibration_capture(), api.py's own
    finish_status_frame_capture()). Retrying re-enters this same step,
    which used to only call set_status_frame_capture_phase() - a silent
    no-op once no capture is open (see api.py) - so the retry captured
    zero status frames: _wait_for_movement_start()/_wait_for_stop_event()
    still resolve normally, since they listen on a separate dispatcher
    signal that does not depend on the capture buffer, so the retry could
    still look like it succeeded while the blind actually ended up at
    "status unknown" with no position tracking and no indication the
    earlier timeout was the cause.
    """
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    # First attempt: nothing ever moves, so the movement-start wait times
    # out, same path as test_open_instruction_movement_start_timeout_
    # shows_error_and_reshows_form above - this is what tears the capture
    # down.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["errors"] == {"base": "calibration_start_timeout"}
    assert connected_api._status_discovery_frames is None

    # Retry: re-enters the same step. Before the fix, the capture stayed
    # None for the rest of this attempt.
    wait_for_registration = _spy_on_dispatcher_connect(monkeypatch)
    task = asyncio.ensure_future(
        hass.config_entries.subentries.async_configure(result["flow_id"], user_input={})
    )
    await wait_for_registration()

    assert connected_api._status_discovery_frames is not None

    # Let the retry actually succeed too, proving the reopened capture is
    # a real, working one and not just a non-None placeholder.
    async_dispatcher_send(
        hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STARTED_MOVING_UP
    )
    await wait_for_registration()
    async_dispatcher_send(hass, f"{SIGNAL_DEVICE_EVENT}_{DEVICE_ID}", EVENT_STOPPED)
    result = await task

    assert result["step_id"] == "calibration_close_instruction"


async def test_calibration_close_busy_error_does_not_steal_capture_buffer(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
) -> None:
    """Regression guard for audit finding 6.

    _start_calibration_capture() used to catch start_status_frame_capture()'s
    RuntimeError and force its way in anyway - finishing and discarding
    whatever capture already held the one hub-wide buffer, then starting a
    fresh one in its place. Nothing in this integration serializes two
    blinds' calibration/discovery flows against each other, so this is a
    realistic scenario, not a contrived one - and the steal silently
    corrupted whichever operation lost the buffer, with no error shown to
    either side. This simulates a second operation (another blind's own
    calibration, or a status-discovery flow) already holding the buffer and
    checks that starting this blind's own capture now backs off with a
    clean error instead of stealing it.
    """
    entry, blind = _build_hub_with_blind(hass, connected_api)

    # Simulate a second operation - another blind's own calibration, or a
    # status-discovery flow - already holding the one hub-wide buffer.
    connected_api.start_status_frame_capture(phase="discovery")

    result = await _start_calibrate_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_close"
    assert result["errors"] == {"base": "calibration_capture_busy"}

    # The other operation's capture must still be exactly as it was - not
    # finished, not replaced.
    assert connected_api._status_discovery_frames is not None
    assert connected_api._status_discovery_phase == "discovery"


async def test_open_retry_busy_error_when_another_capture_took_the_buffer(
    hass: HomeAssistant,
    connected_api: SchellenbergUsbApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for audit finding 6, retry path.

    _set_calibration_capture_phase()'s own retry-reopen (added for finding
    3, see test_open_instruction_retry_after_timeout_reopens_the_capture
    above) calls _start_calibration_capture() again, which can itself find
    a *different* operation now holding the buffer - the gap between this
    leg's own timeout tearing its capture down and the user retrying is
    plenty of time for another blind's flow to grab it. The retry must
    surface the same clean busy error as the fresh-start path above, not
    silently wait for movement that can never be attributed to a capture
    that was never actually reopened.
    """
    monkeypatch.setattr(calibration_module, "CALIBRATION_TIMEOUT", 0.01)
    entry, blind = _build_hub_with_blind(hass, connected_api)
    result = await _start_calibrate_flow(hass, entry, blind)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={"initial_state": "closed"}
    )
    assert result["step_id"] == "calibration_open_instruction"

    # First attempt: nothing ever moves, so the movement-start wait times
    # out - identical setup to the finding-3 retry test above - tearing
    # this blind's own capture down.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["errors"] == {"base": "calibration_start_timeout"}
    assert connected_api._status_discovery_frames is None

    # Before the retry, a different operation claims the now-free buffer.
    connected_api.start_status_frame_capture(phase="discovery")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration_open_instruction"
    assert result["errors"] == {"base": "calibration_capture_busy"}
    # The other operation's capture is still untouched.
    assert connected_api._status_discovery_phase == "discovery"


async def test_calibration_close_step_aborts_without_a_selected_device() -> None:
    """Defensive guard: every step must abort cleanly if never wired up."""
    flow = MagicMock()
    handler = CalibrationFlowHandler(flow)

    await handler.async_step_calibration_close(None)

    flow.async_abort.assert_called_once_with(reason="device_not_found")


async def test_calibration_complete_step_aborts_without_recorded_times() -> None:
    flow = MagicMock()
    handler = CalibrationFlowHandler(flow)
    handler.set_selected_device({"id": DEVICE_ID, "entity_id": DEVICE_ID, "name": "X"})

    await handler.async_step_calibration_complete(None)

    flow.async_abort.assert_called_once_with(reason="device_not_found")
