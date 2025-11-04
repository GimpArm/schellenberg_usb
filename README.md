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

### Step 1: Download files

#### Option 1: Via HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gimparm&repository=schellenberg_usb&category=integration)

Make sure you have HACS installed. If you don't, run `wget -O - https://get.hacs.xyz | bash -` in HA.  
Choose Integrations under HACS. Click the '+' button on the bottom of the page, search for "schellenberg usb", choose it, and click install in HACS.

#### Option 2: Manual
Clone this repository or download the source code as a zip file and add/merge the `custom_components/` folder with its contents in your configuration directory.


### Step 2: Restart HA
In order for the newly added integration to be loaded, HA needs to be restarted.

### Step 3: Add integration to HA (<--- this is a step that a lot of people forget)
In HA, go to Configuration > Integrations.
In the bottom right corner, click on the big button with a '+'.

If the component is properly installed, you should be able to find 'Schellenberg USB' in the list. You might need to clear you browser cache for the integration to show up.

Select it, and the schellenberg usb integration is ready for use.

### Step 4: Add the devices
Follow instructions on [Schellenberg USB](https://github.com/gimparm/schellenberg_usb) to pair and calibrate blinds.
