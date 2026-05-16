"""DataUpdateCoordinator for Narwal integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
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
    CONF_ROOMS_CACHE,
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
from .narwal_client.models import RoomInfo

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_ACTIVE = timedelta(seconds=60)
POLL_INTERVAL_IDLE = timedelta(minutes=5)
MAX_CONSECUTIVE_FAILURES = 3

# The active_robot_publish payload tells the vacuum keepalive=60_000ms.
# If we don't re-register within that window the vacuum drops us from
# its push-broadcast list and stops sending state updates. Send a
# keepalive on its own timer (independent of the adaptive poll cadence,
# which may be 5 min). 50s leaves a small safety margin.
KEEPALIVE_INTERVAL = timedelta(seconds=50)

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
        self._keepalive_unsub = None

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

        # Restore cached rooms before connecting so start_clean works
        # even if the vacuum is asleep and we can't fetch a fresh map.
        cached = self.config_entry.data.get(CONF_ROOMS_CACHE, [])
        if cached:
            self.client.state.rooms = [RoomInfo(**r) for r in cached]
            _LOGGER.info("Restored %d rooms from cache", len(cached))

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
            self._persist_rooms_cache()
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
        self._start_keepalive()

    def _persist_rooms_cache(self) -> None:
        """Save the current room list to the config entry data."""
        rooms = self.client.state.rooms
        if not rooms:
            return
        cache = [
            {
                "room_id": r.room_id,
                "room_sub_type": r.room_sub_type,
                "name": r.name,
                "category": r.category,
                "instance_index": r.instance_index,
            }
            for r in rooms
        ]
        if self.config_entry.data.get(CONF_ROOMS_CACHE) == cache:
            return
        new_data = {**self.config_entry.data, CONF_ROOMS_CACHE: cache}
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        _LOGGER.info("Cached %d rooms to config entry", len(rooms))

    def _start_keepalive(self) -> None:
        """Schedule notify_active every 50s to stay on the vacuum's push list."""
        if self._keepalive_unsub is not None:
            return
        self._keepalive_unsub = async_track_time_interval(
            self.hass, self._send_keepalive, KEEPALIVE_INTERVAL
        )

    async def _send_keepalive(self, _now=None) -> None:
        if not self.client.connected:
            return
        try:
            await self.client.notify_active()
        except Exception:
            _LOGGER.debug("keepalive notify_active failed", exc_info=True)

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
        """Full reconnect cycle: refresh token, disconnect, reconnect.

        disconnect() no longer flips availability, so the entity stays
        in its last-known state during the brief disconnect→connect
        window. If reconnect fails outright, flip to unavailable here.
        """
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
            self.client.state.device_reachable = False
            self.async_set_updated_data(self.client.state)

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
        # notify_active is on its own 50s timer (_start_keepalive); no
        # need to send it here.
        status_ok = False
        if self.client.connected:
            try:
                await self.client.request_status_update()
                self._consecutive_failures = 0
                status_ok = True
            except NarwalCommandError:
                self._consecutive_failures += 1
                _LOGGER.warning(
                    "Status poll failed (%d/%d before reconnect)",
                    self._consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                )

            # Only retry the map fetch when the vacuum is actually responsive.
            # Otherwise we just burn 15s of the poll cycle waiting for a
            # second timeout the camera is already (politely) chasing too.
            if status_ok and not self.client.state.rooms:
                try:
                    await self.client.fetch_rooms()
                    self._persist_rooms_cache()
                except NarwalCommandError:
                    _LOGGER.debug("Room re-fetch still failing — will retry next poll")
        else:
            self._consecutive_failures += 1
            _LOGGER.warning("MQTT not connected, failure count: %d", self._consecutive_failures)

        self._adjust_poll_interval()
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect MQTT."""
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        await self.client.disconnect()
