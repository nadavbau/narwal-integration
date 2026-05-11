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

### Deep sleep behavior

The vacuum maintains its MQTT broker connection during deep sleep but does NOT process incoming commands. Publishes succeed (`rc=0`) but no response arrives. This is NOT a token/connection issue — the device is simply not listening. No amount of reconnecting will help; wait for it to wake.

### Token expiry

The JWT access token (used as MQTT password) expires. The broker may silently stop routing responses before the calculated expiry time. Track consecutive command failures and force token refresh + reconnect after 3 failures.

### Two status paths

- `status/robot_base_status` — contains `WorkingStatus` enum, battery, boolean flags (`is_cleaning`, `is_paused`, `is_returning`, `is_docked`). Sent as push broadcast AND as command response to `status/get_device_base_status`.
- `status/working_status` — contains ONLY `elapsed_time` (field 3) and `cleaned_area` (field 13). Does NOT contain `WorkingStatus` enum. Sent only during active cleaning.

### Explicit subscriptions only

Narwal's Aliyun IoT broker accepts wildcard subscriptions (e.g. `base_topic/#`) but does NOT route messages through them — only EXPLICIT topic subscriptions get messages delivered.

- `_on_connect` subscribes to each broadcast topic individually (`status/robot_base_status`, `status/working_status`).
- `_send_command_locked` subscribes to the specific `{topic}/response` before each publish and waits briefly for SUBACK. Do not remove these per-command subscribes — without them responses won't arrive.

### Map data

Map grid is zlib-compressed protobuf containing packed repeated varints. Each pixel: `room_id = val >> 8`, `pixel_type = val & 0xFF`. Map Y-axis is flipped (mathematical coords → image coords).

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

The vacuum only sends push broadcasts after receiving `active_robot_publish`. Re-send this every poll cycle — the vacuum may have woken from deep sleep since the last send and doesn't know about us.

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

Support explicit config AND auto-detection fallback for all entities (camera, battery sensor, mode select). Search `hass.states` for `*narwal*` patterns as fallback.
