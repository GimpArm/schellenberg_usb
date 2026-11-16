"""Live calibration-drive flow shared by pairing and reconfigure.

CalibrationFlowHandler below drives one blind/shutter through an actual
open-and-close calibration run: the person picks which direction to
calibrate first, each leg then waits for the real movement-start/stop
telegrams that leg produces (not a fixed delay), and a hub-wide status-
frame capture runs alongside to find which device ID/enum combination
that blind is actually sending its position telegrams as. Both the
fresh-pairing subentry flow and the reconfigure subentry flow
(config_flow.py) delegate to the same handler instance for this - a
fresh pairing turns the result into a brand-new subentry, a
recalibration updates the existing one instead; see
enable_subentry_creation()/disable_subentry_creation() for how each
flow selects its own route, and async_step_calibration_complete() for
where that split actually happens.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect as async_dispatcher_connect,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send,
)

from .api import SchellenbergUsbApi
from .blind_id import generate_blind_id, normalize_blind_id
from .const import (
    CALIBRATION_TIMEOUT,
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_INITIAL_POSITION,
    CONF_INVERT_DIRECTION,
    CONF_LAST_CALIBRATION,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    SIGNAL_CALIBRATION_COMPLETED,
    SIGNAL_DEVICE_EVENT,
    SIGNAL_DEVICE_EVENT_CAPTURE,
    STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
)
from .identities import normalize_status_identity
from .runtime_translation_text import (
    runtime_translation_text as _runtime_translation_text,
)
from .runtime_translation_text import translate_phrase as _translate_phrase

if TYPE_CHECKING:
    from .config_flow import SchellenbergPairingSubentryFlow

_LOGGER = logging.getLogger(__name__)

type FlowResult = SubentryFlowResult

# How often _claim_or_advance_capture() below re-polls a capture claim that
# first came back busy. Short enough that a genuinely concurrent claim
# (the only way to still be busy once start_status_frame_capture()'s own
# staleness reclaim applies - api.py) clears within a couple of polls of
# whichever flow actually holds it finishing normally, long enough not to
# spin needlessly while the person simply waits.
CAPTURE_CLAIM_RETRY_INTERVAL_SECONDS = 2.0


class CalibrationFlowHandler:
    """Drive one blind through a live calibration run, on behalf of `flow`.

    Not a ConfigSubentryFlow itself - `flow` is the actual
    SchellenbergPairingSubentryFlow or reconfigure subentry flow this
    handler acts for, and every HA-facing call (async_show_form,
    async_abort, the config entry/subentry lookups) goes through that
    object rather than being implemented here. Holding the calibration-
    specific state (the open/close timings as they are measured, the
    capture-buffer ownership, the pending data for a not-yet-created
    subentry) in a separate object instead of on the flow class itself
    keeps that state - and the retry/timeout logic built on it - shared
    identically between the two very different flows that both need a
    live calibration drive.
    """

    def __init__(self, flow: ConfigSubentryFlow) -> None:
        """Set up fresh per-run state for a calibration attempt on `flow`.

        One instance is lazily created per flow run, the first time
        config_flow.py's _get_calibration_handler() is called on it, and
        then reused for the rest of that same run - see that method's
        own docstring. Nothing here needs to be reset between attempts
        because of that: only enable_subentry_creation()/
        disable_subentry_creation() below mutate this state afterwards,
        to pick which of async_step_calibration_complete()'s two
        finishing routes a given run takes.
        """
        self.flow = flow
        self._selected_device: dict[str, Any] | None = None
        self._calibration_start_time: float | None = None
        # Shared by _wait_for_movement_start() for whichever event it is
        # currently waiting for (a movement-start event, or - via
        # _wait_for_stop_event()'s delegation to it with EVENT_STOPPED - the
        # stop event); the two waits never overlap, so one attribute is
        # enough.
        self._wait_event: asyncio.Event | None = None
        self._event_listener_unsubs: list[Any] = []
        self._open_time: float | None = None
        self._close_time: float | None = None
        # 100 if the open leg drove last, 0 if the close leg drove last -
        # whichever leg runs second always finishes at that leg's own end
        # stop, regardless of which leg ran first. Set by whichever of
        # async_step_calibration_open_instruction /
        # async_step_calibration_close_instruction finds the other leg
        # already done; read once calibration completes.
        self._calibration_ending_position: int | None = None
        self._create_subentry_after_calibration = False
        self._pending_blind_id: str | None = None
        self._pending_device_id: str | None = None
        self._pending_device_enum: str | None = None
        self._pending_device_name: str | None = None
        self._pending_status_device_id: str | None = None
        self._pending_status_enum: str | None = None
        self._pending_secondary_status_identities: list[dict[str, str]] = []
        self._pending_status_identity_source: str | None = None
        self._calibration_discovery_result: dict[str, Any] | None = None
        self._pending_invert_direction = False
        # Tracks whether this flow currently believes it owns an open
        # status-frame capture - see _set_calibration_capture_phase()'s own
        # comment for why this is needed on top of api.py's own capture
        # state.
        self._capture_active = False
        # The SchellenbergUsbApi instance the currently-active capture was
        # actually started on, if any - see _start_calibration_capture()'s
        # own comment for why _set_calibration_capture_
        # phase()/_finish_calibration_capture() read this instead of each
        # re-fetching entry.runtime_data while a capture is active.
        self._capture_api: SchellenbergUsbApi | None = None

    def _runtime_api(self) -> SchellenbergUsbApi | None:
        """Return this flow's config entry's current API instance, if any.

        A fresh lookup every time, deliberately not cached on self the
        way _capture_api below is while a capture is active - this one
        is meant to always reflect whichever SchellenbergUsbApi instance
        entry.runtime_data currently points at, including right after a
        reload has replaced it. _start_calibration_capture()'s own
        comment on why it caches the result instead, right after calling
        this, explains the one case where that is not what is wanted.
        """
        entry = self.flow._get_entry()
        api = entry.runtime_data
        return api if isinstance(api, SchellenbergUsbApi) else None

    def _start_calibration_capture(self, *, phase: str) -> bool:
        """Claim the hub-wide status-frame capture buffer for this blind.

        Returns False, without starting anything, when a different
        blind's calibration or status-discovery flow already owns the
        buffer. This used to catch that RuntimeError and force its way
        in anyway - finishing and discarding whatever capture was
        already running, then starting a new one in its place - which
        let two concurrent flows for two *different* blinds silently
        corrupt each other's result, up to and including writing the
        wrong status identity into a blind's permanent config data.
        async_discover_status_identities() (api.py), called from
        config_flow.py's async_step_discover_status, already gets this
        right for the exact same buffer: it lets the RuntimeError
        propagate so that caller can show a "busy" error instead of
        stealing. This mirrors that behavior here.
        """
        api = self._runtime_api()
        if api is None:
            return False
        try:
            api.start_status_frame_capture(phase=phase)
        except RuntimeError:
            return False
        self._capture_active = True
        # Cache the instance this capture actually
        # started on, rather than letting _set_calibration_capture_phase()/
        # _finish_calibration_capture() each re-fetch entry.runtime_data
        # (via self._runtime_api()) on every call. Every subentry add/
        # remove/edit on this hub - including some other, unrelated
        # blind's, not just this one's own - reloads the whole config
        # entry, which replaces entry.runtime_data with a brand-new
        # SchellenbergUsbApi. Re-fetching it mid-capture would hand
        # _finish_calibration_capture() that new, never-started instance
        # instead - nothing to read, a silent, empty result - while the
        # real frames this capture actually buffered stay stranded on the
        # old, now-abandoned one. Caching it here means the capture that
        # was actually opened is also the one that gets read back,
        # finishing correctly on its own real data even if a reload raced
        # it in between - the same class of hazard already solved for
        # _fresh_calibration_positions, a different piece of per-instance
        # state (see cover.py's own comment on it for that one).
        self._capture_api = api
        return True

    def _set_calibration_capture_phase(self, phase: str) -> bool:
        """Advance the active capture to `phase`, returning whether it is
        (still) this blind's own capture afterwards.
        """
        if not self._capture_active:
            # A previous timeout or error in this same leg already tore
            # down the capture via _finish_calibration_capture() below,
            # and this call is a retry re-entering the same step. api.py's
            # set_status_frame_capture_phase() silently no-ops once no
            # capture is open, so without this check a retry would wait
            # for the movement-start/stop dispatcher signals as normal
            # (those do not depend on the capture buffer) and can still
            # look like it succeeded, while actually capturing zero status
            # frames - leaving the blind at "status unknown" with no
            # position tracking and no indication the earlier timeout was
            # the cause. Start a fresh capture instead of labelling one
            # that no longer exists. That fresh start can itself find
            # another blind's capture already running, so callers must
            # still check the return value rather than assuming a retry
            # always succeeds.
            return self._start_calibration_capture(phase=phase)
        # Read the instance this same capture actually
        # started on (see _start_calibration_capture()'s own comment),
        # never a fresh self._runtime_api() lookup - entry.runtime_data may
        # already point at a different, newer instance by now.
        api = self._capture_api
        if api is None:
            return False
        api.set_status_frame_capture_phase(phase)
        return True

    def _finish_calibration_capture(self, end_reason: str) -> None:
        """End the active capture and store its result for this run.

        Safe to call even when no capture is active (self._capture_api
        is None) - every caller already knows, from its own branch, that
        a capture was started, but a shared helper covering both the
        "ever claimed one" and "did not" cases rather than requiring
        every call site to check first is simpler and cannot silently
        skip tearing one down.
        """
        # Same reasoning as _set_calibration_capture_
        # phase() above - read back from the instance this capture was
        # actually started on, not whatever entry.runtime_data currently
        # points at.
        api = self._capture_api
        if api is None:
            return
        # Same foreign-id set _wait_for_movement_start() already uses to
        # keep a different, already-paired blind's own movement from
        # falsely satisfying *this* blind's movement-detected wait (see
        # that method's own comment) - without also passing it here, that
        # other blind's real frames, captured into this same hub-wide
        # buffer regardless of either protection, could still outscore
        # this blind's own and get chosen as its identity instead. The
        # wait was already scoped this way; the actual
        # identity decision below was not.
        foreign_device_ids: frozenset[str] = (self._selected_device or {}).get(
            "foreign_capture_device_ids"
        ) or frozenset()
        self._calibration_discovery_result = api.finish_status_frame_capture(
            end_reason=end_reason, exclude_device_ids=foreign_device_ids
        )
        self._capture_active = False
        self._capture_api = None

    async def _claim_or_advance_capture(self, claim: Callable[[], bool]) -> bool:
        """Run `claim` (either _start_calibration_capture or
        _set_calibration_capture_phase, bound to its phase), waiting out a
        busy capture instead of failing on the first try.

        start_status_frame_capture()'s own staleness reclaim (api.py)
        already means a capture abandoned by a flow that never finished it
        stops blocking anyone on its own, within
        STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS - so the only way
        `claim` can still come back False here is a second flow's own
        genuinely still-running capture for a *different* blind, which
        this waits out rather than surfacing as a dead end the person has
        to notice and manually retry (2026-11-08, replacing the
        "calibration_capture_busy" error this step used to show - see
        CALIBRATION_TIMEOUT's neighbour in const.py for the fuller story).
        Bounded by that same staleness window plus one extra poll's worth
        of margin: by the time that elapses, either `claim` has already
        succeeded on an earlier poll, or the other flow's own capture has
        itself gone stale and this call's own next poll reclaims it.
        """
        deadline = (
            time.monotonic()
            + STATUS_FRAME_CAPTURE_STALE_TIMEOUT_SECONDS
            + CAPTURE_CLAIM_RETRY_INTERVAL_SECONDS
        )
        while True:
            if claim():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(CAPTURE_CLAIM_RETRY_INTERVAL_SECONDS)

    def _apply_calibration_status_candidates(self) -> None:
        """Copy the just-finished capture's result into the pending fields.

        Called once, right after _finish_calibration_capture() populates
        self._calibration_discovery_result, from both
        async_step_calibration_open_instruction() and
        async_step_calibration_close_instruction()'s own "both legs done"
        branch. Always overwrites self._pending_status_device_id/_enum/
        _identity_source and self._pending_secondary_status_identities
        wholesale with what this capture actually found (or, if it found
        nothing, with the explicit "unknown" state) - there is nothing
        from an earlier round for a fresh calibration run to preserve,
        unlike config_flow.py's async_step_confirm_status_discovery(),
        which has to guard against doing exactly that to an already-
        confirmed identity.
        """
        result = self._calibration_discovery_result
        if not result:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
            return
        primary = result.get("primary")
        if primary is None:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        else:
            self._pending_status_device_id = str(primary["device_id"])
            self._pending_status_enum = str(primary["enum"])
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_CALIBRATION
        self._pending_secondary_status_identities = [
            {"device_id": str(group["device_id"]), "enum": str(group["enum"])}
            for group in result.get("secondary", [])
        ]

    def _calibration_record(self) -> dict[str, Any] | None:
        """Build the CONF_LAST_CALIBRATION value for this run, or None.

        None exactly when no capture ever actually ran this attempt
        (self._calibration_discovery_result is still unset) - both
        async_step_calibration_complete() call sites already guard the
        assignment with `if calibration_record := self._calibration_record()`
        for that reason, rather than ever writing an empty record. When a
        capture did run, this is that capture's own result dict (primary/
        secondary candidates, end reason, observed frames - see
        api.py's finish_status_frame_capture()) with the measured open/
        close times merged in, so CONF_LAST_CALIBRATION ends up self-
        contained for diagnostics without needing those two separately-
        stored fields re-joined by whatever reads it back later.
        """
        if self._calibration_discovery_result is None:
            return None
        return {
            **self._calibration_discovery_result,
            "open_time": (
                round(self._open_time, 2) if self._open_time is not None else None
            ),
            "close_time": (
                round(self._close_time, 2) if self._close_time is not None else None
            ),
        }

    def _calibration_summary_placeholders(self) -> dict[str, str]:
        """Build the description_placeholders for async_step_calibration_complete().

        Renders whatever this run's capture found (or didn't) into the
        translated strings that step's "calibration_complete" form
        actually shows - the primary identity and the commands it was
        seen on, the secondary identities, whether position tracking is
        available at all, the capture's end reason, and how many frames
        were observed in total. Safe to call even when no capture ran
        (self._calibration_discovery_result is still None): every field
        below falls back to its own "not discovered"/"none"/empty
        rendering in that case instead of raising.
        """
        hass = self.flow.hass
        none_word = _runtime_translation_text(hass, "none_word")
        result = self._calibration_discovery_result or {}
        primary = result.get("primary")
        secondary = result.get("secondary", [])
        primary_text = (
            f"{primary['device_id']}/{primary['enum']}"
            if primary is not None
            else _runtime_translation_text(hass, "not_discovered")
        )
        primary_frames = (
            ", ".join(primary.get("commands", [])) if primary is not None else none_word
        )
        secondary_text = (
            ", ".join(
                f"{group['device_id']}/{group['enum']} "
                f"({','.join(group.get('commands', []))})"
                for group in secondary
            )
            or none_word
        )
        return {
            "primary_status_identity": primary_text,
            "primary_frames": primary_frames,
            "secondary_status_identities": secondary_text,
            "position_tracking": (
                _runtime_translation_text(hass, "position_tracking_available")
                if primary is not None
                else _runtime_translation_text(hass, "position_tracking_unavailable")
            ),
            "calibration_end_reason": _translate_phrase(
                hass, str(result.get("end_reason", "completed"))
            ),
            "observed_frame_count": str(len(result.get("frames", []))),
        }

    def set_selected_device(self, device: dict[str, Any]) -> None:
        """Record which device this calibration run is for.

        Called once by the owning flow before any async_step_calibration_*
        method on this handler is reached, for both the fresh-pairing and
        the reconfigure route - every one of those methods aborts with
        "device_not_found" instead of proceeding if this was never
        called, whether via its own guard or - for async_step_
        calibration_retry(), which has none of its own - by
        unconditionally delegating to async_step_calibration_open_
        instruction()/_close_instruction(), which do. `device` carries
        the same "foreign_capture_device_ids"
        key both call sites already populate it with; see
        _wait_for_movement_start()'s own docstring for why that key
        matters once a live capture starts.
        """
        self._selected_device = device

    def recorded_leg_times(self) -> tuple[float | None, float | None]:
        """Return (open_time, close_time) as actually measured so far.

        Used by async_step_manual_times() (config_flow.py) to pre-fill
        whichever leg already has a real, measured value when the person
        abandons a live calibration for manual entry after the *other*
        leg's own timeout, instead of asking them to re-enter or guess a
        number Home Assistant already knows precisely - see that
        method's own docstring. At most one of the two is ever non-None
        here: both becoming set means the live run already completed and
        moved on to async_step_calibration_complete() instead of ever
        offering manual entry again.
        """
        return self._open_time, self._close_time

    async def async_step_calibration_initial_state(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask which direction to calibrate first, then claim a capture.

        The entry point into a live calibration run, reached from both
        the fresh-pairing route (after a successful motor test) and the
        reconfigure route's own "Calibrate" menu option. The person's
        answer only decides which leg's instruction step comes first
        (async_step_calibration_open_instruction() for "closed",
        async_step_calibration_close_instruction() for "open" - the
        *other* leg of whichever state the blind is already in); the
        actual open/close times get measured from real movement
        telegrams regardless of which leg runs first. Also where the
        hub-wide status-frame capture buffer first gets claimed for this
        run, via _claim_or_advance_capture() - a capture that is still
        busy with some *other* blind's own flow at this point surfaces
        as the generic "unknown" error below rather than letting this
        step silently proceed uncaptured.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            initial_state = user_input.get("initial_state")
            if initial_state == "open":
                if await self._claim_or_advance_capture(
                    lambda: self._start_calibration_capture(phase="closing")
                ):
                    return await self.async_step_calibration_close_instruction()
                # Only reachable if a *different* flow's own capture somehow
                # stayed genuinely active (never stale) for the entire wait
                # in _claim_or_advance_capture() above - see that method's
                # own docstring. Generic "unknown" rather than a dedicated
                # string: honestly unexpected at this point, not worth a
                # fourth-language string for a case this close to
                # unreachable.
                errors["base"] = "unknown"
            elif initial_state == "closed":
                if await self._claim_or_advance_capture(
                    lambda: self._start_calibration_capture(phase="opening")
                ):
                    return await self.async_step_calibration_open_instruction()
                errors["base"] = "unknown"
            else:
                # Neither real option was chosen - see the schema comment
                # below for why this is reachable even though the field is
                # required.
                errors["base"] = "initial_state_required"

        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        return self.flow.async_show_form(
            step_id="calibration_initial_state",
            data_schema=vol.Schema(
                {
                    # A plain BooleanSelector used to default to "closed"
                    # (unchecked) whenever the person left it alone, which
                    # looked identical to an actual, considered answer - the
                    # checkbox's own label only ever stated what *checking*
                    # it meant, with nothing on the form saying what leaving
                    # it unchecked meant instead. Replacing it with a
                    # two-option required select was meant to force one of
                    # the two real states to be picked explicitly every
                    # time, on the assumption that Home Assistant's frontend
                    # could not submit a required select with neither option
                    # chosen - but in its "list" (radio) rendering it visibly
                    # highlights the first option before anything is picked,
                    # which looks exactly like a considered answer again and
                    # reintroduces the original problem. A third, clearly
                    # non-answer placeholder option now occupies that
                    # highlighted first slot instead; submitting it (or
                    # anything other than "open"/"closed", belt and braces)
                    # is rejected above with an explicit on-screen error
                    # instead of silently being treated as "closed".
                    vol.Required("initial_state"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["unselected", "open", "closed"],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="initial_state",
                        )
                    ),
                }
            ),
            description_placeholders={
                "device_name": self._selected_device["name"],
                # Empty for a fresh pairing - there is nothing to warn
                # about yet. Non-empty only for recalibration, where an
                # already-confirmed status identity exists and the person
                # asked for a reminder that it depends on them actually
                # using that same remote again during this run (see
                # CHANGELOG.md for the real-world report this came from).
                "recalibration_notice": (
                    ""
                    if self._create_subentry_after_calibration
                    else _runtime_translation_text(
                        self.flow.hass, "recalibration_notice"
                    )
                ),
            },
            errors=errors,
            last_step=False,
        )

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Measure the open leg: wait for real movement, then for the stop.

        Shown once with no `user_input`, purely to let the person read
        the instruction and press Start; the actual wait only starts on
        the form's own submit. Advances the capture to the "opening"
        phase, then waits in turn for a movement-started telegram and a
        stop telegram (_wait_for_movement_start()/_wait_for_stop_event())
        to measure self._open_time from real device behaviour rather
        than a fixed delay. Either wait timing out takes the person to
        async_step_calibration_retry()'s menu during a fresh pairing, or
        back to this same form with an on-screen error during a
        recalibration - see the first timeout branch below for why those
        two routes differ. Once both legs have a measured time, hands
        off to async_step_calibration_complete(); otherwise moves on to
        this leg's counterpart, async_step_calibration_close_instruction().
        """
        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        errors = {}

        if user_input is None:
            return self.flow.async_show_form(
                step_id="calibration_open_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                last_step=False,
            )

        if not await self._claim_or_advance_capture(
            lambda: self._set_calibration_capture_phase("opening")
        ):
            # See async_step_calibration_initial_state()'s own comment on
            # its identical fallback - near-unreachable once
            # _claim_or_advance_capture() has already waited out any
            # genuinely concurrent capture.
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_open_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )
        try:
            start_ok = await self._wait_for_movement_start(EVENT_STARTED_MOVING_UP)
            if not start_ok:
                self._finish_calibration_capture("opening_start_timeout")
                if self._create_subentry_after_calibration:
                    # Issue #77 (2026-11-07): a fresh pairing gets an
                    # explicit choice instead of only an on-screen error and
                    # an implicit "submit this same form again" - see
                    # async_step_calibration_retry()'s own docstring below
                    # and CALIBRATION_TIMEOUT's own comment in const.py.
                    # Recalibrating an already-paired blind
                    # (_create_subentry_after_calibration is False there -
                    # see async_step_calibrate() in config_flow.py) falls
                    # through to the unchanged error-and-resubmit behavior
                    # below instead; every other timeout branch in this
                    # method mirrors this same check without repeating this
                    # comment.
                    return self.flow.async_show_menu(
                        step_id="calibration_timeout_choice",
                        menu_options=["calibration_retry", "manual_times"],
                    )
                errors["base"] = "calibration_start_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_open_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            self._calibration_start_time = time.monotonic()

            stop_ok = await self._wait_for_stop_event()
            if not stop_ok:
                self._finish_calibration_capture("opening_stop_timeout")
                if self._create_subentry_after_calibration:
                    return self.flow.async_show_menu(
                        step_id="calibration_timeout_choice",
                        menu_options=["calibration_retry", "manual_times"],
                    )
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_open_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            self._open_time = max(time.monotonic() - self._calibration_start_time, 0.1)
            _LOGGER.debug("Calibration open_time: %s seconds", self._open_time)

            if self._close_time is None:
                self._set_calibration_capture_phase("idle_between_legs")
                return await self.async_step_calibration_close_instruction()

            self._calibration_ending_position = 100
            self._finish_calibration_capture("completed")
            self._apply_calibration_status_candidates()
            return await self.async_step_calibration_complete()

        except asyncio.CancelledError:
            self._finish_calibration_capture("opening_cancelled")
            raise
        except Exception:
            self._finish_calibration_capture("opening_error")
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_open_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )

    async def async_step_calibration_close_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Measure the close leg: the mirror image of the open-leg step.

        Same shape as async_step_calibration_open_instruction() above -
        shown once to let the person press Start, then waits for a
        movement-started telegram and a stop telegram to measure
        self._close_time - just for EVENT_STARTED_MOVING_DOWN and the
        "closing" capture phase instead. See that method's own docstring
        for the shared timeout/retry behaviour and what happens once
        both legs are measured.
        """
        if self._selected_device is None:
            return self.flow.async_abort(reason="device_not_found")

        errors = {}

        if user_input is None:
            return self.flow.async_show_form(
                step_id="calibration_close_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                last_step=False,
            )

        if not await self._claim_or_advance_capture(
            lambda: self._set_calibration_capture_phase("closing")
        ):
            # See async_step_calibration_initial_state()'s own comment on
            # its identical fallback - near-unreachable once
            # _claim_or_advance_capture() has already waited out any
            # genuinely concurrent capture.
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_close_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )
        try:
            start_ok = await self._wait_for_movement_start(EVENT_STARTED_MOVING_DOWN)
            if not start_ok:
                self._finish_calibration_capture("closing_start_timeout")
                if self._create_subentry_after_calibration:
                    return self.flow.async_show_menu(
                        step_id="calibration_timeout_choice",
                        menu_options=["calibration_retry", "manual_times"],
                    )
                errors["base"] = "calibration_start_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_close_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            self._calibration_start_time = time.monotonic()

            stop_ok = await self._wait_for_stop_event()
            if not stop_ok:
                self._finish_calibration_capture("closing_stop_timeout")
                if self._create_subentry_after_calibration:
                    return self.flow.async_show_menu(
                        step_id="calibration_timeout_choice",
                        menu_options=["calibration_retry", "manual_times"],
                    )
                errors["base"] = "calibration_timeout"
                return self.flow.async_show_form(
                    step_id="calibration_close_instruction",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": self._selected_device["name"],
                    },
                    errors=errors,
                    last_step=False,
                )

            self._close_time = max(time.monotonic() - self._calibration_start_time, 0.1)
            _LOGGER.debug("Calibration close_time: %s seconds", self._close_time)

            if self._open_time is None:
                self._set_calibration_capture_phase("idle_between_legs")
                return await self.async_step_calibration_open_instruction()

            self._calibration_ending_position = 0
            self._finish_calibration_capture("completed")
            self._apply_calibration_status_candidates()
            return await self.async_step_calibration_complete()

        except asyncio.CancelledError:
            self._finish_calibration_capture("closing_cancelled")
            raise
        except Exception:
            self._finish_calibration_capture("closing_error")
            errors["base"] = "unknown"
            return self.flow.async_show_form(
                step_id="calibration_close_instruction",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "device_name": self._selected_device["name"],
                },
                errors=errors,
                last_step=False,
            )

    async def async_step_calibration_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the measured results, then finish via whichever route applies.

        Reached once both legs have a measured time, from either
        instruction step. Shown first with no `user_input` purely as a
        summary screen (open/close times, discovered identities); the
        actual finishing only happens on that form's own submit, and
        always starts by dispatching SIGNAL_CALIBRATION_COMPLETED so
        cover.py's live entity picks up the new travel times and ending
        position immediately, regardless of which route runs next.
        Which route that is depends on self._create_subentry_after_
        calibration (see enable_subentry_creation()/
        disable_subentry_creation()): a fresh pairing assembles a brand-
        new subentry's data and - instead of finishing immediately -
        detours through async_step_status_discovery_choice() (config_
        flow.py) for a chance at a real remote-control identity first;
        a recalibration instead updates the existing subentry in
        place, always adopting this run's own freshly captured status
        identity - primary and secondary alike - over whatever was
        persisted before, including clearing a previously stored
        secondary identity (for example a remote control) that this
        round's own capture did not reconfirm. A live calibration
        run's self-echo capture is direct, first-hand evidence of
        what is actually driving this blind right now, so it is
        trusted outright rather than merged with stale data (see the
        _LOGGER.info() call below, which documents when this changes
        the previously confirmed identity, without guarding against
        it).
        """
        if (
            self._selected_device is None
            or self._open_time is None
            or self._close_time is None
        ):
            return self.flow.async_abort(reason="device_not_found")

        if user_input is not None:
            # Whichever leg ran last drove all the way to that leg's own end
            # stop, so that is the blind's true physical position now -
            # defaulting to 0 should never actually trigger (both instruction
            # steps always set this before reaching this step), but matches
            # this integration's existing closed-by-default assumption if it
            # somehow did.
            ending_position = (
                self._calibration_ending_position
                if self._calibration_ending_position is not None
                else 0
            )
            await self._notify_calibration_completed(
                self._open_time, self._close_time, ending_position
            )

            if (
                self._create_subentry_after_calibration
                and self._pending_device_id
                and self._pending_device_enum
                and self._pending_device_name
            ):
                blind_id = self._pending_blind_id or generate_blind_id()
                if api := self._runtime_api():
                    api.set_fresh_calibration_position(blind_id, ending_position)
                data: dict[str, Any] = {
                    CONF_BLIND_ID: blind_id,
                    CONF_DEVICE_ID: self._pending_device_id,
                    CONF_DEVICE_ENUM: self._pending_device_enum,
                    CONF_COMMAND_DEVICE_ID: self._pending_device_id,
                    CONF_COMMAND_ENUM: self._pending_device_enum,
                    CONF_SECONDARY_STATUS_IDENTITIES: list(
                        self._pending_secondary_status_identities
                    ),
                    CONF_OPEN_TIME: round(self._open_time, 2),
                    CONF_CLOSE_TIME: round(self._close_time, 2),
                    CONF_INITIAL_POSITION: ending_position,
                    CONF_INVERT_DIRECTION: self._pending_invert_direction,
                }
                if (
                    self._pending_status_device_id is not None
                    and self._pending_status_enum is not None
                ):
                    data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
                    data[CONF_STATUS_ENUM] = self._pending_status_enum
                if self._pending_status_identity_source is not None:
                    data[CONF_STATUS_IDENTITY_SOURCE] = (
                        self._pending_status_identity_source
                    )
                if calibration_record := self._calibration_record():
                    data[CONF_LAST_CALIBRATION] = calibration_record
                pairing_flow = cast("SchellenbergPairingSubentryFlow", self.flow)
                # Offer a real status-identity discovery attempt before
                # finishing, the same as async_step_manual_times()'s own
                # pairing route already does (config_flow.py) - this
                # live-calibration route used to finish immediately here
                # with no discovery attempted at all, so a freshly-paired
                # blind's secondary (and, unless a person separately ran
                # "Discover status" from Developer Tools afterward,
                # primary) status identity stayed permanently unset.
                # `_pending_finish_data` carries this already-assembled
                # `data` dict through that detour; see its own docstring
                # in config_flow.py's __init__. Routed through async_step_
                # status_discovery_choice() rather than straight into
                # async_step_discover_status() itself (2026-11-08, project
                # owner's own request) so a person without their original
                # remote to hand - or who simply doesn't want to run this
                # right now - can skip it instead of having to abandon
                # this whole pairing attempt to get past it.
                pairing_flow._pending_finish_data = data
                return await pairing_flow.async_step_status_discovery_choice()

            existing_data = self.flow._get_reconfigure_subentry().data
            existing_status_identity = normalize_status_identity(
                existing_data.get(CONF_STATUS_DEVICE_ID),
                existing_data.get(CONF_STATUS_ENUM),
            )
            existing_status_confirmed = (
                existing_data.get(CONF_STATUS_IDENTITY_SOURCE)
                != STATUS_IDENTITY_SOURCE_UNKNOWN
            )
            data_updates: dict[str, Any] = {
                CONF_OPEN_TIME: round(self._open_time, 2),
                CONF_CLOSE_TIME: round(self._close_time, 2),
                # Recalibrating an already-paired blind used to correct
                # only its *live* position (via the SIGNAL_CALIBRATION_
                # COMPLETED/_handle_calibration_completed hand-off above -
                # unchanged by this addition) without ever refreshing this
                # persisted fallback, unlike a fresh pairing's own "create
                # subentry" branch a few lines above, which always has.
                # That meant a blind recalibrated today still had nothing
                # but Home Assistant's own, separately-volatile restore
                # state to fall back on at the next startup where that
                # restore state happens to come up empty - the exact gap
                # a real-world report (2026-11-01) traced a mix of
                # "Unbekannt"/correctly-restored blinds on one hub back
                # to: the "Unbekannt" ones had all been calibrated before
                # CONF_INITIAL_POSITION existed at all, and recalibrating
                # them under current code still would not have persisted
                # anything for next time. Now it does, the same ending_
                # position already computed above for the live hand-off.
                CONF_INITIAL_POSITION: ending_position,
            }
            if calibration_record := self._calibration_record():
                data_updates[CONF_LAST_CALIBRATION] = calibration_record
            if (
                self._pending_status_device_id is not None
                and self._pending_status_enum is not None
                and self._pending_status_identity_source
                == STATUS_IDENTITY_SOURCE_CALIBRATION
            ):
                discovered_status_identity = normalize_status_identity(
                    self._pending_status_device_id, self._pending_status_enum
                )
                if (
                    existing_status_identity is not None
                    and existing_status_confirmed
                    and discovered_status_identity != existing_status_identity
                ):
                    _LOGGER.info(
                        "Recalibration for %s discovered status identity %s/%s, "
                        "which differs from the already-configured %s/%s; "
                        "adopting the newly discovered identity, since this "
                        "run's own live capture is direct evidence of what is "
                        "actually driving this blind right now. Any "
                        "previously stored secondary status identity not "
                        "reconfirmed by this run (for example a remote "
                        "control that was not pressed this time) is cleared "
                        "along with it",
                        self._selected_device["name"]
                        if self._selected_device is not None
                        else "unknown device",
                        self._pending_status_device_id,
                        self._pending_status_enum,
                        existing_status_identity[0],
                        existing_status_identity[1],
                    )
                data_updates.update(
                    {
                        CONF_STATUS_DEVICE_ID: self._pending_status_device_id,
                        CONF_STATUS_ENUM: self._pending_status_enum,
                        CONF_STATUS_IDENTITY_SOURCE: (
                            STATUS_IDENTITY_SOURCE_CALIBRATION
                        ),
                        CONF_SECONDARY_STATUS_IDENTITIES: list(
                            self._pending_secondary_status_identities
                        ),
                    }
                )
            return self.flow.async_update_and_abort(
                self.flow._get_entry(),
                self.flow._get_reconfigure_subentry(),
                data_updates=data_updates,
                reason="calibration_successful",
            )

        return self.flow.async_show_form(
            step_id="calibration_complete",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._selected_device["name"],
                "open_time": f"{self._open_time:.2f}",
                "close_time": f"{self._close_time:.2f}",
                **self._calibration_summary_placeholders(),
            },
            last_step=True,
        )

    async def async_step_calibration_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-enter whichever leg's instruction step is still pending.

        The sole redirect target for calibration_timeout_choice's "retry"
        option (see async_step_calibration_open_instruction's own comment
        on its first timeout branch for when that menu is shown at all) -
        shared by all four timeout cases (open/close leg, movement-start/
        stop wait) rather than one retry step per case, since which leg is
        still pending is exactly the same check async_step_calibration_
        open_instruction/_close_instruction's own success tail already uses
        to decide what comes after a leg completes: self._open_time is
        None until the open leg has actually finished, regardless of which
        leg's own wait just timed out.
        """
        if self._open_time is None:
            return await self.async_step_calibration_open_instruction(user_input)
        return await self.async_step_calibration_close_instruction(user_input)

    async def _wait_for_movement_start(self, event_type: str) -> bool:
        """Wait for one specific device-event command, or time out.

        Despite the name, also used (via _wait_for_stop_event()'s
        delegation below, with event_type=EVENT_STOPPED) to wait for the
        stop event ending a leg - the two waits never overlap within one
        calibration run, so sharing this one implementation and its
        _wait_event attribute is safe.
        """
        if self._selected_device is None:
            return False
        device_id = self._selected_device["id"]
        # Device ids positively confirmed to belong to some *other*,
        # already-paired blind on this hub - never this one's own
        # not-yet-confirmed identity, see _foreign_capture_device_ids()'s
        # own docstring in config_flow.py (both set_selected_device() call
        # sites always provide this key; the empty-frozenset default below
        # only guards a handler used directly in a test without going
        # through either of them).
        foreign_device_ids: frozenset[str] = (
            self._selected_device.get("foreign_capture_device_ids") or frozenset()
        )
        self._wait_event = asyncio.Event()

        @callback
        def handle_device_event(command: str) -> None:
            """Wake the wait once this blind's own signal fires `event_type`."""
            if command == event_type and self._wait_event:
                self.flow.hass.loop.call_soon_threadsafe(self._wait_event.set)

        @callback
        def handle_capture_event(_captured_device_id: str, command: str) -> None:
            """Wake the wait on a not-yet-confirmed device's matching frame.

            The hub-wide fallback for a blind whose real device id is not
            known yet - see the comment below for why it must still
            reject frames already attributed to a *different* blind.
            """
            # Unlike handle_device_event above (whose signal name already
            # encodes device_id), this hub-wide fallback fires for every
            # device's frames while a capture window is open (see
            # SIGNAL_DEVICE_EVENT_CAPTURE's own comment in api.py) - without
            # this check it accepted a *different*, unrelated blind moving
            # on the same hub at the same moment as a false positive for
            # this one, corrupting the open/close time being measured here.
            # Only ever
            # rejects a frame positively attributed to one of those other
            # blinds; anything not yet claimed by anyone - including this
            # same blind's own not-yet-confirmed real identity, exactly the
            # case this fallback exists for - is still accepted, same as
            # before this fix.
            if command != event_type or not self._wait_event:
                return
            if _captured_device_id.strip().upper() in foreign_device_ids:
                return
            self.flow.hass.loop.call_soon_threadsafe(self._wait_event.set)

        self._event_listener_unsubs = [
            async_dispatcher_connect(
                self.flow.hass,
                f"{SIGNAL_DEVICE_EVENT}_{device_id}",
                handle_device_event,
            ),
            async_dispatcher_connect(
                self.flow.hass,
                SIGNAL_DEVICE_EVENT_CAPTURE,
                handle_capture_event,
            ),
        ]

        try:
            await asyncio.wait_for(self._wait_event.wait(), timeout=CALIBRATION_TIMEOUT)
        except TimeoutError:
            return False
        else:
            return True
        finally:
            for unsub in self._event_listener_unsubs:
                unsub()
            self._event_listener_unsubs = []
            self._wait_event = None

    async def _wait_for_stop_event(self) -> bool:
        """Wait for the stop event ending the current leg, or time out."""
        return await self._wait_for_movement_start(EVENT_STOPPED)

    async def _notify_calibration_completed(
        self, open_time: float, close_time: float, ending_position: int
    ) -> None:
        """Tell the live cover entity about this run's measured results.

        A no-op if self._selected_device was somehow never set - every
        real caller already aborted earlier in that case, so this only
        guards against being called out of order. Otherwise dispatches
        SIGNAL_CALIBRATION_COMPLETED with the entity (or device) id,
        rounded travel times, and ending position; cover.py's own
        _handle_calibration_completed() listens for this to update the
        running entity's travel times and current position without
        waiting for the subentry/config-entry update this same call is
        about to trigger to reload the whole integration.
        """
        if self._selected_device is not None:
            async_dispatcher_send(
                self.flow.hass,
                SIGNAL_CALIBRATION_COMPLETED,
                self._selected_device.get("entity_id", self._selected_device["id"]),
                round(open_time, 2),
                round(close_time, 2),
                ending_position,
            )

    def enable_subentry_creation(
        self,
        *,
        blind_id: str | None = None,
        device_id: str,
        device_enum: str,
        device_name: str,
        status_device_id: str | None = None,
        status_enum: str | None = None,
        secondary_status_identities: list[dict[str, str]] | None = None,
        status_identity_source: str | None = None,
        invert_direction: bool = False,
    ) -> None:
        """Switch this handler onto the fresh-pairing finishing route.

        Called once by the pairing subentry flow's own
        _apply_paired_device_identity() (config_flow.py) - from async_
        step_name_device()'s first call, or a wrong-motor retry, both
        strictly *before* the motor test/calibration itself ever starts,
        not after it (see _apply_paired_device_identity()'s own
        docstring) - and before handing off to
        async_step_calibration_initial_state(), with everything a brand-
        new subentry will need that this handler does not otherwise
        discover itself (the paired device's own identity, any status
        identity already found via remote discovery earlier in that
        same pairing attempt). Once set, async_step_calibration_
        complete() assembles and creates that subentry instead of
        updating an existing one - see disable_subentry_creation() for
        the reconfigure route's own, opposite default.
        """
        self._create_subentry_after_calibration = True
        self._pending_blind_id = normalize_blind_id(blind_id) or generate_blind_id()
        self._pending_device_id = device_id
        self._pending_device_enum = device_enum
        self._pending_device_name = device_name
        self._pending_status_device_id = status_device_id
        self._pending_status_enum = status_enum
        self._pending_status_identity_source = status_identity_source
        self._pending_secondary_status_identities = list(
            secondary_status_identities or []
        )
        self._pending_invert_direction = invert_direction

    def disable_subentry_creation(self) -> None:
        """Switch this handler onto the reconfigure finishing route.

        Called at the top of config_flow.py's async_step_calibrate() -
        the reconfigure flow's own entry point into a calibration run -
        so that recalibrating an already-paired blind always starts
        from this handler's default state, even though the same handler
        instance may previously have been used (and left in the fresh-
        pairing state by enable_subentry_creation()) earlier in the same
        flow run. Clears every pending-subentry field back to its
        __init__ default, not only the flag itself, so a stale device
        id/name from an earlier attempt in this same run can never leak
        into async_step_calibration_complete()'s update-in-place branch.
        """
        self._create_subentry_after_calibration = False
        self._pending_blind_id = None
        self._pending_device_id = None
        self._pending_device_enum = None
        self._pending_device_name = None
        self._pending_status_device_id = None
        self._pending_status_enum = None
        self._pending_secondary_status_identities = []
        self._pending_status_identity_source = None
        self._calibration_discovery_result = None
        self._pending_invert_direction = False
