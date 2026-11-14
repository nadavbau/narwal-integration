# Narwal Freo X Ultra — Protocol & Data Reference

Reference for the Narwal vacuum's cloud API, MQTT protocol and protobuf
messages. Transport details come from live traffic captures; message
layouts (§3 onward) come from the message definitions compiled into the
official Narwal Android app, and every behaviour marked "live" was checked
against a real Freo X Ultra (firmware v01.09.01.00).

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

`GET /user-device-platform-server/device-info/getDeviceInfoList` (the call the
app makes after login) lists the robots bound to the account:

```json
{"code": 0, "result": {
  "deviceInfoList": [{"deviceId": "<32-char hex MQTT device name>",
                      "productId": "<product key>", "robotName": "...",
                      "firmwareVersion": "v01.09.01.00", "snNumber": "..."}],
  "productInfoList": [{"productId": "<product key>",
                       "productModel": "Narwal Freo X Ultra", "productGroup": "J4"}]}}
```

`deviceId` **is** the MQTT device name and `productId` the topic prefix —
everything the MQTT client needs. Verified on the il, us and eu API hosts.
The config flow uses it, so users only enter email, password and region.

Older notes used `GET /app-message-server/v1/device-message/listPage`; its
`device_id` is a numeric cloud ID (e.g. `123456`), **not** the MQTT device
name — with it every command times out silently (broker ACL).

### Known Product Key

`EHf6cRNRGT` — Narwal Freo X Ultra.
`hEA7OEshlx` — Narwal Freo Z Ultra (four suction levels too: Max is
`FAN_LEVEL_DEEP`, per the app captures in PRs #4 and #6).
`3rIGshGNAj` — Narwal Freo X Plus. Same protocol and schema, except:
- suction: the app's Noise reduction / Standard / Turbo / Max are
  `FAN_LEVEL_MUTE` / `NORMAL` / `STRONG` / `DEEP` (no `SUPER`);
- faults: `errorCode` is pushed once, then the interrupted task just shows
  `pauseStatus` (the next pushes carry an empty `ErrorCode`);
- `task/force_end`: the app sends `exceptTasks = [1, 2]`
  (`DRY_STATION_BAG`, `STATION_BAG_STERILIZATION`, packed).

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

The broker (Aliyun IoT) accepts wildcard subscriptions but only delivers
messages to **explicit** topic subscriptions. On connect the client
subscribes to each push topic (`status/robot_base_status`,
`status/working_status`, `map/display_map`, `status/upgrade_status`) and
pre-subscribes the `/response` topic of every command it uses.

The broker enforces ACLs per device name: the wrong device name makes every
command time out silently.

### Command/Response Pattern

1. Make sure `{base_topic}/{command}/response` is subscribed (pre-subscribed
   on connect; otherwise subscribe and wait for SUBACK)
2. Publish at **QoS 0** (like the app) to `{base_topic}/{command}` with MQTT5 properties:
   - `ResponseTopic`: the response topic string
   - `CorrelationData`: protobuf with request UUID, zero, and timestamp
3. Wait for the reply on the response topic (timeout varies by command)

The broker never PUBACKs app publishes. At QoS 1 paho keeps every publish
in flight; once its window (20) is full, every later publish is queued
locally and never sent, so all commands time out until a reconnect
(measured on `eu-01`: 0/25 QoS 1 publishes acknowledged, the next QoS 1
command timed out, a QoS 0 one was answered in 0.7 s).

### Payload Framing (Narwal Frame)

All payloads use a custom framing: `0x01 + varint(inner_length) + inner_protobuf`

The framed protobuf is the user identity: field 1 and field 2 are both the
user UUID. Like the app, field 5 carries the reply routing
`{1: response_topic, 2: request_id}` (same id as in `CorrelationData`):
without it a dozing robot ignores the MQTT5 `ResponseTopic` property and
only answers once woken. The command's request message (§3) is appended **after** the
frame, not inside it.

### Response Framing

Responses and pushes use the same frame. Skip the whole frame
(`0x01`, the length varint and that many bytes); what follows is the
response message, e.g. `GetDeviceBaseStatus_Response` or a generic
`{1: result}` where `result` is the `ServiceResult` enum (§4).

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


## 3. Message Schema

Payloads are protobuf. Field numbers, types and enum values come from the
message definitions compiled into the official Android app
(`com.narwal.pita_global`, 2.11.90), recovered by decompiling its Dart code
(see `tools/schema/README.md`). They are **not guesses** — do not renumber
anything by hand.

- `custom_components/narwal/narwal_client/schema.py` — generated subset of the
  app's schema (the messages the integration uses). Regenerate, don't edit.
- `custom_components/narwal/narwal_client/proto.py` — encode/decode dicts keyed
  by the app's field names: `encode("SetConfig_Request", {"childLockEnabled": True})`,
  `decode("RobotBaseStatus", payload)`. Enums decode to their label strings.

Message names below are the app's. Where two app files define a message
with the same name, the generated schema qualifies it with the file stem
(e.g. `CleanParam__CleanTask` vs `CleanParam__ReportBaseTypes`). The clean
task uses the **CleanTask.pb** one; confusing the two was the root of the old
room-cleaning bugs.

Every request is `[Narwal frame] + encoded request message`; every response
is `[Narwal frame] + encoded response message` (see §2).

## 4. Commands

Over 300 command topics exist in the app; these are the ones the
integration uses. "Live" = verified against a Freo X Ultra (firmware
v01.09.01.00).

| Topic | Request → Response | Notes |
|-------|--------------------|-------|
| `common/active_robot_publish` | `ActiveRobotPublish_Request` (no reply) | Registers an app client; robot pushes status every `topicsDuration` ms for `publishDuration` ms. Re-send every ~50 s. Live |
| `status/get_device_base_status` | — → `GetDeviceBaseStatus_Response` | `{result: RESULT_TYPE_SUCCESS, robotBaseStatus}`. Live |
| `common/get_feature_list` | — → `GetFeatureList_Response` | Feature flags. Live |
| `common/upgrade/get_firmware_version` | — → `GetFirmwareVersion_Response` | `firmwareVersionText`. `common/get_device_info` field 3 also holds it |
| `config/get` | — → `GetConfig_Response` | `config` = settings (§5.5). Live |
| `config/set` | `SetConfig_Request` → `SetConfig_Response` | Send only the fields to change; **explicit `false`/0 is honoured**. Field numbers differ from `GetConfig` (§5.5). Live |
| `consumable/get_consumable_info` | — → `GetConsumableInfo_Response` | Items *due* (maintain / replace), not wear percentages. Live |
| `consumable/reset_consumable_info` | `ResetConsumableInfo_Request` | `consumableInfo.replaceItems: [item]` |
| `map/get_map` | `GetMap_Request` (optional) → `GetMap_Response` | `map` = `StaticMapPayload` (§6). Live |
| `clean/start_clean` | `StartClean_Request` → `StartClean_Response` | §5.6. Live |
| `clean/set_fan_level` | `SetFanLevel_Request` | `{level, operationType: OPERATION_TYPE_SET_VALUE}`. **Only accepted during a clean** (GENERAL_FAILED while docked). Live |
| `clean/set_mop_humidity` | `SetMopHumidity_Request` | `{mopHumidity, operationType}`. Only during a clean. Live |
| `task/pause` / `task/resume` / `task/force_end` / `task/cancel` | empty | Live (pause/resume/force_end) |
| `supply/recall` | empty | Return to dock. Live |
| `supply/dry_mop`, `supply/wash_and_dry_mop`, `supply/dust_gathering` | empty | Live |
| `supply/wash_mop` | empty | Rejected while idle on the dock (mid-clean wash) |
| `common/yell` | empty | Locate. Live |

Most responses carry `result` (enum `ServiceResult`) in field 1:
`SERVICE_RESULT_PERFORM_SUCCESS` = 1; failures include
`PERFORM_GENERAL_FAILED` (2), `ALREADY_EXECUTING` (3), `POWER_LOWER` (4),
`ROBOT_IN_STATION_WHEN_RECALL` (6), `DUST_BOX_NOT_INSTALLED` (9),
`MOP_NOT_INSTALLED` (10), `FORBIDDEN_TASK_IF_CAN_NOT_REACH_SPECIFIED_AREA` (18)
… (`const.py` → `SERVICE_RESULT_MESSAGES` has readable text). Read-only
commands may omit `result`.

### Deep sleep and timeouts

The robot keeps its MQTT connection while in deep sleep but ignores
commands: publishes succeed, no reply arrives. Re-sending
`active_robot_publish` wakes it; the client retries a timed-out command once
after that. Repeated timeouts are not a token problem — but the broker can
also silently stop routing when the JWT nears expiry, so after 3 consecutive
failures the coordinator refreshes the token and reconnects.

## 5. Status and Requests

### 5.1 `RobotBaseStatus` (push `status/robot_base_status`, and inside `GetDeviceBaseStatus_Response`)

| Field | Name | Notes |
|-------|------|-------|
| 1 | `errorCode` (repeated `ErrorCode`) | `{identityCode, level, debugDetail}`; an empty entry = no error. Codes → `error_codes.py` |
| 2 | `batteryPercentage` | float32 |
| 3 | `robotTaskStatus` | §5.2 |
| 11 | `stationContactType` | `NOT_CONTACTED` / `CONTACTED` / `CHARGING` |
| 12 | `detergentState` | `INSTALLED_AND_NOT_EMPTY`, `INSTALLED_AND_EMPTY`, `RUNNING_OUT`, … |
| 15 | `terminateReason` | `TaskResult` of the last task |
| 16 | `autoAddDetergentEnabled` | bool |
| 20 / 23 / 24 | `dustBoxState` / `cleanWaterTankState` / `sewageTankState` | |
| 26 | `fanLevel` | `FanLevel` |
| 29 | `mopHumidity` | `MopHumidity` |
| 30 | `mapId` | |
| 32 | `robotTaskStatusList` | parallel tasks (e.g. drying while idle) |
| 47 | `chargingStatus` | `CHG_STATUS_CHARGING`, `_FULL`, `_NOT_CHARGING`, … |

### 5.2 `RobotTaskStatus` — what the robot is doing

`task` (field 1, enum `RobotTaskStatus_TaskType`) plus **one** task-specific
sub-status field, plus `pauseStatus` (field 2, bool).

| Value | `task` | Sub-status field |
|-------|--------|------------------|
| 1 | `TASK_TYPE_IDLE` | `idleStatus`: `IN_STATION`, `LOW_POWER_IN_STATION` (asleep on dock), `OUTSIDE_STATION`, … |
| 2–6 | `SWEEP`, `MOP`, `SWEEP_THEN_MOP`, `SWEEP_AND_MOP`, `SMART_CLEAN` | `sweepStatus`, `mopStatus`, … (`ROOM_CLEAN`, `REGION_CLEAN`, `AUTO_RECALL_TO_WASH`, `WAITING_COMMAND`, …) |
| 7 | `BUILD_MAP` | `buildMapStatus` |
| 10 | `RECALL` | `recallStatus`: `BACK_TO_STATION`, `PARKING`, `RELOCALIZATION` |
| 14 | `WASH_AND_DRY_MOP` | `washAndDryStatus`: `WASH`, `DRY`, … |
| 17 | `COMPOSITE_CLEAN` | `compositeCleanStatus`: `SELF_CHECK`, `DEPARTURE`, `ROOM_SWEEP`, `ROOM_SWEEP_AND_MOP`, `AUTO_WASH`, … — **room cleans started with `clean/start_clean` report this** |
| 19 | `DUST_GATHERING` | `dustGatheringStatus` |
| 28 | `WAITING_COMMAND` | |

Full list: `RobotTaskStatus_TaskType` in `schema.py`. Mapping to HA
activity (`models.py` → `NarwalState.activity`):

- `pauseStatus` true, or a `*_WAITING_COMMAND` sub-status → **paused**
- cleaning-type task with a `RECALL`/`BACK_TO_STATION`/`PARKING` sub-status, or `RECALL`/`PARKING` task → **returning**
- cleaning-type task → **cleaning**
- `WASH_AND_DRY_MOP`, `DUST_GATHERING`, `DRY_STATION_BAG` → **docked**
- `IDLE` → **docked** if on the station (`stationContactType` or `idleStatus`), else **idle**
- `IDLE` with a non-empty `errorCode` → **error**

The old integration treated field 3.1 as a single "WorkingStatus" enum
(2 = paused, 10 = docked …). Those values are task types: 2 is vacuuming and
10 is returning.

### 5.3 `WorkingStatus` (push `status/working_status`)

| Field | Name | Notes |
|-------|------|-------|
| 1 | `workingProgress` | float; 0–1 or 0–100 depending on firmware |
| 2 | `coveredArea` | float, m² |
| 3 | `timeConsuming` | s |
| 4 | `remainedTime` | s |
| 6 | `cleaningZoneId` | room being cleaned |
| 8–17 | drying / dust-bag timers | field 13 is `totalDryStationBagTime`, **not** cleaned area |

Keeps arriving (frozen) after a task ends; use `RobotBaseStatus` for state.

### 5.4 Enums used in requests

| Enum | Values |
|------|--------|
| `FanLevel` | 1 MUTE (Quiet), 2 NORMAL, 3 STRONG, 4 DEEP, 5 SUPER (Max). 0 = unspecified |
| `MopHumidity` | 1 DRY, 2 NORMAL, 3 WET |
| `CleanMode` | 1 SMART, 2 SWEEP, 3 MOP, 4 SWEEP_MOP_SYNC, 5 SWEEP_THEN_MOP, 6 COMPOSITE_CLEAN |
| `ZoneType` | 1 ROOM, 2 ZONE, 3 WHOLE_MAP |
| `TaskType` (CleanTask) | 1 GENERAL_SWEEP … 5 CUSTOM_CLEAN, 7 SCHEDULED |

### 5.5 Settings: `GetConfig_Response.config` vs `SetConfig_Request`

Same field **names**, different **numbers** — always go through `proto.py`.

| Setting | Get # | Set # |
|---------|-------|-------|
| `volumePercentage` | 1 | 1 |
| `disturbMode` `{enabled, beginTime, endTime}` (seconds after midnight) | 4 | 5 |
| `autoAddDetergentEnabled` | 6 | 7 |
| `childLockEnabled` | 7 | 8 |
| `cleanCarpetEnabled` | 8 | 9 |
| `dryMopStrength` (`QUIET`/`STRONG`/`SMART`) | 12 | 13 |
| `cleanMopFreq` (`EFFICIENT`/`NORMAL`/`DEEP`) | 13 | 14 |
| `carpetCleanOption` (`ACROSS`/`PRESSURE`/`IGNORE`/`AVOID`) | 15 | 15 |
| `petMode` | 22 | 22 |
| `smartDeepCleanEnabled` | 23 | 23 |
| `dustGatheringEnabled` (auto-empty) | 27 | 27 |
| `quietDustGatheringEnabled` | 30 | 30 |
| `hotWaterWashEnabled` | 32 | 32 |

Proto3: an absent bool reads as false.

### 5.6 Starting a clean — `StartClean_Request`

```
StartClean_Request
  1 task: CleanTask
      1 mapId                (from the map, usually 1)
      2 items: CleanItem     (repeated — ONE PER ROOM)
          1 zone: ZoneOption
              1 zoneType = ZONE_TYPE_ROOM
              2 zoneId   = <room id>
          2 param: CleanParam (CleanTask.pb)
              1 mode        = CleanMode (SWEEP / MOP / SWEEP_MOP_SYNC / SWEEP_THEN_MOP)
              2 fanLevel    = FanLevel     (when vacuuming)
              4 mopHumidity = MopHumidity  (when mopping)
              5 sweepTime / 6 mopTime / 7 sweepMopSyncTime = passes for that phase
          3 order = 1, 2, …
      5 taskType = TASK_TYPE_CUSTOM_CLEAN
```

Live-verified. The robot answers
`FORBIDDEN_TASK_IF_CAN_NOT_REACH_SPECIFIED_AREA` if any room is a no-go room
(§6.3) — skip those for a whole-house clean. Not yet live-verified: MOP /
SWEEP_MOP_SYNC / SWEEP_THEN_MOP modes and `FAN_LEVEL_SUPER` inside a task.

## 6. Map

### 6.1 `GetMap_Response.map` = `StaticMapPayload`

| Field | Name | Notes |
|-------|------|-------|
| 1 | `mapId` | |
| 3 | `resolution` | mm per cell (60) |
| 4 / 5 | `width` / `height` | grid cells |
| 6 | `border` | `{left, right, top, bottom}` in cells; width = right − left + 1 |
| 7 / 8 / 24 | `origin` / `station` / `robotPose` | `PoseData {location {x, y}, theta}` |
| 9 | `forbiddens` | repeated `ForbiddenArea` (§6.3) |
| 12 | `roomInfos` | repeated `RoomInfo` (§6.4) |
| 17 | `mapData` | compressed grid (§6.2) |
| 18 | `mapDataFormat` | `MAP_DATA_FORMAT_COMPRESSED_ON_GZIP` |
| 25 | `rotateAngle` | display rotation, **clockwise** (90 on the test map) |

### 6.2 Grid

`mapData` is zlib/gzip; inside is `{1: packed varints}`, one varint per cell,
row-major from the **bottom** row (flip vertically for images).
`room_id = v >> 8`, `type = v & 0xFF`; `0` unknown, `0x20` unassigned floor,
`0x28` obstacle, `type & 0x10` room border.

**Poses are in grid-cell units**, not metres:
`pixel = (pose.x − border.left, pose.y − border.bottom)` (bottom-up). Verified:
the docked robot lands ~4 cells from a wall, and a mid-clean pose lands in
the room being cleaned.

### 6.3 No-go rooms — `ForbiddenArea`

`{polygon, type, id, roomId, mode}`. `mode = FORBIDDEN_MODE_ROOM` with
`type = FORBIDDEN_TYPE_SWEEP_MOP` marks a whole room no-go.

### 6.4 Rooms — `RoomInfo`

`roomId` (1), `type` (2, room kind — table below), `customName` (3, empty if
not renamed), `roomTypeId` (8, numbering for duplicates, "Bedroom 2").

| type | Name | type | Name |
|------|------|------|------|
| 0 | Room | 8 | Dining Room |
| 1 | Master Bedroom | 9 | Balcony |
| 2 | Bedroom | 10 | Utility Room |
| 3 | Living Room | 11 | Study |
| 4 | Kitchen | 12 | Nursery |
| 5 | Bathroom | 13 | Recreation Room |
| 6 | Toilet | 14 | Storage Room |
| 7 | Dining Room | 15 | Other |

(Names confirmed against the app; some differ from the app's string table.)

### 6.5 Live position — `DisplayMap` (push `map/display_map`)

`robot` (1, `PoseData`), `currentCleanZoneId` (4), `station` (5),
`robotPoseUpdateTime` (10). Sent every `displayMapDuration` ms while an app
client is registered. Same units as §6.2.

## 7. Errors and Consumables

- `ErrorCode.identityCode` → message: `error_codes.py` (229 codes for this
  model from the app's own table; the app's product key `EHf6cRNRGT` is the
  **J4** platform internally).
- Consumables: `GetConsumableInfo_Response.consumableInfo` lists items **due**:
  `maintainItems` (`ConsumableMaintainItem`: DUST_BOX, DUST_FILTER,
  WASH_RIBS, UNIVERSAL_WHEEL, CLIFF_SENSOR, …) and `replaceItems`
  (`ConsumableReplaceItem`: DUST_FILTER, MOP, SIDE_BRUSH, ROLLER_BRUSH,
  DETERGENT, DUST_BAG, …). Empty = nothing due.

## 8. Local Network

The robot listens on TCP 53 (DNS) and 6789 (ZeroMQ ZMTP 3.1, NULL security,
**PULL** socket — accepts pushes from anyone on the LAN), plus a dozen
high ports that answer no common protocol. Pushing commands to 6789 in any
framing (raw, multipart topic + payload, Narwal frame) has no observable
effect, and the official app sends no local traffic at all. Treat the
Freo X Ultra as **cloud-only**.

## 9. Home Assistant Integration Notes

### Threading

paho-mqtt runs its own network thread; blocking calls (SSL context, waiting
on a reply) run in executors; replies are matched with `threading.Event`, and
a per-command lock serialises calls to the same topic (they share one
response topic). Pushes are handed to the event loop with
`call_soon_threadsafe`.

### Coordinator

1. Refresh the token (fallback: full login), connect, subscribe to push
   topics and pre-subscribe the response topics
2. `active_robot_publish`, then status, feature list, firmware, config,
   consumables, map
3. Pushes drive state; polling (30 s active / 5 min idle) is a backup and
   drives reconnects
4. Config and consumables every 15 min; map every 30 min and after each task
5. The camera only renders; the coordinator owns map fetching

### Rooms in HA

The vacuum supports `CLEAN_AREA` (HA 2026.3+): rooms are segments, and a
repair issue is raised when the robot's rooms change after the user mapped
them. `vacuum.send_command` → `clean_rooms` remains for the card and
automations.

## 10. Pitfalls

1. **Device name ≠ device ID.** MQTT needs the 32-char hex device name, not the
   numeric cloud ID.
2. **Two `CleanParam` messages.** Use CleanTask.pb's (§5.6).
3. **Get vs set field numbers differ** for settings (§5.5).
4. **Fan level / mop wetness are rejected while docked**; remember the choice
   and send it in the next clean task.
5. **No-go rooms** make the whole task fail.
6. **Pause is a flag** (`pauseStatus`), not a task type.
7. **Repeated fields** (rooms, items, forbiddens) must not be collapsed to the
   last value.
8. **Map poses are grid cells**; rotation is clockwise.
9. **Explicit subscriptions only** — wildcard subscriptions are accepted by the
   broker but deliver nothing.
10. **Blocking SSL in HA** — create the MQTT client in an executor.
