"""DataUpdateCoordinator for Narwal integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_MQTT_CLIENT_ID,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_PASSWORD,
    CONF_PRODUCT_KEY,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_UUID,
    DOMAIN,
)
from .narwal_client import (
    NarwalClient,
    NarwalCloud,
    NarwalCloudError,
    NarwalConnectionError,
    NarwalState,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Manages communication with the Narwal vacuum via MQTT."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.config_entry = entry
        self._cloud: NarwalCloud | None = None

        if CONF_ACCESS_TOKEN in entry.data:
            self._setup_from_cloud_config(entry)
        else:
            self._setup_from_legacy_config(entry)

    def _setup_from_cloud_config(self, entry: ConfigEntry) -> None:
        region = entry.data.get(CONF_REGION, "us")
        self._cloud = NarwalCloud(region=region)
        self._cloud.session.access_token = entry.data[CONF_ACCESS_TOKEN]
        self._cloud.session.refresh_token = entry.data[CONF_REFRESH_TOKEN]
        self._cloud.session.user_uuid = entry.data[CONF_USER_UUID]
        self._cloud.session.region = region
        self._cloud._update_token_expiry()

        self.client = NarwalClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data[CONF_USER_UUID],
            mqtt_username=entry.data[CONF_USER_UUID],
            mqtt_password=entry.data[CONF_ACCESS_TOKEN],
            broker=self._cloud.mqtt_broker,
        )

    def _setup_from_legacy_config(self, entry: ConfigEntry) -> None:
        self.client = NarwalClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data[CONF_USER_UUID],
            mqtt_username=entry.data[CONF_MQTT_USERNAME],
            mqtt_password=entry.data[CONF_MQTT_PASSWORD],
            mqtt_client_id=entry.data.get(CONF_MQTT_CLIENT_ID) or None,
        )

    async def async_setup(self) -> None:
        """Set up the coordinator -- always refresh token, then connect to MQTT."""
        if self._cloud:
            _LOGGER.info(
                "Token expired=%s, refreshing to ensure valid MQTT credentials",
                self._cloud.session.is_token_expired,
            )
            await self._reauth()

        await self.client.connect()
        self.client.on_state_update = self._on_state_update
        await self.client.request_status_update()

        state = self.client.state
        _LOGGER.info(
            "Initial state: battery=%.1f%%, status=%s, docked=%s",
            state.battery_level,
            state.working_status.name,
            state.is_docked,
        )
        self.async_set_updated_data(state)

    async def _reauth(self) -> None:
        """Re-authenticate: try token refresh first, fall back to full login."""
        if not self._cloud:
            return

        # Try token refresh first
        try:
            session = await self.hass.async_add_executor_job(
                self._cloud.refresh_token
            )
            self._apply_new_token(session.access_token, session.refresh_token)
            _LOGGER.info("Token refreshed successfully")
            return
        except NarwalCloudError as err:
            _LOGGER.info("Token refresh failed (%s), attempting full re-login", err)

        # Fall back to email/password login
        email = self.config_entry.data.get(CONF_EMAIL)
        password = self.config_entry.data.get(CONF_PASSWORD)
        if not email or not password:
            _LOGGER.error("Cannot re-login: no stored credentials")
            return

        try:
            session = await self.hass.async_add_executor_job(
                self._cloud.login, email, password
            )
            self._apply_new_token(session.access_token, session.refresh_token)
            _LOGGER.info("Re-login succeeded")
        except NarwalCloudError as err:
            _LOGGER.error("Re-login failed: %s -- MQTT will likely fail", err)

    def _apply_new_token(self, access_token: str, refresh_token: str) -> None:
        new_data = {**self.config_entry.data}
        new_data[CONF_ACCESS_TOKEN] = access_token
        new_data[CONF_REFRESH_TOKEN] = refresh_token
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=new_data
        )
        self.client._mqtt_password = access_token
        _LOGGER.info("Narwal access token updated")

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle state updates from MQTT."""
        self.async_set_updated_data(state)

    async def _async_update_data(self) -> NarwalState:
        """Poll for status (backup for push updates)."""
        if self._cloud and self._cloud.session.is_token_expired:
            await self._reauth()
            if self.client.connected:
                await self.client.disconnect()
                await self.client.connect()
                self.client.on_state_update = self._on_state_update

        if self.client.connected:
            await self.client.request_status_update()
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect MQTT."""
        await self.client.disconnect()
