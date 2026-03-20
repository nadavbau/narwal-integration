# Narwal Robot Vacuum Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **Narwal** robot vacuums (Freo X Ultra and similar models) that communicates through the Narwal cloud MQTT service.

> **⚠️ Work in Progress** — This integration is under active development. Expect breaking changes, incomplete features, and rough edges. Contributions and bug reports are welcome!

## Features

- **Vacuum entity** with full control: start, stop, pause, return to dock, locate (beep), fan speed
- **Battery sensor** with real-time level
- **Status sensor** showing current activity (Standby, Cleaning, Docked, etc.)
- **Cleaning time** and **cleaned area** sensors
- **Cloud login** with email/password -- no manual token extraction needed
- **Automatic token refresh** to maintain persistent connectivity

## Supported Devices

This integration has been tested with:

- Narwal Freo X Ultra

Other Narwal models using the same cloud platform should also work.

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click the three dots in the top-right corner and select **Custom repositories**
3. Add this repository URL and select **Integration** as the category
4. Search for "Narwal" and install it
5. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/narwal` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Narwal**
3. Enter your Narwal app credentials:
   - **Email** -- your Narwal app account email
   - **Password** -- your Narwal app account password
   - **Region** -- the region your account is registered in (US, Israel, Europe, or China)
4. Enter your device details:
   - **Product Key** -- the product key for your vacuum model (default `EHf6cRNRGT` for Freo X Ultra)
   - **Device Name** -- your device's unique identifier (a 32-character hex string)

### Finding your Device Name

The Device Name is the 32-character hex string that identifies your specific vacuum on the Narwal cloud. You can find it in the Narwal app's network traffic, or by checking the MQTT topics the vacuum subscribes to. It looks like: `abcdef1234567890abcdef1234567890`.

## Entities

### Vacuum

| Feature | Description |
|---------|-------------|
| Start | Begin a cleaning session (or resume if paused) |
| Stop | Force-stop the current task |
| Pause | Pause the current cleaning task |
| Return to Base | Recall the vacuum to its dock |
| Locate | Make the vacuum beep |
| Fan Speed | Quiet, Normal, Strong, Max |

### Sensors

| Sensor | Description |
|--------|-------------|
| Battery | Current battery percentage |
| Status | Working status (Standby, Cleaning, Docked, Charged, Error) |
| Cleaning Time | Elapsed time of the current/last cleaning session |
| Cleaned Area | Area covered in the current/last cleaning session |

## Architecture

This integration communicates with the vacuum via the **Narwal cloud MQTT broker** (MQTT 5 over TLS on port 8883). It does not require local network access to the vacuum.

```
Home Assistant  ──MQTT5/TLS──▶  Narwal Cloud  ──────▶  Vacuum
                                (MQTT Broker)
```

The integration authenticates using the same API as the official Narwal mobile app:

1. **REST API login** (`loginByEmail`) to obtain a JWT access token
2. **MQTT connection** using the JWT as the password
3. **Protobuf-encoded** commands and status messages over MQTT topics
4. **Automatic token refresh** when the JWT approaches expiry

## Development

### Requirements

- Python 3.12+
- Home Assistant 2024.1.0+
- `paho-mqtt` >= 2.0.0

### Project Structure

```
custom_components/narwal/
├── __init__.py              # Integration setup/teardown
├── config_flow.py           # Two-step UI config flow (login + device)
├── const.py                 # HA-level constants
├── coordinator.py           # DataUpdateCoordinator with token refresh
├── entity.py                # Base entity with device info
├── manifest.json            # HA integration manifest
├── sensor.py                # Battery, status, time, area sensors
├── strings.json             # UI strings
├── vacuum.py                # Vacuum entity with full control
├── translations/
│   └── en.json              # English translations
└── narwal_client/           # Standalone Narwal protocol library
    ├── __init__.py           # Library exports
    ├── client.py             # MQTT client (connect, commands, state)
    ├── cloud.py              # REST API client (login, refresh, discovery)
    ├── const.py              # Protocol constants, MQTT topics, enums
    └── models.py             # State models, protobuf parser
```

## License

[MIT](LICENSE)
