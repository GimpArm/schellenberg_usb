"""Tests for the status-identity parsing/normalization helpers."""

from __future__ import annotations

import pytest

from custom_components.schellenberg_usb.const import (
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    CONF_STATUS_IDENTITY_SOURCE,
    STATUS_IDENTITY_SOURCE_MANUAL,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
)
from custom_components.schellenberg_usb.identities import (
    normalize_status_identities,
    normalize_status_identity,
    parse_status_identities_text,
    resolve_blind_identities,
    summarize_status_discovery_frames,
)


def test_normalize_status_identity_accepts_and_rejects() -> None:
    assert normalize_status_identity("5d3e7c", "1") == ("5D3E7C", "01")
    assert normalize_status_identity("5D3E7C", "10") == ("5D3E7C", "10")
    # Too short / not hex -> malformed, must not raise.
    assert normalize_status_identity("5D3E7", "01") is None
    assert normalize_status_identity("5D3E7G", "01") is None


def test_normalize_status_identity_rejects_an_empty_enum_instead_of_padding_it() -> (
    None
):
    """Regression: "".zfill(2) is "00", a real enum some other device could use.

    The zfill guard used to run unconditionally, so a device_enum that was
    empty (rather than merely a short, genuine value like "1") silently
    became the specific, valid-looking enum "00" instead of being rejected
    as the malformed input it actually was.
    """
    assert normalize_status_identity("5D3E7C", "") is None


def test_parse_status_identities_text_accepts_documented_separators() -> None:
    text = "5D3E7C/01, ABCDEF-02\n112233:3"
    assert parse_status_identities_text(text) == (
        ("5D3E7C", "01"),
        ("ABCDEF", "02"),
        ("112233", "03"),
    )


def test_parse_status_identities_text_rejects_malformed_tokens() -> None:
    with pytest.raises(ValueError, match="invalid status identity"):
        parse_status_identities_text("not-an-identity")


def test_normalize_status_identities_accepts_multiple_input_shapes() -> None:
    expected = (("5D3E7C", "01"),)

    assert normalize_status_identities("5D3E7C/01") == expected
    assert normalize_status_identities([("5D3E7C", "01")]) == expected
    assert (
        normalize_status_identities([{"device_id": "5D3E7C", "enum": "01"}]) == expected
    )
    assert normalize_status_identities(None) == ()
    assert normalize_status_identities("") == ()
    # A malformed free-text value must degrade to "no identities", not raise.
    assert normalize_status_identities("garbage") == ()


def test_normalize_status_identities_keeps_valid_tokens_around_a_bad_one() -> None:
    """Regression: one malformed token in a comma/newline-separated string
    used to discard every identity parsed from that string, not just the
    bad token - inconsistent with this function's own docstring ("ignoring
    invalid items") and with the list/iterable branch of the same function,
    which already drops only the bad item and keeps the well-formed ones
    around it.
    """
    assert normalize_status_identities("5D3E7C/01, garbage, ABCDEF/02") == (
        ("5D3E7C", "01"),
        ("ABCDEF", "02"),
    )


def test_summarize_status_discovery_frames_prefers_most_recognized_group() -> None:
    frames = [
        {"device_id": "5D3E7C", "enum": "01", "command": "01", "time": "10:00:00"},
        {"device_id": "5D3E7C", "enum": "01", "command": "00", "time": "10:00:05"},
        # A second, less-frequently-seen transmitter caught on the same
        # channel must not be mistaken for the primary remote.
        {"device_id": "AAAAAA", "enum": "02", "command": "01", "time": "10:00:02"},
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["primary"]["recognized_frame_count"] == 2
    assert result["position_tracking_available"] is True
    assert len(result["secondary"]) == 1
    assert result["secondary"][0]["device_id"] == "AAAAAA"


def test_summarize_status_discovery_frames_with_no_recognized_commands() -> None:
    frames = [
        {"device_id": "5D3E7C", "enum": "01", "command": "40", "time": "10:00:00"}
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"] is None
    assert result["position_tracking_available"] is False
    assert result["unknown_commands"] == [
        {"device_id": "5D3E7C", "enum": "01", "commands": ["40"]}
    ]


def test_summarize_status_discovery_frames_1f82_hardware_regression() -> None:
    """Concrete regression anchor for the same real-world hardware behind
    api.py's _resolve_status_command() fallback (see its docstring, and
    CHANGELOG.md's v1.5.0 entry right after the Issue #85 bullet): two
    real devices whose "ss" frames carry a constant, non-command "1F" at
    message[10:12] - the position api.py reads before trying its
    fallback - with the real command only recoverable from message[14:16].

    This function never sees raw wire bytes, only the already-resolved
    "command" field api.py's _handle_message() put in each frame dict -
    so it cannot fall back itself. Before the api.py fix, every frame
    from this hardware carried "1F" here, exactly like the no-recognized-
    commands case above, and this function could never select it as
    primary: no amount of open/close/stop cycling would ever produce a
    recognized command, so calibration's own post-capture identity
    selection (_finish_calibration_capture() -> this function) would
    permanently fail for this hardware, independently of - and in
    addition to - the narrower movement-wait-never-matches symptom the
    CHANGELOG entry describes. The api.py fix resolves both from the
    same one-line change, since both consume the same "command" field.
    """
    unresolved_frames = [
        {
            "device_id": "6E568B",
            "enum": "09",
            "command": "1F",
            "phase": "opening",
        },
        {
            "device_id": "6E568B",
            "enum": "09",
            "command": "1F",
            "phase": "opening_endstop",
        },
        {
            "device_id": "6E568B",
            "enum": "09",
            "command": "1F",
            "phase": "closing",
        },
        {
            "device_id": "6E568B",
            "enum": "09",
            "command": "1F",
            "phase": "closing_endstop",
        },
    ]
    before_fix = summarize_status_discovery_frames(unresolved_frames)
    assert before_fix["primary"] is None
    assert before_fix["position_tracking_available"] is False
    assert before_fix["unknown_commands"] == [
        {"device_id": "6E568B", "enum": "09", "commands": ["1F"]}
    ]

    # Same capture window, same phases - only the "command" field differs,
    # exactly as api.py's own _resolve_status_command() now resolves it
    # for these real frames (see test_handle_message_ss_frame_command_
    # byte_falls_back_for_1f82_hardware in tests/test_api.py).
    resolved_frames = [
        {**frame, "command": command}
        for frame, command in zip(
            unresolved_frames, ["01", "00", "02", "00"], strict=True
        )
    ]
    after_fix = summarize_status_discovery_frames(resolved_frames)
    assert after_fix["primary"] is not None
    assert after_fix["primary"]["device_id"] == "6E568B"
    assert after_fix["primary"]["phase_confirmed_frame_count"] == 4
    assert after_fix["position_tracking_available"] is True
    assert after_fix["unknown_commands"] == []


def test_summarize_status_discovery_frames_prefers_phase_confirmed_group() -> None:
    """A calibration run's capture window labels each frame with the leg
    it was taken during (see api.py's phase tagging). A frame whose
    command matches the direction actually being driven in that phase is
    strong evidence of being this blind's own motor; weighting by that
    match lets a single ordinary calibration cycle resolve the right
    status identity even when a noisier, unrelated device on the same
    stick (e.g. one of several Gurtwickler sharing it) happens to
    transmit *more* raw frames during the same window - without asking
    the person to cycle the blind extra times to get a reliable reading.

    Without the phase-aware tiebreaker, this exact scenario picks the
    wrong device: both candidates tie on 3 distinct recognized commands,
    and AABBCC/03's 5 recognized frames would outrank E3D0A5/08's 4.
    """
    frames = [
        # --- the real blind, correctly phased ---
        {"device_id": "E3D0A5", "enum": "08", "command": "01", "phase": "opening"},
        {
            "device_id": "E3D0A5",
            "enum": "08",
            "command": "00",
            "phase": "opening_endstop",
        },
        {"device_id": "E3D0A5", "enum": "08", "command": "02", "phase": "closing"},
        {
            "device_id": "E3D0A5",
            "enum": "08",
            "command": "00",
            "phase": "closing_endstop",
        },
        # --- an unrelated device, coincidental noise, wrong phase every
        # time, but *more* raw frames than the real device above ---
        {"device_id": "AABBCC", "enum": "03", "command": "01", "phase": "closing"},
        {"device_id": "AABBCC", "enum": "03", "command": "02", "phase": "opening"},
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "00",
            "phase": "idle_between_legs",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "01",
            "phase": "idle_between_legs",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "01",
            "phase": "idle_between_legs",
        },
    ]

    result = summarize_status_discovery_frames(frames)

    primary = result["primary"]
    assert primary is not None
    assert (primary["device_id"], primary["enum"]) == ("E3D0A5", "08")
    assert primary["phase_confirmed_frame_count"] == 4
    assert primary["recognized_frame_count"] == 4


def test_summarize_status_discovery_frames_phase_match_breaks_a_frame_count_tie() -> (
    None
):
    """Minimal direct tie-break: two devices with the exact same
    recognized-command set and frame count (so the pre-existing
    tiebreakers alone would pick whichever is listed first - here,
    AAAAAA), where only one of them actually matches the phase it was
    heard in.
    """
    frames = [
        {"device_id": "AAAAAA", "enum": "02", "command": "01", "phase": "closing"},
        {"device_id": "AAAAAA", "enum": "02", "command": "00", "phase": "idle"},
        {"device_id": "5D3E7C", "enum": "01", "command": "01", "phase": "opening"},
        {"device_id": "5D3E7C", "enum": "01", "command": "00", "phase": "idle"},
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["primary"]["phase_confirmed_frame_count"] == 1


def test_summarize_status_discovery_frames_prefers_instructed_press_order() -> None:
    """The "Discover status from original remote" step tells the person to
    press OPEN, STOP, CLOSE, STOP on their existing remote (see
    strings.json's discover_status description) - and that capture window
    labels every frame with the same constant phase throughout (api.py's
    "remote_discovery"), so phase_confirmed_frame_count can never help
    disambiguate here (see the unphased test below). A device whose first
    distinct recognized commands follow that exact order is strong
    evidence of being the remote actually being pressed; an unrelated
    device transmitting coincidentally during the same 45-second window
    has no reason to follow it.

    Without that signal, this exact scenario picks the wrong device: both
    candidates tie on 3 distinct recognized commands, and AABBCC's 5
    recognized frames would outrank 5D3E7C's 4.
    """
    frames = [
        # --- the real remote, pressed in the instructed order ---
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "02",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "00",
            "phase": "remote_discovery",
        },
        # --- an unrelated device, coincidental noise during the same
        # window: same 3 distinct commands but in the wrong order, and
        # *more* raw frames than the real remote above ---
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "02",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AABBCC",
            "enum": "03",
            "command": "00",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(frames)

    primary = result["primary"]
    assert primary is not None
    assert (primary["device_id"], primary["enum"]) == ("5D3E7C", "01")
    assert primary["remote_discovery_sequence_match_length"] == 3
    assert primary["recognized_frame_count"] == 4


def test_summarize_status_discovery_frames_sequence_match_breaks_a_tie() -> None:
    """Minimal direct tie-break: two devices with the exact same
    recognized-command set and frame count (so the pre-existing
    tiebreakers alone would pick whichever is listed first - here,
    AAAAAA), both in the unphased "remote_discovery" window where
    phase_confirmed_frame_count can never help, where only one of them
    was actually pressed in the instructed OPEN-before-STOP order.
    """
    frames = [
        {
            "device_id": "AAAAAA",
            "enum": "02",
            "command": "02",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AAAAAA",
            "enum": "02",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "00",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["primary"]["remote_discovery_sequence_match_length"] == 2


def test_summarize_status_discovery_frames_excludes_foreign_identity_by_default() -> (
    None
):
    """A device_id already claimed by a different, already-paired blind
    (see config_flow.py's _foreign_capture_device_ids()) must not become
    this capture's primary or secondary, or even appear in "groups" - no
    matter how many raw frames it sent - unless it also shows a full
    remote-press-order signal or at least one phase-confirmed frame (see
    the tests below). Baseline/regression guard for the exclusion itself.
    """
    frames = [
        # The foreign device: more raw frames than the real candidate
        # below, but no deliberate press-order signal at all - exactly
        # the kind of ambient motor traffic exclude_device_ids exists to
        # keep out.
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "00",
            "phase": "remote_discovery",
        },
        # This blind's own real candidate.
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "00",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"FFFFFF"})
    )

    assert result["primary"] is not None
    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["secondary"] == []
    assert result["groups"] == [result["primary"]]


def test_summarize_status_discovery_frames_full_sequence_admits_foreign_id() -> None:
    """The first exception to the exclusion above (2026-11-15): a foreign
    identity whose own commands fully reproduce the instructed press
    order ("OPEN, STOP, CLOSE") is let back in as a *secondary* identity,
    since ambient motor traffic from an unrelated, already-paired blind
    has no reason to reproduce that specific three-step order - strong,
    specific evidence that a person is actually pressing a *shared*
    remote already linked to that other blind (one physical remote
    commonly operates several blinds).

    Never primary, even here where nothing else was captured at all: see
    this function's own docstring for why admitting an excluded identity
    to primary is unsafe regardless of how strong the evidence is - a
    device excluded by _foreign_capture_device_ids() is, by construction,
    confirmed to belong to a *different* blind, so a full sequence match
    only ever argues it is worth keeping as a secondary reference, never
    that it should become this blind's own (here entirely absent) primary.

    Regression for a real report (2026-11-15): pressing a remote already
    linked to a different, already-paired blind during this blind's own
    calibration used to lose every one of its frames outright, however
    deliberately or clearly it was pressed - reported as no secondary (or
    even primary) status identity ever being captured during either a
    fresh pairing's own calibration or a recalibration, despite genuinely
    pressing the remote each time.
    """
    frames = [
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "02",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"FFFFFF"})
    )

    assert result["primary"] is None
    assert len(result["secondary"]) == 1
    assert result["secondary"][0]["device_id"] == "FFFFFF"
    assert result["secondary"][0]["remote_discovery_sequence_match_length"] == 3


def test_summarize_status_discovery_frames_partial_sequence_match_stays_excluded() -> (
    None
):
    """Maximum safety: only a *complete* press-order match lifts the
    exclusion above, never a partial one. An ordinary move start alone
    already produces a bare "01", so treating a partial match as
    sufficient evidence would reopen exactly the cross-blind cross-talk
    exclude_device_ids exists to prevent.
    """
    frames = [
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "01",
            "phase": "remote_discovery",
        },
        {
            "device_id": "FFFFFF",
            "enum": "09",
            "command": "00",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"FFFFFF"})
    )

    assert result["primary"] is None
    assert result["secondary"] == []
    assert result["groups"] == []


def test_summarize_status_discovery_frames_phase_confirmation_admits_foreign_id() -> (
    None
):
    """The second exception to the exclusion above (2026-11-15, added the
    same day as the first, once a real recalibration log showed the first
    one alone does not cover calibration's own real captures - see this
    function's own docstring): a foreign identity with phase-confirmed
    frames in *both* directions ("01" and "02" both present, not just
    whichever leg happened to overlap with it) is let back in too, as a
    *secondary* identity - never primary, however many such frames it has
    (see below).

    Regression for a real recalibration log (2026-11-15, Küchenrolladen,
    device_id 5755AA/enum 08): a remote already linked to a different,
    already-paired blind (7953A7/43) was held throughout this blind's own
    ordinary full open-then-close calibration cycle - 18 "01" frames
    throughout the open phase and 18 "02" frames throughout the close
    phase (the log's own exact counts, reproduced here in full rather
    than a smaller stand-in - see the next paragraph for why that matters),
    and *no* "00" frame at all, since the blind's own end stops handle
    stopping, not the remote. The first exception above needs a complete
    "01", "00", "02" sequence to admit a foreign identity, so it can never
    fire for a capture like this one - exactly why the secondary identity
    kept disappearing despite the remote genuinely being pressed, even
    though the first exception was already in place.

    Also a regression for a flaw independent review found in the first
    version of this very fix, before it was ever committed: that version
    admitted an excluded device into the *same* candidate pool primary is
    chosen from, and a smaller stand-in (3 foreign frames vs. this blind's
    own 4) happened not to trigger the resulting bug. Re-run against the
    real log's own frame volume, it did: a remote held down for a whole
    leg retransmits continuously for RF reliability (36 frames here)
    while a motor echoes only once per phase transition (4 frames for a
    full cycle), so the excluded device's sheer volume won it primary
    outright - silently handing this blind's live position tracking to
    the *other* blind's remote. Both exceptions now admit a device for
    secondary status only; primary is chosen only among never-excluded
    groups (see this function's own docstring and the code below), so
    this test uses the real log's full, lopsided frame counts specifically
    to prove that asymmetry no longer matters.
    """
    frames = [
        # --- this blind's own motor: one status echo per phase
        # transition, exactly as a real motor sends it, same shape as
        # test_summarize_status_discovery_frames_prefers_phase_confirmed_
        # group's own real-candidate frames above ---
        {"device_id": "5755AA", "enum": "08", "command": "01", "phase": "opening"},
        {
            "device_id": "5755AA",
            "enum": "08",
            "command": "00",
            "phase": "opening_endstop",
        },
        {"device_id": "5755AA", "enum": "08", "command": "02", "phase": "closing"},
        {
            "device_id": "5755AA",
            "enum": "08",
            "command": "00",
            "phase": "closing_endstop",
        },
        # --- a remote already linked to a different blind, genuinely
        # held down throughout this blind's own real drive: 18+18 real
        # frames, never a "00" - see the docstring above for why the full
        # count (not a smaller stand-in) is the point of this test ---
        *(
            {"device_id": "7953A7", "enum": "43", "command": "01", "phase": "opening"}
            for _ in range(18)
        ),
        *(
            {"device_id": "7953A7", "enum": "43", "command": "02", "phase": "closing"}
            for _ in range(18)
        ),
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"7953A7"})
    )

    assert result["primary"] is not None
    assert result["primary"]["device_id"] == "5755AA"
    assert result["primary"]["phase_confirmed_frame_count"] == 4
    assert len(result["secondary"]) == 1
    assert result["secondary"][0]["device_id"] == "7953A7"
    # Admitted on phase confirmation alone - never a full sequence match,
    # since this capture has no "00" frame for the foreign device at all.
    assert result["secondary"][0]["remote_discovery_sequence_match_length"] < 3
    # Far more phase-confirmed frames than the real motor above (36 vs.
    # 4) - the whole point of this test is that this volume, despite
    # being admitted for secondary, must never win it primary instead.
    assert result["secondary"][0]["phase_confirmed_frame_count"] == 36


def test_summarize_status_discovery_frames_single_leg_overlap_stays_excluded() -> None:
    """Phase confirmation in only *one* direction is deliberately not
    enough to lift the exclusion above - see the second exception's own
    docstring paragraph for why: it is indistinguishable from a
    different, already-paired blind's own ordinary, coincidental
    movement happening to overlap with just one of this blind's two
    legs. This is a direct, function-level regression test for exactly
    the real scenario
    test_calibration_excludes_other_blinds_frames_from_identity_result
    (tests/test_options_flow_calibration.py) already guards against
    end-to-end - same device ids, reproduced here at this function's own
    level: without also requiring "02" to be present, these three
    single-leg frames (fewer than this blind's own real candidate's one)
    would otherwise have been enough to admit the foreign device as a
    secondary identity, reopening that already-fixed regression.
    """
    frames = [
        # --- this blind's own motor: one genuine "up" frame ---
        {"device_id": "ABCDEF", "enum": "10", "command": "01", "phase": "opening"},
        # --- a different, already-paired blind, moving on its own
        # remote/automation during this capture window - three frames,
        # all during the same single leg, never during the other ---
        {"device_id": "112233", "enum": "05", "command": "01", "phase": "opening"},
        {"device_id": "112233", "enum": "05", "command": "01", "phase": "opening"},
        {"device_id": "112233", "enum": "05", "command": "01", "phase": "opening"},
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"112233"})
    )

    assert result["primary"] is not None
    assert result["primary"]["device_id"] == "ABCDEF"
    assert result["secondary"] == []
    assert result["groups"] == [result["primary"]]


def test_summarize_status_discovery_frames_both_commands_one_leg_excluded() -> None:
    """A narrower variant of the single-leg overlap above, caught by a
    second independent review of this same exception: a foreign device
    whose commands happen to include both "01" and "02" is *not* by
    itself proof it was driven through both of this blind's own legs -
    both frames can arrive during the *same* single leg (e.g. a "02" that
    genuinely phase-confirms during "closing", alongside an unrelated
    "01" that does not confirm anything there). An earlier version of
    this exception checked "01"/"02" presence in the group's commands as
    a whole, decoupled from which leg each confirming frame actually
    belonged to, and admitted exactly this case - reopening the
    single-leg cross-talk risk
    test_calibration_excludes_other_blinds_frames_from_identity_result
    (tests/test_options_flow_calibration.py) and the test above both
    guard against, just via a different pair of commands. Fixed by
    tracking phase-confirmed evidence *per leg*
    (phase_confirmed_opening_frame_count/phase_confirmed_closing_frame_
    count) and requiring both, rather than one aggregate count plus a
    command-presence check that said nothing about which leg either
    command's confirming frame (if any) came from.
    """
    frames = [
        # --- this blind's own motor, correctly phased on both legs ---
        {"device_id": "ABCDEF", "enum": "10", "command": "01", "phase": "opening"},
        {
            "device_id": "ABCDEF",
            "enum": "10",
            "command": "00",
            "phase": "opening_endstop",
        },
        {"device_id": "ABCDEF", "enum": "10", "command": "02", "phase": "closing"},
        {
            "device_id": "ABCDEF",
            "enum": "10",
            "command": "00",
            "phase": "closing_endstop",
        },
        # --- a different, already-paired blind: both its frames occur
        # during our own single "closing" leg only, never during
        # "opening" - one genuinely phase-confirms there ("02"), the
        # other is just an unrelated "01" that happens not to ---
        {"device_id": "112233", "enum": "05", "command": "02", "phase": "closing"},
        {"device_id": "112233", "enum": "05", "command": "01", "phase": "closing"},
    ]

    result = summarize_status_discovery_frames(
        frames, exclude_device_ids=frozenset({"112233"})
    )

    assert result["primary"] is not None
    assert result["primary"]["device_id"] == "ABCDEF"
    assert result["secondary"] == []
    assert result["groups"] == [result["primary"]]


def test_summarize_status_discovery_frames_unphased_capture_is_unaffected() -> None:
    """The manual "discover status" remote-listening window labels every
    frame with the same constant phase throughout (e.g. "remote_
    discovery"), which never matches a direction in
    _PHASE_CONFIRMING_COMMANDS - so every candidate ties at
    phase_confirmed_frame_count=0 there. Neither candidate here starts
    with the instructed first press ("01"/open) either, so they also tie
    at zero on remote_discovery_sequence_match_length, and the comparison
    falls through to the original distinct-commands/frame-count tiebreaker
    exactly as it did before either signal existed.
    """
    frames = [
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "00",
            "phase": "remote_discovery",
        },
        {
            "device_id": "5D3E7C",
            "enum": "01",
            "command": "02",
            "phase": "remote_discovery",
        },
        {
            "device_id": "AAAAAA",
            "enum": "02",
            "command": "02",
            "phase": "remote_discovery",
        },
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["primary"]["phase_confirmed_frame_count"] == 0
    assert result["primary"]["remote_discovery_sequence_match_length"] == 0


def test_summarize_status_discovery_frames_without_a_phase_key_is_unaffected() -> None:
    """Frames with no "phase" key at all (e.g. from before this field
    existed, or a caller that never set one) must not raise, and must
    behave exactly as if the phase-aware scoring did not exist.
    """
    frames = [
        {"device_id": "5D3E7C", "enum": "01", "command": "01"},
        {"device_id": "5D3E7C", "enum": "01", "command": "00"},
    ]

    result = summarize_status_discovery_frames(frames)

    assert result["primary"]["device_id"] == "5D3E7C"
    assert result["primary"]["phase_confirmed_frame_count"] == 0


# ---------------------------------------------------------------------------
# resolve_blind_identities(): extracted out of config_flow.py's
# _developer_details() (which still uses it) so diagnostics.py can share the
# exact same command/status identity resolution instead of a second,
# independently-drifting copy of it. Previously only exercised indirectly
# through _developer_details()'s own, translation-heavy config-flow tests.
# ---------------------------------------------------------------------------


def test_resolve_blind_identities_prefers_command_over_legacy_device_fields() -> None:
    data = {
        CONF_COMMAND_DEVICE_ID: "abcdef",
        CONF_COMMAND_ENUM: "1",
        CONF_DEVICE_ID: "111111",
        CONF_DEVICE_ENUM: "9",
        CONF_STATUS_DEVICE_ID: "abcdef",
        CONF_STATUS_ENUM: "1",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_MANUAL,
    }

    result = resolve_blind_identities(data)

    assert result["command_device_id"] == "ABCDEF"
    # command_enum itself is only ever upper()-cased, never zero-padded -
    # that only happens inside normalize_status_identity() below, for the
    # identity tuple specifically. "1" (not "01") is the correct,
    # pre-existing behavior carried over unchanged from _developer_details().
    assert result["command_enum"] == "1"
    assert result["primary_identity"] == ("ABCDEF", "01")
    assert result["status_identity_source"] == STATUS_IDENTITY_SOURCE_MANUAL


def test_resolve_blind_identities_falls_back_to_legacy_device_fields() -> None:
    """A subentry predating CONF_COMMAND_DEVICE_ID/CONF_COMMAND_ENUM."""
    data = {CONF_DEVICE_ID: "111111", CONF_DEVICE_ENUM: "9"}

    result = resolve_blind_identities(data)

    assert result["command_device_id"] == "111111"
    # Unpadded, same reasoning as the test above - only the identity tuple
    # itself gets zero-padded.
    assert result["command_enum"] == "9"
    # No explicit status identity at all -> falls back to the command one,
    # zero-padded here because it goes through normalize_status_identity().
    assert result["primary_identity"] == ("111111", "09")
    assert result["status_identity_source"] == "legacy"


def test_resolve_blind_identities_treats_unknown_source_as_no_primary_identity() -> (
    None
):
    """STATUS_IDENTITY_SOURCE_UNKNOWN means "not confirmed yet" even if
    CONF_STATUS_DEVICE_ID/CONF_STATUS_ENUM still hold a stale value from
    before the identity became unconfirmed (see async_step_edit's own
    duplicate-identity check, which relies on exactly this to avoid a false
    collision against an unconfirmed candidate).
    """
    data = {
        CONF_COMMAND_DEVICE_ID: "ABCDEF",
        CONF_COMMAND_ENUM: "01",
        CONF_STATUS_DEVICE_ID: "ABCDEF",
        CONF_STATUS_ENUM: "01",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_UNKNOWN,
    }

    result = resolve_blind_identities(data)

    assert result["primary_identity"] is None
    assert result["status_identities"] == ()


def test_resolve_blind_identities_orders_primary_before_secondary() -> None:
    data = {
        CONF_COMMAND_DEVICE_ID: "ABCDEF",
        CONF_COMMAND_ENUM: "01",
        CONF_STATUS_IDENTITY_SOURCE: STATUS_IDENTITY_SOURCE_MANUAL,
        CONF_SECONDARY_STATUS_IDENTITIES: [{"device_id": "111111", "enum": "2"}],
    }

    result = resolve_blind_identities(data)

    assert result["status_identities"] == (("ABCDEF", "01"), ("111111", "02"))
    assert result["secondary_status_identities"] == (("111111", "02"),)
