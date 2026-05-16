"""Narwal Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeAlias

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
from .coordinator import NarwalCoordinator
from .narwal_client import NarwalConnectionError

_LOGGER = logging.getLogger(__name__)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

# Cache-bust the JS URL with the manifest version so HACS upgrades
# force browsers to fetch the new file instead of serving stale JS
# alongside an updated Python backend.
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
try:
    _CARD_VERSION = json.loads(_MANIFEST_PATH.read_text())["version"]
except Exception:
    _CARD_VERSION = "0"

CARD_JS_URL = f"/{DOMAIN}/narwal-vacuum-card.js"
CARD_JS_URL_VERSIONED = f"{CARD_JS_URL}?v={_CARD_VERSION}"
CARD_JS_PATH = Path(__file__).parent / "frontend" / "narwal-vacuum-card.js"


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    await _register_card(hass)

    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Narwal MQTT: {err}") from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _register_card(hass: HomeAssistant) -> None:
    """Register the Narwal Lovelace card.

    Two layers of registration:
      1. async_register_static_paths + add_extra_js_url — injects a
         <script type="module"> tag into every page render. Works for
         dashboards loaded AFTER this call returns.
      2. Lovelace resources storage — same place the UI's "Resources"
         tab writes to. Persists across restarts and is loaded on
         every dashboard render from boot, eliminating the race where
         a dashboard renders before the integration finishes setup.
    """
    if hass.data.get("narwal_card_registered"):
        return
    if not CARD_JS_PATH.is_file():
        _LOGGER.warning("Card JS not found at %s — skipping", CARD_JS_PATH)
        return
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_JS_URL, str(CARD_JS_PATH), False)]
        )
        add_extra_js_url(hass, CARD_JS_URL_VERSIONED)
        await _ensure_lovelace_resource(hass)
        hass.data["narwal_card_registered"] = True
    except Exception:
        _LOGGER.warning("Failed to register Narwal card frontend", exc_info=True)


async def _ensure_lovelace_resource(hass: HomeAssistant) -> None:
    """Add the card to Lovelace's persistent resource list if missing.

    Idempotent — updates the URL (cache-bust) if it already exists, or
    creates it if not. Quietly no-ops on yaml-mode Lovelace setups
    (no resource storage to write to).
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None) if lovelace else None
    if resources is None:
        return
    try:
        if not resources.loaded:
            await resources.async_load()
        existing = None
        for item in list(resources.async_items()):
            url = item.get("url", "")
            if url == CARD_JS_URL or url.startswith(f"{CARD_JS_URL}?"):
                existing = item
                break
        if existing is None:
            await resources.async_create_item(
                {"res_type": "module", "url": CARD_JS_URL_VERSIONED}
            )
            _LOGGER.info("Registered Narwal card as Lovelace resource")
        elif existing.get("url") != CARD_JS_URL_VERSIONED:
            await resources.async_update_item(
                existing["id"],
                {"res_type": "module", "url": CARD_JS_URL_VERSIONED},
            )
            _LOGGER.info("Updated Narwal card resource URL to v%s", _CARD_VERSION)
    except Exception:
        _LOGGER.debug("Could not register Lovelace resource", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
