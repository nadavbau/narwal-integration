"""DataUpdateCoordinator for Narwal integration."""

from __future__ import annotations

import logging
import time
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
from .narwal_client.models import ACTIVITY_CLEANING, ACTIVITY_DOCKED, ACTIVITY_IDLE, RoomInfo

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_ACTIVE = timedelta(seconds=30)
POLL_INTERVAL_IDLE = timedelta(minutes=5)
MAX_CONSECUTIVE_FAILURES = 3
# Re-register with the robot before its 60 s publish window lapses.
KEEPALIVE_INTERVAL = timedelta(seconds=50)
# Settings / consumables / map change rarely; refresh on a slower cadence.
SLOW_REFRESH_SECONDS = 15 * 60
MAP_REFRESH_SECONDS = 30 * 60


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Manages communication with the Narwal vacuum via MQTT."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=POLL_INTERVAL_ACTIVE)
        self.config_entry = entry
        self._cloud: NarwalCloud | None = None
        self.selected_clean_mode: CleanMode = CleanMode.VACUUM_AND_MOP
        self.clean_passes: int = 1
        self._consecutive_failures = 0
        self._keepalive_unsub = None
        self._last_slow_refresh = 0.0
        self._last_activity: str | None = None
        self.map_version = 0  # bumped whenever the map or robot pose changes

        if CONF_ACCESS_TOKEN in entry.data:
            self._setup_from_cloud_config(entry)
        else:
            self._setup_from_legacy_config(entry)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

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
        """Refresh the token, connect, and load what the robot will tell us."""
        cached = self.config_entry.data.get(CONF_ROOMS_CACHE, [])
        if cached:
            self.client.state.rooms = [RoomInfo(**r) for r in cached]

        if self._cloud:
            await self._reauth()

        self.client.on_state_update = self._on_state_update
        await self.client.connect()

        try:
            await self.client.request_status_update()
        except NarwalCommandError:
            _LOGGER.warning("Initial status request timed out — vacuum may be asleep")

        await self.client.fetch_device_info()
        await self._slow_refresh()
        await self._refresh_map()

        self._consecutive_failures = 0
        self.async_set_updated_data(self.client.state)
        self._start_keepalive()

    async def _slow_refresh(self) -> None:
        """Settings and consumables: best effort, never fatal."""
        self._last_slow_refresh = time.monotonic()
        for fetch in (self.client.fetch_config, self.client.fetch_consumables):
            try:
                await fetch()
            except (NarwalCommandError, NarwalConnectionError) as err:
                _LOGGER.debug("Slow refresh %s failed: %s", fetch.__name__, err)

    async def _refresh_map(self) -> None:
        try:
            await self.client.fetch_map()
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.debug("Map refresh failed: %s", err)
            return
        self.map_version += 1
        self._persist_rooms_cache()

    def _persist_rooms_cache(self) -> None:
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
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_ROOMS_CACHE: cache}
        )

    def _start_keepalive(self) -> None:
        if self._keepalive_unsub is None:
            self._keepalive_unsub = async_track_time_interval(
                self.hass, self._send_keepalive, KEEPALIVE_INTERVAL
            )

    async def _send_keepalive(self, _now=None) -> None:
        if self.client.connected:
            await self.client.notify_active()

    # ------------------------------------------------------------------
    # Auth / reconnect
    # ------------------------------------------------------------------

    async def _reauth(self) -> None:
        """Refresh the token, falling back to a full login."""
        if not self._cloud:
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.refresh_token)
            self._apply_new_token(session.access_token, session.refresh_token)
            return
        except NarwalCloudError as err:
            _LOGGER.info("Token refresh failed (%s), attempting full re-login", err)

        email = self.config_entry.data.get(CONF_EMAIL)
        password = self.config_entry.data.get(CONF_PASSWORD)
        if not email or not password:
            _LOGGER.error("Cannot re-login: no stored credentials")
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.login, email, password)
            self._apply_new_token(session.access_token, session.refresh_token)
        except NarwalCloudError as err:
            _LOGGER.error("Re-login failed: %s", err)

    def _apply_new_token(self, access_token: str, refresh_token: str) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
            },
        )
        self.client._mqtt_password = access_token

    async def _reconnect_with_fresh_token(self) -> None:
        _LOGGER.warning("Refreshing token and reconnecting (failures=%d)", self._consecutive_failures)
        try:
            await self._reauth()
            await self.client.reset_connection()
            self._consecutive_failures = 0
            self.client.needs_reconnect = False
        except Exception:  # noqa: BLE001 - keep the coordinator alive
            _LOGGER.error("Reconnect failed", exc_info=True)
            self.client.state.device_reachable = False
            self.async_set_updated_data(self.client.state)

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def _on_state_update(self, state: NarwalState) -> None:
        """Push from MQTT (runs on the event loop)."""
        self._consecutive_failures = 0
        activity = state.activity
        if self._last_activity == ACTIVITY_CLEANING and activity in (ACTIVITY_DOCKED, ACTIVITY_IDLE):
            # A task just finished: the map may have changed, and
            # consumable counters move after every clean.
            self.hass.async_create_task(self._after_task_finished())
        self._last_activity = activity
        if state.robot_pose is not None:
            self.map_version += 1
        self._adjust_poll_interval()
        self.async_set_updated_data(state)

    async def _after_task_finished(self) -> None:
        await self._refresh_map()
        await self._slow_refresh()
        self.async_set_updated_data(self.client.state)

    def _adjust_poll_interval(self) -> None:
        idle = self.client.state.activity in (ACTIVITY_DOCKED, ACTIVITY_IDLE)
        new_interval = POLL_INTERVAL_IDLE if idle else POLL_INTERVAL_ACTIVE
        if self.update_interval != new_interval:
            self.update_interval = new_interval

    async def _async_update_data(self) -> NarwalState:
        """Poll as a backup for pushes; also drives reconnects."""
        if self._cloud and (
            self._cloud.session.is_token_expired
            or self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES
            or self.client.needs_reconnect
        ):
            await self._reconnect_with_fresh_token()

        if not self.client.connected:
            self._consecutive_failures += 1
            return self.client.state

        # Availability follows the MQTT connection; a robot in deep sleep is
        # reachable-but-quiet. Failures only drive the periodic reconnect.
        try:
            await self.client.request_status_update()
            self._consecutive_failures = 0
        except NarwalCommandError:
            if self.client.pushing_recently():
                # Broadcasts arrive but our request got no reply: the
                # connection went stale. Rebuild it now rather than after
                # three more failed polls.
                _LOGGER.warning("Status poll timed out while the robot is broadcasting; resetting the connection")
                try:
                    await self.client.reset_connection()
                except NarwalConnectionError as err:
                    _LOGGER.warning("Connection reset failed: %s", err)
                return self.client.state
            # A robot that reported going to sleep ignores polls; that's
            # expected and not a connection problem, so don't reconnect over it.
            if not self.client.state.sub_status.startswith("IDLE_STATUS_LOW_POWER"):
                self._consecutive_failures += 1
            _LOGGER.debug("Status poll failed (%d)", self._consecutive_failures)
            self._adjust_poll_interval()
            return self.client.state

        if time.monotonic() - self._last_slow_refresh > SLOW_REFRESH_SECONDS:
            await self._slow_refresh()
        state = self.client.state
        map_age = time.time() - state.map.fetched_at if state.map else None
        if not state.rooms or map_age is None or map_age > MAP_REFRESH_SECONDS:
            await self._refresh_map()

        self._adjust_poll_interval()
        return self.client.state

    async def async_shutdown(self) -> None:
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        await super().async_shutdown()
        await self.client.disconnect()
