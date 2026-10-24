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

6. **The shared status-frame capture buffer has no per-operation owner, and two concurrent calibration/discovery flows for *different* blinds can and do steal it from each other — settled (2026-10-24): confirmed reachable through the real HA UI today.**
   `api.py`'s `_status_discovery_frames` is one slot per USB-stick hub, shared by every blind on it. `start_status_frame_capture()` doesn't queue or fail when a capture is already open — it forcibly destroys the existing one (discarding its contents, uncounted) and opens a fresh one for itself. `finish_status_frame_capture()` has no owner-check at all: it unconditionally tears down and returns whatever is *currently* live, whether or not that's the caller's own capture.
   Nothing in the integration prevents two different subentry flows (two "Calibrate"/"Add device" wizards for two different blinds) from running at once — confirmed no exclusivity guard exists anywhere (`manifest.json` has no `single_config_entry`, no lock, no `already_in_progress` check), and both flows share the one hub-wide `SchellenbergUsbApi` instance via `runtime_data`. This needs two browser tabs (or two people) open against the same Home Assistant instance at once — a normal way to use an integration whose "whole point," per its own code comments, is driving several blinds together.
   **Worst case, fully traced:** pairing a brand-new blind ("New") and recalibrating an already-working one ("Hallway") at the same time. Hallway's calibration start steals New's open capture; New's own remote operation then gets appended into Hallway's buffer; whichever flow finishes last receives *whichever buffer is currently live*, not necessarily its own. For a brand-new pairing specifically, there is no existing identity to sanity-check the result against (that protection only exists for recalibrating an already-confirmed blind), so the wrong identity gets written straight into permanent config data with zero UI warning. When the new entity registers that identity, it silently steals it from Hallway's own entity too (one `_LOGGER.warning` line, nothing user-visible) — breaking a second, previously-working blind as collateral damage.
   No existing test exercises this. The steal itself is a clean synchronous operation with no `await` in between — confirmed this is a semantic ownership bug, not a literal torn-write race condition (see finding 7 for an actual race).

7. **Unlocked, untracked `control_blind()` callers can steal/corrupt the hub-wide move-ack slot of an in-flight, lock-holding move or stop — a genuine asyncio race, distinct from finding 6.**
   `_move_awaiting_ack`/`_move_ack_event`/`_move_ack_result` (`api.py`) is a *single hub-wide slot*. `_handle_message()`'s t1/t0/tE reply handling attributes *any* reply to whichever call currently has `_move_awaiting_ack == True` — the wire protocol carries no correlation id, so this is only safe if at most one write is outstanding against this slot at a time. That invariant is enforced for move-vs-move and move-vs-stop (`_move_lock`) and for keeping `control_blind()` quiet during pairing/teach/raw-transmit's active window (`_pairing_active`) — but **not** in the other direction: several call sites call `control_blind()` directly, bypassing `_move_lock` entirely, and `control_blind()`'s only gate (`_transmit_capability_block_reason()`) never checks `_move_lock`/`_move_awaiting_ack` at all:
   - `config_flow.py`'s motor-direction test + its stop (`async_step_test_motor`) — reachable both from "Add shutter from remote" pairing and from "Test existing" on an already-live blind.
   - `_async_developer_command()` — the reconfigure wizard's "Test open/close/stop" on an already-live blind.
   - The post-teach open+stop test (`async_step_teach_motor`) — runs *after* `_pairing_active` is already cleared.
   - `__init__.py`'s `_handle_test_command()`, backing the public `schellenberg_usb.test_command` service — callable at any time, for any device, by any script or automation.
   **Concrete trace:** blind A's ordinary open/close is mid-ack-wait (its own normal up-to-1.0s window). Someone tests blind B via any path above; B's write goes out on the same wire while A's ack slot is still marked outstanding. The stick's reply for B gets attributed to A by `_handle_message()` (A's `_move_awaiting_ack` is still `True`) — so A's move resolves as "accepted" or "busy, retrying" based on a hardware reply that has nothing to do with A's own write. A "busy" misattribution can make A retry a command that may have already gone out cleanly, risking a real duplicate RF transmission.
   Confirmed as a genuine interleaving race (two coroutines actually overlapping at a real `await`), not a restatement of finding 6 — and confirmed, separately, that this does *not* corrupt pairing/teach/raw-transmit's own retry bookkeeping, since `control_blind()` refuses to write at all while `_pairing_active` is set.

## Medium severity

8. **`cover.py`'s own status-identity resolution in `async_setup_entry()` diverges from the canonical logic in `identities.py`.**
   `identities.py`'s `resolve_blind_identities()` nulls a stale `status_device_id` when the status identity source is `UNKNOWN`. `async_setup_entry()` doesn't, and that raw stale value briefly reaches `api.register_entity()` as this entity's primary status identity before the entity's own, separately-correct re-derivation corrects it — the first, wrong registration is never retracted. Not reachable through the current UI (today's write paths clean up the fields correctly); would strike with hand-edited or old/legacy config data.

9. **Calibration's cross-blind interference protection only guards the "movement detected" wait, not the final identity result.**
   If another already-paired, normally-working blind moves (its own automation, its own remote) *while* a different blind is being calibrated, that other blind's real frames can win the race and get wrongly stored as the identity of the blind currently being calibrated — without the collision check manual editing has. Confirmed as a real test gap, not just theoretical.

10. **USB-stick (hub) diagnostics download returns only a generic error instead of real data.**
    The diagnostics check only covers blind subentries, not the hub device itself.

11. **Incoming `"ss"` frame length check is stricter than necessary.**
    Could silently discard valid short frames; unclear whether the stick actually sends such short frames in practice.

12. **Unbounded buffer growth in the serial receive loop if no newline ever arrives.**
    `api.py`, `SchellenbergProtocol.data_received()`: `self.buffer += data.decode(...)` with nothing draining it except finding a `"\n"`. If a flaky USB-CDC connection, a firmware glitch, a brownout, or a misconfigured port ever stops producing a clean line terminator, the buffer grows without limit for as long as that lasts — and since Python strings are immutable, each `+=` also re-copies the whole buffer, so per-chunk CPU cost grows too, not just memory. A reconnect clears it, but nothing detects runaway growth and forces one on its own.

13. **`_last_received_messages` grows without bound, keyed directly off unvalidated wire bytes, and isn't cleared by a reconnect.**
    `identity = (normalized_device_id, normalized_device_enum)` is built from raw substrings of the decoded line with no validation (unlike `identities.normalize_status_identity()`, used everywhere else). The write happens unconditionally, before checking whether this identity is even registered. No cap or cleanup exists except a full config-entry reload or an HA restart. In a noisy RF environment, every distinct spurious/corrupted frame becomes a permanent entry, accumulating indefinitely.

14. **(Lower confidence/severity) Capture-window frame list has no cap on frame count.**
    `_status_discovery_frames` is appended to for every `"ss"` frame received while a capture window (up to 300s/45s) is open, with no size limit. The real calibration decision is still properly validated downstream, so garbage can't masquerade as a real identity — but the raw list itself has no cap during the window.

15. **Reloading the hub for one blind's subentry change silently discards another blind's in-progress calibration/discovery capture.**
    Every subentry add/remove/edit reloads the whole hub entry, building a brand-new `SchellenbergUsbApi` instance. The calibration flow fetches `entry.runtime_data` fresh on every call with no caching — so if a reload happens between a capture's start and finish, finish reads the *new*, never-started instance (empty) while the *old* instance, which actually buffered the blind's real frames, is dropped. Degrades gracefully (no live position feedback) rather than corrupting an existing identity — silent either way. The codebase already fixed this exact "reload replaces runtime_data" hazard for a *different* piece of state (`_fresh_calibration_positions`) — the status-discovery buffer never got the same treatment. Smaller related inaccuracy: a reload caused by a sibling blind mid-pairing gets reported as an ordinary "pairing timeout."

16. **Removing a blind's subentry doesn't cancel its in-flight move/stop; only the side-effect hub reload eventually does, hub-wide.**
    There's no "remove blind" code in this integration — subentry deletion goes through Home Assistant's generic UI, which only triggers the same generic full reload as any other change. `cover.py` cancels only its position-polling task on removal, never the move/stop itself — there's no task handle kept anywhere to cancel. The only thing that unblocks an in-flight retry loop is `disconnect()`, which only runs once the subsequent reload's unload phase reaches it, and then wakes every blind on the hub, not just the removed one. Between removal and that point, a just-deleted blind's retry loop can still transmit a real RF command, or process a stray ack, for a blind HA no longer has an entity for. Whether a still-in-flight `async_write_ha_state()` on the now-removed entity raises or no-ops is Home-Assistant-version-dependent and wasn't independently confirmed.

## Low / cosmetic

17. A logging field for blind moves is never updated (diagnostics-only impact, no functional bug).
18. A percentage calculation has no floor, relevant only for very short/distorted travel times.
19. A brief, self-correcting LED flicker at Home Assistant startup.
20. A dead translation key that happens to be identical across all languages anyway.
21. The "Copy diagnostics" text export shows literal `None` instead of `--` for the previous position on a freshly-set-up or just-restarted blind (the live dev-tools panel already handles this correctly). Purely cosmetic.
22. No listener for Home Assistant Core stopping exists at the hub/API level (only at the entity level, where it's handled correctly) — the serial transport is never deliberately closed on Core stop/restart, just abandoned to process exit. Low-impact (nothing buffered to flush; HA's own shutdown sequence cancels remaining tasks anyway).
23. A translation that's never shown: `"superseded_by_calibration"` has proper, distinct translations in three languages, but the only code path that produces this value (finding 6's steal branch) immediately discards its return value — nothing ever stores or displays it.
24. `control_blind()` never sets `_last_transmit_source`, so after any direct `control_blind()` write (the same call sites as finding 7), t1/t0 log lines and the `_busy_latched` reset reflect whatever the *previous* caller's source was. Diagnostic/log-attribution only — `_busy_latched` is documented as never blocking a new command regardless.

## Also checked, found clean — not bugs

- The documented secondary-remote-control direction limitation's own handling in `cover.py` — correct as implemented.
- Translation consistency across `strings.json` and all four `translations/*.json` files — no deviation found.
- Test suite ordering / shared-state issues — none found; a sampled changelog claim matched its test.
- The "Hybrid" pairing workflow can still reach full calibration even though three places in code/changelog explicitly describe this as excluded — **documentation/test gap only, not a functional bug.**
- Every slice/substring operation on wire data in `_handle_message()` is a plain Python string slice — never raises regardless of length/content.
- No wire-derived value is ever used for indexing or passed to `int()`/similar without validation.
- `data.decode("ascii", errors="ignore")` means stray non-ASCII noise can never raise `UnicodeDecodeError`.
- The `"tE"` (busy) retry mechanism dedupes in-flight retry tasks, so a flood of `"tE"` replies can't spawn concurrent retries or grow any structure.
- `verify_device()`, `get_device_id()`, `pair_device_and_wait()`, `async_discover_status_identities()` all bound their waits with `asyncio.wait_for(...)` — no unbounded await tied to incoming serial data.
- Several per-blind-keyed dicts (`_last_position_updates`, `_last_manual_position_syncs`, `_registered_devices`, `_registered_entity_keys`, `_fresh_calibration_positions`, `_last_primary_tracking_messages`) don't share finding 13's or finding 6's cross-blind risk — all bounded by configured blind count or written only for already-registered entities.
- `teach_motor()`, `send_raw_transmit()`, `pair_device_and_wait()` — confirmed clean of finding 6's mechanism (separate, correctly-scoped `_pairing_active` flag) and mutually exclusive with each other via a clean check-then-set with no intervening `await`.
- `_foreign_capture_device_ids()` — confirmed scoped exactly as documented, only to the movement-detection wait signal; not a new gap beyond findings 6 and 9.
- `_PriorityLock`/`_move_lock` move-vs-move and move-vs-stop serialization, including the post-`release()` read of `_move_ack_result` — confirmed correctly synchronized, no interleaving possible there.
- `send_command()`'s `_transmit_lock` + `_pending_retry_command`/`_retry_task` bookkeeping — whole critical section has no `await` inside it, runs atomically.
- `data_received()`/`_handle_message()` are pure synchronous callbacks with no internal `await`, so they can't be preempted mid-execution by each other (the real risk is them running *during another coroutine's* await — see finding 7).
- Dispatcher subscribe/unsubscribe in the calibration movement-wait — correctly scoped per-call, including under cancellation.
- `connect()`/`disconnect()`/`reset_and_reconnect()` reentrancy guards — all check-then-set with no intervening `await`.
- No config-entry migration exists (`VERSION` has always been `1`) — genuinely nothing to migrate, not an oversight. The one real data-shape migration (legacy-unique-ID backfill) is naturally idempotent and re-derived fresh on every setup, so an interrupted run can't leave an inconsistent state.
- Startup ordering (`runtime_data` set before connecting starts) and the early-service-call race (`test_command` before the hub finishes connecting) — both handled cleanly.
- Shutdown via `disconnect()` while pairing/teach is in flight — both fail cleanly with proper `finally` cleanup, no stuck state.
- Per-entity `EVENT_HOMEASSISTANT_STOP` handling — correctly self-cleaning, freezes the position estimate (contrast with finding 22's hub-level gap).

## Audit status — breadth-of-angle coverage complete

Eight distinct angles have now each had at least one dedicated audit pass: entity/cover-state layer; config-flow/calibration layer; translations + build files; cross-cutting data-path tracing + test suite; security/serial-data robustness; the shared capture-buffer/identity mechanism; asyncio concurrency/race conditions; and Home Assistant entity/config-entry lifecycle robustness. The last two rounds (concurrency, lifecycle) each still turned up genuine new findings (7, 15, 16, 24 above) rather than coming back clean.

That doesn't reset the plan. The criterion for "thoroughly audited" was covering this full, named breadth — not every round individually coming back empty — and that breadth is now covered. No further rounds are being dispatched automatically from here. Chasing additional, increasingly narrow angles beyond this list would mean inventing new categories on the fly rather than following a bounded methodology, and isn't planned unless specifically asked for.

Nothing above is fixed yet — all of it, including finding 7 (probably the most practically significant thing found across the whole audit: an ordinary motor test on one blind can corrupt another blind's in-flight move/stop state), is scoped for a future release.
