"""Helpers for persisted Schellenberg status identities."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
)

type StatusIdentity = tuple[str, str]

_STATUS_IDENTITY_PATTERN = re.compile(
    r"^([0-9A-Fa-f]{6})\s*[/:-]\s*([0-9A-Fa-f]{1,2})$"
)


def normalize_status_identity(
    device_id: object, device_enum: object
) -> StatusIdentity | None:
    """Normalize one status identity, returning None when it is malformed."""
    normalized_id = str(device_id).strip().upper()
    normalized_enum = str(device_enum).strip().upper()
    # Guarded on non-empty first: "".zfill(2) is "00", a real, specific
    # enum some other device could legitimately use - padding a genuinely
    # missing enum into it would silently accept malformed input as that
    # enum instead of correctly falling through to the rejection below.
    if normalized_enum and len(normalized_enum) <= 2:
        normalized_enum = normalized_enum.zfill(2)
    if (
        len(normalized_id) != 6
        or len(normalized_enum) != 2
        or any(character not in "0123456789ABCDEF" for character in normalized_id)
        or any(character not in "0123456789ABCDEF" for character in normalized_enum)
    ):
        return None
    return normalized_id, normalized_enum


def _split_status_identity_tokens(text: str) -> list[str]:
    """Split comma/semicolon/newline-separated text into stripped, non-empty tokens.

    Shared by parse_status_identities_text() (strict - raises on the first
    malformed token) and normalize_status_identities()'s own string-input
    branch (lenient - skips a malformed token instead), so the two only
    ever differ in how they react to a bad token, never in how the text is
    split into tokens to begin with.
    """
    return [token for raw in re.split(r"[,;\n]+", text) if (token := raw.strip())]


def normalize_status_identities(value: object) -> tuple[StatusIdentity, ...]:
    """Normalize persisted list or text representations, ignoring invalid items.

    A string is tokenized the same way parse_status_identities_text() does,
    but a malformed token is skipped here rather than discarding every
    other, well-formed token found in the same string - matching this
    function's own docstring and the list/iterable branch below, which
    already tolerates one bad item without losing the valid ones around it.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        token_identities: list[StatusIdentity] = []
        for token in _split_status_identity_tokens(value):
            match = _STATUS_IDENTITY_PATTERN.fullmatch(token)
            if match is None:
                continue
            # Named distinctly from the iterable branch's own `identity`
            # below - both live in this same function's scope, and mypy
            # treats a second *annotated* declaration of one name in one
            # scope as a redefinition even across mutually-exclusive
            # branches (this one is unannotated, so it would not itself
            # trigger that check, but naming it `identity` too would still
            # read as the same variable reused for two different things).
            token_identity = normalize_status_identity(match.group(1), match.group(2))
            if token_identity is not None and token_identity not in token_identities:
                token_identities.append(token_identity)
        return tuple(token_identities)
    if not isinstance(value, Iterable) or isinstance(value, (bytes, Mapping)):
        return ()

    identities: list[StatusIdentity] = []
    for item in value:
        identity: StatusIdentity | None = None
        if isinstance(item, Mapping):
            identity = normalize_status_identity(
                item.get("device_id", ""),
                item.get("enum", item.get("device_enum", "")),
            )
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            identity = normalize_status_identity(item[0], item[1])
        if identity is not None and identity not in identities:
            identities.append(identity)
    return tuple(identities)


def parse_status_identities_text(value: object) -> tuple[StatusIdentity, ...]:
    """Parse comma/newline-separated DEVICE_ID/ENUM identities strictly.

    Unlike normalize_status_identities()'s own string-input handling above,
    this raises ValueError on the first malformed token instead of skipping
    it - relied on directly by config_flow.py's manual-edit text field so a
    typo is rejected with a visible form error instead of silently dropped.
    """
    text = str(value or "").strip()
    if not text:
        return ()
    identities: list[StatusIdentity] = []
    for token in _split_status_identity_tokens(text):
        match = _STATUS_IDENTITY_PATTERN.fullmatch(token)
        if match is None:
            raise ValueError(f"invalid status identity: {token}")
        identity = normalize_status_identity(match.group(1), match.group(2))
        if identity is None:
            raise ValueError(f"invalid status identity: {token}")
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


def serialize_status_identities(
    identities: Iterable[StatusIdentity],
) -> list[dict[str, str]]:
    """Return stable JSON-compatible status identity dictionaries."""
    return [
        {"device_id": device_id, "enum": device_enum}
        for device_id, device_enum in identities
    ]


def format_status_identities(value: object) -> str:
    """Format persisted identities for a multiline config-flow text field."""
    return "\n".join(
        f"{device_id}/{device_enum}"
        for device_id, device_enum in normalize_status_identities(value)
    )


def resolve_blind_identities(data: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one blind subentry's command/status identities from its data.

    Shared by config_flow.py's developer-tools diagnostics step and
    diagnostics.py's structured diagnostics dump, so the fallback priority
    below only has to be right in one place instead of two drifting copies
    of it: CONF_COMMAND_DEVICE_ID/CONF_COMMAND_ENUM take priority over the
    legacy CONF_DEVICE_ID/CONF_DEVICE_ENUM, and
    CONF_STATUS_IDENTITY_SOURCE == STATUS_IDENTITY_SOURCE_UNKNOWN means "no
    confirmed primary status identity yet" regardless of whatever
    CONF_STATUS_DEVICE_ID/CONF_STATUS_ENUM values happen to still be sitting
    in the subentry's data from before it became unconfirmed.
    """
    command_device_id = str(
        data.get(CONF_COMMAND_DEVICE_ID) or data.get(CONF_DEVICE_ID, "")
    ).upper()
    command_enum = str(
        data.get(CONF_COMMAND_ENUM) or data.get(CONF_DEVICE_ENUM, "")
    ).upper()
    configured_source = str(data.get(CONF_STATUS_IDENTITY_SOURCE) or "legacy")
    if configured_source == STATUS_IDENTITY_SOURCE_UNKNOWN:
        primary_identity = None
    else:
        primary_identity = normalize_status_identity(
            data.get(CONF_STATUS_DEVICE_ID) or command_device_id,
            data.get(CONF_STATUS_ENUM) or command_enum,
        )
    secondary_status_identities = normalize_status_identities(
        data.get(CONF_SECONDARY_STATUS_IDENTITIES)
    )
    status_identities = (
        *((primary_identity,) if primary_identity is not None else ()),
        *secondary_status_identities,
    )
    return {
        "command_device_id": command_device_id,
        "command_enum": command_enum,
        "status_identity_source": configured_source,
        "primary_identity": primary_identity,
        "secondary_status_identities": secondary_status_identities,
        "status_identities": status_identities,
    }


#: Commands api.py's frame handler accepts as confirming the direction *it
#: itself just commanded* during that capture phase (see its "{phase}_endstop"
#: relabelling of a stop frame received mid-leg). "00"/"01"/"02" are
#: CMD_STOP/CMD_UP/CMD_DOWN from const.py, duplicated here as literals
#: rather than imported, matching how `recognized` below already does it.
_PHASE_CONFIRMING_COMMANDS = {
    "opening": {"01"},
    "opening_endstop": {"00", "01"},
    "closing": {"02"},
    "closing_endstop": {"00", "02"},
}

#: The exact order "Discover status from original remote" instructs the
#: person to press on their *existing* remote (strings.json's
#: discover_status description: "press OPEN, STOP, CLOSE, STOP"), reduced
#: to first-seen distinct commands: "01" (open) before "00" (the first
#: stop) before "02" (close). Unlike _PHASE_CONFIRMING_COMMANDS above,
#: nothing commands the blind during this capture - api.py labels every
#: frame the same constant "remote_discovery" phase throughout (there is no
#: leg to tag), so that mechanism cannot help here at all. This instructed
#: press order is the one ground-truth fact this flow still has to go on.
_REMOTE_DISCOVERY_EXPECTED_SEQUENCE = ("01", "00", "02")


def _remote_discovery_sequence_match_length(commands: list[str]) -> int:
    """Return how many leading commands match the instructed press order.

    A device actually being operated per the on-screen instructions should
    see its first (up to) three distinct recognized commands in this order -
    group["commands"] already collapses to first-occurrence order (see
    summarize_status_discovery_frames below), so this does not care how many
    raw frames each press produced or how many times "00" was sent. An
    unrelated device transmitting coincidentally during the same window has
    no reason to follow the instructed order, so a longer match is real
    evidence this group is the one actually being pressed.
    """
    match_length = 0
    # strict=False: commands may be shorter than the 3-element expected
    # sequence (fewer distinct commands seen so far) or longer (more than
    # open/stop/close were recognized) - both are fine, zip() should just
    # stop at whichever is shorter rather than require equal length.
    for expected, actual in zip(
        _REMOTE_DISCOVERY_EXPECTED_SEQUENCE, commands, strict=False
    ):
        if expected != actual:
            break
        match_length += 1
    return match_length


def summarize_status_discovery_frames(
    frames: Iterable[Mapping[str, Any]],
    *,
    exclude_device_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Group one bounded remote-capture window and choose its tracking stream.

    exclude_device_ids: device ids positively confirmed to belong to some
    *other*, already-paired blind on this hub (see config_flow.py's
    _foreign_capture_device_ids() for how calibration computes this).
    Frames from one of these never form their own group and can never
    become "primary"/"secondary"/appear in "groups" or "unknown_commands" -
    excluded from identity *selection* entirely, not merely deprioritized -
    while still counting toward the capture's own total frame count (kept
    by finish_status_frame_capture()'s caller, outside this function's own
    return value) for diagnostic transparency. Without this, a capture
    window open for one blind that happens to overlap with a different,
    already-working blind's own ordinary movement (its own automation, its
    own remote) could have that other blind's real frames outscore the
    blind actually being worked on and get stored as its identity instead -
    previously only guarded for the separate "movement detected" wait
    (handle_capture_event in options_flow_calibration.py), never for this,
    the actual identity decision.
    Empty by default: the first blind on a fresh hub (nothing else to
    exclude yet) and the manual "Discover status from original remote"
    flow (which never passes this at all - overwriting an existing
    identity is its whole deliberate purpose, so it is never scoped the
    same way calibration is) are both unaffected.

    A calibration run's capture window is not a neutral listening post: for
    each leg, *we* already know which direction we just commanded (the phase
    recorded alongside each frame - see api.py). A frame whose command
    matches the direction being driven in that phase (an "01" while
    "opening", a "02" while "closing", a "00" at either leg's end stop) is
    strong evidence of being this blind's own motor; one that does not (an
    "01" received mid-"closing", say) is more likely another device on the
    same stick coincidentally transmitting during the window - exactly the
    ambiguity a busy installation with several blinds/winders on one stick
    runs into with only a single up/down cycle to go on. Weighting by that
    match lets a single ordinary calibration run resolve this correctly far
    more often, without asking the person to cycle the blind extra times.

    Frames from a plain, unphased capture (the manual "Discover status from
    original remote" window, which labels everything the same constant
    phase throughout, since nothing is commanded there) never match the
    phase check above, so every candidate ties at zero on it and the
    comparison falls through to _remote_discovery_sequence_match_length
    instead: that flow's own on-screen instructions name a specific press
    order ("OPEN, STOP, CLOSE, STOP"), which is exactly the kind of
    ground-truth signal the phase check relies on for calibration, just
    sourced from the person's button presses instead of a commanded
    direction. Frames from *neither* flow (e.g. identities normalized from
    plain historical/manual data with no "phase" key at all) tie at zero on
    both and fall through to the original distinct-commands/frame-count
    comparison, exactly as before either signal existed.
    """
    recognized = {"00", "01", "02"}
    groups: dict[StatusIdentity, dict[str, Any]] = {}
    for frame in frames:
        identity = normalize_status_identity(
            frame.get("device_id", ""), frame.get("enum", "")
        )
        if identity is None:
            continue
        if identity[0] in exclude_device_ids:
            continue
        command = str(frame.get("command", "")).strip().upper()
        if len(command) != 2 or any(
            character not in "0123456789ABCDEF" for character in command
        ):
            continue
        group = groups.setdefault(
            identity,
            {
                "device_id": identity[0],
                "enum": identity[1],
                "commands": [],
                "timestamps": [],
                "frame_count": 0,
                "recognized_frame_count": 0,
                "phase_confirmed_frame_count": 0,
            },
        )
        group["frame_count"] += 1
        if command in recognized:
            group["recognized_frame_count"] += 1
        phase = str(frame.get("phase", "")).strip().lower()
        if command in _PHASE_CONFIRMING_COMMANDS.get(phase, ()):
            group["phase_confirmed_frame_count"] += 1
        if command not in group["commands"]:
            group["commands"].append(command)
        timestamp = str(frame.get("time", "")).strip()
        if timestamp:
            group["timestamps"].append(timestamp)

    # Computed once per group from its finished "commands" list (unlike
    # phase_confirmed_frame_count above, nothing here needs per-frame state),
    # and stored on the group - not just inlined in the max() key below - so
    # it is visible for diagnostics/tests the same way phase_confirmed_frame_
    # count already is.
    for group in groups.values():
        group["remote_discovery_sequence_match_length"] = (
            _remote_discovery_sequence_match_length(group["commands"])
        )

    candidates = [
        group for group in groups.values() if recognized.intersection(group["commands"])
    ]
    primary = (
        max(
            candidates,
            key=lambda group: (
                group["phase_confirmed_frame_count"],
                group["remote_discovery_sequence_match_length"],
                len(recognized.intersection(group["commands"])),
                group["recognized_frame_count"],
            ),
        )
        if candidates
        else None
    )
    secondary = [group for group in groups.values() if group is not primary]
    unknown_commands = [
        {
            "device_id": group["device_id"],
            "enum": group["enum"],
            "commands": [
                command for command in group["commands"] if command not in recognized
            ],
        }
        for group in groups.values()
        if any(command not in recognized for command in group["commands"])
    ]
    return {
        "primary": primary,
        "secondary": secondary,
        "groups": list(groups.values()),
        "unknown_commands": unknown_commands,
        "position_tracking_available": primary is not None,
    }
