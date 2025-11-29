"""Constants for the Schellenberg USB protocol."""

from __future__ import annotations

# Device commands (Schellenberg protocol) - for controlling devices
CMD_STOP = "00"  # 0x00 - Stop
CMD_UP = "01"  # 0x01 - Up
CMD_DOWN = "02"  # 0x02 - Down
CMD_ALLOW_PAIRING = "40"  # 0x40 - Allow Pairing (make device listen to new remote)
CMD_MANUAL_UP = "41"  # 0x41 - Manual Up (as long as button held)
CMD_MANUAL_DOWN = "42"  # 0x42 - Manual Down (as long as button held)
CMD_PAIR = "60"  # 0x60 - Pair with device / Change rotation direction
CMD_SET_UPPER_ENDPOINT = "61"  # 0x61 - Set upper endpoint
CMD_SET_LOWER_ENDPOINT = "62"  # 0x62 - Set lower endpoint

# Sensor status codes
SENSOR_WINDOW_HANDLE_0 = "1A"  # 0x1A - Window handle at 0°
SENSOR_WINDOW_HANDLE_90 = "1B"  # 0x1B - Window handle at 90°
SENSOR_WINDOW_HANDLE_180 = "3B"  # 0x3B - Window handle at 180°

# Motor events from stick (same as some commands)
EVENT_STARTED_MOVING_UP = "01"
EVENT_STARTED_MOVING_DOWN = "02"
EVENT_STOPPED = "00"

# USB Stick system commands (prefixed with !)
CMD_VERIFY = "!?"  # Get version and current mode
CMD_ENTER_BOOTLOADER = "!B"  # Enter B:0 bootloader mode
CMD_ENTER_INITIAL = "!G"  # Enter B:1 initial mode
CMD_GET_TRANSCEIVER = "!F"  # Get transceiver info (Si446x)
CMD_REBOOT = "!R"  # Reboot device (only in B:0)
CMD_ECHO_ON = "!E1"  # Enable local echo
CMD_ECHO_OFF = "!E0"  # Disable local echo

# USB Stick lowercase commands (for various functions)
CMD_LED_ON = "so+"  # Turn LED on
CMD_LED_OFF = "so-"  # Turn LED off
CMD_LED_BLINK_1 = "so1"  # Blink LED 1 time
CMD_LED_BLINK_2 = "so2"  # Blink LED 2 times
CMD_LED_BLINK_3 = "so3"  # Blink LED 3 times
CMD_LED_BLINK_4 = "so4"  # Blink LED 4 times
CMD_LED_BLINK_5 = "so5"  # Blink LED 5 times
CMD_LED_BLINK_6 = "so6"  # Blink LED 6 times
CMD_LED_BLINK_7 = "so7"  # Blink LED 7 times
CMD_LED_BLINK_8 = "so8"  # Blink LED 8 times
CMD_LED_BLINK_9 = "so9"  # Blink LED 9 times
CMD_GET_DEVICE_ID = "sr"  # Get device ID
CMD_GET_PARAM_P = "sp"  # Get parameters P
CMD_GET_PARAM_Q = "sq"  # Get parameters Q
CMD_GET_PARAM_V = "sv"  # Get parameters V
CMD_GET_PARAM_W = "sw"  # Get parameters W
CMD_GET_SG = "sg"  # Unknown function

# Command prefixes
CMD_TRANSMIT = "ss"  # Schellenberg transmit prefix for device commands

# Device verification
VERIFY_TIMEOUT = 5  # seconds to wait for verification response
# Expected response format: RFTU_V20 F:20180510_DFBD B:1

# Pairing constants
PAIRING_TIMEOUT = 120  # seconds to wait for pairing response
PAIRING_DEVICE_ENUM_START = 0x10  # Start from 0x10 for new devices
