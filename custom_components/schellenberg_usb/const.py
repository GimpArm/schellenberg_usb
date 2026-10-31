"""Constants for the Schellenberg USB integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import SchellenbergUsbApi

# The domain of your integration. Should be unique.
DOMAIN = "schellenberg_usb"

# Type alias for config entry with runtime data
#
# NOTE: the `type X = Y` syntax below is a PEP 695 type alias statement,
# available only on Python 3.12+ (it raises SyntaxError on 3.11 and
# earlier). Home Assistant's own minimum supported Python version already
# satisfies this, but it means this file (and identities.py /
# options_flow_calibration.py, which use the same syntax) cannot be
# parsed or imported by a 3.11 interpreter, e.g. in a dev/test tool that
# pins an older Python.
type SchellenbergConfigEntry = ConfigEntry[SchellenbergUsbApi]

# Platforms this hub forwards entity setup to. Platform is a StrEnum, so
# each member still compares/hashes equal to its plain string value
# ("cover", "sensor", "switch") for any existing code or test that checks
# membership that way.
PLATFORMS = [Platform.COVER, Platform.SENSOR, Platform.SWITCH]

# Subentry types
SUBENTRY_TYPE_HUB = "hub"
SUBENTRY_TYPE_BLIND = "blind"

# Configuration keys
CONF_SERIAL_PORT = "serial_port"
CONF_BLIND_ID = "blind_id"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_ENUM = "device_enum"
CONF_COMMAND_DEVICE_ID = "command_device_id"
CONF_COMMAND_ENUM = "command_enum"
CONF_STATUS_DEVICE_ID = "status_device_id"
CONF_STATUS_ENUM = "status_enum"
CONF_SECONDARY_STATUS_IDENTITIES = "secondary_status_identities"
CONF_STATUS_IDENTITY_SOURCE = "status_identity_source"
CONF_LAST_CALIBRATION = "last_calibration"
STATUS_IDENTITY_SOURCE_CALIBRATION = "calibration"
STATUS_IDENTITY_SOURCE_REMOTE_DISCOVERY = "remote_discovery"
STATUS_IDENTITY_SOURCE_MANUAL = "manual"
STATUS_IDENTITY_SOURCE_UNKNOWN = "unknown"
CONF_INVERT_DIRECTION = "invert_direction"
CONF_COMMAND = "command"
CONF_ENUM = "enum"
CONF_CONFIG_ENTRY_ID = "config_entry_id"
# Form field keys for the manual/edit config-subentry steps only. The values
# entered under these keys are converted to float seconds and stored under
# the *different* keys CONF_OPEN_TIME / CONF_CLOSE_TIME below (defined near
# the other calibration constants) once they land in subentry.data - the
# "_SECONDS" suffix marks these as the schema/form field names, not the
# storage keys. Kept separate rather than merged into a single constant
# because the manual/edit forms and the calibration flow serialize the same
# quantity slightly differently; see cover.py and options_flow_calibration.py
# for the read side.
CONF_OPEN_TIME_SECONDS = "open_time_seconds"
CONF_CLOSE_TIME_SECONDS = "close_time_seconds"

# Device commands (Schellenberg protocol) - for controlling devices
CMD_STOP = "00"  # 0x00 - Stop
CMD_UP = "01"  # 0x01 - Up
CMD_DOWN = "02"  # 0x02 - Down
CMD_ALLOW_PAIRING = "40"  # 0x40 - Allow Pairing (make device listen to new remote)
CMD_PAIR = "60"  # 0x60 - Pair with device / Change rotation direction


def resolve_direction_command(logical_direction: str, invert_direction: bool) -> str:
    """Return the physical CMD_UP/CMD_DOWN byte for one logical direction.

    "open" normally sends CMD_UP and "close" normally sends CMD_DOWN; a
    device whose CONF_INVERT_DIRECTION flag is set gets the opposite byte
    for each, since its motor is wired/oriented so that "up" on the wire
    drives it closed instead of open. Centralizes a mapping that used to be
    reimplemented independently at five call sites (cover.py's
    async_open_cover/async_close_cover, and config_flow.py's test_motor,
    _async_developer_command, and teach_motor steps) so a future change
    only has to happen once, instead of risking the sites drifting out of
    sync with each other.
    """
    if logical_direction == "open":
        return CMD_DOWN if invert_direction else CMD_UP
    if logical_direction == "close":
        return CMD_UP if invert_direction else CMD_DOWN
    raise ValueError(f"Unknown logical direction: {logical_direction!r}")


# Motor events from stick (same as some commands)
EVENT_STARTED_MOVING_UP = "01"
EVENT_STARTED_MOVING_DOWN = "02"
EVENT_STOPPED = "00"

# USB Stick system commands (prefixed with !)
CMD_VERIFY = "!?"  # Get version and current mode

# USB Stick lowercase commands (for various functions)
CMD_LED_ON = "so+"  # Turn LED on
CMD_LED_OFF = "so-"  # Turn LED off
CMD_GET_DEVICE_ID = "sr"  # Get device ID
CMD_GET_PARAM_P = "sp"  # Get parameters P

# Command prefixes
CMD_TRANSMIT = "ss"  # Schellenberg transmit prefix for device commands

# Dispatcher signals
SIGNAL_DEVICE_EVENT = f"{DOMAIN}_device_event"
# Broadcast for every incoming "ss" frame while a status-frame capture
# window is open (calibration, remote-status discovery, teach_motor),
# regardless of which device_id the frame itself carries. The capture
# window exists precisely because the device being calibrated/taught may
# not yet have a confirmed status identity, and that status identity can
# differ from the command/pairing identity learned during pairing (see
# README.md's "Advanced protocol notes"). SIGNAL_DEVICE_EVENT above is
# keyed to one specific device_id and therefore never fires for such a
# device; this capture-wide signal is the fallback that lets
# options_flow_calibration.py's movement-wait listeners still see the
# device's real status frames during that single active capture window.
SIGNAL_DEVICE_EVENT_CAPTURE = f"{DOMAIN}_device_event_capture"
SIGNAL_DEVICE_PAIRED = f"{DOMAIN}_device_paired"
SIGNAL_PAIRING_STARTED = f"{DOMAIN}_pairing_started"
SIGNAL_PAIRING_TIMEOUT = f"{DOMAIN}_pairing_timeout"
SIGNAL_STICK_STATUS_UPDATED = f"{DOMAIN}_stick_status_updated"
SIGNAL_CALIBRATION_COMPLETED = f"{DOMAIN}_calibration_completed"
SIGNAL_MANUAL_POSITION_SYNC = f"{DOMAIN}_manual_position_sync"


# Services
SERVICE_TEST_COMMAND = "test_command"
TEST_COMMAND_DELAY = 0.75

# Measured on real hardware (t1-to-t0 across the stick's own transmit cycle -
# see the pairing-test fix's own comments in config_flow.py for the same
# figure): one open/close/stop frame consistently takes roughly 1.1-1.2s to
# transmit end to end. Used on two, symmetric sides of the same elapsed-time
# position estimate in cover.py:
#
# - The stop side: set_cover_position's tracking loop fires its follow-up
#   stop in software once the elapsed-time estimate reaches the target, but
#   the blind keeps moving under the already-issued open/close command for
#   about this long before that stop is actually transmitted and acted on -
#   uncompensated, that made any target within roughly this fraction of a
#   blind's own travel time of either end stop unreachable: the earliest a
#   stop could physically land was already past it.
# - The start side: async_open_cover()/async_close_cover() push their own
#   optimistic _move_start_time this far into the future rather than
#   leaving it at plain time.monotonic(), for the mirror-image reason - the
#   stick accepting an open/close write is not the same instant the motor
#   physically starts moving, and that gap used to run the position
#   estimate ahead of the real motor for the rest of the move, invisible
#   for a plain 0/100 open/close (the real end-stop echo corrects
#   everything regardless) but making an intermediate target's auto-stop
#   fire measurably early. See both call sites' own comments.
#
# Both compensations are deliberately the same figure, not two separately-
# tuned constants, because the underlying hardware latency measured above
# is already described as applying to "one open/close/stop frame"
# generically - a real difference between the two sides has never been
# measured, so splitting them would just be two names for one number.
# _async_position_update_loop() widens the stop-side use further still,
# only when api.py's move_lock_busy reports contention, for the time this
# blind's own stop can additionally spend queued behind another blind's
# move/stop sharing the same transmit lock - see that call site's own
# comment for the real-log numbers behind it. Kept as one named constant,
# rather than inline in cover.py, so every call site stays in sync and a
# real-hardware test on a different stick/motor combination only needs to
# change one number.
STOP_COMMAND_LATENCY_SECONDS = 1.2

# Sanity ceiling on the latency-compensation lead that STOP_COMMAND_
# LATENCY_SECONDS above feeds into (stop_lead_percent, in
# _async_position_update_loop() in cover.py), expressed in the same
# percentage points as that lead. Not a tuned value, like the travel-time
# floor (0.1s) that same calculation's divisor already has - for any real
# shutter, whose travel time is measured in seconds to tens of seconds,
# the lead this produces is a small single-digit percentage, nowhere near
# this cap. It only ever binds for a travel time so short or corrupted
# that the uncapped lead would run into the hundreds of percent, pushing
# the comparison it feeds so far past the valid 0-100 range that any
# position at all would satisfy "already there" the instant a move
# starts, regardless of how far from the real target it actually was
# (audit finding 18 - unlike that divisor floor, this had no matching
# ceiling at all before).
MAX_STOP_LEAD_PERCENT = 50.0

# How long cover.py's own optimistic guess from a secondary identity's
# frame (_handle_secondary_activity) is allowed to stand before it is
# withdrawn if nothing ever confirms it. This exists because decoding a
# secondary frame's own command byte to learn its real direction has been
# tried twice on real Gurtwickler-remote hardware and failed both times
# (see commits e911236 and 1db9e69) - so that byte is deliberately never
# inspected at all any more. Instead, the guess is corrected for free the
# moment the primary status identity's own echo of the same physical move
# arrives - _handle_event()'s STARTED_MOVING branch overwrites is_opening/
# is_closing with the real, reliably decoded direction and starts real
# tracking, exactly as it already does for any other externally-triggered
# move. This timeout only covers the case that echo never arrives at all
# (the same real-world RF loss this project's own debug logs have shown
# happens occasionally) - long enough that a genuinely-landing-late echo
# is not preempted, short enough that a wrong guess with nothing left to
# correct it does not stand for long. Tune here only; cover.py has no
# literal of its own.
OPTIMISTIC_SECONDARY_MOVE_TIMEOUT_SECONDS = 5.0

# Device verification
VERIFY_TIMEOUT = 5  # seconds to wait for verification response
# Expected response format: RFTU_V20 F:20180510_DFBD B:1

# Pairing constants
PAIRING_TIMEOUT = 120  # seconds to wait for pairing response
PAIRING_DEVICE_ENUM_START = 0x10  # Start from 0x10 for new devices
STATUS_DISCOVERY_TIMEOUT = 45  # seconds to capture original-remote frames

# Calibration constants
CALIBRATION_TIMEOUT = 300  # Maximum 5 minutes (300 seconds) for calibration
# Fallback travel time (seconds) used whenever a blind has no calibrated/manual
# open or close time yet. Shared by cover.py (runtime default) and
# config_flow.py (form defaults shown before a blind has been calibrated) so
# both stay in sync instead of each hardcoding the same literal separately.
DEFAULT_TRAVEL_TIME_SECONDS = 60.0
# Storage keys actually persisted in subentry.data (see CONF_OPEN_TIME_SECONDS
# / CONF_CLOSE_TIME_SECONDS above for the corresponding form field keys).
CONF_OPEN_TIME = "open_time"  # Time it takes to open (up) in seconds
CONF_CLOSE_TIME = "close_time"  # Time it takes to close (down) in seconds
# The blind's actual physical position (0 closed / 100 open) once calibration
# - which now lets the person choose whether open or close is driven first,
# see options_flow_calibration.py's calibration_close step - has finished.
# Only ever 0 or 100: calibration always ends at whichever end stop the
# second/last leg drove to. Only present on a subentry created fresh from a
# pair_device calibration; cover.py falls back to 0 when it is absent, which
# covers every blind paired before this key existed and the hybrid
# (add_shutter_from_remote) workflow, which never runs this calibration flow.
CONF_INITIAL_POSITION = "initial_position"
# Legacy top-level command-device-ID key from before CONF_COMMAND_DEVICE_ID
# and the separate status identity existed. Despite living in this
# "calibration constants" block (for historical reasons), it is read as a
# fallback throughout config_flow.py and cover.py, not just during
# calibration - see e.g. cover.py's legacy_device_id handling.
CONF_DEVICE_ID = "device_id"
