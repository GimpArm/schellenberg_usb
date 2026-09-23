# Schellenberg USB for Home Assistant

[![GitHub Release](https://img.shields.io/github/release/GimpArm/schellenberg_usb.svg)](https://github.com/GimpArm/schellenberg_usb/releases)
[![License](https://img.shields.io/github/license/GimpArm/schellenberg_usb.svg)](https://github.com/GimpArm/schellenberg_usb/blob/main/LICENSE)
![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/GimpArm/schellenberg_usb/build-test.yaml)

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
4. Open the Schellenberg USB integration and choose **Add blind**.
5. Select **Pair and test**, the recommended setup method.
6. Follow the on-screen instructions and put the correct motor or channel into
   learning mode using the original remote or motor manual.
7. Start pairing. Home Assistant listens for pairing/status information and sends
   the teach sequence to the USB stick.
8. Give the blind a friendly name, for example `Living room window`.
9. Confirm whether the short movement test actually moved the blind.
10. Run automatic calibration and save the blind.

If the blind does not move during the test, repeat the learning procedure. A
successful USB stick ACK alone does not mean that the motor learned the stick.

### Other setup methods

- **Pair and calibrate (legacy)** keeps the older guided pairing workflow. It is
  still supported, but **Pair and test** is recommended because it verifies motor
  movement before calibration.
- **Add manually** is intended for advanced users who already know the command
  identity, optional status identities, and travel times. You can still run teach,
  test, discovery, and calibration actions afterward.

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

1. Start with the blind in the position requested by the dialog.
2. Follow the prompts to move it fully open.
3. Let it finish moving.
4. Follow the prompts to move it fully closed.
5. Review and save the measured times.

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
| `ziel` | Requested position, on this helper's own scale: **0 = fully open**, **100 = fully closed** (see note below) |
| `helfer` | The number helper for this blind, from step 1 |
| `auf` | Measured seconds for a full **opening** run |
| `ab` | Measured seconds for a full **closing** run |
| `entity` | The blind's real cover entity from this integration, e.g. `cover.living_room_blind` |

**Script A — for normal use** (dashboards, automations). It does nothing if
the requested position already matches the stored one:

```yaml
interne_rollladenlogik_fuer_home_assistant:
  alias: Interne Rollladenlogik für Home Assistant
  mode: parallel
  max: 12
  sequence:
    - condition: template
      value_template: "{{ ziel | int != states(helfer) | int }}"
    - variables:
        soll: "{{ ziel | float(0) }}"
        ist: "{{ states(helfer) | float(0) }}"
        f_auf: "{{ auf | float(25) }}"
        f_ab: "{{ ab | float(25) }}"
        richtung_runter: "{{ soll > ist }}"
        dauer: >
          {% set diff = (ist - soll) | abs %} {% set faktor = f_ab if
          richtung_runter else f_auf %} {{ ((diff / 100.0) * faktor) | round(2) }}
        rest_dauer: "{{ [0, dauer - 1.0] | max | round(2) }}"
        korrektur_dauer: |
          {% if soll == 0 %}
            {{ (ist / 100.0 * f_auf) | round(2) }}
          {% elif soll == 100 %}
            {{ ((100 - ist) / 100.0 * f_ab) | round(2) }}
          {% else %}
            0
          {% endif %}
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ not richtung_runter }}"
          sequence:
            - action: cover.open_cover
              target:
                entity_id: "{{ entity }}"
            - delay:
                hours: 0
                minutes: 0
                seconds: 1
                milliseconds: 0
            - action: cover.open_cover
              target:
                entity_id: "{{ entity }}"
        - conditions:
            - condition: template
              value_template: "{{ richtung_runter }}"
          sequence:
            - action: cover.close_cover
              target:
                entity_id: "{{ entity }}"
            - delay:
                hours: 0
                minutes: 0
                seconds: 1
                milliseconds: 0
            - action: cover.close_cover
              target:
                entity_id: "{{ entity }}"
    - delay: "{{ rest_dauer }}"
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ soll | int != 0 and soll | int != 100 }}"
          sequence:
            - action: cover.stop_cover
              target:
                entity_id: "{{ entity }}"
            - delay:
                hours: 0
                minutes: 0
                seconds: 1
                milliseconds: 0
            - action: cover.stop_cover
              target:
                entity_id: "{{ entity }}"
        - conditions:
            - condition: template
              value_template: "{{ soll | int == 0 or soll | int == 100 }}"
          sequence:
            - delay:
                seconds: "{{ korrektur_dauer }}"
    - action: input_number.set_value
      target:
        entity_id: "{{ helfer }}"
      data:
        value: "{{ soll | float(0) }}"
```

**Script B — for the Alexa-facing copy.** Alexa's smart-home skill can
re-send the same target position it already believes a blind is at, which
script A would then ignore. Script B instead treats "target equals current"
as an instruction to run a full close (target ≥ 50) or full open (target <
50), so a repeated Alexa command still moves the blind:

```yaml
rollladenlogik_fuer_alexasprachsteuerungen_fuer_rollladen:
  alias: Rollladenlogik fuer Alexasprachsteuerungen für Rollladen
  mode: parallel
  max: 12
  sequence:
    - variables:
        soll: "{{ ziel | float(0) }}"
        ist: "{{ states(helfer) | float(0) }}"
        f_auf: "{{ auf | float(25) }}"
        f_ab: "{{ ab | float(25) }}"
        reset_richtung_runter: "{{ soll >= 50 }}"
        richtung_runter: |
          {% if 
