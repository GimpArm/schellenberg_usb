# PySchellenberg

Python library for communicating with the [Schellenberg USB Funk-Stick](https://www.schellenberg.de/smart-home-produkte/smart-home-steuerzentralen/funk-stick/21009/).

## Installation

```bash
pip install pyschellenberg
```

## Usage

```python
import asyncio
from pyschellenberg import SchellenbergStick, DeviceEvent, StickStatus

def on_device_event(event: DeviceEvent):
    print(f"Device {event.device_id} sent command: {event.command}")

def on_status_change(status: StickStatus):
    print(f"Stick connected: {status.is_connected}, mode: {status.device_mode}")

async def main():
    loop = asyncio.get_event_loop()
    
    stick = SchellenbergStick(
        port="/dev/ttyACM0",
        loop=loop,
        on_device_event=on_device_event,
        on_status_change=on_status_change,
    )
    
    # Connect to the USB stick
    if await stick.connect():
        print(f"Connected! Hub ID: {stick.hub_id}")
        
        # Control a blind (requires device_enum from pairing)
        await stick.control_blind("10", "01")  # Move up
        
        # LED control
        await stick.led_on()
        await asyncio.sleep(1)
        await stick.led_off()
        
        # Disconnect
        await stick.disconnect()

asyncio.run(main())
```

## Features

- Async serial communication with Schellenberg USB stick
- Device pairing support
- Blind control (up, down, stop)
- LED control (on, off, blink patterns)
- Device calibration commands (set endpoints)
- Status monitoring

## Protocol Documentation

The Schellenberg USB Funk-Stick uses a simple ASCII-based serial protocol at 112500 baud.

### Commands

- `!?` - Verify device (returns version info)
- `ss<enum>9<cmd>0000` - Send command to device
- `so+` / `so-` - LED on/off
- `sr` - Get device ID

### Device Commands

- `00` - Stop
- `01` - Up
- `02` - Down
- `60` - Pair
- `61` - Set upper endpoint
- `62` - Set lower endpoint

## License

MIT License - see LICENSE file for details.
