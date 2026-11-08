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
# transmit end to end.
#
# _async_position_update_loop() in cover.py (stop_lead_percent) is the one
# place this figure's start-side and stop-side roles both matter at once -
# see that call site's own comment for the full derivation (Issue #78,
# 2026-11-01 originally, corrected 2026-11-07 after an intervening version
# double-counted them). Summary: this same measured figure models both how
# long after accepting an open/close write the real motor takes to actually
# start moving (D_start) and how long after a stop command is actually
# issued the real motor takes to actually stop (D_stop). Uncontended, D_start
# and D_stop are equal and enter stop_lead_percent's own baseline with
# opposite sign, so they cancel out of it completely - this constant doesn't
# appear in that baseline at all any more. It still widens that same lead
# further when api.py's move_lock_busy reports contention, for the time this
# blind's own stop can additionally spend queued behind another blind's
# move/stop sharing the same transmit lock (D_stop grows under contention,
# D_start doesn't, so the cancellation above no longer holds) - see that
# call site's own comment for the real-log numbers behind it.
#
# Kept as one named constant, rather than inline at each call site, because
# the underlying hardware latency measured above is already described as
# applying to "one open/close/stop frame" generically - a real difference
# between D_start and D_stop has never been measured, so splitting them
# would just be two names for one number - and so a real-hardware test on a
# different stick/motor combination only needs to change one number.
STOP_COMMAND_LATENCY_SECONDS = 1.2

# Sanity ceiling on the latency-compensation lead that stop_lead_percent
# (in _async_position_update_loop() in cover.py) computes, expressed in
# the same percentage points as that lead. Not a tuned value, like the
# travel-time floor (0.1s) that same calculation's divisor already has.
# Real calibrated travel times stay well clear of it: the project owner's
# own six blinds range 8.45-23.27s (2026-11-07), and even the shortest of
# those, contended, only reaches about 20 points (1.7s / 8.45s - see
# STOP_COMMAND_LATENCY_SECONDS's own comment for where that 1.7s comes
# from) - comfortably under this 50-point cap. A blind would need a real
# open/close travel time under roughly 3.4s to hit this cap on a
# contended stop with an entirely genuine calibration - mainly a guard,
# in practice, against corrupt/legacy config data (a near-zero or wildly
# wrong travel time), same as the divisor floor above it, rather than
# something a real blind's own calibration is expected to approach.
# Binds hardest for a travel time so short or corrupted that the
# uncapped lead would run into the hundreds of percent, pushing the
# comparison it feeds so far past the valid 0-100 range that any
# position at all would satisfy "already there" the instant a move
# starts, regardless of how far from the real target it actually was -
# unlike that divisor floor, this has no matching ceiling at all
# otherwise.
MAX_STOP_LEAD_PERCENT = 50.0

# How many *additional* stop frames control_blind_stop() fires, as plain,
# best-effort control_blind() writes with no lock and no ack-wait of their
# own, once the primary, fully-serialized-and-acked stop above has already
# been accepted by the stick. Exists because "accepted by the stick" is as
# far as this integration can ever see: every transmit, stop included, is
# logged with motor_result=unknown, since the stick only confirms it put
# the frame on the air, never that the motor's receiver actually caught
# it - this protocol has no end-to-end delivery confirmation at all, only
# ever the one-hop stick ack already handled above. In most RF environments
# that single frame is enough, which is why this integration has run on
# exactly one stop frame since control_blind_stop() first existed. It is
# not enough in a strong-RF-interference environment: a real six-blind
# "close to an intermediate target" batch, run back to back through the
# shared transmitter under exactly that kind of interference, left two of
# the six still closing with no visible stop at all - not merely late,
# which the contention-aware lead above already compensates for, but
# missing outright, each riding on past its target all the way to its
# physical end stop while every other blind in the same batch landed
# close to target. A second stop frame is harmless if the first one
# already landed - this blind is either already stopped, in which case
# another stop is a no-op exactly like a second press of a physical
# remote's own stop button, or a brand new move for the very same blind
# has started in the meantime, in which case that move's own retry loop
# (control_blind_with_retry()) is already serialized against this same
# _move_lock and simply queues behind these extra writes for the ~1-2
# transmit cycles they take, rather than racing them. Originally kept to
# one extra frame, not several, for exactly that contention reason: each
# additional write still costs one more shared-transmitter slot that
# every other blind in the same batch is queued behind.
#
# Raised from one extra frame to two (2026-11-07, project owner's own
# direct decision, the same high-RF-interference environment the real-
# world report above came from): the contention cost described above is
# real and still applies - a third frame queues behind other blinds the
# same way a second one does - but is being accepted further, for
# additional margin against exactly the failure mode this section opened
# with, in an environment that keeps producing it. Harmless for the same
# reason a second frame already was: a third plain repeat of the
# identical stop write is still either a no-op (blind already stopped) or
# queues safely behind a genuinely new move (as above) - nothing about
# going from one extra frame to two changes which of those two cases
# applies, only how many times the harmless one repeats.
STOP_COMMAND_EXTRA_RESENDS = 2

# How long control_blind_stop() waits after its primary, acked stop before
# firing each of the extra resends above, so they do not immediately
# follow the first frame into what may be the same brief interference
# window that cost it. Not tied to STOP_COMMAND_LATENCY_SECONDS - that
# figure models a *delay already on the wire* (contention, transmit time),
# this is a deliberately-introduced gap on an otherwise-idle transmitter,
# a different thing being measured for a different reason. Short enough
# that the blind has travelled only a negligible, sub-percent distance
# past where the primary stop already landed it by the time the resend
# goes out.
STOP_COMMAND_RESEND_DELAY_SECONDS = 0.25

# How long async_stop_cover() additionally waits, once control_blind_
# stop() has returned StopOutcome.CONFIRMED, for this same blind's own
# primary-identity SS status-frame to echo the stop back before treating
# the position estimate as genuinely motor-confirmed rather than merely
# stick-confirmed - closing, for the one call site that can afford the
# wait, exactly the gap STOP_COMMAND_EXTRA_RESENDS's own comment above
# already describes: CONFIRMED there only ever meant "the stick put the
# frame on the air" (motor_result=unknown, always), never that the motor
# itself actually received and acted on it.
#
# 2.0s, not a shorter figure: comfortably above STOP_COMMAND_LATENCY_
# SECONDS' own 1.2s measured mechanical-stop latency, with the status
# frame's own transmit-and-parse time still to come on top of that. A
# real log gathered for this same evaluation (2026-11-07) showed a
# Gurtwickler's secondary-identity "button held" repeats, sent roughly
# every 0.11s, followed by the matching primary-identity confirmation
# (for both a stop and a later open on the same blind) only 0.55-0.89s
# after the last repeat - comfortably under this 2.0s ceiling, which
# leaves real margin rather than being tuned to the exact figure
# observed in one sample.
#
# Bounded and best-effort only, same spirit as STOP_RETRY_BUDGET and
# _wait_for_transmitter_idle() elsewhere: this never holds _move_lock
# (control_blind_stop() has already released it by the time this runs)
# and never turns a missing frame into a retry or a hang - on timeout,
# async_stop_cover() falls back to exactly the stick-ack-only confirmed
# behavior it always had, so an RF-interference episode that drops the
# motor's own echo frame still leaves every command processed normally,
# just without the extra confidence this adds when the frame does
# arrive.
#
# Deliberately not extended to control_blind_with_retry()'s own move
# confirmation: the same evaluation found the matching primary-identity
# confirmation for a *move* lagging materially further behind the
# triggering action than a stop's does (same real log: on the order of
# 1.4s from the first secondary repeat to the primary open confirmation,
# against a much shorter hold-to-confirm gap for the stop case above) -
# almost certainly the physical button needing to be held for a real
# stretch before the motor commits to a move at all, not anything this
# integration's own code could shorten. Waiting on it there would cost
# materially more latency per move for no corresponding gain, and risks
# a double-send if MOVE_RETRY_OVERALL_TIMEOUT's own retry fires before
# a delayed frame ever catches up. Decided with the project owner
# (2026-11-07) to scope this to the stop side only for exactly that
# reason.
MANUAL_STOP_MOTOR_CONFIRMATION_GRACE_SECONDS = 2.0

# Total time _async_position_update_loop()'s auto-stop-at-target branch
# keeps retrying control_blind_stop() for one target-reached episode
# before giving up and reporting the position as unknown instead of a
# stale number (see StopOutcome in api.py). This is deliberately much
# longer than api.py's own STOP_RETRY_BUDGET (2.0s), which only bounds
# one single call to control_blind_stop() - this constant bounds this
# loop calling it again, and again, once per ~0.2s tick, across however
# many of those single calls it takes. The two have different jobs:
# STOP_RETRY_BUDGET exists so a *manual* stop (async_stop_cover()) stays
# responsive for whoever just pressed it; this loop has no one waiting
# on it synchronously, so it can afford to keep trying for much longer
# before concluding the blind's own fate is genuinely unknown - a real
# six-blind Alexa "25%" command (2026-11-01) produced exactly one
# STOP_RETRY_BUDGET-exhausted UNCONFIRMED outcome, after which the
# fallback write still very plausibly reached the stick (most of what
# exhausts STOP_RETRY_BUDGET is lock/transmitter contention, not an
# actually-unreachable stick), so declaring the position unknown after
# only one more 2.0s attempt, the way the old code effectively did by
# mistake, would throw away a real answer a few seconds later would
# have given it. Not unbounded either: nothing here can distinguish
# "still just contended" from "this blind stopped answering entirely",
# and an entity whose auto-stop never gives up looks, from the dashboard,
# exactly like one that is still confidently tracking a position it can
# no longer vouch for. A drop to 15.0 was considered (2026-11-01) and
# explicitly decided against (2026-11-07, project owner's own direction)
# in favour of keeping the full margin above - noted here so this isn't
# mistaken for an overlooked follow-up later.
AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS = 30.0

# Hard ceiling on how long _async_position_update_loop() itself may run for
# one continuous tracking episode (captured once, when the loop's own task
# starts, and checked every ~0.2s tick) before it gives up entirely and
# reports the position as unknown - added as part of the Issue #78 fix
# (2026-11-07, confirmed directly with the project owner), which is what
# first made this loop's own worst-case running time worth bounding
# explicitly rather than trusting it to always self-terminate.
#
# This is deliberately a different, *outer* bound from
# AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS above: that one only times the
# auto-stop-at-target branch's own retry-control_blind_stop() sub-loop,
# which is just one of several ways this same task can end a tracking
# episode (the others being a plain open/close reaching 0/100 with its
# full-travel resync cleared, or - before now - never, if neither of those
# conditions was ever satisfied). This constant instead bounds the whole
# task, across every branch and however many times self._target_position is
# changed underneath it (a same-direction retarget keeps the same task
# running rather than starting a fresh one - see its own "continue" comment
# above), so a bug or a misconfigured travel time in any one branch cannot
# leave this task polling every 0.2s forever with the entity stuck
# reporting "opening"/"closing" and a position estimate nobody can still
# vouch for.
#
# Set well above anything this loop is expected to ever actually take: the
# longest real travel time measured against the project owner's own six
# blinds is 23.27s (see MAX_STOP_LEAD_PERCENT's own comment below), and even
# DEFAULT_TRAVEL_TIME_SECONDS (60.0, a deliberately generous fallback for an
# un-calibrated blind) plus a full AUTO_STOP_CONFIRMATION_BACKSTOP_SECONDS
# retry budget on top only reaches 90s - a third below this. Ordinary
# operation should never come close to tripping this; it exists purely as
# the backstop of last resort for whatever this fix's own doubled stop_lead_
# percent compensation, or some other interaction entirely, fails to
# anticipate.
POSITION_UPDATE_LOOP_TIMEOUT_SECONDS = 120.0

# Mirrors STOP_COMMAND_EXTRA_RESENDS, for the same reason, for a move
# (open/close) command instead of a stop: control_blind_with_retry() only
# ever retries while the *stick* reports busy/no-reply, so once the stick
# has accepted the write there was, before this, still exactly one frame
# standing between "accepted" and the motor actually receiving it -  the
# same zero-end-to-end-confirmation gap STOP_COMMAND_EXTRA_RESENDS exists
# for, just on the move side rather than the stop side. Requested directly
# (2026-10-31) once that asymmetry was pointed out: a user-maintained pair
# of external scripts had been covering exactly this gap for move commands
# (re-sending open/close ~0.3s later for RF reliability - see
# _has_live_tracking_in_direction()'s own docstring in cover.py, written
# for exactly that pattern) since before this integration had any answer
# of its own for it.
#
# Not quite as unconditionally safe as the stop side's resend, and worth
# being explicit about rather than silently assuming otherwise: a second
# "stop" is always a no-op if the first already landed, but a second,
# identical "open"/"close" landing after a *different*, genuine command
# has since reached the motor - most plausibly a fast-following stop -
# would restart it briefly in the original direction, undoing that stop
# rather than merely repeating a no-op.
#
# Raised directly (2026-10-31) over exactly this, worried that a short
# set_cover_position() leg (a target only a few percent away) would have
# its own auto-stop-at-target fire inside this resend's own delay window
# below and get undone by it, making positions in roughly that range
# unreachable. Originally traced and confirmed that specific case could
# not happen at all, because this resend loop used to run awaited inline
# before this function's own `return True` - so async_open_cover()/
# async_close_cover() never set self._move_start_time or started the
# tracking loop that could observe "target reached" until the resend was
# already done; there was nothing yet running to race.
#
# That inline timing is gone (2026-11-08 - see this constant's own next
# comment below for since when and why): this resend loop is now a
# detached background task, so a short enough leg's own tracking loop
# can genuinely be running, and reach its own auto-stop, while this same
# move's own resend is still pending. That no longer makes the race
# impossible, only exactly as survivable as the cross-source race
# described in the paragraph just below - cancel_pending_move() snapshots
# every token this device_id currently has in flight (see its own
# docstring in this module), this move's own included, so the auto-stop
# path reaches this resend's own pre-write check the same way and with
# the same accepted residual window either way.
#
# What this resend *can* still race is a stop for the same device_id
# coming from a second, concurrent source: a brand new async_stop_cover()
# call (UI, automation, Alexa's own "stop"), or the auto-stop-at-target
# branch of an *earlier*, still-live move this call's own write was only
# ever a redundant piggyback onto (is_redundant_resend - typically the
# user's own external scripts' intentional double-send). Both already
# call cancel_pending_move() for this exact device_id before anything
# else - control_blind_with_retry() itself checks that signal before each
# retry attempt, and (as of this same date) its post-accept resend loop
# now checks it too, immediately before each write, skipping the resend
# rather than letting it undo a stop requested in the meantime. See that
# check's own comment at its call site for why the stop side's resend
# does not need the same check.
#
# What that check cannot see is a stop that never goes through this
# integration's own command path at all - a physical remote or a
# Gurtwickler winder's own local buttons, observed here only as a passive
# wire-level echo with no corresponding cancel_pending_move() call. That
# case has no timing floor to bound it by (unlike a stop issued through
# control_blind_stop(), which needs to acquire the lock, wait for the
# transmitter to go idle, write, and get acked - comfortably more than
# one full transmit cycle, ~1.1-1.2s per real logs) and is not closed by
# anything in this module today. The residual window is the same kind of
# accepted, documented trade-off STOP_COMMAND_EXTRA_RESENDS's own resend
# already carries (there, colliding with a different blind's write; here,
# racing an externally-originated stop for this same blind) - not a
# defect introduced by adding this, just the same category of real-world
# RF/timing uncertainty this whole module already has to live with.
#
# Raised from one extra resend to two (2026-11-07, project owner's own
# direct decision, mirroring the same change and the same reasoning on
# the stop side above) for further margin in the same high-RF-
# interference environment this whole constant exists for. This stretches
# the externally-originated-stop residual window described just above
# proportionally further - out to roughly two resend intervals after the
# primary write instead of one - since there is now one more best-effort
# write such a stop has no way to prevent. Accepted for the same reason
# the window existed at all before this change: nothing in this module
# can close it outright, and the realistic alternative - going back to a
# single, unconfirmed frame - reintroduces the exact lost-command failure
# this resend exists to cover, the more frequent, better-evidenced risk of
# the two.
#
# Used to also be the other factor (together with
# MOVE_COMMAND_RESEND_DELAY_SECONDS below) in stop_lead_percent's own
# uncontended baseline in cover.py (T_stamp), back when this resend ran
# inline and delayed stamping _move_start_time past the primary write.
# That inline loop is now a detached background task (2026-11-08, see
# cover.py's own stop_lead_percent comment), so _move_start_time is
# stamped on the primary write's ack alone; that baseline is gone, and
# neither constant here affects it any more.
MOVE_COMMAND_EXTRA_RESENDS = 2

# How long control_blind_with_retry() waits after its primary, acked move
# write before firing each of the extra resends above - see
# STOP_COMMAND_RESEND_DELAY_SECONDS's own comment for why this is not
# tied to STOP_COMMAND_LATENCY_SECONDS, which applies here unchanged.
# Kept equal to STOP_COMMAND_RESEND_DELAY_SECONDS deliberately: both
# model the same kind of deliberately-introduced gap on an otherwise-idle
# transmitter. Also still worth keeping short for the one case
# MOVE_COMMAND_EXTRA_RESENDS's own comment above says this module cannot
# actually close - an externally-originated (physical remote/Gurtwickler)
# stop racing this resend has no floor to stay safely under, so the best
# this delay can do for that case is simply not stay open any longer than
# it has to; lengthening it would only widen that window for no offsetting
# benefit, since the failure this resend targets (a single lost frame)
# does not get any likelier to have happened the longer this waits.
# No longer also stop_lead_percent's own baseline in cover.py (together
# with MOVE_COMMAND_EXTRA_RESENDS above) - see that constant's own
# comment for why.
MOVE_COMMAND_RESEND_DELAY_SECONDS = 0.25

# How close cover.py's own elapsed-time position estimate must already be
# to the edge it was heading toward (0 for closing, 100 for opening), AND
# how close that same move's own *starting* position must have been to
# the opposite edge, for a real STOPPED echo on an untargeted move
# (_handle_event()'s EVENT_STOPPED branch, self._target_position already
# None) to be treated as that move reaching its own physical end stop and
# snapped the rest of the way to the exact value, rather than left at the
# estimate as-is.
#
# An untargeted move - always a paired remote, a wall switch, or a
# Gurtwickler winder's own local buttons, never this integration's own
# async_open_cover()/async_close_cover(), which always set a 0/100
# _target_position of their own first - has no intermediate target by
# definition, so it can only end one of two ways: the motor runs to its
# own mechanical limit, or something stops it part-way (most plausibly a
# deliberate press of the remote's own stop button). Nothing in the
# STOPPED echo itself says which one happened; this is the only signal
# available to tell them apart. Real-world reports (2026-10-31): closing
# this way consistently lands the estimate at 2%, opening at 98%, never
# the exact 0/100 the motor physically reached - calibration's own
# travel_time is necessarily an approximation of a real motor's, and the
# two rarely agree to the second, so the *true* end stop almost never
# lines up with elapsed time crossing exactly 0/100.
#
# Position alone is NOT enough to tell a genuine end stop apart from a
# deliberate mid-travel stop, though: a first version of this check
# assumed a deliberate stop would never land within a few percent of
# either edge "by coincidence", which a direct user report immediately
# disproved - nudging a blind open to a deliberately chosen 98% (open,
# but intentionally not quite all the way) via the remote is exactly the
# kind of real, intentional near-edge stop that assumption missed, and a
# position-only check would snap it straight to 100% regardless, silently
# overriding the position the stop button was actually pressed to reach.
# Requiring the move to have also *started* within this same tolerance of
# the opposite edge (_move_start_position - see its own clearing a few
# lines below the check that reads it) closes that gap for exactly the
# cases these real-world reports described: a deliberate nudge from some
# other, non-edge position never satisfies it, while a genuine full-range
# traversal (closed-to-open or open-to-closed) always does. It still
# cannot tell a deliberate stop chosen a few percent short of full travel
# *when that move also genuinely started from the opposite edge* (closing
# all the way, then choosing to reopen to exactly 98%, say) apart from a
# real end stop with the same drift - no further signal distinguishes
# those two at the position/timing level available here - but it narrows
# the false-positive window from "any near-edge stop" down to that one
# specific, much rarer case. Kept comfortably above the reported 2% gap,
# with headroom for other blinds' own calibration drift, while staying
# far below any realistic deliberate stop position reached from a
# non-edge start.
NEAR_END_STOP_SNAP_TOLERANCE_PERCENT = 5

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
#
# Shared by both of _wait_for_movement_start()'s uses in options_flow_
# calibration.py: waiting for the user's own manual open/close to register
# (no RF command is sent by this integration during calibration - see
# calibration_open_instruction/calibration_close_instruction's own strings)
# and, via _wait_for_stop_event()'s delegation to the same method, waiting
# for the matching stop afterward.
#
# Lowered from 300 (5 minutes) to 120 (2 minutes) for Issue #77 (RolloDrive
# 65 Premium reported calibration "hanging endlessly" - 2026-11-07): the old
# value just meant a silent 5-minute wait before anything was shown at all,
# which read indistinguishably from a genuine hang to someone watching the
# pairing wizard. Still long enough that an ordinary, if slightly slow,
# manual open/close is never cut short mid-motion - the project owner's own
# six real blinds (see MAX_STOP_LEAD_PERCENT's own comment in this file) all
# finish within 24s, nowhere near this budget either way.
#
# A fresh pairing (CalibrationFlowHandler._create_subentry_after_calibration
# is True) now also gets an explicit choice - retry this same leg, or switch
# to manual time entry - the moment either wait in that leg times out,
# rather than only an on-screen error and an implicit "submit the form
# again" (see calibration_open_instruction/calibration_close_instruction's
# own comments in options_flow_calibration.py). Recalibrating an already-
# paired blind keeps the old error-and-resubmit behavior unchanged - that
# workflow already has its own direct manual-entry escape hatch one menu
# level up (reconfigure's own "edit" option), so adding a second one here
# would just be two routes to the same place.
CALIBRATION_TIMEOUT = 120
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
# second/last leg drove to. Set on every subentry created by a fresh pairing
# regardless of which calibration_method_choice option was picked (Issue #78
# pairing-flow unification, 2026-11-07, see config_flow.py's async_step_user()
# docstring) - a live calibration run sets it to the real ending position
# (options_flow_calibration.py), and the manual-entry route sets it directly
# from the person's own stated current position (_pending_data(), fed by
# async_step_manual_times()). cover.py falls back to 0 only when the key is
# absent entirely, which now covers just blinds paired before this key
# existed in the first place.
CONF_INITIAL_POSITION = "initial_position"
# Legacy top-level command-device-ID key from before CONF_COMMAND_DEVICE_ID
# and the separate status identity existed. Despite living in this
# "calibration constants" block (for historical reasons), it is read as a
# fallback throughout config_flow.py and cover.py, not just during
# calibration - see e.g. cover.py's legacy_device_id handling.
CONF_DEVICE_ID = "device_id"
