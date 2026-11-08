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
    def __init__(self, flow: ConfigSubentryFlow) -> None:
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

    async def async_step_calibration_close(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
            step_id="calibration_close",
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
            },
            errors=errors,
            last_step=False,
        )

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
            # See async_step_calibration_close()'s own comment on its
            # identical fallback - near-unreachable once
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
            # See async_step_calibration_close()'s own comment on its
            # identical fallback - near-unreachable once
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
                return pairing_flow._finish_pairing(
                    title=self._pending_device_name, data=data
                )

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
                        "keeping the existing one and discarding the newly "
                        "discovered guess",
                        self._selected_device["name"]
                        if self._selected_device is not None
                        else "unknown device",
                        self._pending_status_device_id,
                        self._pending_status_enum,
                        existing_status_identity[0],
                        existing_status_identity[1],
                    )
                else:
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
            if command == event_type and self._wait_event:
                self.flow.hass.loop.call_soon_threadsafe(self._wait_event.set)

        @callback
        def handle_capture_event(_captured_device_id: str, command: str) -> None:
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
