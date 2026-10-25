"""Config flow for Schellenberg USB integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .api import check_serial_port
from .blind_id import generate_blind_id
from .const import (
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
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    DEFAULT_TRAVEL_TIME_SECONDS,
    DOMAIN,
    STATUS_IDENTITY_SOURCE_CALIBRATION,
    STATUS_IDENTITY_SOURCE_MANUAL,
    STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
    TEST_COMMAND_DELAY,
    resolve_direction_command,
)
from .identities import (
    format_status_identities,
    normalize_status_identity,
    normalize_status_identities,
    parse_status_identities_text,
    resolve_blind_identities,
    serialize_status_identities,
)
from .options_flow import SchellenbergOptionsFlowHandler
from .options_flow_calibration import CalibrationFlowHandler
from .runtime_translation_text import (
    runtime_translation_text as _runtime_translation_text,
)
from .runtime_translation_text import translate_block_reason as _translate_block_reason
from .runtime_translation_text import translate_phrase as _translate_phrase
from .runtime_translation_text import yes_no as _yes_no

_LOGGER = logging.getLogger(__name__)

# A plain list of step ids - NOT a dict. async_show_menu() resolves a
# list's labels from strings.json/translations for every language this
# integration ships (de/en/es/fr), the same way every other menu in this
# file does. A dict (the previous form of this constant) is displayed with
# its *values* used verbatim as hardcoded, untranslated English labels,
# which silently ignored the fully translated de/es/fr menu_options that
# already existed for this exact step in every strings/translation file.
DEVELOPER_TOOLS_MENU_OPTIONS = [
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
]


class SchellenbergUsbConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Schellenberg USB."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return SchellenbergOptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        # Use constant for subentry type so strings/json and code stay in sync
        return {SUBENTRY_TYPE_BLIND: SchellenbergPairingSubentryFlow}

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_port: str | None = None
        self._discovered_title: str | None = None
        self._discovered_unique: str | None = None

    # -------------------------
    # USER-INITIATED FLOW
    # -------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step started by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            try:
                await self.hass.async_add_executor_job(check_serial_port, port)

                # Use the port path as the unique ID when set up manually.
                await self.async_set_unique_id(port, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Schellenberg USB ({port})", data=user_input
                )
            except (OSError, TimeoutError):
                errors["base"] = "cannot_connect"
                _LOGGER.error("Failed to connect to serial port %s", port)
            except AbortFlow:
                # _abort_if_unique_id_configured() signals "already
                # configured" by raising this - let it propagate instead of
                # it being swallowed as an "unknown error" below.
                raise
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("An unexpected error occurred")

        return self._form_schema(errors, default_port="/dev/ttyUSB0")

    # -------------------------
    # USB DISCOVERY FLOW
    # -------------------------
    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        """Handle a USB-subsystem discovery of a possible Schellenberg stick.

        Derives the most stable identifier available (serial number if
        the OS reports one, else a vid:pid:device fallback) and uses
        it to deduplicate against an already-configured entry, updating
        its stored port if the OS now maps the same device differently.
        Otherwise stashes the discovered port/title and continues on
        to the confirmation step.
        """
        # Try to get the most stable unique identifier we can (serial
        # number if present).
        unique = getattr(discovery_info, "serial_number", None) or (
            f"{getattr(discovery_info, 'vid', 'unknown')}:"
            f"{getattr(discovery_info, 'pid', 'unknown')}:"
            f"{getattr(discovery_info, 'device', 'unknown')}"
        )

        # Prefer the OS device path for the default value in the confirmation form.
        port = getattr(discovery_info, "device", None)
        manufacturer = getattr(discovery_info, "manufacturer", None) or "Schellenberg"
        description = getattr(discovery_info, "description", None) or "USB device"

        # Save for the confirm step
        self._discovered_port = port
        self._discovered_unique = unique
        self._discovered_title = f"{manufacturer} {description}".strip()

        # Deduplicate if already configured; update the stored port if it changed.
        await self.async_set_unique_id(unique, raise_on_progress=False)
        self._abort_if_unique_id_configured(
            updates={CONF_SERIAL_PORT: port} if port else None
        )

        # Ask for confirmation (and allow editing the port if the host
        # maps it differently)
        return await self.async_step_usb_confirm()

    async def async_step_usb_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm USB-discovered device and create the entry."""
        errors: dict[str, str] = {}

        # If we don't have a port path, let the user supply one.
        default_port = self._discovered_port or "/dev/ttyUSB0"

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            try:
                await self.hass.async_add_executor_job(check_serial_port, port)

                # unique_id was already set in async_step_usb(),
                # re-assert and create the entry
                await self.async_set_unique_id(
                    self._discovered_unique, raise_on_progress=False
                )
                self._abort_if_unique_id_configured()

                title = self._discovered_title or f"Schellenberg USB ({port})"
                return self.async_create_entry(
                    title=title, data={CONF_SERIAL_PORT: port}
                )
            except (OSError, TimeoutError):
                errors["base"] = "cannot_connect"
                _LOGGER.error("Failed to connect to serial port %s", port)
            except AbortFlow:
                # Same reasoning as async_step_user(): don't let the abort
                # signal be swallowed as an "unknown error" below.
                raise
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("An unexpected error occurred during USB confirm")

        # Mark as confirm-only so the UI shows a simple confirmation experience
        self._set_confirm_only()
        return self._form_schema(
            errors, default_port=default_port, step_id="usb_confirm"
        )

    # -------------------------
    # Helpers
    # -------------------------
    @callback
    def _form_schema(
        self, errors: dict[str, str], default_port: str, step_id: str = "user"
    ) -> ConfigFlowResult:
        """Return a form with a (prefilled) serial port field."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SERIAL_PORT, default=default_port
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )


def _foreign_capture_device_ids(
    entry: Any, *, exclude_subentry_id: str | None = None
) -> frozenset[str]:
    """Collect every *other* blind's own confirmed device identities.

    Used to scope CalibrationFlowHandler's capture-wide fallback listener
    (options_flow_calibration.py's handle_capture_event) to frames that
    cannot be positively attributed to a different, already-paired blind
    on this same hub - without this, a second blind's RF traffic arriving
    while this one is being paired or recalibrated could silently satisfy
    this one's movement-start/stop wait instead, corrupting the open/close
    time being measured - found during the independent audit alongside
    the CHANGELOG entries above this one.

    Deliberately permissive otherwise: an id belonging to *nobody* yet -
    including this same blind's own not-yet-discovered or not-yet-
    persisted real status identity - is never excluded by this, since
    that is exactly the ambiguity the capture-wide fallback exists to
    tolerate for a brand-new pairing or a first recalibration (see
    test_differing_status_identity_still_completes_via_capture_signal in
    tests/test_options_flow_calibration.py). Only a device id positively
    claimed by some *other* blind subentry is ever excluded.
    """
    foreign_ids: set[str] = set()
    for other_subentry in entry.subentries.values():
        if other_subentry.subentry_id == exclude_subentry_id:
            continue
        if other_subentry.subentry_type != SUBENTRY_TYPE_BLIND:
            continue
        data = other_subentry.data
        for key in (CONF_COMMAND_DEVICE_ID, CONF_DEVICE_ID, CONF_STATUS_DEVICE_ID):
            value = data.get(key)
            if value:
                foreign_ids.add(str(value).strip().upper())
        for identity in normalize_status_identities(
            data.get(CONF_SECONDARY_STATUS_IDENTITIES)
        ):
            foreign_ids.add(identity[0])
    return frozenset(foreign_ids)


class SchellenbergPairingSubentryFlow(ConfigSubentryFlow):
    """Flow for adding new blind devices as subentries."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the subentry flow's pairing/teaching state.

        Besides the lazily-created `calibration_handler`, the
        `_pending_*` attributes describe a new blind being paired
        (eventually written to a new subentry). See the comments on
        the individual attributes below for the less obvious ones.
        """
        super().__init__()
        self.calibration_handler: CalibrationFlowHandler | None = None
        self._pending_blind_id = generate_blind_id()
        self._pending_device_id: str | None = None
        self._pending_device_enum: str | None = None
        self._pending_device_name: str | None = None
        self._pending_status_device_id: str | None = None
        self._pending_status_enum: str | None = None
        self._pending_secondary_status_identities: list[dict[str, str]] = []
        self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        self._status_discovery_result: dict[str, Any] | None = None
        self._status_discovery_updates_existing = False
        self._pending_open_time: float | None = None
        self._pending_close_time: float | None = None
        self._pending_invert_direction = False
        self._pairing_workflow = "legacy"
        # The direction chosen on the test_motor form (see
        # async_step_test_motor()) - "open" or "close" - remembered only so
        # did_motor_move's own description can report back what was just
        # tested. None only before the form has ever been submitted for
        # this blind.
        self._pending_test_direction: str | None = None
        # Sticky per-blind flag: once a retry has happened, the test_motor
        # form keeps showing the retry_notice explaining why it is being
        # shown again - set once, on the first inconclusive attempt, and
        # never cleared again for this blind. There is no retry limit: the
        # person keeps choosing a direction for as long as they keep
        # confirming, until an attempt both sends successfully and is
        # confirmed to have moved the blind.
        self._test_motor_retry_attempted = False
        # Resolved lazily (via runtime_translation_text()) at display time instead of
        # translated here, because self.hass is not attached to a flow
        # handler yet at __init__ time - None means "nothing to report
        # yet", shown as the translated no_test_command placeholder text.
        self._developer_notice: str | None = None

    def _get_calibration_handler(self) -> CalibrationFlowHandler:
        """Return (and lazily create) the calibration flow handler."""
        if self.calibration_handler is None:
            self.calibration_handler = CalibrationFlowHandler(self)
        return self.calibration_handler

    async def _await_subentry_result(
        self,
        step_coro: Awaitable[ConfigFlowResult | SubentryFlowResult],
    ) -> SubentryFlowResult:
        """Await a calibration step and cast to SubentryFlowResult for mypy."""
        return cast(SubentryFlowResult, await step_coro)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose how to pair/calibrate a new device."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[
                "pair_device",
                "add_shutter_from_remote",
            ],
        )

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Run the pair-then-calibrate workflow."""
        self._pairing_workflow = "legacy"
        return await self._async_pair_device("pair_device", user_input)

    async def async_step_add_shutter_from_remote(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pair a device by teaching the stick onto an existing remote's channel.

        Captures whatever identity the person's already-paired physical
        remote uses for the motor/channel they select, and teaches the
        stick onto that same identity - rather than pair_device's own
        fresh teach sequence. A short test move follows; see
        async_step_did_motor_move()'s "hybrid" branch below.
        """
        self._pairing_workflow = "hybrid"
        return await self._async_pair_device("add_shutter_from_remote", user_input)

    async def _async_pair_device(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Pair a device for either supported pairing workflow."""
        _LOGGER.debug("Pairing step user input: %s", user_input)
        if user_input is None:
            _LOGGER.info("Showing pairing form")
            return self.async_show_form(step_id=step_id, data_schema=vol.Schema({}))

        # Get the hub entry (parent config entry)
        hub_entry = self._get_entry()
        api = hub_entry.runtime_data

        # Initiate pairing and wait for response (up to 10 seconds)
        pairing_result = await api.pair_device_and_wait()

        if pairing_result is None:
            # Pairing timeout
            return self.async_abort(reason="pairing_timeout")

        # Pairing successful! Store device_id and device_enum in context
        device_id, device_enum = pairing_result
        self._pending_device_id = device_id
        self._pending_device_enum = device_enum
        self._pending_status_device_id = None
        self._pending_status_enum = None
        self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        self._pending_secondary_status_identities = []
        self._pending_device_name = None
        return await self.async_step_name_device()

    @staticmethod
    def _is_hex_value(value: str, length: int) -> bool:
        """Return whether value is an exact-length hexadecimal string."""
        return len(value) == length and all(
            character in "0123456789ABCDEF" for character in value
        )

    def _pending_data(self) -> dict[str, Any]:
        """Return config-subentry data for the pending blind."""
        assert self._pending_device_id is not None
        assert self._pending_device_enum is not None

        assert self._pending_open_time is not None
        assert self._pending_close_time is not None
        data: dict[str, Any] = {
            CONF_BLIND_ID: self._pending_blind_id,
            # Legacy command keys stay populated for backward compatibility.
            CONF_DEVICE_ID: self._pending_device_id,
            CONF_DEVICE_ENUM: self._pending_device_enum,
            CONF_COMMAND_DEVICE_ID: self._pending_device_id,
            CONF_COMMAND_ENUM: self._pending_device_enum,
            CONF_STATUS_IDENTITY_SOURCE: self._pending_status_identity_source,
            CONF_SECONDARY_STATUS_IDENTITIES: list(
                self._pending_secondary_status_identities
            ),
            CONF_OPEN_TIME: self._pending_open_time,
            CONF_CLOSE_TIME: self._pending_close_time,
            CONF_INVERT_DIRECTION: self._pending_invert_direction,
        }
        if (
            self._pending_status_device_id is not None
            and self._pending_status_enum is not None
        ):
            data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
            data[CONF_STATUS_ENUM] = self._pending_status_enum
        return data

    def _finish_pairing(
        self, *, title: str, data: dict[str, Any]
    ) -> SubentryFlowResult:
        """Create the subentry for the newly paired blind."""
        return self.async_create_entry(
            title=title, data=data, unique_id=self._pending_device_id
        )

    def _test_motor_placeholders(self) -> dict[str, str]:
        """Build the description placeholders for the test_motor form."""
        return {
            "device_id": self._pending_device_id or "",
            "device_enum": self._pending_device_enum or "",
            "retry_notice": (
                " " + _runtime_translation_text(self.hass, "test_motor_retry_notice")
                if self._test_motor_retry_attempted
                else ""
            ),
        }

    @staticmethod
    def _test_direction_schema() -> vol.Schema:
        """Build the required, no-default direction-choice field.

        A plain empty schema used to mean Home Assistant had already
        decided the direction itself (away from the blind's last tracked
        position, defaulting to open when nothing was tracked yet) and
        silently alternated it on a retry - the person only ever
        confirmed or reported afterward, never chose. Replacing it with a
        required "open"/"close" select was meant to force an explicit
        choice every time, on the assumption that Home Assistant's
        frontend could not submit a required select with neither option
        chosen - but in its "list" (radio) rendering it visibly highlights
        the first option before anything is picked, which looks exactly
        like a considered answer and reintroduces the original problem
        (see options_flow_calibration.py's calibration_close step, where
        the same thing was found and fixed first). A third, clearly
        non-answer placeholder option now occupies that highlighted first
        slot instead; async_step_test_motor rejects submitting it (or
        anything other than "open"/"close") with an explicit on-screen
        error instead of picking a direction on its own.
        """
        return vol.Schema(
            {
                vol.Required("test_direction"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["unselected", "open", "close"],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="test_direction",
                    )
                )
            }
        )

    async def async_step_test_motor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask which direction to test, then send that short test move.

        Nothing is transmitted until the person submits this form - that
        submission *is* both the direction choice and the confirmation.
        The "retry_notice" placeholder is this form's other dynamic bit of
        text: empty on the first attempt, and - once a failed send or a
        "didn't move" report has set _test_motor_retry_attempted - a short
        explanation that the previous attempt was inconclusive, so a retry
        doesn't look like an unexplained repeat of the same step. There is
        no retry limit: the person keeps choosing a direction for as long
        as they keep confirming.
        """
        if not self._pending_device_id or not self._pending_device_enum:
            return self.async_abort(reason="device_not_found")

        if user_input is None:
            return self.async_show_form(
                step_id="test_motor",
                data_schema=self._test_direction_schema(),
                description_placeholders=self._test_motor_placeholders(),
            )

        test_direction = user_input.get("test_direction")
        if test_direction not in ("open", "close"):
            # Neither real option was chosen - see _test_direction_schema()'s
            # own comment for why this is reachable despite the field being
            # required.
            return self.async_show_form(
                step_id="test_motor",
                data_schema=self._test_direction_schema(),
                description_placeholders=self._test_motor_placeholders(),
                errors={"base": "test_direction_required"},
            )

        test_toward_open = test_direction == "open"
        self._pending_test_direction = test_direction
        open_action = resolve_direction_command("open", self._pending_invert_direction)
        close_action = resolve_direction_command(
            "close", self._pending_invert_direction
        )
        action = open_action if test_toward_open else close_action

        api = self._get_entry().runtime_data
        if not await api.control_blind_exclusive(
            self._pending_device_enum,
            action,
            device_id=self._pending_device_id,
        ):
            # The RF command never went out, so nothing about this
            # direction was actually tested. Inconclusive either way - the
            # person simply chooses again, same direction or the other one.
            self._test_motor_retry_attempted = True
            return self.async_show_form(
                step_id="test_motor",
                data_schema=self._test_direction_schema(),
                description_placeholders=self._test_motor_placeholders(),
                errors={"base": "command_failed"},
            )
        try:
            # Wait for the stick to actually finish sending the direction
            # command (t0) before starting the hold timer: on real hardware
            # one RF transmission can take over a second (observed ~1.1-1.2s
            # end to end), well past the old fixed delay below. Sending stop
            # before that transmission completed meant the two writes could
            # land on the shared transmitter back to back, so the motor
            # often received neither cleanly and never visibly moved - see
            # TEST_COMMAND_DELAY's own comment in const.py.
            await api.wait_for_transmitter_idle(
                "finishing the short motor test move before stopping it"
            )
            await asyncio.sleep(TEST_COMMAND_DELAY)
        finally:
            stopped = await api.control_blind_exclusive(
                self._pending_device_enum,
                CMD_STOP,
                device_id=self._pending_device_id,
            )
        if not stopped:
            # The direction command went out, but the follow-up stop never
            # did - the motor may still be running. Just as inconclusive as
            # the send-failure case above.
            self._test_motor_retry_attempted = True
            return self.async_show_form(
                step_id="test_motor",
                data_schema=self._test_direction_schema(),
                description_placeholders=self._test_motor_placeholders(),
                errors={"base": "command_failed"},
            )
        return await self.async_step_did_motor_move()

    async def async_step_did_motor_move(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask whether the short command moved the motor.

        Reached by async_step_test_motor() from two different places: the
        "hybrid" add_shutter_from_remote workflow's own quick test right
        after pairing, and the "existing" setup's re-test of an
        already-paired blind. The default "legacy" (pair_device) workflow
        never reaches this step at all - async_step_name_device() sends
        it straight into calibration instead.
        """
        if user_input is None:
            return self.async_show_form(
                step_id="did_motor_move",
                data_schema=vol.Schema(
                    {
                        vol.Required("motor_moved", default=True): (
                            selector.BooleanSelector()
                        )
                    }
                ),
                description_placeholders={
                    "device_id": self._pending_device_id
                    or _translate_phrase(self.hass, "unknown"),
                    "device_enum": self._pending_device_enum
                    or _translate_phrase(self.hass, "unknown"),
                    "test_action": _translate_phrase(
                        self.hass, self._pending_test_direction or "unknown"
                    ),
                },
            )

        if not user_input["motor_moved"]:
            if self._pairing_workflow == "existing":
                return await self.async_step_edit()
            # "No movement" doesn't necessarily mean the pairing failed -
            # the blind may simply already have been sitting at the end
            # stop in the direction just chosen. Show a fresh
            # async_step_test_motor() form so the person explicitly chooses
            # a direction again (the same one, or the other one) before
            # anything more is sent - for as long as they keep saying it
            # didn't move. There is no attempt limit and no fallback to
            # manual entry here: the person can always leave the wizard if
            # the blind genuinely isn't responding, but the flow itself
            # never gives up on their behalf.
            self._test_motor_retry_attempted = True
            return await self.async_step_test_motor()

        if self._pairing_workflow == "existing":
            return self.async_abort(reason="command_test_successful")
        # "hybrid" (add_shutter_from_remote) is the only workflow that can
        # still be set here: "existing" already returned above, and the
        # default "legacy" (pair_device) never reaches this step at all -
        # async_step_name_device() sends it straight into calibration
        # instead (see its own branch there).
        return await self.async_step_test_success()

    async def async_step_test_success(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Offer calibration or manual timing after a successful motor test.

        Home Assistant requires a menu's step_id to match a real
        async_step_<id>() handler on this class; a bare
        async_show_menu(step_id="test_success", ...) call with no such
        method raises UnknownStep as soon as the menu is shown.
        """
        return self.async_show_menu(
            step_id="test_success",
            menu_options=["calibration_close", "manual_times"],
        )

    async def async_step_manual_times(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect travel times after a successful paired command test."""
        if user_input is not None:
            self._pending_open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            self._pending_close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])
            return await self.async_step_discover_status()

        return self.async_show_form(
            step_id="manual_times",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OPEN_TIME_SECONDS): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_CLOSE_TIME_SECONDS): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def async_step_name_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask user to provide a friendly name for the paired device."""
        device_id = self._pending_device_id
        device_enum = self._pending_device_enum

        if user_input is None:
            # Initial call - show form
            if not device_id:
                return self.async_abort(reason="pairing_failed")

            return self.async_show_form(
                step_id="name_device",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_DEVICE_NAME): selector.TextSelector(),
                    }
                ),
                description_placeholders={
                    "device_id": device_id,
                },
            )

        # User provided a name; configure calibration state for either workflow.
        if not device_id or not device_enum:
            return self.async_abort(reason="pairing_failed")

        device_name = user_input.get(CONF_DEVICE_NAME) or _runtime_translation_text(
            self.hass, "device_fallback_name", device_id=device_id
        )
        self._pending_device_name = device_name

        handler = self._get_calibration_handler()

        # Provide minimal device to handler
        handler.set_selected_device(
            {
                # Pairing identity is used transiently to preserve the existing
                # calibration listener; it is not persisted as primary status.
                "id": device_id,
                "entity_id": device_id,
                "name": device_name,
                "enum": device_enum,
                # This is a brand-new pairing, so there is no subentry of its
                # own yet to exclude - every *other* blind already on this
                # hub is a candidate to exclude from the capture-wide
                # fallback below (see _foreign_capture_device_ids()'s own
                # docstring).
                "foreign_capture_device_ids": _foreign_capture_device_ids(
                    self._get_entry()
                ),
            }
        )
        handler.enable_subentry_creation(
            blind_id=self._pending_blind_id,
            device_id=device_id,
            device_enum=device_enum,
            device_name=device_name,
            status_device_id=self._pending_status_device_id,
            status_enum=self._pending_status_enum,
            secondary_status_identities=(self._pending_secondary_status_identities),
            status_identity_source=self._pending_status_identity_source,
            invert_direction=self._pending_invert_direction,
        )
        if self._pairing_workflow == "hybrid":
            return await self.async_step_test_motor()

        _LOGGER.debug(
            "Starting calibration for paired device %s (%s) before creating subentry",
            device_id,
            device_name,
        )
        return await self._await_subentry_result(
            handler.async_step_calibration_close(None)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose whether to edit settings or recalibrate a blind."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["edit", "test_existing", "developer_tools", "calibrate"],
        )

    def _developer_details(self) -> dict[str, Any]:
        """Return normalized protocol details for the selected blind."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        resolved = resolve_blind_identities(data)
        command_device_id = resolved["command_device_id"]
        command_enum = resolved["command_enum"]
        configured_source = resolved["status_identity_source"]
        primary_identity = resolved["primary_identity"]
        secondary_status_identities = resolved["secondary_status_identities"]
        status_identities = resolved["status_identities"]
        primary_status_device_id = (
            primary_identity[0]
            if primary_identity is not None
            else _runtime_translation_text(self.hass, "unknown_label")
        )
        primary_status_enum = (
            primary_identity[1] if primary_identity is not None else "--"
        )
        source_label = {
            STATUS_IDENTITY_SOURCE_MANUAL: _runtime_translation_text(
                self.hass, "manually_entered"
            ),
            STATUS_IDENTITY_SOURCE_CALIBRATION: _runtime_translation_text(
                self.hass, "source_calibration"
            ),
            STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY: _runtime_translation_text(
                self.hass, "source_remote_discovery"
            ),
            STATUS_IDENTITY_SOURCE_UNKNOWN: _runtime_translation_text(
                self.hass, "source_unknown"
            ),
        }.get(configured_source, _runtime_translation_text(self.hass, "source_legacy"))
        last_calibration_value = data.get(CONF_LAST_CALIBRATION)
        last_calibration = (
            last_calibration_value if isinstance(last_calibration_value, dict) else {}
        )
        calibration_frames = last_calibration.get("frames", [])
        calibration_groups = last_calibration.get("groups", [])
        unknown_label = _runtime_translation_text(self.hass, "unknown_label")
        calibration_frames_text = "\n".join(
            f"{frame.get('time', '--')} "
            f"{frame.get('device_id', unknown_label)}/{frame.get('enum', '--')} "
            f"cmd={frame.get('command', '--')} "
            f"phase={_translate_phrase(self.hass, frame.get('phase', 'unknown'))}"
            for frame in calibration_frames
            if isinstance(frame, dict)
        ) or _runtime_translation_text(self.hass, "none_recorded")
        calibration_candidates_text = "\n".join(
            f"{group.get('device_id', unknown_label)}/{group.get('enum', '--')}: "
            f"{','.join(group.get('commands', []))}"
            for group in calibration_groups
            if isinstance(group, dict)
        ) or _runtime_translation_text(self.hass, "none_word")
        return {
            "name": subentry.title,
            "command_device_id": command_device_id,
            "command_enum": command_enum,
            # Backward-compatible names used in existing command logs/tests.
            "status_device_id": primary_status_device_id,
            "status_enum": primary_status_enum,
            "primary_status_device_id": primary_status_device_id,
            "primary_status_enum": primary_status_enum,
            "status_identity_source": source_label,
            "secondary_status_identities": secondary_status_identities,
            "secondary_status_identities_text": (
                format_status_identities(secondary_status_identities)
                or _runtime_translation_text(self.hass, "none_word")
            ),
            "status_identities": status_identities,
            "invert_direction": bool(data.get(CONF_INVERT_DIRECTION, False)),
            "open_time": float(data.get(CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME_SECONDS)),
            "close_time": float(data.get(CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME_SECONDS)),
            "last_calibration_time": (
                str(last_calibration["completed_at"])
                if last_calibration.get("completed_at")
                else _runtime_translation_text(self.hass, "never")
            ),
            "calibration_end_reason": (
                _translate_phrase(self.hass, str(last_calibration["end_reason"]))
                if last_calibration.get("end_reason")
                else _runtime_translation_text(self.hass, "not_recorded")
            ),
            "calibration_frames_text": calibration_frames_text,
            "calibration_candidates_text": calibration_candidates_text,
        }

    def _developer_snapshot(
        self,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        """Return separated frame and position diagnostics for one blind."""
        details = self._developer_details()
        api = self._get_entry().runtime_data
        empty_frame = {
            "device_id": _runtime_translation_text(self.hass, "frame_none_received"),
            "enum": "--",
            "command": "--",
            "time": "--",
            "identity_role": "none",
            "interpreted_command": "unknown",
            "position_tracking": False,
        }
        last_matched = api.get_last_received_for_identities(
            details["status_identities"]
        ) or dict(empty_frame)
        last_primary = api.get_last_primary_tracking_frame(
            details["primary_status_device_id"],
            details["primary_status_enum"],
        ) or dict(empty_frame)
        last_secondary = api.get_last_secondary_frame(
            details["secondary_status_identities"]
        ) or dict(empty_frame)
        last_position = api.get_last_position_update(details["command_device_id"]) or {
            "source": _runtime_translation_text(self.hass, "position_none_recorded"),
            "direction": "--",
            "position_source": "unknown",
            "confirmed_since_restart": False,
            "previous_position": None,
            "new_position": None,
            "status": "--",
            "time": "--",
        }
        last_manual_sync = api.get_last_manual_position_sync(
            details["command_device_id"]
        )
        if not isinstance(last_manual_sync, dict):
            last_manual_sync = {
                "new_position": None,
                "time": _runtime_translation_text(self.hass, "never"),
            }
        return (
            details,
            last_matched,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        )

    async def async_step_developer_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show live protocol diagnostics and direct test actions."""
        (
            details,
            last_received,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        ) = self._developer_snapshot()
        api = self._get_entry().runtime_data
        hass = self.hass
        return self.async_show_menu(
            step_id="developer_tools",
            menu_options=DEVELOPER_TOOLS_MENU_OPTIONS,
            description_placeholders={
                "selected_blind": str(details["name"]),
                "last_device_id": last_received["device_id"],
                "last_enum": last_received["enum"],
                "last_command": last_received["command"],
                "last_time": last_received["time"],
                "command_device_id": details["command_device_id"],
                "command_enum": details["command_enum"],
                "primary_status_device_id": details["primary_status_device_id"],
                "primary_status_enum": details["primary_status_enum"],
                "status_identity_source": details["status_identity_source"],
                "last_calibration_time": details["last_calibration_time"],
                "calibration_end_reason": details["calibration_end_reason"],
                "calibration_frames": details["calibration_frames_text"],
                "calibration_candidates": details["calibration_candidates_text"],
                "secondary_status_identities": details[
                    "secondary_status_identities_text"
                ],
                "last_identity_role": _translate_phrase(
                    hass, last_received["identity_role"]
                ),
                "last_interpretation": _translate_phrase(
                    hass, last_received["interpreted_command"]
                ),
                "last_position_tracking": _yes_no(
                    hass, last_received["position_tracking"]
                ),
                "primary_last_device_id": last_primary["device_id"],
                "primary_last_enum": last_primary["enum"],
                "primary_last_command": last_primary["command"],
                "primary_last_interpretation": _translate_phrase(
                    hass, last_primary["interpreted_command"]
                ),
                "primary_last_time": last_primary["time"],
                "secondary_last_device_id": last_secondary["device_id"],
                "secondary_last_enum": last_secondary["enum"],
                "secondary_last_command": last_secondary["command"],
                "secondary_last_interpretation": _translate_phrase(
                    hass, last_secondary["interpreted_command"]
                ),
                "secondary_last_time": last_secondary["time"],
                "position_source": _translate_phrase(
                    hass,
                    last_position.get("position_source", last_position["source"]),
                ),
                "position_direction": _translate_phrase(
                    hass, last_position["direction"]
                ),
                "position_previous": (
                    "--"
                    if last_position["previous_position"] is None
                    else f"{last_position['previous_position']}%"
                ),
                "position_new": (
                    "--"
                    if last_position["new_position"] is None
                    else f"{last_position['new_position']}%"
                ),
                "position_status": _translate_phrase(hass, last_position["status"]),
                "position_time": last_position["time"],
                "current_position": (
                    "--"
                    if last_position["new_position"] is None
                    else f"{last_position['new_position']}%"
                ),
                "last_manual_sync_time": last_manual_sync["time"],
                "position_confidence": (
                    _translate_phrase(hass, last_position["status"])
                    if last_position["new_position"] is not None
                    else _translate_phrase(hass, "unknown")
                ),
                "position_confirmed_since_restart": _yes_no(
                    hass, bool(last_position.get("confirmed_since_restart"))
                ),
                "stick_connected": _yes_no(hass, api.is_connected),
                "stick_mode": _translate_phrase(hass, api.device_mode or "unknown"),
                "stick_ready": _yes_no(hass, api.transmit_ready),
                "stick_busy": _yes_no(hass, api.busy_latched),
                "result": self._developer_notice
                or _runtime_translation_text(hass, "no_test_command"),
            },
        )

    async def _async_developer_command(self, command: str) -> SubentryFlowResult:
        """Send one logical command from the developer tools menu."""
        api = self._get_entry().runtime_data
        details = self._developer_details()
        _LOGGER.warning(
            "Developer Tools command clicked selected_blind=%s "
            "command_requested=%s command_device_id=%s command_enum=%s "
            "status_device_id=%s status_enum=%s secondary_statuses=%s "
            "stick_connected=%s stick_mode=%s stick_ready=%s pairing=%s "
            "transmitter_active=%s "
            "busy_latched=%s",
            details["name"],
            command,
            details["command_device_id"],
            details["command_enum"],
            details["status_device_id"],
            details["status_enum"],
            details["secondary_status_identities_text"],
            api.is_connected,
            api.device_mode or "unknown",
            api.transmit_ready,
            api.pairing_active,
            api.transmitter_active,
            api.busy_latched,
        )

        if reason := api.transmit_block_reason:
            _LOGGER.error(
                "Developer Tools command blocked selected_blind=%s "
                "command_requested=%s reason=%s",
                details["name"],
                command,
                reason,
            )
            self._developer_notice = _runtime_translation_text(
                self.hass,
                "notice_command_blocked",
                command=_translate_phrase(self.hass, command).capitalize(),
                reason=_translate_block_reason(self.hass, reason),
            )
            return await self.async_step_developer_tools()

        invert_direction = details["invert_direction"]
        action = {
            "open": resolve_direction_command("open", invert_direction),
            "close": resolve_direction_command("close", invert_direction),
            "stop": CMD_STOP,
        }[command]
        _LOGGER.warning(
            "Developer Tools command dispatching selected_blind=%s "
            "command_requested=%s protocol_action=%s command_device_id=%s "
            "command_enum=%s",
            details["name"],
            command,
            action,
            details["command_device_id"],
            details["command_enum"],
        )
        try:
            sent = await api.control_blind_exclusive(
                details["command_enum"],
                action,
                device_id=details["command_device_id"],
                source="developer_tools",
            )
        except Exception:
            _LOGGER.exception(
                "Developer Tools command raised an exception selected_blind=%s "
                "command_requested=%s",
                details["name"],
                command,
            )
            sent = False

        if sent:
            _LOGGER.warning(
                "Developer Tools command result selected_blind=%s "
                "command_requested=%s result=written_awaiting_ack",
                details["name"],
                command,
            )
            self._developer_notice = _runtime_translation_text(
                self.hass,
                "notice_command_sent",
                command=_translate_phrase(self.hass, command).capitalize(),
            )
        else:
            _LOGGER.error(
                "Developer Tools command result selected_blind=%s "
                "command_requested=%s result=failed",
                details["name"],
                command,
            )
            self._developer_notice = _runtime_translation_text(
                self.hass,
                "notice_command_failed",
                command=_translate_phrase(self.hass, command).capitalize(),
            )
        return await self.async_step_developer_tools()

    async def async_step_test_open(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct logical-open test command."""
        return await self._async_developer_command("open")

    async def async_step_test_close(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct logical-close test command."""
        return await self._async_developer_command("close")

    async def async_step_test_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send a direct stop test command."""
        return await self._async_developer_command("stop")

    async def _async_manual_position_sync(self, position: int) -> SubentryFlowResult:
        """Apply one Developer Tools position correction to the live cover."""
        api = self._get_entry().runtime_data
        details = self._developer_details()
        _LOGGER.warning(
            "Developer Tools manual position sync clicked selected_blind=%s "
            "command_device_id=%s position=%d",
            details["name"],
            details["command_device_id"],
            position,
        )
        try:
            synced = api.manual_sync_position(details["command_device_id"], position)
        except (TypeError, ValueError):
            _LOGGER.exception(
                "Developer Tools manual position sync rejected "
                "selected_blind=%s position=%s",
                details["name"],
                position,
            )
            synced = False

        if synced:
            self._developer_notice = _runtime_translation_text(
                self.hass, "notice_position_confirmed", position=position
            )
        else:
            self._developer_notice = _runtime_translation_text(
                self.hass, "notice_position_sync_unregistered"
            )
        return await self.async_step_developer_tools()

    async def async_step_set_position_open(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manually confirm that the blind is fully open."""
        return await self._async_manual_position_sync(100)

    async def async_step_set_position_closed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manually confirm that the blind is fully closed."""
        return await self._async_manual_position_sync(0)

    async def async_step_set_position_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Accept and apply an exact manual blind position."""
        if user_input is not None:
            return await self._async_manual_position_sync(int(user_input["position"]))

        *_, last_position, _last_manual_sync = self._developer_snapshot()
        default_position = last_position["new_position"]
        if not isinstance(default_position, int):
            default_position = 50
        return self.async_show_form(
            step_id="set_position_manual",
            data_schema=vol.Schema(
                {
                    vol.Required("position", default=default_position): (
                        selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0,
                                max=100,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        )
                    )
                }
            ),
            description_placeholders={
                "selected_blind": str(self._developer_details()["name"]),
                "current_position": f"{default_position}%",
            },
        )

    def _prepare_existing_status_discovery(self) -> None:
        """Load the selected persisted blind as a discovery update target."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        self._pending_device_name = subentry.title
        self._pending_device_id = str(
            data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
        ).upper()
        self._pending_device_enum = (
            str(data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, ""))
            .upper()
            .zfill(2)
        )
        self._status_discovery_updates_existing = True

    def _apply_remote_status_discovery(self, result: dict[str, Any]) -> None:
        """Apply one guided capture result without aliasing transmit identity."""
        self._status_discovery_result = result
        primary = result.get("primary")
        if primary is None:
            self._pending_status_device_id = None
            self._pending_status_enum = None
            self._pending_status_identity_source = STATUS_IDENTITY_SOURCE_UNKNOWN
        else:
            self._pending_status_device_id = str(primary["device_id"])
            self._pending_status_enum = str(primary["enum"])
            self._pending_status_identity_source = (
                STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY
            )
        self._pending_secondary_status_identities = [
            {"device_id": str(group["device_id"]), "enum": str(group["enum"])}
            for group in result.get("secondary", [])
        ]

    def _status_discovery_placeholders(self) -> dict[str, str]:
        """Format a complete confirmation summary without requiring log access."""
        result = self._status_discovery_result or {}
        primary = result.get("primary")
        secondary = result.get("secondary", [])
        unknown = result.get("unknown_commands", [])
        unknown_label = _runtime_translation_text(self.hass, "unknown_label")
        none_word = _runtime_translation_text(self.hass, "none_word")
        return {
            "command_identity": (
                f"{self._pending_device_id or unknown_label}/"
                f"{self._pending_device_enum or '--'}"
            ),
            "primary_identity": (
                f"{primary['device_id']}/{primary['enum']}"
                if primary is not None
                else _runtime_translation_text(self.hass, "not_discovered")
            ),
            "primary_commands": (
                ", ".join(primary.get("commands", []))
                if primary is not None
                else none_word
            ),
            "primary_timestamps": (
                ", ".join(primary.get("timestamps", []))
                if primary is not None
                else none_word
            ),
            "secondary_identities": (
                "\n".join(
                    f"{group['device_id']}/{group['enum']}: "
                    f"{','.join(group.get('commands', []))}"
                    for group in secondary
                )
                or none_word
            ),
            "unknown_commands": (
                "\n".join(
                    f"{group['device_id']}/{group['enum']}: "
                    f"{','.join(group.get('commands', []))}"
                    for group in unknown
                )
                or none_word
            ),
            "position_tracking": (
                _runtime_translation_text(self.hass, "position_tracking_available")
                if primary is not None
                else _runtime_translation_text(
                    self.hass, "position_tracking_unavailable"
                )
            ),
            "frame_count": str(len(result.get("frames", []))),
        }

    async def async_step_discover_status(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Guide an original-remote sequence and capture all received identities."""
        if not self._pending_device_id:
            self._prepare_existing_status_discovery()
        unknown_label = _runtime_translation_text(self.hass, "unknown_label")
        placeholders = {
            "selected_blind": self._pending_device_name
            or _runtime_translation_text(self.hass, "device_generic"),
            "command_identity": (
                f"{self._pending_device_id or unknown_label}/"
                f"{self._pending_device_enum or '--'}"
            ),
        }
        if user_input is None:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
            )

        api = self._get_entry().runtime_data
        try:
            result = await api.async_discover_status_identities()
        except ConnectionError:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                errors={"base": "status_discovery_unavailable"},
                description_placeholders=placeholders,
            )
        except RuntimeError:
            return self.async_show_form(
                step_id="discover_status",
                data_schema=vol.Schema({}),
                errors={"base": "status_discovery_busy"},
                description_placeholders=placeholders,
            )
        self._apply_remote_status_discovery(result)
        return await self.async_step_confirm_status_discovery()

    async def async_step_confirm_status_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm detected primary/secondary identities before persistence."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm_status_discovery",
                data_schema=vol.Schema({}),
                description_placeholders=self._status_discovery_placeholders(),
            )

        if self._status_discovery_updates_existing:
            entry = self._get_entry()
            subentry = self._get_reconfigure_subentry()
            data = dict(subentry.data)
            if (
                self._pending_status_device_id is not None
                and self._pending_status_enum is not None
            ):
                # Discovery found something - this flow's whole purpose on
                # an already-configured blind is to let the person
                # deliberately (re)set its status identity via the
                # remote, so a clean result always applies, even if it
                # differs from whatever was there before.
                data[CONF_STATUS_IDENTITY_SOURCE] = self._pending_status_identity_source
                data[CONF_SECONDARY_STATUS_IDENTITIES] = list(
                    self._pending_secondary_status_identities
                )
                data[CONF_STATUS_DEVICE_ID] = self._pending_status_device_id
                data[CONF_STATUS_ENUM] = self._pending_status_enum
            elif (
                normalize_status_identity(
                    data.get(CONF_STATUS_DEVICE_ID), data.get(CONF_STATUS_ENUM)
                )
                is not None
                and data.get(CONF_STATUS_IDENTITY_SOURCE) != STATUS_IDENTITY_SOURCE_UNKNOWN
            ):
                # Discovery found nothing usable - wrong button, pressed
                # too fast, RF interference - which is an inconclusive
                # attempt, not a deliberate "clear this" action. Before
                # this fix, clicking "Save" anyway unconditionally popped
                # the existing, already-working CONF_STATUS_DEVICE_ID/
                # CONF_STATUS_ENUM with no warning at all, silently
                # erasing a working identity mapping - found during the
                # lifecycle audit round. Mirrors the kind of protection
                # recalibration's own async_step_calibration_complete()
                # (options_flow_calibration.py) already has for its own,
                # analogous "discovered nothing" case. Leave data
                # untouched: keeps the existing identity exactly as it was.
                _LOGGER.info(
                    "Status discovery for %s found nothing usable; "
                    "keeping the existing status identity instead of "
                    "erasing it",
                    self._pending_device_name or "unknown device",
                )
            else:
                # Nothing existing worth protecting - safe to record this
                # as unknown, same as always.
                data[CONF_STATUS_IDENTITY_SOURCE] = self._pending_status_identity_source
                data[CONF_SECONDARY_STATUS_IDENTITIES] = list(
                    self._pending_secondary_status_identities
                )
                data.pop(CONF_STATUS_DEVICE_ID, None)
                data.pop(CONF_STATUS_ENUM, None)
            return self.async_update_and_abort(entry, subentry, data=data)

        if (
            not self._pending_device_name
            or self._pending_open_time is None
            or self._pending_close_time is None
        ):
            return self.async_abort(reason="device_not_found")
        return self._finish_pairing(
            title=self._pending_device_name, data=self._pending_data()
        )

    async def async_step_teach_motor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Teach the USB transmitter to a motor, then send Open and Stop."""
        details = self._developer_details()
        if user_input is None:
            return self.async_show_form(
                step_id="teach_motor",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "selected_blind": str(details["name"]),
                    "command_device_id": details["command_device_id"],
                    "command_enum": details["command_enum"],
                },
            )

        api = self._get_entry().runtime_data
        if reason := api.transmit_block_reason:
            _LOGGER.error(
                "Motor teach blocked selected_blind=%s reason=%s",
                details["name"],
                reason,
            )
            self._developer_notice = _runtime_translation_text(
                self.hass,
                "notice_teach_blocked",
                reason=_translate_block_reason(self.hass, reason),
            )
            return await self.async_step_developer_tools()

        _LOGGER.warning(
            "Motor teach requested selected_blind=%s command_device_id=%s "
            "command_enum=%s status_device_id=%s status_enum=%s",
            details["name"],
            details["command_device_id"],
            details["command_enum"],
            details["status_device_id"],
            details["status_enum"],
        )
        try:
            taught = await api.teach_motor(
                details["command_enum"],
                device_id=details["command_device_id"],
                source="developer_tools",
            )
            opened = False
            stopped = False
            if taught:
                open_action = resolve_direction_command(
                    "open", details["invert_direction"]
                )
                opened = await api.control_blind_exclusive(
                    details["command_enum"],
                    open_action,
                    device_id=details["command_device_id"],
                    source="developer_tools",
                )
                if opened:
                    # See the matching comment in async_step_test_motor:
                    # wait for the stick's own t0 before starting the hold
                    # timer, so the follow-up stop below never lands while
                    # the open/close command is still physically going out.
                    await api.wait_for_transmitter_idle(
                        "finishing the teach-and-test move before stopping it"
                    )
                    await asyncio.sleep(TEST_COMMAND_DELAY)
                    stopped = await api.control_blind_exclusive(
                        details["command_enum"],
                        CMD_STOP,
                        device_id=details["command_device_id"],
                        source="developer_tools",
                    )
        except Exception:
            _LOGGER.exception(
                "Motor teach/test raised an exception selected_blind=%s",
                details["name"],
            )
            taught = opened = stopped = False

        if taught and opened and stopped:
            self._developer_notice = _runtime_translation_text(
                self.hass, "notice_teach_test_sent"
            )
        else:
            self._developer_notice = _runtime_translation_text(
                self.hass, "notice_teach_test_failed"
            )
        return await self.async_step_developer_tools()

    async def async_step_send_raw_command(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Validate and send an exact Schellenberg RF command payload."""
        details = self._developer_details()
        default_payload = f"ss{details['command_enum']}9{CMD_UP}0000"
        errors: dict[str, str] = {}
        if user_input is not None:
            payload = str(user_input.get("payload", "")).strip()
            _LOGGER.warning(
                "Developer Tools raw RF requested selected_blind=%s payload=%s",
                details["name"],
                payload,
            )
            try:
                sent = await self._get_entry().runtime_data.send_raw_transmit(
                    payload, source="developer_tools"
                )
            except ValueError:
                sent = False
                errors["payload"] = "invalid_raw_payload"
            if sent:
                self._developer_notice = _runtime_translation_text(
                    self.hass, "notice_raw_sent", payload=payload
                )
                return await self.async_step_developer_tools()
            if not errors:
                errors["base"] = "transmit_failed"

        return self.async_show_form(
            step_id="send_raw_command",
            data_schema=vol.Schema(
                {
                    vol.Required("payload", default=default_payload): (
                        selector.TextSelector()
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "selected_blind": str(details["name"]),
                "command_enum": details["command_enum"],
            },
        )

    async def async_step_reset_stick(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reset local stick state and reopen the serial connection."""
        api = self._get_entry().runtime_data
        ready = await api.reset_and_reconnect()
        self._developer_notice = (
            _runtime_translation_text(self.hass, "notice_reset_ready")
            if ready
            else _runtime_translation_text(
                self.hass,
                "notice_reset_not_ready",
                connected=_yes_no(self.hass, api.is_connected),
                mode=_translate_phrase(self.hass, api.device_mode or "unknown"),
            )
        )
        return await self.async_step_developer_tools()

    async def async_step_copy_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show a copyable text snapshot for troubleshooting."""
        if user_input is not None:
            return await self.async_step_developer_tools()

        (
            details,
            last_received,
            last_primary,
            last_secondary,
            last_position,
            last_manual_sync,
        ) = self._developer_snapshot()
        api = self._get_entry().runtime_data
        hass = self.hass

        def _t(key: str, **kwargs: Any) -> str:
            return _runtime_translation_text(hass, key, **kwargs)

        def _position_text(value: int | None) -> str:
            """Render a position percentage the same way the live panel does.

            Mirrors _developer_snapshot()'s own position_previous/
            position_new description placeholders below, which already
            guard this - this text export used to pass last_position's
            raw, possibly-None previous_position/new_position straight
            into _t(...) instead, rendering the literal word "None" here
            for a freshly set up or just-restarted blind (nothing
            recorded yet) where the live panel already showed "--"
            (audit finding 21). Applied to current_estimated_position
            too, just below - the same field as new_position, read a
            second time for the confidence section, and wrong there for
            the exact same reason.
            """
            return "--" if value is None else f"{value}%"

        diagnostics = "\n".join(
            (
                _t("diag_title"),
                _t("diag_selected_blind", value=details["name"]),
                "",
                _t("diag_section_stick_state"),
                _t("diag_connected", value=_yes_no(hass, api.is_connected)),
                _t(
                    "diag_mode",
                    value=_translate_phrase(hass, api.device_mode or "unknown"),
                ),
                _t("diag_ready", value=_yes_no(hass, api.transmit_ready)),
                _t("diag_pairing_active", value=_yes_no(hass, api.pairing_active)),
                _t(
                    "diag_transmitter_active",
                    value=_yes_no(hass, api.transmitter_active),
                ),
                _t("diag_busy_latched", value=_yes_no(hass, api.busy_latched)),
                "",
                _t("diag_section_ack_semantics"),
                _t("diag_ack_note_1"),
                _t("diag_ack_note_2"),
                "",
                _t("diag_section_last_matched_frame"),
                _t("diag_device_id", value=last_received["device_id"]),
                _t("diag_enum", value=last_received["enum"]),
                _t(
                    "diag_identity_role",
                    value=_translate_phrase(hass, last_received["identity_role"]),
                ),
                _t("diag_command", value=last_received["command"]),
                _t(
                    "diag_interpretation",
                    value=_translate_phrase(hass, last_received["interpreted_command"]),
                ),
                _t(
                    "diag_position_tracking_value",
                    value=_yes_no(hass, last_received["position_tracking"]),
                ),
                _t("diag_time", value=last_received["time"]),
                "",
                _t("diag_section_last_primary_frame"),
                _t("diag_device_id", value=last_primary["device_id"]),
                _t("diag_enum", value=last_primary["enum"]),
                _t("diag_command", value=last_primary["command"]),
                _t(
                    "diag_interpretation",
                    value=_translate_phrase(hass, last_primary["interpreted_command"]),
                ),
                _t("diag_time", value=last_primary["time"]),
                "",
                _t("diag_section_last_secondary_frame"),
                _t("diag_device_id", value=last_secondary["device_id"]),
                _t("diag_enum", value=last_secondary["enum"]),
                _t("diag_command", value=last_secondary["command"]),
                _t(
                    "diag_interpretation",
                    value=_translate_phrase(
                        hass, last_secondary["interpreted_command"]
                    ),
                ),
                _t("diag_time", value=last_secondary["time"]),
                "",
                _t("diag_section_last_position_update"),
                _t(
                    "diag_source",
                    value=_translate_phrase(
                        hass,
                        last_position.get("position_source", last_position["source"]),
                    ),
                ),
                _t(
                    "diag_details",
                    value=_translate_phrase(hass, last_position["source"]),
                ),
                _t(
                    "diag_direction",
                    value=_translate_phrase(hass, last_position["direction"]),
                ),
                _t(
                    "diag_previous_position",
                    value=_position_text(last_position["previous_position"]),
                ),
                _t(
                    "diag_new_position",
                    value=_position_text(last_position["new_position"]),
                ),
                _t(
                    "diag_status",
                    value=_translate_phrase(hass, last_position["status"]),
                ),
                _t("diag_time", value=last_position["time"]),
                "",
                _t("diag_section_position_confidence"),
                _t(
                    "diag_current_estimated_position",
                    value=_position_text(last_position["new_position"]),
                ),
                _t("diag_last_manual_sync_time", value=last_manual_sync["time"]),
                _t(
                    "diag_confidence",
                    value=_translate_phrase(hass, last_position["status"]),
                ),
                _t(
                    "diag_confirmed_since_restart",
                    value=_yes_no(
                        hass, bool(last_position.get("confirmed_since_restart"))
                    ),
                ),
                "",
                _t("diag_section_current_transmit_target"),
                _t("diag_device_id", value=details["command_device_id"]),
                _t("diag_enum", value=details["command_enum"]),
                "",
                _t("diag_section_configured_primary_identity"),
                _t("diag_device_id", value=details["primary_status_device_id"]),
                _t("diag_enum", value=details["primary_status_enum"]),
                _t("diag_source", value=details["status_identity_source"]),
                _t("diag_section_configured_secondary_identities"),
                details["secondary_status_identities_text"],
                "",
                _t("diag_section_last_calibration_run"),
                _t("diag_completed", value=details["last_calibration_time"]),
                _t("diag_end_reason", value=details["calibration_end_reason"]),
                _t("diag_frames_observed"),
                details["calibration_frames_text"],
                _t("diag_candidate_identities"),
                details["calibration_candidates_text"],
                _t("diag_open_time", value=f"{details['open_time']:.2f}"),
                _t("diag_close_time", value=f"{details['close_time']:.2f}"),
                _t(
                    "diag_invert_direction",
                    value=_yes_no(hass, details["invert_direction"]),
                ),
            )
        )
        return self.async_show_form(
            step_id="copy_diagnostics",
            data_schema=vol.Schema(
                {
                    vol.Required("diagnostics", default=diagnostics): (
                        selector.TextSelector(
                            selector.TextSelectorConfig(multiline=True)
                        )
                    )
                }
            ),
        )

    async def async_step_test_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Load an existing blind into the short command test."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        self._pending_device_name = subentry.title
        # Normalized the same way as _prepare_existing_status_discovery()
        # below, so both entry points into the short command test present
        # identities consistently regardless of the case they happen to be
        # stored in.
        self._pending_device_id = str(
            data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
        ).upper()
        self._pending_device_enum = (
            str(data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, ""))
            .upper()
            .zfill(2)
        )
        self._pending_status_device_id = str(
            data.get(CONF_STATUS_DEVICE_ID) or self._pending_device_id
        ).upper()
        self._pending_status_enum = str(
            data.get(CONF_STATUS_ENUM) or self._pending_device_enum
        ).upper()
        self._pending_secondary_status_identities = serialize_status_identities(
            normalize_status_identities(data.get(CONF_SECONDARY_STATUS_IDENTITIES))
        )
        self._pending_open_time = float(
            data.get(CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME_SECONDS)
        )
        self._pending_close_time = float(
            data.get(CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME_SECONDS)
        )
        self._pending_invert_direction = bool(data.get(CONF_INVERT_DIRECTION, False))
        self._pairing_workflow = "existing"
        return await self.async_step_test_motor(user_input)

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a blind while preserving its subentry and entity unique IDs."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        current_data = dict(subentry.data)
        command_device_id = str(
            current_data.get(CONF_COMMAND_DEVICE_ID)
            or current_data.get(CONF_DEVICE_ID, "")
        )
        command_enum = str(
            current_data.get(CONF_COMMAND_ENUM)
            or current_data.get(CONF_DEVICE_ENUM, "")
        )
        current_status_source = current_data.get(CONF_STATUS_IDENTITY_SOURCE)
        if current_status_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
            status_device_id = str(current_data.get(CONF_STATUS_DEVICE_ID) or "")
            status_enum = str(current_data.get(CONF_STATUS_ENUM) or "")
        else:
            status_device_id = str(
                current_data.get(CONF_STATUS_DEVICE_ID) or command_device_id
            )
            status_enum = str(current_data.get(CONF_STATUS_ENUM) or command_enum)
        secondary_status_text = format_status_identities(
            current_data.get(CONF_SECONDARY_STATUS_IDENTITIES)
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            device_name = str(user_input[CONF_DEVICE_NAME]).strip()
            command_device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
            command_enum = str(user_input[CONF_DEVICE_ENUM]).strip().upper()
            status_device_id = (
                str(user_input.get(CONF_STATUS_DEVICE_ID, "")).strip().upper()
            )
            status_enum = str(user_input.get(CONF_STATUS_ENUM, "")).strip().upper()
            secondary_status_text = str(
                user_input.get(CONF_SECONDARY_STATUS_IDENTITIES, "")
            )
            try:
                secondary_identities = parse_status_identities_text(
                    secondary_status_text
                )
            except ValueError:
                secondary_identities = ()
                errors[CONF_SECONDARY_STATUS_IDENTITIES] = "invalid_status_identities"
            primary_identity = (
                (status_device_id, status_enum)
                if status_device_id and status_enum
                else None
            )
            secondary_identities = tuple(
                identity
                for identity in secondary_identities
                if primary_identity is None or identity != primary_identity
            )
            open_time = float(user_input[CONF_OPEN_TIME_SECONDS])
            close_time = float(user_input[CONF_CLOSE_TIME_SECONDS])

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"
            if not self._is_hex_value(command_device_id, 6):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            if not self._is_hex_value(command_enum, 2):
                errors[CONF_DEVICE_ENUM] = "invalid_device_enum"
            if bool(status_device_id) != bool(status_enum):
                errors[
                    CONF_STATUS_ENUM if status_device_id else CONF_STATUS_DEVICE_ID
                ] = "status_identity_incomplete"
            if status_device_id and not self._is_hex_value(status_device_id, 6):
                errors[CONF_STATUS_DEVICE_ID] = "invalid_device_id"
            if status_enum and not self._is_hex_value(status_enum, 2):
                errors[CONF_STATUS_ENUM] = "invalid_device_enum"
            # This form's schema deliberately has no selector-level `min`
            # for either field, so a non-positive value reaches here
            # instead of being rejected earlier as a raw, untranslated
            # schema error that would abort the whole flow rather than
            # re-showing this form with a normal, translated field error.
            if open_time <= 0:
                errors[CONF_OPEN_TIME_SECONDS] = "invalid_travel_time"
            if close_time <= 0:
                errors[CONF_CLOSE_TIME_SECONDS] = "invalid_travel_time"

            if any(
                candidate.subentry_id != subentry.subentry_id
                and str(
                    candidate.data.get(CONF_COMMAND_DEVICE_ID)
                    or candidate.data.get(CONF_DEVICE_ID, "")
                ).upper()
                == command_device_id
                for candidate in entry.subentries.values()
            ):
                errors[CONF_DEVICE_ID] = "already_configured"

            # The command_device_id check above has no equivalent for the
            # primary status identity: _register_status_identity() in
            # api.py silently reassigns a status identity from one entity
            # to another at runtime (only a _LOGGER.warning, no UI error, no
            # repair issue - this integration never uses HA's issue
            # registry), so two subentries could otherwise end up listening
            # for the same device_id/enum with no indication anywhere in
            # the UI, and whichever registers last steals the other's
            # position-tracking frames. A candidate whose own status source
            # is STATUS_IDENTITY_SOURCE_UNKNOWN is skipped: it has no real,
            # confirmed status identity to collide with (see the matching
            # STATUS_IDENTITY_SOURCE_UNKNOWN check this function already
            # does above when pre-filling this same subentry's own display
            # value) - treating its defaulted-to-command fallback as a real
            # identity here would produce false-positive collisions against
            # perfectly normal setups where a *different* blind's status
            # identity legitimately equals its own command identity. Only
            # the primary identity is covered here - secondary status
            # identities are not cross-checked against other subentries.
            if status_device_id and status_enum:
                new_status_identity = (status_device_id, status_enum)
                if any(
                    candidate.subentry_id != subentry.subentry_id
                    and candidate.data.get(CONF_STATUS_IDENTITY_SOURCE)
                    != STATUS_IDENTITY_SOURCE_UNKNOWN
                    and (
                        str(
                            candidate.data.get(CONF_STATUS_DEVICE_ID)
                            or candidate.data.get(CONF_COMMAND_DEVICE_ID)
                            or candidate.data.get(CONF_DEVICE_ID, "")
                        ).upper(),
                        str(
                            candidate.data.get(CONF_STATUS_ENUM)
                            or candidate.data.get(CONF_COMMAND_ENUM)
                            or candidate.data.get(CONF_DEVICE_ENUM, "")
                        ).upper(),
                    )
                    == new_status_identity
                    for candidate in entry.subentries.values()
                ):
                    errors[CONF_STATUS_DEVICE_ID] = "already_configured"

            if not errors:
                current_data.update(
                    {
                        CONF_DEVICE_ID: command_device_id,
                        CONF_DEVICE_ENUM: command_enum,
                        CONF_COMMAND_DEVICE_ID: command_device_id,
                        CONF_COMMAND_ENUM: command_enum,
                        CONF_STATUS_IDENTITY_SOURCE: (
                            STATUS_IDENTITY_SOURCE_MANUAL
                            if status_device_id and status_enum
                            else STATUS_IDENTITY_SOURCE_UNKNOWN
                        ),
                        CONF_SECONDARY_STATUS_IDENTITIES: (
                            serialize_status_identities(secondary_identities)
                        ),
                        CONF_OPEN_TIME: open_time,
                        CONF_CLOSE_TIME: close_time,
                        CONF_INVERT_DIRECTION: bool(
                            user_input.get(CONF_INVERT_DIRECTION, False)
                        ),
                    }
                )
                if status_device_id and status_enum:
                    current_data[CONF_STATUS_DEVICE_ID] = status_device_id
                    current_data[CONF_STATUS_ENUM] = status_enum
                else:
                    current_data.pop(CONF_STATUS_DEVICE_ID, None)
                    current_data.pop(CONF_STATUS_ENUM, None)
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=device_name,
                    data=current_data,
                )

        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=subentry.title,
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=command_device_id,
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_DEVICE_ENUM,
                        default=command_enum,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_STATUS_DEVICE_ID,
                        default=status_device_id,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_STATUS_ENUM,
                        default=status_enum,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_SECONDARY_STATUS_IDENTITIES,
                        default=secondary_status_text,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    # No `min` here either - see the matching comment on
                    # this step's own `invalid_travel_time` validation above.
                    vol.Required(
                        CONF_OPEN_TIME_SECONDS,
                        default=current_data.get(
                            CONF_OPEN_TIME, DEFAULT_TRAVEL_TIME_SECONDS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_CLOSE_TIME_SECONDS,
                        default=current_data.get(
                            CONF_CLOSE_TIME, DEFAULT_TRAVEL_TIME_SECONDS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_INVERT_DIRECTION,
                        default=current_data.get(CONF_INVERT_DIRECTION, False),
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_calibrate(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Run calibration for the selected blind subentry."""
        handler = self._get_calibration_handler()
        handler.disable_subentry_creation()

        subentry = self._get_reconfigure_subentry()
        command_device_id = subentry.data.get(
            CONF_COMMAND_DEVICE_ID, subentry.data.get(CONF_DEVICE_ID)
        )
        command_enum = subentry.data.get(
            CONF_COMMAND_ENUM, subentry.data.get(CONF_DEVICE_ENUM)
        )
        status_device_id = subentry.data.get(CONF_STATUS_DEVICE_ID, command_device_id)
        status_enum = subentry.data.get(CONF_STATUS_ENUM, command_enum)
        # The enum halves need the same guard as the device_id halves just
        # above: a subentry missing both CONF_COMMAND_ENUM/CONF_DEVICE_ENUM
        # (or their status equivalents) leaves command_enum/status_enum
        # None, which would otherwise reach handler.set_selected_device()
        # and from there the serial payload itself unchecked.
        if (
            not command_device_id
            or not status_device_id
            or not command_enum
            or not status_enum
        ):
            return self.async_abort(reason="device_not_found")

        stable_id = (
            subentry.unique_id
            if isinstance(subentry.unique_id, str) and subentry.unique_id
            else command_device_id
        )
        device_name = subentry.title or _runtime_translation_text(
            self.hass, "device_fallback_name", device_id=stable_id
        )
        handler.set_selected_device(
            {
                "id": status_device_id,
                "entity_id": stable_id,
                "name": device_name,
                CONF_OPEN_TIME: subentry.data.get(CONF_OPEN_TIME),
                CONF_CLOSE_TIME: subentry.data.get(CONF_CLOSE_TIME),
                CONF_INVERT_DIRECTION: subentry.data.get(CONF_INVERT_DIRECTION, False),
                "enum": status_enum,
                # Excludes only this same subentry - its own not-yet-
                # reconfirmed identity must never be treated as "foreign"
                # (see _foreign_capture_device_ids()'s own docstring).
                "foreign_capture_device_ids": _foreign_capture_device_ids(
                    self._get_entry(), exclude_subentry_id=subentry.subentry_id
                ),
            }
        )

        return await self._await_subentry_result(
            handler.async_step_calibration_close(user_input)
        )

    # Delegate all calibration steps to the handler
    async def async_step_calibration_close(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_close(user_input)
        )

    async def async_step_calibration_open_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_open_instruction(user_input)
        )

    async def async_step_calibration_close_instruction(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_close_instruction(user_input)
        )

    async def async_step_calibration_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Delegate to calibration handler (handler now creates entry)."""
        handler = self._get_calibration_handler()
        return await self._await_subentry_result(
            handler.async_step_calibration_complete(user_input)
        )
