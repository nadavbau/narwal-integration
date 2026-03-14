"""Narwal Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

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

CARD_JS_URL = f"/{DOMAIN}/narwal-vacuum-card.js"
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
    """Register the Narwal vacuum Lovelace card (once per HA session)."""
    if hass.data.get("narwal_card_registered"):
        return
    if not CARD_JS_PATH.is_file():
        _LOGGER.warning("Card JS not found at %s — skipping", CARD_JS_PATH)
        return
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_JS_URL, str(CARD_JS_PATH), False)]
        )
        add_extra_js_url(hass, CARD_JS_URL)
        hass.data["narwal_card_registered"] = True
    except Exception:
        _LOGGER.warning("Failed to register Narwal card frontend", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
