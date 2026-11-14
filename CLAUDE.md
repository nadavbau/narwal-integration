# Narwal HA Integration — Claude Guide

## Project Structure

- `custom_components/narwal/` — Home Assistant integration
- `custom_components/narwal/narwal_client/` — MQTT client library (protocol layer)
- `custom_components/narwal/frontend/` — Lovelace custom card
- Keep HA-specific code (entities, config flow) separate from the MQTT client library.

## Code Style

- Python 3.12+, type hints on all public functions.
- Use `from __future__ import annotations` in all modules.
- Follow Home Assistant coding standards for entity classes.

## Tests

Run `pytest` (Python 3.14, `pip install -r requirements_test.txt`). The tests load the integration in Home Assistant against a fake robot (`tests/conftest.py`) that answers with schema-encoded messages; extend `FakeRobot` when adding commands. CI runs them on every pull request.

## Git Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` new features or entities
- `fix:` bug fixes
- `refactor:` code restructuring without behavior change
- `docs:` documentation changes
- `chore:` build, CI, dependencies

Examples:
- `feat: add locate button entity`
- `fix: battery sensor showing 0% on startup`
- `refactor: extract protobuf parsing into utility module`

## Security

- NEVER commit personal identifiers, credentials, API keys, tokens, passwords, device IDs, or email addresses.
- Use placeholder values in examples/templates (e.g. `your_email@example.com`, `your_device_id`, `abcdef1234567890abcdef1234567890`).
- Credentials in test scripts must be read from environment variables, never hardcoded.

## Version & Release

### Always bump version

When making functional changes (not docs-only), bump the patch version in `custom_components/narwal/manifest.json`. HACS detects updates by comparing the manifest version against the GitHub release tag.

### Push command

The `GH_TOKEN` env var may be set to a different GitHub account. Always push with it unset:

```bash
GH_TOKEN="" GITHUB_TOKEN="" git push origin main
```

### Do NOT create tags or releases

A GitHub Actions workflow automatically creates the git tag and GitHub release when changes are pushed to `main`. Never manually run `git tag` or `gh release create` — it will conflict with the automated workflow.

---

## Narwal Protocol Notes
*Applies to: `custom_components/narwal/narwal_client/**/*.py`*

### Source of truth: the app's schema

Field numbers and enums come from the official Narwal app's message definitions, generated into `narwal_client/schema.py` (see `tools/schema/README.md` to regenerate). Never hand-number protobuf fields: build and read messages with `narwal_client/proto.py` (`encode("SetConfig_Request", {...})`, `decode("RobotBaseStatus", payload)`). Full reference: `PROTOCOL_REFERENCE.md`.

### Framing

Request = Narwal frame (`0x01` + varint(len) + `{1: uuid, 2: uuid}`) + encoded request message appended after the frame. Responses and pushes use the same frame; skip all of it before decoding. Most responses carry `result` (`ServiceResult`, 1 = success).

### Status

- `status/robot_base_status` push / `GetDeviceBaseStatus_Response.robotBaseStatus` = `RobotBaseStatus`: battery (2), `robotTaskStatus` (3), `fanLevel` (26), `mopHumidity` (29), tank/dust bin/detergent states, `errorCode` (1).
- `robotTaskStatus` = `task` (task TYPE: 1 idle, 2 sweep, 10 recall, 17 composite clean, …) + one task-specific sub-status + `pauseStatus` (field 2). Pause is that flag, not a task type. There is no single "WorkingStatus" enum — the old code misread task types 2 (sweep) as paused and 10 (recall) as docked.
- `status/working_status` = `WorkingStatus`: `coveredArea` (2, m²), `timeConsuming` (3), `remainedTime` (4), `cleaningZoneId` (6). Field 13 is a dust-bag timer, not area.

### Commands

- Room clean: `clean/start_clean` with `StartClean_Request.task` = `CleanTask`, one `CleanItem` per room, `CleanParam` from **CleanTask.pb** (mode, fanLevel, mopHumidity, sweepTime/mopTime/sweepMopSyncTime), `taskType` = `TASK_TYPE_GENERAL_<mode>` like the app (`CUSTOM_CLEAN` only for per-room settings; see `CLEAN_MODE_TASK_TYPE`). A second message named `CleanParam` exists in ReportBaseTypes.pb — don't use it.
- Rooms the map marks no-go (`ForbiddenArea` with `FORBIDDEN_MODE_ROOM`) make the robot refuse the whole task; skip them for whole-house cleans.
- `clean/set_fan_level` / `clean/set_mop_humidity` are only accepted during a clean; while docked, store the choice and send it in the next task. Fan levels: 1 quiet … 3 strong, 5 max (0 = unspecified).
- Settings: `config/get` / `config/set` share field names but NOT numbers (e.g. `disturbMode` is 4 in Get, 5 in Set). Explicit `false` must be sent to turn a setting off.

### Deep sleep, waking and token expiry

The vacuum goes into deep sleep after a while idle (`IDLE_STATUS_LOW_POWER_IN_STATION`): it keeps its MQTT connection but ignores commands. The fire-and-forget keepalive (`notify_active`) does NOT wake it. Wake it like the app: `notify_app_event` (`APP_EVENT_ENTER_DEVICE_PAGE`), then `active_robot_publish` as a request, waiting for the reply with timeouts 1, 1, 2, 2, 4, 4, 8, 8, 16, 30 s, and only then send the command — that's `NarwalClient.wake()`, which `send_command` runs when the robot looks asleep and after a timeout. Background reads (polls, config, map) pass `wake=False, retry=False` and must never wake it. Poll timeouts while the robot reported sleeping are expected; separately the broker may stop routing near JWT expiry, so reconnect after 3 unexplained poll failures, on token expiry, or when a wake-up gets no answer. Every client on the account receives every reply; match replies by the echoed MQTT5 CorrelationData, not by topic. A request that times out while broadcasts are still arriving can mean a dozing robot (it keeps broadcasting ~1 min after it stops answering, e.g. while drying mops) or a connection that stopped receiving replies: `reset_connection()` and resend, then wake and resend (`_recover_and_resend`). Don't assume SUBSCRIBE succeeded; check SUBACK reason codes.

### Explicit subscriptions only

The Aliyun IoT broker accepts wildcard subscriptions but only delivers to explicit ones. `_on_connect` subscribes each push topic and pre-subscribes every command's `/response` topic; `_ensure_subscribed` covers any other command. Don't replace these with wildcards.

### Map

`StaticMapPayload`: grid in `mapData` (zlib/gzip, packed varints, `room_id = v >> 8`, bottom row first — flip vertically). Poses (station, robot, `DisplayMap.robot`) are in grid-cell units: `pixel = pose - border.left/bottom`. `rotateAngle` is clockwise.

---

## HA Integration Resilience
*Applies to: `custom_components/narwal/**/*.py`*

### Setup must tolerate offline devices

The vacuum enters deep sleep when idle. `async_setup` must never fail due to command timeouts — catch `NarwalCommandError` and start with empty state.

```python
# ❌ BAD — crashes setup when vacuum is asleep
await self.client.request_status_update()

# ✅ GOOD — setup succeeds, state populates when vacuum wakes
try:
    await self.client.request_status_update()
except NarwalCommandError:
    _LOGGER.warning("Vacuum may be asleep — will retry on next poll")
```

### Never block the event loop

All MQTT and SSL operations must run in executors. HA detects blocking calls (`ssl.create_default_context`, `threading.Event.wait`) and logs errors.

### Re-raise vs swallow errors

- **Setup path**: catch and log command errors (integration must load).
- **Poll path (`_async_update_data`)**: catch errors, track failure count for reconnect.
- **User-triggered actions** (locate, start, stop): let errors propagate to show in UI.

### Broadcast registration

The vacuum only sends pushes while an app client is registered via `active_robot_publish` (publish window 60 s). The coordinator re-sends it on its own 50 s timer, independent of the poll interval, That keeps an awake vacuum pushing; it does not wake a sleeping one (see *Deep sleep, waking and token expiry*).

---

## Lovelace Card Patterns
*Applies to: `custom_components/narwal/frontend/**/*.js`*

### Visual editor

Use `ha-form` with a schema array for card editors — this is how HA's built-in editors work and handles entity pickers reliably.

```javascript
// ❌ BAD — ha-entity-picker in shadow DOM won't render
const picker = document.createElement("ha-entity-picker");
picker.hass = this._hass;  // often fails to trigger render

// ✅ GOOD — ha-form handles everything
const SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "vacuum" } } },
  { name: "camera_entity", selector: { entity: { domain: "camera" } } },
];
this._form = document.createElement("ha-form");
this._form.schema = SCHEMA;
this._form.hass = this._hass;
this._form.data = this._config;
```

The editor element must NOT use shadow DOM — append directly to `this` (not `this.shadowRoot`) so HA's styling works.

### Entity resolution

Support explicit config AND auto-detection for all related entities. Auto-detect through the vacuum's device: find entities in `hass.entities` with the same `device_id` and the integration's `translation_key` (`map`, `battery`, `clean_mode`, `mop_humidity`, `error`, `consumables_due`, `dry_mop`, …). Entity IDs vary per install (the clean-mode select may be `select.narwal_<key>` with no `clean_mode` in it), so matching `*narwal*` names in `hass.states` is only a last-resort fallback.
