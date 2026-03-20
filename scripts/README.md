# Narwal Helper Scripts

Standalone scripts for debugging and exploring the Narwal MQTT protocol outside of Home Assistant.

## Prerequisites

```bash
pip install paho-mqtt cryptography
```

## Configuration

All scripts read credentials from environment variables. Copy `.env.example` and fill in your values:

```bash
cp scripts/.env.example scripts/.env
# Edit scripts/.env with your credentials
source scripts/.env && export NARWAL_EMAIL NARWAL_PASSWORD NARWAL_REGION NARWAL_DEVICE_ID NARWAL_PRODUCT_KEY
```

| Variable | Required | Description |
|---|---|---|
| `NARWAL_EMAIL` | Yes | Narwal account email |
| `NARWAL_PASSWORD` | Yes | Narwal account password |
| `NARWAL_REGION` | No | API region: `us`, `il`, `eu`, `cn` (default: `il`) |
| `NARWAL_DEVICE_ID` | Yes | Device hex ID (32-char hex string from MQTT topic) |
| `NARWAL_PRODUCT_KEY` | Yes | Product key (from MQTT topic path) |

## Finding Your Device ID (MQTT Device Name)

The Narwal cloud API does not expose the MQTT device name (a 32-character hex string). You need to intercept the MQTT traffic between the Narwal app and the cloud broker to discover it. The `mqtt_mitm.py` script automates this.

### Requirements

- A Mac with an **Ethernet** connection to your network
- Your phone running the Narwal app on the same network
- macOS Internet Sharing (to route phone traffic through the Mac)

### Steps

**1. Connect your Mac via Ethernet** and enable Internet Sharing:

- Go to **System Settings → General → Sharing → Internet Sharing**
- Share from: **Ethernet** (or USB Ethernet adapter)
- To computers using: **Wi-Fi**
- Enable Internet Sharing — this creates a Wi-Fi hotspot on the Mac

**2. Connect your phone** to the Mac's Wi-Fi hotspot.

**3. Set up the packet redirect** so MQTT traffic (port 8883) from the phone goes to the MITM proxy:

```bash
sudo pfctl -d
echo 'rdr on bridge100 proto tcp from 192.168.2.0/24 to any port 8883 -> 127.0.0.1 port 18883' | sudo pfctl -ef -
```

**4. Start the MQTT MITM proxy:**

```bash
cd scripts
python3 mqtt_mitm.py
```

**5. Open the Narwal app** on your phone. The MITM proxy will intercept the MQTT connection and log all topics. The output will show:

```
  *** DEVICE FOUND ***
  Product Key:  <your_product_key>
  Device Name:  <your_32_char_hex_device_id>
```

The discovered values are also saved to `scripts/discovered_device.txt`.

**6. Clean up** when done:

```bash
sudo pfctl -d
```

Then disable Internet Sharing and reconnect your phone to your normal Wi-Fi.

### How It Works

The Narwal app communicates with the cloud via MQTT5 over TLS on port 8883. The MQTT topics follow the format `/{productKey}/{deviceName}/...`. The MITM proxy:

1. Accepts the TLS connection from the app using a self-signed certificate (the Narwal app's MQTT client does not pin certificates)
2. Opens a TLS connection to the real Narwal MQTT broker
3. Relays traffic bidirectionally while logging decoded MQTT packets
4. HTTPS traffic (port 443) passes through Internet Sharing normally, so the app authenticates without issues

## Scripts

All scripts should be run from the `scripts/` directory:

```bash
cd scripts
```

### `mqtt_mitm.py` — MQTT MITM for Device Discovery

Intercepts the MQTT connection between the Narwal app and cloud broker to discover the product key and device name. See [Finding Your Device ID](#finding-your-device-id-mqtt-device-name) above for full instructions.

```bash
python3 mqtt_mitm.py
```

### `sniff_app.py` — MQTT Traffic Sniffer

Subscribes to all known MQTT topics and logs traffic in real-time. Use this to capture what the official Narwal app sends when performing actions. Requires `NARWAL_DEVICE_ID` and `NARWAL_PRODUCT_KEY`.

```bash
# Listen for 5 minutes (default 300s)
python3 sniff_app.py

# Listen for 10 minutes
python3 sniff_app.py --duration 600
```

For each message it prints the topic, payload size, raw hex, and MQTT5 response topic. For `clean/start_clean` messages it also decodes the protobuf clean configuration showing room IDs, passes, and cleaning modes.

### `test_clean_rooms.py` — Room Cleaning Tester

Tests room cleaning commands by starting a clean for each room, verifying the response, then force-stopping before the next room.

```bash
# Test all rooms (auto-discovered from map)
python3 test_clean_rooms.py

# Test specific rooms
python3 test_clean_rooms.py --rooms 1,2,7

# Vacuum-only mode with 1 pass
python3 test_clean_rooms.py --vacuum-only --passes 1

# Test multi-room cleaning
python3 test_clean_rooms.py --rooms 2 --multi 1,4,7
```

### `discover_device.py` — MQTT Broker Discovery (Diagnostic)

Attempts to discover the MQTT device name by subscribing to various wildcard and system topics on the broker. Primarily a diagnostic tool — the broker does not support wildcard subscriptions for message delivery.

```bash
python3 discover_device.py
```

### `_common.py` — Shared Helpers

Shared module used by the MQTT scripts. Contains:
- Cloud API login
- Protobuf encoding/decoding helpers
- MQTT client wrapper (`NarwalMQTT`)
- Command result parsing

## Important Notes

- **Never commit `.env` files** — they contain credentials
- Scripts connect to the real Narwal MQTT broker and send real commands
- `test_clean_rooms.py` will briefly start the vacuum for each room being tested
- The vacuum must be in standby/docked state for clean commands to succeed
- `mqtt_mitm.py` does **not** need environment variables — it only needs the `cryptography` package
