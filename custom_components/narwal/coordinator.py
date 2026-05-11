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
    CleanMode,
    NarwalClient,
    NarwalCloud,
    NarwalCloudError,
    NarwalCommandError,
    NarwalConnectionError,
    NarwalState,
)
from .narwal_client.const import WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_ACTIVE = timedelta(seconds=60)
POLL_INTERVAL_IDLE = timedelta(minutes=5)
MAX_CONSECUTIVE_FAILURES = 3

IDLE_STATUSES = frozenset({
    WorkingStatus.SLEEPING,
    WorkingStatus.CHARGING,
    WorkingStatus.DOCKED,
    WorkingStatus.CHARGED,
})


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Manages communication with the Narwal vacuum via MQTT."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL_ACTIVE,
        )
        self.config_entry = entry
        self._cloud: NarwalCloud | None = None
        self.selected_clean_mode: CleanMode = CleanMode.VACUUM_AND_MOP
        self._consecutive_failures: int = 0

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
        _LOGGER.info(
            "Setting up Narwal: device_name=%s, product_key=%s, broker=%s",
            self.client.device_name,
            self.client.product_key,
            self.client.broker,
        )
        if self._cloud:
            _LOGGER.info(
                "Token expired=%s, refreshing to ensure valid MQTT credentials",
                self._cloud.session.is_token_expired,
            )
            await self._reauth()

        await self.client.connect()
        self.client.on_state_update = self._on_state_update

        try:
            await self.client.request_status_update()
        except NarwalCommandError:
            _LOGGER.warning("Initial status request timed out — vacuum may be asleep")

        try:
            await self.client.fetch_rooms()
        except NarwalCommandError:
            _LOGGER.warning("Initial room fetch timed out — will retry on next poll")

        state = self.client.state
        _LOGGER.info(
            "Initial state: battery=%.1f%%, status=%s, docked=%s",
            state.battery_level,
            state.working_status.name,
            state.is_docked,
        )
        self._consecutive_failures = 0
        self.async_set_updated_data(state)

    async def _reauth(self) -> None:
        """Re-authenticate: try token refresh first, fall back to full login."""
        if not self._cloud:
            return

        try:
            session = await self.hass.async_add_executor_job(
                self._cloud.refresh_token
            )
            self._apply_new_token(session.access_token, session.refresh_token)
            _LOGGER.info("Token refreshed successfully")
            return
        except NarwalCloudError as err:
            _LOGGER.info("Token refresh failed (%s), attempting full re-login", err)

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

    async def _reconnect_with_fresh_token(self) -> None:
        """Full reconnect cycle: refresh token, disconnect, reconnect."""
        _LOGGER.warning(
            "Forcing token refresh + reconnect after %d consecutive failures",
            self._consecutive_failures,
        )
        try:
            await self._reauth()
            if self.client.connected:
                await self.client.disconnect()
            await self.client.connect()
            self.client.on_state_update = self._on_state_update
            self._consecutive_failures = 0
            _LOGGER.warning("Reconnected successfully with fresh token")
        except Exception:
            _LOGGER.error("Reconnect failed", exc_info=True)

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle state updates from MQTT."""
        self._consecutive_failures = 0
        self._adjust_poll_interval()
        self.async_set_updated_data(state)

    def _adjust_poll_interval(self) -> None:
        """Switch between active/idle poll cadence based on last-known status.

        When the vacuum is docked/sleeping, polls just time out (the device
        doesn't process commands in deep sleep), so we back off to reduce
        radio traffic. Push broadcasts still arrive in real time when the
        vacuum wakes, so we'll switch back to active cadence on next push.
        """
        new_interval = (
            POLL_INTERVAL_IDLE
            if self.client.state.working_status in IDLE_STATUSES
            else POLL_INTERVAL_ACTIVE
        )
        if self.update_interval != new_interval:
            _LOGGER.info(
                "Poll interval -> %s (status=%s)",
                new_interval, self.client.state.working_status.name,
            )
            self.update_interval = new_interval

    async def _async_update_data(self) -> NarwalState:
        """Poll for status (backup for push updates)."""
        needs_reconnect = False

        if self._cloud and self._cloud.session.is_token_expired:
            needs_reconnect = True
        elif self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and self._cloud:
            needs_reconnect = True

        if needs_reconnect:
            await self._reconnect_with_fresh_token()

        # Availability is driven by MQTT connection state (set in the
        # client's connect/disconnect callbacks), not by command success.
        # A vacuum in deep sleep is reachable-but-quiet, not unreachable.
        # Failure tracking here only drives the periodic reconnect.
        if self.client.connected:
            try:
                await self.client.notify_active()
            except Exception:
                _LOGGER.debug("active_robot_publish failed", exc_info=True)

            try:
                await self.client.request_status_update()
                self._consecutive_failures = 0
            except NarwalCommandError:
                self._consecutive_failures += 1
                _LOGGER.warning(
                    "Status poll failed (%d/%d before reconnect)",
                    self._consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                )
        else:
            self._consecutive_failures += 1
            _LOGGER.warning("MQTT not connected, failure count: %d", self._consecutive_failures)

        self._adjust_poll_interval()
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect MQTT."""
        await self.client.disconnect()
