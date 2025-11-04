# schellenberg_usb Home Assistant Component

[![GitHub Release](https://img.shields.io/github/release/GimpArm/schellenberg_usb.svg)](https://github.com/GimpArm/schellenberg_usb/releases)
[![License](https://img.shields.io/github/license/GimpArm/schellenberg_usb.svg)](https://github.com/GimpArm/schellenberg_usb/blob/main/LICENSE)
![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/GimpArm/schellenberg_usb/build-test.yaml)

Home Assistant component that interfaces with the [Schellenberg Usb Funk-Stick](https://www.schellenberg.de/smart-home-produkte/smart-home-steuerzentralen/funk-stick/21009/).

> [!WARNING] 
> This integration is not affiliated with Schellenberg, the developers take no responsibility for anything that happens to
> your devices because of this library.

![Schellenberg](https://raw.githubusercontent.com/GimpArm/schellenberg_usb/main/logo.png)

## Features

* Supports blind movement Up, Down, and Stop
* After calibation, position tracking is possible.

## Installation

### HACS

1. [Install HACS](https://hacs.xyz/docs/setup/download)
2. Search for the Schellenerg USB integration in HACS and install it
3. Restart Home Assistant
4. [Add Schellenberg USB integration](https://my.home-assistant.io/redirect/config_flow_start/?domain=schellenberg_usb)
5. Set the device input of the USB stick like `/dev/ttyUSB0` or `/dev/TTY/ACM0`
6. Use the integration options to initiate the pairing process and calibration

### Manual

1. Download [the latest release](https://github.com/GimpArm/schellenberg_usb/releases)
2. Extract the `custom_components` folder to your Home Assistant's config folder, the resulting folder structure should
   be `config/custom_components/schellenber_usb`
3. Restart Home Assistant
4. [Add Schellenberg USB integration](https://my.home-assistant.io/redirect/config_flow_start/?domain=schellenberg_usb), or go to
   Settings > Integrations and add Schellenberg USB
5. Set the device input of the USB stick like `/dev/ttyUSB0` or `/dev/TTY/ACM0`
6. Use the integration options to initiate the pairing process and calibration