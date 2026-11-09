# Schellenberg USB for Home Assistant

[![GitHub Release](https://img.shields.io/github/release/Freedreams93/schellenberg_usb.svg?color=red)](https://github.com/Freedreams93/schellenberg_usb/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Freedreams93/schellenberg_usb/blob/main/LICENSE)
![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/Freedreams93/schellenberg_usb/build-test.yaml?color=orange)

Control Schellenberg RF shutters and blinds in Home Assistant with a Schellenberg
USB FunkStick.

The integration guides you through teaching the USB stick to a motor, testing
movement, measuring travel times, and discovering status messages from the original
remote. It also provides estimated position tracking, manual position correction,
and built-in diagnostics.

![Schellenberg](https://raw.githubusercontent.com/GimpArm/schellenberg_usb/main/images/schellenberg-logo.png)

## Important safety and limitation notes

> [!WARNING]
> This is a community integration and is not affiliated with or supported by
> Schellenberg.

> [!CAUTION]
> Shutter motors may involve mains voltage. Do not open a motor, wall switch, or
> electrical enclosure, and do not modify wiring unless you are qualified to do so.
> Follow the instructions supplied with your motor and remote.

Please keep these limitations in mind:

- RF control cannot confirm that the motor received or carried out a command.
- USB stick responses such as `t1` and `t0` confirm only that the stick started and
  finished transmitting. They do **not** prove that the blind moved.
- Home Assistant does not receive an absolute physical position from the motor.
  Position is estimated from commands, recognized status messages, and measured
  travel times.
- Estimated position can drift if a radio message is missed. You can correct it
  with the manual position sync actions.

## Features

- Open, close, and stop control
- Support for multiple blinds
- Guided pairing and motor teach-in
- Automatic travel-time calibration
- Status identity discovery during calibration
- Guided **Discover status from original remote** action
- Support for secondary RF identities
- Estimated position tracking
- Manual position sync: fully open, fully closed, or any value from 0–100%
- Stable Home Assistant entity unique IDs
- Diagnostics and troubleshooting tools
- Compatibility with legacy blind configurations
- Available in English, German, Spanish, and French

## Requirements

- Home Assistant
- A Schellenberg USB FunkStick
- A compatible Schellenberg RF shutter or blind motor
- The original Schellenberg remote, strongly recommended for pairing and status
  discovery. Another method is also suitable if it can put the correct motor or
  channel into learning mode.
- USB access from the Home Assistant host

When available, select a stable serial path such as
`/dev/serial/by-id/...` instead of a changing path such as `/dev/ttyACM0`.

## Installation

### HACS custom repository

Unless this integration is listed in the default HACS catalog, add it as a custom
repository:

1. Open HACS in Home Assistant.
2. Open the HACS menu and choose **Custom repositories**.
3. Add this GitHub repository and select **Integration** as the category.
4. Find **Schellenberg USB** in HACS and install it.
5. Restart Home Assistant.

### Manual installation

1. Copy `custom_components/schellenberg_usb` into
   `/config/custom_components/schellenberg_usb`.
2. Confirm that `manifest.json` is directly inside that folder.
3. Restart Home Assistant.

### Updating an existing installation

Replace or update the integration files, then restart Home Assistant. A full restart
is recommended because a simple configuration reload may not load changed Python
files.

Recent versions assign every blind a stable unique ID. Existing blinds without one
are migrated automatically during setup. After restarting, entities should support
normal Home Assistant management such as renaming, area assignment, icon changes,
and entity registry settings. The default entity name also avoids repeated names
such as `cover.extension_0_extension_0`.

## Quick start: add your first blind

1. Go to **Settings > Devices & services**.
2. Select **Add integration**, search for **Schellenberg USB**, and add it.
3. Select the USB stick path. Prefer `/dev/serial/by-id/...` when offered.
4. Open the Schellenberg USB integration and choose **Add device**.
5. Select **Pair a device and calibrate it**.
6. Follow the on-screen instructions.
7. Start pairing. Home Assistant listens for pairing/status information and sends
   the teach sequence to the USB stick.
8. Give the device a friendly name, for example `Livingroomshutter`.
9. Run the calibration and save the device.

### Other setup methods

Besides the recommended **Pair a device and calibrate it**, the menu offers:

- **Add a shutter from a remote** can pair and then run a short movement test
  before calibration. Its optional remote-programming step also does not require
  waiting for an additional motor signal.

The same pairing method works for any Schellenberg RF receiver - electronic
belt/strap winders, awning motors, and similar motorized devices included -
which is why the menu and this guide both say "device" rather than "blind".

## Pairing and teach-in explained simply

The USB FunkStick acts like an additional remote control. Before it can move a
blind, the motor must learn and authorize it.

Use the original Schellenberg remote or the product manual to put the correct motor
or remote channel into learning mode. Then run the integration's pairing or
**Teach motor / activate USB transmitter** action. The integration sends the
required teach sequence and performs a short Open/Stop test.

If the motor moves during the test, pairing was probably successful. Always confirm
movement yourself: the stick's ACK only confirms transmission.

### Product-specific pairing examples

The original product manual takes precedence. Button combinations vary by motor,
remote, and timer model.

#### ROLLODRIVE 65 PREMIUM / 75 PREMIUM

Common article numbers include 22567, 22576, 22578, 22726, 22727, 22728, and
22767.

1. Press and hold **Sun** and **Up** together.
2. Keep holding for about five seconds until the LED flashes.
3. Continue the teach/pair action in Home Assistant.

#### ROLLOPOWER PLUS / STANDARD

Common article numbers include 20106, 20110, 20406, 20410, 20610, 20615, 20620,
20640, 20710, 20720, and 20740.

These motors are often paired through the connected Schellenberg remote control or
timer switch. Follow that device's manual to enter learning mode.

#### Radio shutter motors PREMIUM

Common article numbers include 21106, 21110, 21210, 21220, and 21240.

Use the pairing sequence for the remote control or timer assigned to the motor.
The required button combination depends on the control device.

## Calibration explained simply

Calibration measures how long the blind takes to travel fully open and fully
closed. Home Assistant uses those times to estimate its position:

- `100%` means fully open.
- `0%` means fully closed.

During calibration:

1. Start with the blind completely closed.
2. Press the Open/Up control when the dialog asks you to start.
3. The integration measures the physical open travel time until the blind stops.
4. Follow the prompt to move the blind completely closed.
5. Press the Close/Down control when asked; the integration measures the physical close travel time.
6. Review and save the measured times.

Calibration always measures physical Open first and physical Close second. The
normal `invert_direction` setting does not reverse these two calibration legs.
The optional pairing instructions do not require waiting for an additional motor signal.

Calibration does not install or detect an absolute position sensor. It measures
travel time and may also discover useful RF status messages.

If automatic calibration gives wrong travel times, you can recalibrate or enter
the travel times manually.

While calibration runs, the integration groups received frames by their radio
identity. If it hears a clear Open, Close, and Stop stream, it saves that group as
the primary status identity. Companion groups can be saved as secondary identities
for diagnostics without affecting position.

## Status discovery from the original remote

Use **Discover status from original remote** when:

- Home Assistant can move the blind, but using the physical remote does not update
  the estimated position.
- Diagnostics says that no matching frame has been received.
- Calibration did not discover a primary status identity.

To run discovery:

1. Open the blind's configuration menu.
2. Choose **Developer tools > Discover status from original remote**.
3. Select the correct shutter channel on the original remote.
4. Press **Open**.
5. Press **Stop**.
6. Press **Close**.
7. Press **Stop**.
8. Review the detected primary and secondary identities, then save.

If no clear status stream is found, the blind can still be controlled. Position
will continue to be estimated from Home Assistant commands and travel times.

## Position tracking

Home Assistant estimates position using:

- Open, Close, and Stop commands sent by Home Assistant
- Recognized Open, Close, and Stop frames from the original remote or motor
- The measured open and close travel times
- Manual position corrections

There is no encoder or absolute physical position sensor in this integration. If
the stick misses RF frames, or if the blind is moved outside Home Assistant without
a recognized status frame, the estimate may drift.

### Multiple remotes

A Schellenberg motor can usually learn and answer to several remotes, or several
remote channels, at once - that's a property of the motor's own hardware, not a
limit of this integration. Home Assistant's own position tracking is narrower
than that: it only recognizes the specific RF identities it has actually
discovered and saved as the primary (and optional secondary) status identity,
during calibration or **Discover status from original remote**.

In practice:

- Moving the blind with a remote or channel that was included when you ran
  discovery/calibration keeps the estimated position in sync, the same as a
  command sent from Home Assistant itself.
- Moving it with a *different* remote or channel - one Home Assistant has never
  seen a recognized frame from - still moves the blind normally, but Home
  Assistant has no way to notice. The estimated position will drift until a
  manual position sync or the next Home Assistant-initiated move corrects it.
- If you regularly use more than one remote on the same motor, run **Discover
  status from original remote** once per remote. A frame stream that doesn't
  match the primary identity is kept as a secondary identity (visible in
  [Diagnostics](#diagnostics)) instead of being mixed into position tracking.
- Pressing a remote or wall switch that *is* saved as a secondary identity still
  does not feed position tracking, but does cause one small, temporary effect on
  the dashboard: the cover briefly shows as opening/closing - a guess toward
  whichever end stop is farther from the current position, not a decoded
  direction - until the primary identity's own echo of the same move confirms or
  corrects it a moment later, or a few seconds pass with no such echo and the
  guess is automatically withdrawn. The tracked position percentage itself is
  never touched by this, only the opening/closing indicator.

## Manual position sync

Open the blind's **Developer tools** menu and use:

- **Set position fully open** to set the displayed position to `100%`
- **Set position fully closed** to set it to `0%`
- **Set position manually** to enter any value from `0–100%`

For example:

- If the blind is physically closed but Home Assistant shows `8%`, choose
  **Set position fully closed**.
- If the blind is physically open but Home Assistant shows `96%`, choose
  **Set position fully open**. If this happens often, recalibrate or adjust the
  travel time.

A manual sync is marked as manually confirmed. Position becomes estimated again
after the next movement.

## Diagnostics

The blind's **Developer tools** menu provides a readable diagnostic summary,
including:

- USB stick connection, mode, and ready/busy state
- Current transmit target
- Primary status identity
- Secondary status identities
- Last matched frame
- Last primary tracking frame
- Last secondary frame
- Last position update
- Calibration history and end reason
- Status identity candidates observed during calibration or discovery

The primary identity is the preferred source for position tracking. Secondary
identities are often harmless companion messages from the same motor or remote.
Unknown commands on a secondary identity are normal and are ignored for position.

Use **Copy diagnostics** when preparing an issue report. Diagnostics may contain
device identifiers, so review the output before posting it publicly.

## Troubleshooting

### The integration does not appear

- Confirm that the folder is
  `/config/custom_components/schellenberg_usb`.
- Confirm that `manifest.json` is directly inside that folder.
- Restart Home Assistant.
- Refresh or clear the browser cache if the integration still does not appear.

### The USB stick is not connected

- Prefer a stable `/dev/serial/by-id/...` path.
- Check USB passthrough when Home Assistant runs in a virtual machine or container.
- Unplug and reconnect the stick, then restart Home Assistant.
- Use **Developer tools > Reset stick / reconnect serial**.

### The blind does not move from Home Assistant

- The motor may not have learned the USB stick.
- Put the correct motor/channel into learning mode and repeat pairing or teach-in.
- Move the USB stick closer to the motor.
- Make sure you selected the correct blind or channel during teach-in.
- Remember that an ACK confirms only USB stick transmission, not motor movement.

### The blind moves from Home Assistant but remote position updates are missing

- Run **Discover status from original remote**.
- Check diagnostics for a primary status identity and a last primary tracking
  frame.
- Position can still be estimated from Home Assistant commands even without remote
  status tracking.

### The displayed position is wrong

- Use a manual position sync.
- Run calibration again.
- Adjust the open and close travel times in **Advanced settings**.
- If it consistently stops at `96–98%` or `2–4%`, slightly adjust the travel time
  or use manual sync.

### Diagnostics shows unknown secondary frames

This is usually normal. Secondary identities may send companion commands that are
not yet interpreted. They remain visible in diagnostics but are ignored for
position tracking.

### The entity cannot be renamed or managed

Recent versions use stable unique IDs:

1. Update the integration files.
2. Restart Home Assistant.
3. Reload the integration page.

Legacy blind entries are migrated automatically. After migration, Home Assistant
should allow renaming, area assignment, icon changes, and other entity registry
settings.

### Developer tools refuse commands (pending transmit or busy stick)

In v1.0.3, two bookkeeping bugs in the USB stick handling (`api.py`) could make
**Developer tools** refuse commands although the stick was working. Both are
fixed in v1.0.4. Neither of them switched cover control off: apart from the
single dropped command described below, cover commands were still sent normally.

- **`transmit is pending for payload sr`** (or `!?`, `hello`, and similar):
  plain requests to the stick, such as the device ID query, were remembered as a
  pending RF transmission. Only the `t0` acknowledgement of a real transmission
  clears that state, so after connecting, the Developer tools commands,
  **Teach motor**, and the ready state shown by diagnostics and by
  **Reset stick / reconnect serial** reported the stick as not ready until a
  blind had completed one normal transmission. If you cannot update yet, operate
  a blind once, for example with **Stop**, and try again.
- **`stick reported busy repeatedly and needs a reset`**: the retry counter for
  "stick busy" answers was never reset after a successful transmission. It
  counted over the whole connection instead of per command, so the fourth busy
  answer since the connection was opened, however long after the first, was not
  retried. That one command was dropped and the blind did not move, the log
  showed `Transmit retry limit reached ... latching busy state`, and Developer
  tools stayed blocked. If you cannot update yet, use
  **Developer tools > Reset stick / reconnect serial** or restart Home Assistant.
  As of this version, a "stick busy" reply has no retry limit at all - it is
  retried for as long as it keeps happening - so this symptom cannot recur
  even on a long-lived connection; that log line no longer exists.

## Advanced settings

Most users do not need these values. They are available for unusual installations
or manual recovery:

- Command/transmit device ID and enum
- Primary status device ID and enum
- Additional secondary status identities
- Open and close travel times
- Invert direction
- Raw RF payload testing
- Reset/reconnect and detailed diagnostics

Example identities in this documentation are intentionally fake:

- Command identity: `ABC123/10`
- Status identity: `123ABC/0D`

To enable debug logging, add this to `configuration.yaml` and restart or reload
logging:

```yaml
logger:
  logs:
    custom_components.schellenberg_usb: debug
```

For a local development checkout, copy the integration directory into
`/config/custom_components`, restart Home Assistant, and inspect
**Settings > System > Logs**. HACS may overwrite locally copied test files during
an update.

## Optional: fine position control and Alexa voice control (example configuration)

> [!NOTE]
> This is not part of the integration itself. It is an example configuration
> contributed by a user, built entirely from standard Home Assistant building
> blocks (a helper, two scripts, and template covers) on top of the `cover.*`
> entities this integration already creates. Names, entity IDs, and travel
> times below are examples — replace them with your own.

> [!IMPORTANT]
> As of the version adding `MOVE_COMMAND_EXTRA_RESENDS`/`STOP_COMMAND_EXTRA_RESENDS`
> (see `CHANGELOG.md`), the integration itself already re-sends a lost
> open/close command twice, re-sends a lost stop command twice, and already
> widens its own stop-call timing when it detects another blind's command
> using the radio at that moment — the same reliability problems the two
> scripts below were written to work around. The scripts below have
> therefore had their own, now-redundant copies of the first two removed:
> - The double-send behind `min_fahrzeit_doppel` (open/close sent twice,
>   0.3s apart, for a long enough move) is gone — the integration now does
>   this for you on every open/close call.
> - The conditional stop retry (wait up to 3s for the entity to actually
>   report stopped, resend `stop_cover` once if it didn't by then) is gone
>   too — the integration now resends a lost stop command on its own.
>
> If you're still running an older copy of either script with these still
> in place, replace it with the version below, or remove the pieces
> yourself: the `if: dauer >= min_fahrzeit_doppel` block (and the
> then-unused `min_fahrzeit_doppel` variable) in each open/close branch,
> and the `wait_template`/conditional retry right after the first
> `stop_cover` call. Until you do, running both the old script-side safety
> net and the integration's own new one at once is harmless, not just
> wasteful — `cover.py`'s own `_has_live_tracking_in_direction()` already
> tolerates a redundant open/close resend without disturbing position
> tracking, by design, from before this integration-side fix existed, and
> a redundant stop is harmless the same way a manual double-press of a
> physical stop button would be.
>
> `stop_latenz` and `korrektur_dauer` (the fixed margin shortening a timed
> `stop_cover`'s own wait, and the extra wait added after driving into an
> end stop) have now been removed from both scripts below too, at the
> project owner's own request, to match their own current, real-world
> copy of these scripts rather than this more elaborate variant. The
> timing that remains — `start_ts` plus the `remaining_duration` correction
> described in the first Note below the scripts — already accounts for
> the open/close call's own actual duration, which matters more in
> practice than the smaller, further delay a queued `stop_cover` call can
> still see on top of that; `stop_latenz` only ever compensated for the
> latter. If you command several blinds at once and an intermediate
> target lands a percentage point or two short, that is most likely this
> trade-off rather than a regression.
>
> If you're running a copy with `stop_latenz`/`korrektur_dauer` still in
> place, replace it with the version below, or remove the pieces
> yourself: both variables, the `korrektur_dauer`-vs-`stop_cover` branch
> in the final `choose:`, and `stop_latenz`'s own subtraction inside
> `remaining_duration`. The same "harmless while both run at once" reasoning
> above applies here too.
>
> Every field and variable name in both scripts is now English
> (`ziel`→`requested_position`, `helfer`→`helper`, `auf`→`open_seconds`,
> `ab`→`close_seconds`, and the rest inside the scripts themselves), and
> both scripts were renamed too (`interne_rollladenlogik_fuer_home_assistant`
> → `internal_blind_position_logic`,
> `rollladenlogik_fuer_alexasprachsteuerungen_fuer_rollladen` →
> `alexa_blind_position_logic`). If you're updating from an older copy,
> replace *both* halves together: the scripts (Step 2) and the `data:`
> blocks that call them from your template cover (Step 3/4). A template
> cover still sending the old field names to a script expecting the new
> ones fails outright - `UndefinedError`, the same failure mode the
> `min_fahrzeit_doppel` fix further above already had to account for - not
> just imprecisely, so update both halves in the same edit rather than one
> at a time.

This setup gives you two extra things on top of the integration's own cover
entities:

1. A cover that can be commanded to **any position from 0–100%**, by timing
   open/close commands using the travel times you already measured during
   [calibration](#calibration-explained-simply).
2. A second, English-named copy of that cover meant to be exposed to Alexa
   only, so voice control keeps working even when Alexa re-sends a position
   it already thinks the blind is at.

It uses three building blocks: one **number helper** per blind, two
**scripts** that translate a requested position into a timed move, and one or
two **template covers** per blind that tie it all together.

### Step 1: Create a number helper for each blind

For every blind, go to **Settings > Devices & services > Helpers > Add
helper > Number** and create one with:

- Minimum value: `0`
- Maximum value: `100`
- Step size: `1`

Give it a clear name, for example "Helper: Living room blind". Note the
resulting entity ID, for example `input_number.helper_living_room_blind` —
you'll need it below. Both template covers for the same physical blind (the
normal one and its Alexa twin) share this single helper.

### Step 2: Add the two scripts

Add both scripts below, for example to `scripts.yaml`. If you prefer the UI,
create a new script under **Settings > Automations & scenes > Scripts**,
switch it to YAML mode, and paste in everything from `alias:` down.

| Field | Meaning |
|---|---|
| `requested_position` | Requested position, on this helper's own scale: **0 = fully open**, **100 = fully closed** (see note below) |
| `helper` | The number helper for this blind, from step 1 |
| `open_seconds` | Measured seconds for a full **opening** run |
| `close_seconds` | Measured seconds for a full **closing** run |
| `entity` | The blind's real cover entity from this integration, e.g. `cover.living_room_blind` |

**Script A — for normal use** (dashboards, automations). It does nothing if
the requested position already matches the stored one:

```yaml
internal_blind_position_logic:
  alias: Internal Blind Position Logic
  mode: parallel
  max: 12
  sequence:
    - condition: template
      value_template: "{{ requested_position | int != states(helper) | int }}"
    - delay:
        milliseconds: "{{ range(0, 300) | random }}"
    - variables:
        target_position: "{{ requested_position | float(0) }}"
        current_position: "{{ states(helper) | float(0) }}"
        open_time: "{{ open_seconds | float(25) }}"
        close_time: "{{ close_seconds | float(25) }}"
        moving_down: "{{ target_position > current_position }}"
        duration: >
          {% set diff = (current_position - target_position) | abs %} {% set factor = close_time if
          moving_down else open_time %} {{ ((diff / 100.0) * factor) | round(2) }}
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ not moving_down }}"
          sequence:
            - action: cover.open_cover
              target:
                entity_id: "{{ entity }}"
            - variables:
                start_ts: "{{ now().timestamp() }}"
        - conditions:
            - condition: template
              value_template: "{{ moving_down }}"
          sequence:
            - action: cover.close_cover
              target:
                entity_id: "{{ entity }}"
            - variables:
                start_ts: "{{ now().timestamp() }}"
    - variables:
        remaining_duration: "{{ [0, duration - (now().timestamp() - start_ts)] | max | round(2) }}"
    - delay: "{{ remaining_duration }}"
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ target_position | int != 0 and target_position | int != 100 }}"
          sequence:
            - action: cover.stop_cover
              target:
                entity_id: "{{ entity }}"
    - action: input_number.set_value
      target:
        entity_id: "{{ helper }}"
      data:
        value: "{{ target_position | float(0) }}"
```

**Script B — for the Alexa-facing copy.** Alexa's smart-home skill can
re-send the same target position it already believes a blind is at, which
script A would then ignore. Script B instead treats "target equals current"
as an instruction to run a full close (target ≥ 50) or full open (target <
50), so a repeated Alexa command still moves the blind:

```yaml
alexa_blind_position_logic:
  alias: Blind Position Logic for Alexa Voice Control
  mode: parallel
  max: 12
  sequence:
    - delay:
        milliseconds: "{{ range(0, 300) | random }}"
    - variables:
        target_position: "{{ requested_position | float(0) }}"
        current_position: "{{ states(helper) | float(0) }}"
        open_time: "{{ open_seconds | float(25) }}"
        close_time: "{{ close_seconds | float(25) }}"
        reset_moving_down: "{{ target_position >= 50 }}"
        moving_down: |
          {% if target_position == current_position %}
            {{ reset_moving_down }}
          {% else %}
            {{ target_position > current_position }}
          {% endif %}
        duration: |
          {% if target_position == current_position %}
            {% if reset_moving_down %}
              {{ close_time }}
            {% else %}
              {{ (current_position / 100.0) * open_time }}
            {% endif %}
          {% else %}
            {% set diff = (current_position - target_position) | abs %}
            {% set factor = close_time if moving_down else open_time %}
            {{ ((diff / 100.0) * factor) | round(2) }}
          {% endif %}
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ not moving_down }}"
          sequence:
            - action: cover.open_cover
              target:
                entity_id: "{{ entity }}"
            - variables:
                start_ts: "{{ now().timestamp() }}"
        - conditions:
            - condition: template
              value_template: "{{ moving_down }}"
          sequence:
            - action: cover.close_cover
              target:
                entity_id: "{{ entity }}"
            - variables:
                start_ts: "{{ now().timestamp() }}"
    - variables:
        remaining_duration: "{{ [0, duration - (now().timestamp() - start_ts)] | max | round(2) }}"
    - delay: "{{ remaining_duration }}"
    - choose:
        - conditions:
            - condition: template
              value_template: >-
                {{ not (target_position == current_position) and (target_position | int != 0 and target_position | int != 100) }}
          sequence:
            - action: cover.stop_cover
              target:
                entity_id: "{{ entity }}"
    - action: input_number.set_value
      target:
        entity_id: "{{ helper }}"
      data:
        value: |
          {% if target_position == current_position %}
            {{ 100 if reset_moving_down else 0 }}
          {% else %}
            {{ target_position | float(0) }}
          {% endif %}
```

Keep `mode: parallel` and a `max` at least as high as your number of blinds
on both scripts. With the default `single` mode, moving several blinds at
the same time would make later calls wait for earlier ones, or get skipped.

### Step 3: Add the template cover

Add this to `configuration.yaml`, or merge the `cover:` list into an
existing `template:` section:

```yaml
template:
  - cover:
      - name: "Living Room Blind"
        device_class: shutter
        unique_id: living_room_blind_percent
        position: "{{ states('input_number.helper_living_room_blind') | int(0) }}"
        set_cover_position:
          action: script.internal_blind_position_logic
          data:
            requested_position: "{{ position }}"
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        open_cover:
          action: script.internal_blind_position_logic
          data:
            requested_position: 0
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        close_cover:
          action: script.internal_blind_position_logic
          data:
            requested_position: 100
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        stop_cover:
          action: cover.stop_cover
          target:
            entity_id: cover.living_room_blind
```

Replace `living_room_blind` everywhere (in the name, `unique_id`, helper, and
`entity`), and `12.00` / `11.50` with your own measured travel times. Repeat
the whole block for each additional blind.

### Step 4 (optional): Add an Alexa-facing twin

For each blind you also want on Alexa, add a second template cover that
points at script B and reuses the **same** helper and the **same** real
`entity`, but gets its own name and `unique_id`:

```yaml
      - name: "Living Room Blind Alexa"
        device_class: shutter
        unique_id: living_room_blind_percent_alexa
        position: "{{ states('input_number.helper_living_room_blind') | int(0) }}"
        set_cover_position:
          action: script.alexa_blind_position_logic
          data:
            requested_position: "{{ position }}"
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        open_cover:
          action: script.alexa_blind_position_logic
          data:
            requested_position: 0
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        close_cover:
          action: script.alexa_blind_position_logic
          data:
            requested_position: 100
            helper: input_number.helper_living_room_blind
            open_seconds: 12.00
            close_seconds: 11.50
            entity: cover.living_room_blind
        stop_cover:
          action: cover.stop_cover
          target:
            entity_id: cover.living_room_blind
```

Then, in your Alexa integration (for example the Alexa section of Home
Assistant Cloud, or your Alexa smart-home skill's exposed-entities list),
expose only the "Alexa" template covers and keep the normal ones hidden from
Alexa, so each blind doesn't show up twice in the Alexa app.

### Notes

- The 0–100 scale used above is this helper's own convention (0 = open, 100
  = closed) and is independent of the position this integration tracks
  internally on the real `cover.*` entity — the two don't need to match.
- `start_ts` and the second `remaining_duration` (recomputed right after the
  open/close `choose:` block, replacing the first, rougher estimate) measure
  how much time the open/close call above actually took, rather than
  assuming a fixed value — this corrects for whatever the stick's
  reply/retry time turned out to be: `open_cover`/`close_cover` on this
  integration's own `cover.*` entity wait for the stick's reply before
  returning, and retry for as long as it answers busy, so neither call is
  always instant.
- When the target is exactly 0 or 100, the scripts intentionally keep
  driving into the mechanical end stop instead of stopping at the
  calculated time, which corrects any position drift that built up over
  previous moves.
- A manual `stop_cover` — including any these scripts trigger — briefly
  tries to get priority over other blinds' own *waiting* `open`/`close`
  commands before transmitting, but not over whichever command currently
  holds the radio, nor over another blind's own stop that asked for it
  first — falling back to an instant, unretried write if that brief
  window (a couple of seconds at most) runs out before its turn comes.
  For an intermediate target, that queueing is the main source of the
  small overshoot the note above (under Step 2) describes trading away
  `stop_latenz` for. `open`/`close` are different: both wait (a few seconds
  at most, best-effort) for the stick to report itself idle before
  sending, and if the stick still answers that it is busy right as the
  command goes out (`Transmit error - stick busy`), they keep retrying -
  with no limit on the number of attempts, the same policy raw RF payload
  testing, teach/pairing, and similar non-movement requests already use -
  but for no longer than 15 seconds in total. Several blinds commanded
  together are serialized one at a time rather than raced against each
  other, so a busy reply to one is normal, not a sign of a problem, and
  does not mean its command is lost - it is simply retried once the stick
  is free; that 15-second budget also only starts once a blind's own turn
  actually arrives, not while it is still waiting behind others. A stop
  for a blind that is still waiting out one of these retries ends that
  wait immediately instead of letting it resend the open/close command
  after the stop has already gone out. Because retrying now happens one
  blind at a time, a blind that is stuck retrying - stick still busy, or
  not answering at all - also holds up every other blind's own open/close
  (and, within the couple of seconds above, its own stop too) behind it
  for as long as it keeps trying: pressing **Stop** on that specific
  blind ends its wait and frees the others right away; failing that, it
  gives up on its own after 15 seconds, the same way any other failed
  open/close command does (the command is reported as failed, and
  diagnostics show the stick reported busy repeatedly and needs a reset).

## Optional: synchronizing multiple blinds from one switch (example automation)

> [!NOTE]
> This is not part of the integration itself. It is an example automation
> contributed by a user, anonymized here to remove personal device
> identifiers and translated to English. Device IDs, entity IDs, and delay
> values below are examples — replace them with your own.

A single Schellenberg USB stick transmits one RF command at a time, and this
integration estimates each blind's position purely from timing — how long a
motor has been commanded to run, using the open and close travel times you
measured during [calibration](#calibration-explained-simply). There is no
position feedback from the motor itself.

That has one practical consequence when you trigger several blinds from a
single switch press with `cover.set_cover_position`: if every blind starts
moving at the same instant, they will *not* stop moving at the same instant,
because each blind's own travel time — and how far it has to move for the
requested position change — is different. A blind that only has to move a
short distance stops long before one that has to travel its full range.

The two automations below (one per button on a wall switch) work around this
by starting each blind's move with a small, deliberately staggered `delay`,
so the *slower* moves start first (`delay: 0s`) and the *faster* ones start
later — timed so that all of them finish at close to the same moment instead
of visibly stopping one after another.

```yaml
alias: All blinds down (hallway switch, button 3)
description: ""
triggers:
  - device_id: YOUR_SWITCH_DEVICE_ID
    domain: hue
    type: initial_press
    subtype: 3
    unique_id: YOUR_SWITCH_BUTTON_UNIQUE_ID
    trigger: device
conditions:
  - condition: time
    after: "07:00:00"
    before: "21:59:00"
actions:
  - parallel:
      - sequence:
          - action: cover.set_cover_position
            data:
              position: 100
            target:
              entity_id: cover.living_room_blind
      - sequence:
          - delay:
              seconds: 2
          - action: cover.set_cover_position
            data:
              position: 35
            target:
              entity_id: cover.kitchen_blind_1
      - sequence:
          - delay:
              seconds: 6
          - action: cover.set_cover_position
            data:
              position: 100
            target:
              entity_id: cover.kitchen_blind_2
      - sequence:
          - delay:
              seconds: 9
          - action: cover.set_cover_position
            data:
              position: 7
            target:
              entity_id: cover.kitchen_blind_3
      - sequence:
          - delay:
              seconds: 12
          - action: cover.set_cover_position
            data:
              position: 100
            target:
              entity_id: cover.bedroom_blind
      - sequence:
          - delay:
              seconds: 15
          - action: cover.set_cover_position
            data:
              position: 52
            target:
              entity_id: cover.bathroom_blind
mode: single
```

```yaml
alias: All blinds up (hallway switch, button 2)
description: ""
triggers:
  - device_id: YOUR_SWITCH_DEVICE_ID
    domain: hue
    type: initial_press
    subtype: 2
    unique_id: YOUR_SWITCH_BUTTON_UNIQUE_ID
    trigger: device
conditions:
  - condition: time
    after: "07:00:00"
    before: "21:59:00"
actions:
  - parallel:
      - sequence:
          - action: cover.set_cover_position
            data:
              position: 0
            target:
              entity_id: cover.kitchen_blind_2
      - sequence:
          - delay:
              seconds: 2
          - action: cover.set_cover_position
            data:
              position: 0
            target:
              entity_id: cover.kitchen_blind_1
      - sequence:
          - delay:
              seconds: 4
          - action: cover.set_cover_position
            data:
              position: 0
            target:
              entity_id: cover.kitchen_blind_3
      - sequence:
          - delay:
              seconds: 8
          - action: cover.set_cover_position
            data:
              position: 18
            target:
              entity_id: cover.living_room_blind
      - sequence:
          - delay:
              seconds: 10
          - action: cover.set_cover_position
            data:
              position: 17
            target:
              entity_id: cover.bedroom_blind
      - sequence:
          - delay:
              seconds: 12
          - action: cover.set_cover_position
            data:
              position: 0
            target:
              entity_id: cover.bathroom_blind
mode: single
```

### Working out your own delays

There's no universal formula, because it depends on each blind's own
calibrated travel time and how far *this particular move* takes it — but the
approach is:

1. For each blind, work out roughly how long the requested move will take:
   `(distance as a fraction of full travel) × (that blind's measured open or
   close time from calibration)`. A blind moving from fully open to 50% takes
   about half its full close time; one moving from 90% to 100% only takes a
   fraction of it.
2. Find the *longest* of those move times across all the blinds in the
   automation.
3. For every other blind, set its `delay` to roughly `longest move time −
   this blind's own move time`, so the blind with the longest move starts
   immediately (`delay: 0`) and every other blind starts later, timed to
   finish around the same moment.
4. Treat the result as a starting point and adjust by watching your blinds —
   small differences in mechanical speed, mounting, or how the position was
   last tracked mean the exact numbers are usually tuned by eye rather than
   computed once and forgotten.

Because the USB stick can only transmit one command at a time, sending many
`set_cover_position` calls in the same second can make the stick briefly
report busy. That is not a lost command - open and close retry automatically
until the stick accepts them (see the note on open/close behavior earlier in
this section) - but each one still has to wait its turn, so spreading the
moves out, as the `delay` steps above already do, keeps them synchronized
more tightly than letting several queue up back to back would.

## Advanced protocol notes

> [!NOTE]
> This section is intended for developers and advanced troubleshooting. It is not
> required for normal installation or pairing.

- The command/transmit identity can differ from the receive/status identity.
- A primary status identity is selected from streams containing recognized command
  values: `00` Stop, `01` Open/Up, and `02` Close/Down.
- Secondary identities may emit other command families. Unknown secondary commands
  are matched for diagnostics but do not change position.
- `t1` indicates that the USB stick transmitter started and `t0` indicates that it
  finished. Neither response confirms reception or movement at the motor.
- Movement transmissions use the 11-character format
  `ss{two-digit enum}9{two-digit command}0000`.
- For example, Open on enum `10` is `ss109010000`.
- Motor teach-in uses command `60`, followed by finish/allow-pairing command `40`
  on the same enum. Do not send teach commands casually; on some devices command
  `60` can also affect direction.
- **Send raw RF payload** accepts exactly `ss` followed by nine hexadecimal
  characters. Use it only when you understand the protocol and have a recovery
  path.

Protocol behavior is based on the
[reverse-engineered Schellenberg USB protocol](https://github.com/Hypfer/schellenberg-qivicon-usb).

## Tested setup

Tested with a Schellenberg USB FunkStick and multiple installed Schellenberg RF
shutter motors. Compatibility can vary by motor, remote, firmware, and local RF
conditions.

## Contributing and issue reports

Bug reports and tested improvements are welcome. Please include:

- Home Assistant version
- Integration version, release, or commit
- USB stick path
- Motor and remote model, if known
- Whether movement from Home Assistant works
- Whether movement from the original remote is reflected in Home Assistant
- Copied diagnostics
- Debug logs only when they are needed

Remove or replace any identifiers you do not want to share publicly.

## License

See [LICENSE](LICENSE).
