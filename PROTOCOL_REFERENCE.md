# Narwal Freo X Ultra — Protocol & Data Reference

Comprehensive reference for the Narwal vacuum's cloud API, MQTT protocol,
protobuf message formats, and Home Assistant integration specifics. All
information was reverse-engineered from live traffic captures (MITM proxy,
paho-mqtt packet traces) and validated against a real Narwal Freo X Ultra
robot during March 2026.

---

## 1. Cloud REST API

### Endpoints

| Region | API Host                    | MQTT Broker                    |
|--------|-----------------------------|--------------------------------|
| US     | `us-app.narwaltech.com`     | `us-01.mqtt.narwaltech.com`    |
| IL     | `il-app.narwaltech.com`     | `us-01.mqtt.narwaltech.com`    |
| EU     | `eu-app.narwaltech.com`     | `eu-01.mqtt.narwaltech.com`    |
| CN     | `cn-app.narwaltech.com`     | `cn-mqtt.narwaltech.com`       |

Note: IL (Israel) region uses the US MQTT broker.

### Authentication

**Login** — `POST /user-authentication-server/v2/login/loginByEmail`

```json
{"email": "user@example.com", "password": "..."}
```

Response (`code: 0` = success):
```json
{
  "result": {
    "token": "<JWT access_token>",
    "refresh_token": "<refresh_token>",
    "uuid": "<user_uuid>"
  }
}
```

Error codes: `100202400` and `100202600` indicate bad credentials.

**Token Refresh** — `POST /user-authentication-server/v1/token/refresh`

```json
{"refreshToken": "<refresh_token>"}
```

Response key is `refreshToken` (camelCase), not `refresh_token` like login.

**Auth Header**: All authenticated requests use `Auth-Token: <access_token>`
(not the standard `Authorization: Bearer` header).

### JWT Structure

The access token is a standard JWT. The payload contains:
- `uuid` — the user UUID (used as MQTT username and in payloads)
- `exp` — token expiry timestamp

### Device Discovery

There is **no dedicated device-list endpoint**. Devices are discovered via:

`GET /app-message-server/v1/device-message/listPage?pageNum=1&pageSize=50`

Each message contains `device_id` (numeric cloud ID) and `robot_name`.

**CRITICAL**: The `device_id` from the cloud API (e.g., `132250`) is the
numeric cloud identifier. The MQTT device name is a **different** 32-character
hex string (e.g., `abcdef1234567890abcdef1234567890`). Using the wrong one
causes all commands to time out silently because the broker's ACL rejects
the topics.

The MQTT device name can be found by:
1. Monitoring MQTT traffic from the official Narwal app
2. Using MQTT wildcard subscription `/{product_key}/+/#` to discover active devices
3. The `discover_devices_via_mqtt()` method in `client.py`

### Known Product Key

`EHf6cRNRGT` — Narwal Freo X Ultra. This may differ for other models.

---

## 2. MQTT Protocol

### Connection

- **Protocol**: MQTT 5.0 over TLS (port 8883)
- **Broker**: Region-dependent (see table above)
- **Username**: `user_uuid` from cloud login
- **Password**: `access_token` (JWT) from cloud login
- **Client ID**: `app_{user_uuid}_{random_uuid4}`
- **Keepalive**: 30 seconds

### Topic Structure

```
/{product_key}/{device_name}/{command_path}
/{product_key}/{device_name}/{command_path}/response
```

Example:
```
/EHf6cRNRGT/<device_name>/status/get_device_base_status
/EHf6cRNRGT/<device_name>/status/get_device_base_status/response
```

### Subscription Model

On connect, subscribe to `/{product_key}/{device_name}/#` (QoS 1) to receive
all push broadcasts. For command responses, the client **also** explicitly
subscribes to the specific `/response` topic before publishing.

**Important**: The broker uses ACLs. Wildcard subscriptions like
`/{product_key}/+/#` work for discovery but individual device topics
require the correct device_name.

### Command/Response Pattern

1. Subscribe to `{base_topic}/{command}/response` (QoS 1)
2. Wait for SUBACK (timeout 5s)
3. Publish to `{base_topic}/{command}` with MQTT5 properties:
   - `ResponseTopic`: the response topic string
   - `CorrelationData`: protobuf with request UUID, zero, and timestamp
4. Wait for response on the response topic (timeout varies by command)

### Payload Framing (Narwal Frame)

All payloads use a custom framing: `0x01 + varint(inner_length) + inner_protobuf`

The inner protobuf always starts with the user identity:
- Field 1 (string): user_uuid
- Field 2 (string): user_uuid (duplicate for backward compat?)
- Additional command-specific fields follow

### Response Framing

Responses also use the `0x01 + varint + protobuf` frame. After stripping
the frame, the response protobuf contains:
- Field 1 (varint): result code (1=SUCCESS, 2=NOT_APPLICABLE, 3=CONFLICT)
- Field 2 (bytes): response data payload (command-specific protobuf)

### CorrelationData Format

The MQTT5 CorrelationData property is a protobuf with:
- Field 1 (string): request UUID (UUID1)
- Field 3 (varint): always 0
- Field 4 (varint): timestamp in milliseconds since epoch

### Concurrency

Concurrent calls to the **same** command topic collide because they share
the same response topic key in the pending-response map. The integration
uses per-command `threading.Lock` to serialize these. Different commands
can run concurrently without issues.

---

## 3. Command Topics

### General

| Topic                          | Timeout | Description                            |
|--------------------------------|---------|----------------------------------------|
| `common/yell`                  | 10s     | Locate — robot plays a sound           |
| `common/reboot`                | 10s     | Reboot the robot                       |
| `common/shutdown`              | 10s     | Shut down the robot                    |
| `common/get_device_info`       | 10s     | Get device info (firmware, model)      |
| `common/get_feature_list`      | 10s     | Get supported features                 |
| `common/active_robot_publish`  | —       | Tell robot an app client is active (fire-and-forget, triggers push broadcasts) |
| `common/notify_app_event`      | 10s     | Notify app event                       |

### Status

| Topic                             | Timeout | Description                         |
|-----------------------------------|---------|-------------------------------------|
| `status/get_device_base_status`   | 10s     | Request current status + battery    |

### Task Control

| Topic            | Timeout | Description                  |
|------------------|---------|------------------------------|
| `task/pause`     | 10s     | Pause current task           |
| `task/resume`    | 10s     | Resume paused task           |
| `task/force_end` | 10s     | Force stop current task      |
| `task/cancel`    | 10s     | Cancel current task          |

### Cleaning

| Topic                          | Timeout | Description                           |
|--------------------------------|---------|---------------------------------------|
| `clean/start_clean`            | 10s     | Start cleaning (requires clean config payload OUTSIDE the Narwal frame) |
| `clean/plan/start`             | 10s     | Legacy plan start (returns NOT_APPLICABLE — use `clean/start_clean` instead) |
| `clean/easy_clean/start`       | 10s     | Start easy/quick clean                |
| `clean/set_fan_level`          | 10s     | Set vacuum fan speed                  |
| `clean/set_mop_humidity`       | 10s     | Set mop wetness level                 |
| `clean/current_clean_task/get` | 10s     | Get current cleaning task info        |

**`clean/start_clean` payload** — the auth frame is sent first, then a clean
configuration protobuf is appended **OUTSIDE** (after) the Narwal frame:

```
[Narwal frame: 0x01 + varint_len + auth_protobuf]
[Clean config protobuf (field 1 sub-message)]
```

The clean config structure (field numbers relative to the outer message):
```
field 1 (sub-message) {           // CleanConfig
  field 1 = 1                     // selective clean flag
  field 2 (sub-message) {         // RoomList
    field 1 (sub-message) {       // GlobalConfig: {1:1, 2:passes}
      field 1 = 1
      field 2 = <passes>          // number of cleaning passes (default: 2)
    }
    field 2 (sub-message, repeated) {  // RoomEntry (one per room)
      field 1 = <room_id>
      field 2 = <passes>
      field 3 = <vacuum_on>       // 1=on, 0=off
      field 4 = <mop_on>          // 1=on, 2=off
      field 5 = <fan_level>       // 0=quiet, 1=normal, 2=strong, 3=max
      field 6 = <mop_humidity>    // 0=dry, 1=normal, 2=wet
      field 7 = 1
      field 8 = 1
      field 9 = 1
      field 10 = 0
    }
    field 3 = 1
  }
  field 3 = 1
  field 4 (sub-message) { field 1=1, field 5=0 }
  field 5 = 1
}
```

`clean/plan/start` is **not recommended** — it returns NOT_APPLICABLE (code=2)
on Freo X Ultra and likely other newer models.

### Dock/Supply

| Topic                  | Timeout | Description                  |
|------------------------|---------|------------------------------|
| `supply/recall`        | 10s     | Return to dock               |
| `supply/wash_mop`      | 10s     | Wash the mop pads            |
| `supply/dry_mop`       | 10s     | Dry the mop pads             |
| `supply/dust_gathering` | 10s    | Empty the dustbin            |

### Map

| Topic                       | Timeout | Description                    |
|-----------------------------|---------|--------------------------------|
| `map/get_map`               | 30s     | Get full map (large response)  |
| `map/get_all_reduced_maps`  | 10s     | Get all saved maps (reduced)   |

### Config

| Topic              | Timeout | Description               |
|--------------------|---------|---------------------------|
| `config/get`       | 10s     | Get current configuration |
| `config/set`       | 10s     | Set configuration values  |
| `config/volume/set`| 10s     | Set speaker volume        |

### Consumables

| Topic                            | Timeout | Description                   |
|----------------------------------|---------|-------------------------------|
| `consumable/get_consumable_info` | 10s     | Get consumable wear levels    |
| `consumable/reset_consumable_info`| 10s    | Reset consumable counter      |

### Schedule

| Topic                        | Timeout | Description                |
|------------------------------|---------|----------------------------|
| `schedule/clean_schedule/get`| 10s     | Get cleaning schedule      |

---

## 4. Push Broadcast Topics (Robot → Client)

These are unsolicited messages pushed by the robot:

| Topic Suffix                    | Description                        |
|---------------------------------|------------------------------------|
| `status/robot_base_status`      | Status + battery (periodic + on change) |
| `status/working_status`         | Cleaning progress (during active clean) |
| `status/upgrade_status`         | Firmware update status             |
| `map/display_map`               | Real-time robot position during cleaning (~1.5s interval) |

The robot starts sending broadcasts after receiving `common/active_robot_publish`.

---

## 5. Protobuf Message Formats

All messages use custom protobuf (not compiled from .proto files — reverse
engineered). Our parser handles wire types 0 (varint), 1 (64-bit), 2
(length-delimited), and 5 (32-bit fixed).

### 5.1 `status/robot_base_status` (base status)

Field 2 of the response data (or pushed broadcast after frame stripping):

| Field | Wire Type | Description                                    |
|-------|-----------|------------------------------------------------|
| 2     | 5 (fixed32) | Battery level as IEEE 754 float32 (e.g., `1118175232` → `83.0%`) |
| 3     | 2 (LEN)  | Sub-message: working state (see below)         |
| 11    | 0 (varint) | Dock indicator: 2=docked, 1=undocked          |
| 13    | 2 (LEN)  | User UUID (string)                             |
| 36    | 0 (varint) | Timestamp                                     |
| 38    | 0 (varint) | Battery health (always 100, design capacity)  |
| 47    | 0 (varint) | Dock indicator: 3=docked, 2=undocked          |

**Field 3 sub-message (working state)**:

| Sub-Field | Description                                       |
|-----------|---------------------------------------------------|
| 1         | WorkingStatus enum (see below)                    |
| 2         | is_paused: 1 = paused                             |
| 3         | dock_presence: 1/6=on dock, 2=off dock            |
| 7         | is_returning: 1 = returning to dock               |
| 10        | dock_sub_state: 1=docked, 2=docking in progress   |
| 12        | dock_activity: values 2, 6 when docked            |

**Note**: Field 32 mirrors field 3 exactly (redundant).

### 5.2 WorkingStatus Enum

| Value | Name             | HA Activity   |
|-------|------------------|---------------|
| 0     | UNKNOWN          | idle          |
| 1     | STANDBY          | idle          |
| 2     | PAUSED           | paused        |
| 3     | SLEEPING         | idle          |
| 4     | CLEANING         | cleaning      |
| 5     | CLEANING_ALT     | cleaning      |
| 6     | RETURNING        | returning     |
| 7     | CHARGING         | docked        |
| 8     | MOP_WASHING      | docked        |
| 9     | MOP_DRYING       | docked        |
| 10    | DOCKED           | docked        |
| 11    | DUST_COLLECTING  | docked        |
| 12    | UPGRADING        | idle          |
| 13    | SELF_CHECK       | idle          |
| 14    | CHARGED          | docked        |
| 99    | ERROR            | error         |

Dock detection for STANDBY/UNKNOWN uses multiple signals: `dock_sub_state==1`,
`dock_activity>0`, `dock_field11==2`, or `dock_field47==3`.

### 5.3 `status/working_status` (cleaning progress)

| Field | Description                        |
|-------|------------------------------------|
| 3     | Elapsed time (seconds)             |
| 13    | Cleaned area (cm²; 18000 = 1.8m²) |
| 15    | Unknown (600 during cleaning)      |

### 5.4 Battery Decoding

Battery is field 2 of base_status, wire type 5 (fixed32). The raw uint32
must be reinterpreted as IEEE 754 float32:

```python
import struct
battery = struct.unpack('<f', struct.pack('<I', raw_uint32))[0]
```

Example: `1118175232` → `83.0%`

---

## 6. Map Data Format

### 6.1 `map/get_map` Response

The response data (field 2 of the command response protobuf) contains:

| Field | Wire Type | Description                                    |
|-------|-----------|------------------------------------------------|
| 1     | 0 (varint) | Always 1                                     |
| 2     | 0 (varint) | Grid full width (may differ from clipped width — e.g., 228) |
| 3     | 0 (varint) | Resolution in mm/pixel (e.g., 60)            |
| 4     | 0 (varint) | **Map grid width** in pixels (e.g., 224)     |
| 5     | 0 (varint) | **Map grid height** in pixels (e.g., 258)    |
| 6     | 2 (LEN)  | Coordinate transform (origin offsets)          |
| 7     | 2 (LEN)  | Robot position                                 |
| 8     | 2 (LEN)  | Dock position                                  |
| 9     | 2 (LEN)  | Unknown (52 bytes)                             |
| 11    | 2 (LEN)  | Unknown                                        |
| 12    | 2 (LEN)  | **Room list** (REPEATED — see §6.3)           |
| 13    | 2 (LEN)  | Unknown                                        |
| 15    | 0 (varint) | Always 1                                     |
| 17    | 2 (LEN)  | **Compressed map grid** (zlib — see §6.2)     |
| 18    | 0 (varint) | Unknown (3)                                  |
| 20    | 0 (varint) | Unknown (63)                                 |
| 21    | 2 (LEN)  | Unknown                                        |
| 22    | 0 (varint) | Unknown (101293)                             |
| 23    | 0 (varint) | Always 1                                     |
| 24    | 2 (LEN)  | Dock position (duplicate of field 8)           |
| 25    | 0 (varint) | Unknown (90)                                 |
| 30    | 0 (varint) | Always 1                                     |
| 32    | 2 (LEN)  | Furniture/obstacle annotations (537 bytes)     |
| 33    | 0 (varint) | Map area                                     |
| 34    | 0 (varint) | Created-at timestamp (epoch seconds)          |
| 35    | 0 (varint) | Always 1                                     |
| 36    | 0 (varint) | Unknown large number (nanosecond timestamp?)  |

### 6.2 Map Grid Pixel Format

Field 17 is zlib-compressed. After decompression:

1. **Protobuf wrapper**: byte 0 = `0x0a` (field 1, wire type 2), followed
   by a varint length, then the actual pixel data.
2. **Packed varints**: Each pixel is a protobuf varint (1-3 bytes).
3. **Pixel encoding**: `room_id = value >> 8`, `pixel_type = value & 0xFF`

**Special pixel values**:
- `0x00` = unknown/outside map (dark background)
- `0x20` = unassigned floor (light gray)
- `0x28` = unassigned obstacle (dark gray)

**Room pixels** (value > 0x28):
- Upper bits (`>> 8`): room_id (1-based, matches field 12 room entries)
- Lower byte (`& 0xFF`): pixel type
  - `pixel_type & 0x10` = wall/border edge (render as darker shade)
  - Otherwise = room floor

**Grid layout**:
- Pixel index `i`: x = `i % width`, y = `i // width`
- Y-axis is inverted: grid Y=0 is the bottom of the map (math coordinates)
- Must `FLIP_TOP_BOTTOM` for correct display orientation

**Dimensions**: Use field 4 (width) and field 5 (height) from the map data.
Field 2 is a slightly larger width (full grid including padding) — do NOT
use it for rendering.

**Example**: 224×258 grid, 4535 bytes compressed → 77752 bytes decompressed →
57792 pixel varints (matches 224×258 exactly).

### 6.3 Room List (Field 12, Repeated)

Field 12 appears **multiple times** in the map protobuf (one per room).
Standard protobuf parsers that overwrite duplicate fields will only keep
the last room. Must use `parse_protobuf_repeated()` to collect all entries.

Each field 12 entry is a sub-message:

| Sub-Field | Type    | Description                                    |
|-----------|---------|------------------------------------------------|
| 1         | varint  | **room_id** (matches `pixel_value >> 8` in grid) |
| 2         | varint  | room_sub_type (ROOM_TYPE enum — see below)     |
| 3         | string  | User-assigned name (UTF-8, empty if not renamed) |
| 4         | varint  | Category: 1=room, 2=utility/small space        |
| 8         | varint  | Instance index (1-based, for duplicate room types: "Bathroom 2") |

### 6.4 Room Sub-Type Enum (from APK)

| Value | Name             | Notes                               |
|-------|------------------|-------------------------------------|
| 0     | Room             | Generic/unspecified                  |
| 1     | Master Bedroom   |                                     |
| 2     | Bedroom          | Secondary bedroom                   |
| 3     | Living Room      |                                     |
| 4     | Kitchen          |                                     |
| 5     | Bathroom         | APK says "Study" — user confirmed Bathroom |
| 6     | Toilet           | APK says "Bathroom" — user confirmed Toilet |
| 7     | Dining Room      | APK says "Corridor" but app shows "Dining Room" for some users |
| 8     | Dining Room      | APK says "Corridor" — user confirmed Dining Room |
| 9     | Balcony          |                                     |
| 10    | Utility Room     |                                     |
| 11    | Study            | APK says "Cloak Room" — user confirmed Study |
| 12    | Nursery          |                                     |
| 13    | Recreation Room  |                                     |
| 14    | Storage Room     | APK says "Shower Room" — user confirmed Storage Room |
| 15    | Other            |                                     |

**Note**: Sub-types 7, 8, 11, and 14 have discrepancies between the APK
string table and what the Narwal app actually displays. The values above
were confirmed by the user against their app's room labels.

### 6.5 Room Display Name Logic

1. If user has renamed the room (field 3 non-empty): use the custom name
2. Otherwise: look up `room_sub_type` in the ROOM_TYPE table
3. If `instance_index > 1`: append the index (e.g., "Bathroom 2")

---

## 7. Cleaning Modes

### CleanMode Enum

| Value | Name              |
|-------|-------------------|
| 1     | Vacuum & Mop      |
| 2     | Vacuum then Mop   |
| 3     | Vacuum Only        |
| 4     | Mop Only           |

### FanLevel Enum

| Value | Name   |
|-------|--------|
| 0     | Quiet  |
| 1     | Normal |
| 2     | Strong |
| 3     | Max    |

### MopHumidity Enum

| Value | Name   |
|-------|--------|
| 0     | Dry    |
| 1     | Normal |
| 2     | Wet    |

---

## 8. Protobuf Encoding Helpers

### Varint Encoding

```python
def _encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)
```

### Field Encoding

```python
# Varint field (wire type 0)
tag = (field_num << 3) | 0
encoded = bytes([tag]) + _encode_varint(value)

# Length-delimited field (wire type 2, for strings/bytes/sub-messages)
tag = (field_num << 3) | 2
encoded = bytes([tag]) + _encode_varint(len(data)) + data
```

### Repeated Fields

Standard protobuf parsers (including ours) overwrite duplicate field
numbers. For fields that appear multiple times (like room entries in field
12), use `parse_protobuf_repeated()` which returns `{field_num: [list_of_values]}`.

---

## 9. Home Assistant Integration Notes

### Threading Model

- paho-mqtt runs its own network thread (`loop_start()`)
- HA's event loop is asyncio-based; all blocking I/O must use
  `run_in_executor()`
- SSL context creation (`ssl.create_default_context()`) is a blocking call
  that HA detects — must run in executor during `connect()`
- Command send/receive uses `threading.Event` (not asyncio.Future) to
  avoid cross-thread asyncio issues
- Per-command `threading.Lock` prevents concurrent calls to the same
  command from colliding in the pending-response map

### Entity Architecture

- **VacuumEntity**: Main entity with start/stop/pause/locate/return_home.
  Uses `clean/start_clean` with room-specific payload matching the Narwal
  app's protocol. Room list exposed via `extra_state_attributes["rooms"]`.
- **CameraEntity**: Map rendering. Fetches map every 120s (cached),
  renders via `map_renderer.render_map()` in executor.
- **SelectEntity**: Clean mode selector (Vacuum & Mop, Vacuum Only, etc.).
  Stores selection on `coordinator.selected_clean_mode`.
- **ButtonEntity**: Locate button (calls `common/yell`).
- **SensorEntity**: Battery level, cleaned area, cleaning time.

### Coordinator Flow

1. Cloud login (email/password) → get JWT tokens
2. Token refresh on every setup (tokens expire quickly)
3. MQTT connect with refreshed access_token as password
4. Subscribe to `{base_topic}/#`
5. Send `active_robot_publish` to trigger push broadcasts
6. Request initial status via `get_device_base_status`
7. Fetch rooms via `get_map`
8. Poll status every 60s as backup for push updates

### Config Flow

Step 1: Email + password + region → cloud login
Step 2: Enter product_key and device_name (MQTT hex identifier)

The cloud API returns a numeric `device_id` but MQTT needs the 32-char
hex `device_name`. These are different values. The config flow warns the
user about this.

### Battery Deprecation

HA 2026.8 will remove `battery_level` from `VacuumEntity`. The integration
should migrate to a separate `SensorEntity` with `device_class=BATTERY`
linked to the same device.

### Frontend Card Registration

Uses `hass.http.async_register_static_paths` with `StaticPathConfig` (HA
2024.7+ API). The card JS is served from
`/narwal/narwal-vacuum-card.js` and auto-registers with Lovelace.

---

## 10. Known Issues & Pitfalls

1. **Device name vs device ID**: The #1 cause of "everything times out".
   MQTT device_name is a 32-char hex string, NOT the numeric cloud ID.

2. **`clean/start_clean` requires a special payload**: The clean configuration
   must be appended OUTSIDE (after) the Narwal auth frame, not inside it.
   Without this payload, the vacuum returns NOT_APPLICABLE (code=2).

3. **Repeated protobuf fields**: Room data (field 12) is repeated. A naive
   parser that overwrites on duplicate field numbers will only keep the
   last room. Use `parse_protobuf_repeated()`.

4. **Map grid is packed varints, not raw bytes**: The zlib-decompressed
   data is a protobuf wrapper containing packed varint pixels. Each pixel
   is 1-3 bytes. Treating each byte as a pixel produces garbage.

5. **Map Y-axis is inverted**: Grid Y=0 is the bottom. Must flip vertically.

6. **Concurrent command collision**: Two calls to the same command (e.g.,
   `get_map` from camera + coordinator) overwrite each other's pending
   response entry. Per-command locking is required.

7. **SSL blocking in HA**: `ssl.create_default_context()` calls
   `load_default_certs` which is blocking. Must run MQTT connect in executor.

8. **Token expiry**: JWT tokens expire quickly. Always refresh before
   MQTT connect. The coordinator re-authenticates on each poll if expired.

9. **SUBACK timing**: Must wait for SUBACK before publishing the command,
   otherwise the response may arrive before the subscription is active.

10. **Field 2 vs Field 4 width**: Map field 2 is the full grid width (228)
    including padding. Field 4 is the actual map width (224). Use field 4
    for rendering.

---

## 11. Furniture/Obstacle Annotations (Field 32)

Map field 32 contains `MapFurnitureInfoList` — furniture and obstacle
annotations placed by the user in the app. Each entry:

| Sub-Field | Type    | Description                           |
|-----------|---------|---------------------------------------|
| 1         | int32   | Object ID                             |
| 2         | uint32  | Furniture type enum (see APK)         |
| 3.1.1     | float32 | Center X (world coordinates)          |
| 3.1.2     | float32 | Center Y (world coordinates)          |
| 3.2       | float32 | Width (grid units)                    |
| 3.3       | float32 | Height (grid units)                   |
| 4         | float32 | Angle (degrees)                       |

Furniture type enum (from APK `map_furniture.json`):
1=Single Bed, 2=Double Bed, 3=Baby Bed, 4=Dining Table, 5=Round Table,
6=Tea Table, 7=Round Tea Table, 8=TV Stand, 9=Bedside Table, 10=Locker,
11=Wardrobe, 12=Shoe Cabinet, 13=Armchair, 14=Sofa, 15=L-Shaped Sofa,
16=Lazy Chair, 17=Chair, 18=Bar Chair, 19=Cat Toilet, 20=Pet Feeder,
21=Pet House, 22=Washing Machine, 23=Refrigerator, 24=Air Conditioner,
25=Fan, 26=Potted Plant, 27=Floor Mirror, 28=Toilet, 29=Piano,
30=U-Shaped Sofa, 31=Desk, 32=Grand Piano, 33=Washbasin, 34=Stove,
75=Cat House, 76=Dog House, 77=Round Placeholder, 78=Weighing Scale.

Coordinate transform: `pixel = raw_coord - origin` where origin comes
from map field 6 (`field 6.3` = origin_x, `field 6.1` = origin_y).

---

## 12. Real-Time Position (`map/display_map`)

Broadcast every ~1.5s during active cleaning:

| Field | Description                                         |
|-------|-----------------------------------------------------|
| 1.1   | Robot position: `{1: x_dm, 2: y_dm}` (float32, decimeters) |
| 1.2   | Robot heading (float32, radians)                    |
| 5     | Dock/reference position (same format as 1)          |
| 7     | Cleaned-area grid overlay: `{1: width, 2: height, 3: compressed}` |
| 10    | Timestamp (ms since epoch)                          |
| 12    | Active room list                                    |

Position conversion to grid pixels:
```
pixel_x = robot_x_dm - origin_x
pixel_y = robot_y_dm - origin_y
```

Heading conversion: `degrees = math.degrees(heading_radians)`

---

## 13. File Structure

```
custom_components/narwal/
├── __init__.py          # Entry point, card registration
├── camera.py            # Map camera entity
├── config_flow.py       # Cloud login + device selection
├── const.py             # HA integration constants
├── coordinator.py       # DataUpdateCoordinator (MQTT + polling)
├── entity.py            # Base entity class
├── manifest.json        # Integration manifest
├── select.py            # Clean mode selector entity
├── sensor.py            # Battery/area/time sensors
├── strings.json         # UI strings
├── translations/en.json # English translations
├── vacuum.py            # Main vacuum entity
├── button.py            # Locate button entity
├── frontend/
│   └── narwal-vacuum-card.js  # Custom Lovelace card
└── narwal_client/
    ├── __init__.py      # Public API exports
    ├── client.py        # MQTT client (paho-mqtt)
    ├── cloud.py         # REST API client
    ├── const.py         # Protocol constants & enums
    ├── map_renderer.py  # Map grid → PNG rendering
    └── models.py        # State models, protobuf parsers
```
