# Narwal Helper Scripts

Standalone scripts for debugging and exploring the Narwal MQTT protocol outside of Home Assistant.

## Prerequisites

```bash
pip install paho-mqtt
```

## Configuration

All scripts read credentials from environment variables. Copy `.env.example` and fill in your values:

```bash
cp scripts/.env.example scripts/.env
# Edit scripts/.env with your credentials
source scripts/.env && export NARWAL_EMAIL NARWAL_PASSWORD NARWAL_REGION NARWAL_DEVICE_ID
```

| Variable | Required | Description |
|---|---|---|
| `NARWAL_EMAIL` | Yes | Narwal account email |
| `NARWAL_PASSWORD` | Yes | Narwal account password |
| `NARWAL_REGION` | No | API region: `us`, `il`, `eu`, `cn` (default: `il`) |
| `NARWAL_DEVICE_ID` | Yes | Device hex ID (from MQTT topic path) |
| `NARWAL_PRODUCT_KEY` | No | Product key (found in MQTT topic path) |

## Scripts

All scripts should be run from the `scripts/` directory:

```bash
cd scripts
```

### `sniff_app.py` — MQTT Traffic Sniffer

Subscribes to all known MQTT topics and logs traffic in real-time. Use this to capture what the official Narwal app sends when performing actions.

```bash
# Listen for 5 minutes (default 300s)
python sniff_app.py

# Listen for 10 minutes
python sniff_app.py --duration 600
```

For each message it prints the topic, payload size, raw hex, and MQTT5 response topic. For `clean/start_clean` messages it also decodes the protobuf clean configuration showing room IDs, passes, and cleaning modes.

### `test_clean_rooms.py` — Room Cleaning Tester

Tests room cleaning commands by starting a clean for each room, verifying the response, then force-stopping before the next room.

```bash
# Test all rooms (auto-discovered from map)
python test_clean_rooms.py

# Test specific rooms
python test_clean_rooms.py --rooms 1,2,7

# Vacuum-only mode with 1 pass
python test_clean_rooms.py --vacuum-only --passes 1

# Test multi-room cleaning
python test_clean_rooms.py --rooms 2 --multi 1,4,7
```

### `discover_device.py` — MQTT Device Discovery

Attempts to discover the MQTT device name by subscribing to various wildcard and system topics on the broker. Useful for verifying MQTT connectivity and testing if the broker exposes device info.

```bash
python discover_device.py
```

**Note:** The Narwal MQTT broker does not currently support wildcard subscriptions for message delivery, so this script serves primarily as a diagnostic tool.

### `_common.py` — Shared Helpers

Shared module used by all scripts. Contains:
- Cloud API login
- Protobuf encoding/decoding helpers
- MQTT client wrapper (`NarwalMQTT`)
- Command result parsing

## Important Notes

- **Never commit `.env` files** — they contain credentials
- Scripts connect to the real Narwal MQTT broker and send real commands
- `test_clean_rooms.py` will briefly start the vacuum for each room being tested
- The vacuum must be in standby/docked state for clean commands to succeed
