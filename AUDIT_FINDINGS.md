# Audit Findings — Unfixed (deferred to a future release)

Tracking doc for issues found during the post-v1.2.0 (PR #70) audit rounds. **Nothing below has been fixed yet** — fixes are intentionally deferred to a future release, once repeated audit passes stop turning up anything new. This file exists so a new session or a new audit agent doesn't have to reconstruct the list from conversation history, and so further rounds can be told "these are already known, only report genuinely new issues."

## High severity

1. **`cover.py` — a failed auto-stop at target position is silently ignored.**
   In `_async_position_update_loop()`, when a `set_cover_position()` leg reaches its intermediate target, the return value of `control_blind_stop()` is never checked before `is_opening`/`is_closing`/target-position tracking state is unconditionally cleared. If the stop call fails (a USB hiccup, or another blind's pairing/teach/raw-transmit active on the same hub at that moment), the motor keeps running to its real physical end stop, but Home Assistant permanently reports the wrong position — and discards the motor's later, genuine stop confirmation because the tracking state was already torn down.

2. **`cover.py` — a stale leftover target from a failed position move can prematurely stop an unrelated later move.**
   If a `set_cover_position()` call fails and leaves an old target in place, a later, completely unrelated move triggered by the original remote control can be auto-stopped at that stale, long-invalid target. Related to the already-discussed and already-correct secondary-remote-control direction limitation, but a distinct bug of its own, not an instance of that known limitation.

3. **`options_flow_calibration.py` — a single timeout during calibration permanently kills status-identity discovery for the rest of that run.**
   Every error exit from either calibration leg tears down the capture state. A retry after the error only resets the UI phase, not the capture itself — it looks like it succeeded, but the blind ends up at "status unknown" with no position tracking, with no indication the earlier timeout was the cause.

4. **`config_flow.py` — "Determine status from original remote" can silently overwrite a working blind's status identity.**
   If the detection attempt finds nothing (wrong button, pressed too fast, RF interference), there is no warning and no protection of the kind recalibration already has. Clicking "Save" anyway erases the previously-working identity mapping.

5. **`api.py` — `control_blind_stop()` can overrun its own `STOP_RETRY_BUDGET` by up to ~1 second.**
   The loop's final ack-wait uses the fixed `MOVE_ACK_TIMEOUT` (1.0s) instead of the remaining time left in the budget. Needs lock contention from another near-simultaneous blind command to have already eaten most of the 2.0s budget, *plus* a slow/lost ack for the stop write itself. When it fires, the actual stop write has normally already gone out (the shutter still stops on time) — visible only via two warning log lines (`No t1/tE reply to stop write...`, `Giving up on stop after...falling back to a plain write`), not via the shutter's physical behavior — unless the original write itself (not just its ack) was the thing that got lost, in which case a retry really does delay the physical stop by a similar margin.
   The identical pattern exists in the ordinary move-retry path (`control_blind_with_retry`), but is immaterial there because its overall budget (`MOVE_RETRY_OVERALL_TIMEOUT` = 15.0s) makes the same ~1s slip negligible — not tracked as a separate finding.

## Medium severity

6. **Shared capture buffer for calibration/pairing/discovery has no per-operation owner.**
   Two concurrent operations for different blinds on the same stick can steal each other's captured frames. Confirmed mechanism in the code; not yet settled whether it's reachable through the real HA UI as things stand today. Findings 7 and 8 below are downstream consequences of this same root cause, each reachable via its own independent path.

7. **`cover.py`'s own status-identity resolution in `async_setup_entry()` diverges from the canonical logic in `identities.py`.**
   `identities.py`'s `resolve_blind_identities()` nulls a stale `status_device_id` when the status identity source is `UNKNOWN`. `async_setup_entry()` doesn't, and that raw stale value briefly reaches `api.register_entity()` as this entity's primary status identity before the entity's own, separately-correct re-derivation corrects it — the first, wrong registration is never retracted. Not reachable through the current UI (today's write paths clean up the fields correctly); would strike with hand-edited or old/legacy config data.

8. **Calibration's cross-blind interference protection only guards the "movement detected" wait, not the final identity result.**
   If another already-paired, normally-working blind moves (its own automation, its own remote) *while* a different blind is being calibrated, that other blind's real frames can win the race and get wrongly stored as the identity of the blind currently being calibrated — without the collision check manual editing has. Confirmed as a real test gap, not just theoretical.

9. **USB-stick (hub) diagnostics download returns only a generic error instead of real data.**
   The diagnostics check only covers blind subentries, not the hub device itself.

10. **Incoming `"ss"` frame length check is stricter than necessary.**
    Could silently discard valid short frames; unclear whether the stick actually sends such short frames in practice.

11. **Unbounded buffer growth in the serial receive loop if no newline ever arrives.**
    `api.py`, `SchellenbergProtocol.data_received()` (~lines 2488–2512): `self.buffer += data.decode(...)` with nothing draining it except finding a `"\n"`. If a flaky USB-CDC connection, a firmware glitch, a brownout, or a misconfigured port (pointed at a different, non-Schellenberg device) ever stops producing a clean line terminator, the buffer grows without limit for as long as that lasts — and since Python strings are immutable, each `+=` also re-copies the whole buffer, so per-chunk CPU cost grows too, not just memory. A reconnect clears it (fresh `SchellenbergProtocol` instance), but nothing *detects* runaway growth and forces a reconnect on its own — no circuit breaker. Minor secondary note: no cap on how many lines one `data_received()` call drains in its `while "\n" in self.buffer` loop (bounded in practice by the transport's chunk size, not independently verified).

12. **`_last_received_messages` grows without bound, keyed directly off unvalidated wire bytes, and isn't cleared by a reconnect.**
    `api.py`, `_handle_message()`'s `"ss"` handler (~lines 818–858): `identity = (normalized_device_id, normalized_device_enum)` is built from raw substrings of the decoded line with no validation (contrast with `identities.normalize_status_identity()`, used everywhere else to strictly validate hex IDs/enums, but not here). The write to `_last_received_messages[identity]` happens unconditionally, before the code even checks whether this identity is a registered entity. No cap or cleanup exists anywhere except a full config-entry reload (adding/removing/editing a blind subentry) or a Home Assistant restart — `handle_connection_lost()` resets other connection state on every disconnect but does not touch this dict, so reconnects don't help. In a noisy RF environment, every distinct spurious/corrupted frame with a never-seen device_id/enum pair becomes a permanent entry, accumulating indefinitely on a long-running instance with no blind reconfiguration. (Also applies, more sharply, under a genuinely adversarial framing — anything able to inject arbitrary bytes onto the serial line gets a cheap memory-growth primitive here, not just a flaky-hardware one.)

13. **(Lower confidence/severity) Capture-window frame list has no cap on frame count.**
    `api.py` (~lines 867–874): `_status_discovery_frames` is appended to for every `"ss"` frame received while a calibration (up to 300s) or discovery (up to 45s) window is open, with no size limit. `summarize_status_discovery_frames()` does properly validate every frame through `normalize_status_identity()` before it can affect the actual calibration result, so garbage can't masquerade as a real identity — but the raw, unfiltered list itself (also returned as the result's `"frames"` key, which callers may store/display) has no cap during the window. Needs sustained RF noise specifically coincident with an open, user-initiated calibration flow, so narrower/less likely than 11–12.

## Low / cosmetic

14. A logging field for blind moves is never updated (diagnostics-only impact, no functional bug).
15. A percentage calculation has no floor, relevant only for very short/distorted travel times.
16. A brief, self-correcting LED flicker at Home Assistant startup.
17. A dead translation key that happens to be identical across all languages anyway.
18. The "Copy diagnostics" text export shows literal `None` instead of `--` for the previous position on a freshly-set-up or just-restarted blind (the live dev-tools panel already handles this correctly — only the text export doesn't). Purely cosmetic.

## Also checked, found clean — not bugs

- The documented secondary-remote-control direction limitation's own handling in `cover.py` — correct as implemented.
- Translation consistency across `strings.json` and all four `translations/*.json` files — no deviation found.
- Test suite ordering / shared-state issues — none found; a sampled changelog claim matched its test.
- The "Hybrid" pairing workflow can still reach full calibration even though three places in code/changelog explicitly describe this as excluded — **documentation/test gap only, not a functional bug.**
- Every slice/substring operation on wire data in `_handle_message()` is a plain Python string slice — never raises regardless of length/content; truncated/garbage lines simply match no branch and fall through safely.
- No wire-derived value is ever used for indexing or passed to `int()`/similar without validation; the file's one `int(..., 16)` call operates on HA-config data, not wire data, and is already wrapped in `try/except`.
- `data.decode("ascii", errors="ignore")` means stray non-ASCII noise can never raise `UnicodeDecodeError`.
- The `"tE"` (busy) retry mechanism dedupes in-flight retry tasks, so a burst/flood of `"tE"` replies can't spawn concurrent retries or grow any structure (it can only extend wall-clock retrying, which is already an intentional, documented, no-limit-by-design policy).
- `verify_device()`, `get_device_id()`, `pair_device_and_wait()`, `async_discover_status_identities()` all bound their waits with `asyncio.wait_for(..., timeout=...)` — no unbounded await tied to incoming serial data.
- `_last_position_updates`, `_last_manual_position_syncs`, `_registered_devices`, `_registered_entity_keys`, `_fresh_calibration_positions` are keyed off HA-config-derived identities (bounded by configured blind count) and rebuilt from scratch on a full reload — they don't share finding 12's unbounded-growth risk.
- `_last_primary_tracking_messages` is only written for an already-registered real cover, so unmatched garbage frames can't inflate it the way they can finding 12's dict.

## Audit status

Three initial specialized rounds (entity/cover-state layer; config-flow/calibration layer; translations + build files) plus a cross-cutting round (data-path tracing across layers, and the test suite as its own subject) are done. That cross-cutting round still turned up new findings (7–9 above, plus the hybrid-pairing doc gap), so a further round was dispatched targeting two angles:

- a security/robustness pass (malformed/corrupted serial data, resource exhaustion) — **done, findings 11–13 above are its result.**
- a deep dive on the shared capture-buffer/identity mechanism specifically (root cause behind findings 6 and 8) — **still in progress.**

Plan: keep running further targeted rounds until one comes back with nothing new. Nothing gets fixed yet — all of this is scoped for a future release once the audit is exhausted.
