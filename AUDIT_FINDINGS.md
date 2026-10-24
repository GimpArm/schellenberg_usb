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

## Low / cosmetic

11. A logging field for blind moves is never updated (diagnostics-only impact, no functional bug).
12. A percentage calculation has no floor, relevant only for very short/distorted travel times.
13. A brief, self-correcting LED flicker at Home Assistant startup.
14. A dead translation key that happens to be identical across all languages anyway.
15. The "Copy diagnostics" text export shows literal `None` instead of `--` for the previous position on a freshly-set-up or just-restarted blind (the live dev-tools panel already handles this correctly — only the text export doesn't). Purely cosmetic.

## Also checked, found clean — not bugs

- The documented secondary-remote-control direction limitation's own handling in `cover.py` — correct as implemented.
- Translation consistency across `strings.json` and all four `translations/*.json` files — no deviation found.
- Test suite ordering / shared-state issues — none found; a sampled changelog claim matched its test.
- The "Hybrid" pairing workflow can still reach full calibration even though three places in code/changelog explicitly describe this as excluded — **documentation/test gap only, not a functional bug.**

## Audit status

Three initial specialized rounds (entity/cover-state layer; config-flow/calibration layer; translations + build files) plus a cross-cutting round (data-path tracing across layers, and the test suite as its own subject) are done. That cross-cutting round still turned up new findings (7–9 above, plus the hybrid-pairing doc gap), so a further round is in progress targeting:

- the shared capture-buffer/identity mechanism specifically (root cause behind findings 6 and 8 — deserves its own deep look), and
- a security/robustness angle (malformed or adversarial serial data, resource exhaustion) not yet covered by any prior round.

Plan: keep running further targeted rounds until one comes back with nothing new. Nothing gets fixed yet — all of this is scoped for a future release once the audit is exhausted.
