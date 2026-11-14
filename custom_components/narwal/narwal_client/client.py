"""MQTT client for Narwal robot vacuum cloud communication."""

from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .const import (
    ASLEEP_AFTER_SECONDS,
    BROADCAST_TOPICS,
    CLEAN_MODE_LABEL,
    CLEAN_MODE_TASK_TYPE,
    COMMAND_RESPONSE_TIMEOUT,
    MQTT_BROKER,
    MQTT_PORT,
    SERVICE_RESULT_MESSAGES,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_FIRMWARE_VERSION,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESET_CONSUMABLE,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_WASH_AND_DRY_MOP,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    TOPIC_DISPLAY_MAP,
    TOPIC_ROBOT_BASE_STATUS,
    TOPIC_UPGRADE_STATUS,
    TOPIC_WORKING_STATUS,
    WAKE_TIMEOUTS,
    CleanMode,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, MapSnapshot, NarwalState, Pose, strip_narwal_frame
from .proto import ProtoError, decode, encode, encode_varint

_LOGGER = logging.getLogger(__name__)

# Response message type for each command we decode.
RESPONSE_TYPES: dict[str, str] = {
    TOPIC_CMD_ACTIVE_ROBOT: "ActiveRobotPublish_Response",
    TOPIC_CMD_NOTIFY_APP_EVENT: "NotifyAppEvent_Response",
    TOPIC_CMD_GET_BASE_STATUS: "GetDeviceBaseStatus_Response",
    TOPIC_CMD_GET_CONFIG: "GetConfig_Response",
    TOPIC_CMD_SET_CONFIG: "SetConfig_Response",
    TOPIC_CMD_GET_CONSUMABLE: "GetConsumableInfo_Response",
    TOPIC_CMD_GET_FEATURE_LIST: "GetFeatureList_Response",
    TOPIC_CMD_GET_FIRMWARE_VERSION: "GetFirmwareVersion_Response",
    TOPIC_CMD_GET_MAP: "GetMap_Response",
    TOPIC_CMD_SET_FAN_LEVEL: "SetFanLevel_Response",
    TOPIC_CMD_SET_MOP_HUMIDITY: "SetMopHumidity_Response",
    TOPIC_CMD_START_CLEAN: "StartClean_Response",
}
# Commands whose response just carries a ServiceResult in field 1.
_GENERIC_RESULT = "SetFanLevel_Response"

# Response topics subscribed on connect. Narwal's broker only routes to
# explicit subscriptions, so pre-subscribing saves a SUBACK round trip per
# command (previously a fixed 0.5 s sleep on every call).
_PRESUBSCRIBE = (
    TOPIC_CMD_ACTIVE_ROBOT, TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_GET_BASE_STATUS, TOPIC_CMD_GET_CONFIG, TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE, TOPIC_CMD_GET_MAP, TOPIC_CMD_YELL, TOPIC_CMD_PAUSE,
    TOPIC_CMD_RESUME, TOPIC_CMD_FORCE_END, TOPIC_CMD_RECALL, TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_SET_FAN_LEVEL, TOPIC_CMD_SET_MOP_HUMIDITY, TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_DRY_MOP, TOPIC_CMD_WASH_AND_DRY_MOP, TOPIC_CMD_DUST_GATHERING,
)

_PUSH_TYPES = {
    TOPIC_ROBOT_BASE_STATUS: "RobotBaseStatus",
    TOPIC_WORKING_STATUS: "WorkingStatus",
    TOPIC_DISPLAY_MAP: "DisplayMap",
    TOPIC_UPGRADE_STATUS: "OTAUpgradeStatus",
}


class NarwalConnectionError(Exception):
    """Raised when connection to the vacuum fails."""


class NarwalCommandError(Exception):
    """Raised when a command fails, is rejected, or times out."""

    def __init__(self, message: str, *, timeout: bool = False, result: Any = None) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.result = result


def _pb_string(field_num: int, value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data


def _pb_varint(field_num: int, value: int) -> bytes:
    return encode_varint(field_num << 3) + encode_varint(value)


class NarwalClient:
    """MQTT client for Narwal vacuum cloud communication."""

    def __init__(
        self,
        product_key: str,
        device_name: str,
        user_uuid: str,
        mqtt_username: str,
        mqtt_password: str,
        mqtt_client_id: str | None = None,
        broker: str = MQTT_BROKER,
        port: int = MQTT_PORT,
    ) -> None:
        self.product_key = product_key
        self.device_name = device_name
        self.user_uuid = user_uuid
        self.broker = broker
        self.port = port

        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None

        self._mqtt_username = mqtt_username
        self._mqtt_password = mqtt_password
        self._mqtt_client_id_prefix = mqtt_client_id or f"app_{user_uuid}"
        self._mqtt_client_id = f"{self._mqtt_client_id_prefix}_{uuid.uuid4()}"

        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pending requests keyed by the CorrelationData we sent; the robot echoes
        # it in the reply. Every client on the account receives every reply, so
        # matching by topic alone could hand us another client's answer.
        self._pending_responses: dict[bytes, tuple[threading.Event, list[bytes | None]]] = {}
        self._pending_by_topic: dict[str, bytes] = {}  # fallback for replies without one
        self._pending_subacks: dict[int, threading.Event] = {}
        self._sub_topics: dict[int, str] = {}  # SUBSCRIBE mid -> topic
        self._subscribed: set[str] = set()
        self._reset_lock: asyncio.Lock | None = None
        self.last_push_time: float = 0.0
        self.last_reset_time: float = 0.0
        self._sub_lock = threading.Lock()
        self._command_locks: dict[str, threading.Lock] = {}
        self._wake_lock: asyncio.Lock | None = None
        self.last_message_time: float = 0.0
        self.last_wake_time: float = 0.0
        self.needs_reconnect = False

    @property
    def base_topic(self) -> str:
        return f"/{self.product_key}/{self.device_name}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Framing
    # ------------------------------------------------------------------

    def _auth_frame(self) -> bytes:
        """0x01 + varint(len) + {1: uuid, 2: uuid}; every request starts with it."""
        inner = _pb_string(1, self.user_uuid) + _pb_string(2, self.user_uuid)
        return b"\x01" + encode_varint(len(inner)) + inner

    def _build_publish_properties(self, topic: str) -> Properties:
        corr = (
            _pb_string(1, str(uuid.uuid1()))
            + _pb_varint(3, 0)
            + _pb_varint(4, int(time.time() * 1000))
        )
        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = f"{topic}/response"
        props.CorrelationData = corr
        return props

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the MQTT broker (blocking parts run in an executor)."""
        self._loop = asyncio.get_running_loop()
        self._connected.clear()

        def _connect_blocking() -> None:
            self._setup_mqtt_client()
            if not self._connected.wait(timeout=15.0):
                if self._client:
                    self._client.loop_stop()
                raise NarwalConnectionError("MQTT connection timed out")

        await self._loop.run_in_executor(None, _connect_blocking)
        await self.notify_active()

    def _setup_mqtt_client(self) -> None:
        self._mqtt_client_id = f"{self._mqtt_client_id_prefix}_{uuid.uuid4()}"
        _LOGGER.info("Connecting to %s:%d, base_topic=%s", self.broker, self.port, self.base_topic)
        self._subscribed.clear()
        self._client = mqtt.Client(
            client_id=self._mqtt_client_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.username_pw_set(self._mqtt_username, self._mqtt_password)
        self._client.tls_set_context(ssl.create_default_context())
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.connect(self.broker, self.port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        if not (str(reason_code) == "Success" or reason_code == 0):
            _LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        _LOGGER.info("MQTT connected (%s)", self.base_topic)
        self._subscribed.clear()
        self._sub_topics.clear()
        topics = [f"{self.base_topic}/{t}" for t in BROADCAST_TOPICS]
        topics += [f"{self.base_topic}/{t}/response" for t in _PRESUBSCRIBE]
        for topic in topics:
            rc, mid = client.subscribe(topic, qos=1)
            if rc == 0:
                # Optimistic: _on_subscribe removes it again if the broker refuses.
                self._sub_topics[mid] = topic
                self._subscribed.add(topic)
            else:
                _LOGGER.warning("SUBSCRIBE %s not sent (rc=%s)", topic, rc)
        self._connected.set()
        self.state.device_reachable = True
        self._notify_state_update()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        topic = self._sub_topics.pop(mid, None)
        codes = reason_codes if isinstance(reason_codes, list) else [reason_codes]
        failed = [c for c in codes if getattr(c, "is_failure", False)]
        if failed and topic:
            # Without this subscription the robot's replies never arrive, so
            # forget it; _ensure_subscribed retries before the next command.
            self._subscribed.discard(topic)
            _LOGGER.warning("Broker refused subscription to %s: %s", topic, failed)
        evt = self._pending_subacks.pop(mid, None)
        if evt:
            evt.set()

    def _handle_reply(self, msg) -> None:
        props = getattr(msg, "properties", None)
        corr = getattr(props, "CorrelationData", None) if props is not None else None
        if corr:
            corr = bytes(corr)
            pending = self._pending_responses.pop(corr, None)
            if pending is None:
                _LOGGER.debug("Reply on %s is for another client or arrived late", msg.topic.rsplit("/", 2)[-2])
                return
            if self._pending_by_topic.get(msg.topic) == corr:
                del self._pending_by_topic[msg.topic]
        else:
            key = self._pending_by_topic.pop(msg.topic, None)
            pending = self._pending_responses.pop(key, None) if key is not None else None
            if pending is None:
                return
        event, holder = pending
        holder[0] = msg.payload
        event.set()

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        self._connected.clear()
        self.state.device_reachable = False
        self._notify_state_update()

    def _notify_state_update(self) -> None:
        cb = self.on_state_update
        loop = self._loop
        if cb and loop:
            try:
                loop.call_soon_threadsafe(cb, self.state)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Incoming messages
    # ------------------------------------------------------------------

    def _on_message(self, client, userdata, msg):
        self.last_message_time = time.time()
        if msg.topic.endswith("/response"):
            self._handle_reply(msg)
            return

        suffix = msg.topic[len(self.base_topic):].lstrip("/")
        msg_type = _PUSH_TYPES.get(suffix)
        if not msg_type:
            return
        self.last_push_time = time.time()
        try:
            data = decode(msg_type, strip_narwal_frame(msg.payload))
        except ProtoError:
            _LOGGER.debug("Undecodable %s push: %s", suffix, msg.payload.hex())
            return
        if suffix == TOPIC_ROBOT_BASE_STATUS:
            self.state.update_base_status(data)
        elif suffix == TOPIC_WORKING_STATUS:
            self.state.update_working_status(data)
        elif suffix == TOPIC_DISPLAY_MAP:
            self.state.update_display_map(data)
        elif suffix == TOPIC_UPGRADE_STATUS:
            _LOGGER.info("Firmware upgrade status: %s", data)
        self.state.device_reachable = True
        self._notify_state_update()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send_command(
        self,
        command: str,
        request: bytes = b"",
        *,
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
        response_type: str | None = None,
        check: bool = True,
        retry: bool = True,
        wake: bool = True,
    ) -> CommandResponse:
        """Send ``command`` with an encoded request body; wait for the reply.

        A robot in deep sleep keeps its MQTT connection but ignores commands.
        With ``wake`` (user actions), a robot that looks asleep is woken first;
        with ``retry``, a timed-out command triggers a full wake-up and is sent
        once more after the robot has answered. Background reads pass
        ``wake=False, retry=False`` so polling never wakes the robot.
        With ``check`` a non-success ServiceResult raises NarwalCommandError
        with a readable reason.
        """
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")
        loop = self._loop or asyncio.get_running_loop()
        # response_type=None -> look up / generic ServiceResult; "" -> don't decode.
        rtype = RESPONSE_TYPES.get(command, _GENERIC_RESULT) if response_type is None else response_type
        if wake and self.probably_asleep():
            await self.wake()
        try:
            raw = await loop.run_in_executor(None, self._send_blocking, command, request, timeout)
        except NarwalCommandError as err:
            if not (retry and err.timeout):
                raise
            raw = await self._recover_and_resend(command, request, timeout, err)

        resp = CommandResponse.parse(command, raw, rtype)
        if check and not resp.success:
            reason = SERVICE_RESULT_MESSAGES.get(str(resp.result), str(resp.result))
            raise NarwalCommandError(f"{command}: {reason}", result=resp.result)
        return resp

    async def _recover_and_resend(
        self, command: str, request: bytes, timeout: float, err: NarwalCommandError,
    ) -> bytes:
        """A command timed out: recover, then send it again.

        Seen live, two causes look alike at first: the robot dozing off (while
        drying mops it stops answering, keeps broadcasting for ~1 min, then goes
        quiet) and a connection that stops receiving replies. If the robot was
        broadcasting a moment ago, rebuild the connection and resend; if it
        still doesn't answer, or was already quiet, wake it and resend.
        """
        loop = self._loop or asyncio.get_running_loop()
        if self.pushing_recently():
            _LOGGER.warning("%s timed out while the robot was broadcasting; resetting the connection", command)
            await self.reset_connection()
            try:
                return await loop.run_in_executor(None, self._send_blocking, command, request, timeout)
            except NarwalCommandError as retry_err:
                if not retry_err.timeout:
                    raise
                err = retry_err
        _LOGGER.info("%s timed out; waking the robot and retrying", command)
        if not await self.wake():
            raise NarwalCommandError(f"{command}: the robot did not wake up", timeout=True) from err
        return await loop.run_in_executor(None, self._send_blocking, command, request, timeout)

    def pushing_recently(self, window: float = 15.0) -> bool:
        """True if a status broadcast arrived in the last ``window`` seconds."""
        return time.time() - self.last_push_time < window

    async def reset_connection(self) -> None:
        """Throw away the MQTT client and all request state; connect afresh.

        Equivalent to reloading the integration, which is what recovered a
        connection that kept receiving broadcasts but no replies.
        """
        if self._reset_lock is None:
            self._reset_lock = asyncio.Lock()
        async with self._reset_lock:
            if time.monotonic() - self.last_reset_time < 10:
                return  # another caller just did it
            await self.disconnect()
            for event, _holder in list(self._pending_responses.values()):
                event.set()  # release anyone still waiting on the old client
            self._pending_responses.clear()
            self._pending_by_topic.clear()
            self._pending_subacks.clear()
            self._sub_topics.clear()
            self._subscribed.clear()
            self._command_locks.clear()
            await self.connect()
            self.last_reset_time = time.monotonic()

    def probably_asleep(self) -> bool:
        """True if the robot is likely in deep sleep (ignoring commands)."""
        if self.state.sub_status.startswith("IDLE_STATUS_LOW_POWER"):
            return True
        return time.time() - self.last_message_time > ASLEEP_AFTER_SECONDS

    async def wake(self) -> bool:
        """Wake the robot the way the Narwal app does; True once it answers.

        Sends the app's "entered device page" event, then active_robot_publish
        as a request, waiting for the reply with growing timeouts
        (WAKE_TIMEOUTS, ~76 s worst case). Concurrent callers share one wake-up.
        """
        if self._wake_lock is None:
            self._wake_lock = asyncio.Lock()
        async with self._wake_lock:
            if time.monotonic() - self.last_wake_time < 5:
                return True  # another caller just woke it
            await self._publish_no_response(
                TOPIC_CMD_NOTIFY_APP_EVENT,
                encode("NotifyAppEvent_Request", {"appEvent": "APP_EVENT_ENTER_DEVICE_PAGE"}),
            )
            started = time.monotonic()
            for attempt, attempt_timeout in enumerate(WAKE_TIMEOUTS, start=1):
                try:
                    await self.send_command(
                        TOPIC_CMD_ACTIVE_ROBOT, self._active_request(),
                        timeout=attempt_timeout, check=False, retry=False, wake=False,
                    )
                except NarwalCommandError as err:
                    if not err.timeout:
                        raise
                    continue
                self.last_wake_time = time.monotonic()
                self.needs_reconnect = False
                _LOGGER.info(
                    "Robot answered after %d wake attempt(s) (%.1f s)",
                    attempt, self.last_wake_time - started,
                )
                return True
            _LOGGER.warning("Robot did not wake up after %.0f s", time.monotonic() - started)
            # Either it's offline or our connection silently stopped routing;
            # let the coordinator refresh the token and reconnect.
            self.needs_reconnect = True
            return False

    def _ensure_subscribed(self, topic: str) -> None:
        with self._sub_lock:
            if topic in self._subscribed:
                return
            evt = threading.Event()
            rc, mid = self._client.subscribe(topic, qos=1)
            if rc != 0:
                _LOGGER.warning("SUBSCRIBE %s not sent (rc=%s)", topic, rc)
                return
            self._sub_topics[mid] = topic
            self._subscribed.add(topic)
            self._pending_subacks[mid] = evt
            if not evt.wait(timeout=3.0):
                self._subscribed.discard(topic)
                _LOGGER.warning("No SUBACK for %s within 3 s", topic)

    def _send_blocking(self, command: str, request: bytes, timeout: float) -> bytes:
        lock = self._command_locks.setdefault(command, threading.Lock())
        with lock:
            topic = f"{self.base_topic}/{command}"
            response_topic = f"{topic}/response"
            self._ensure_subscribed(response_topic)
            event = threading.Event()
            holder: list[bytes | None] = [None]
            props = self._build_publish_properties(topic)
            corr = bytes(props.CorrelationData)
            self._pending_responses[corr] = (event, holder)
            self._pending_by_topic[response_topic] = corr
            self._client.publish(topic, self._auth_frame() + request, qos=1, properties=props)
            if not event.wait(timeout=timeout):
                self._pending_responses.pop(corr, None)
                if self._pending_by_topic.get(response_topic) == corr:
                    del self._pending_by_topic[response_topic]
                raise NarwalCommandError(f"{command} timed out", timeout=True)
            if holder[0] is None:  # released by reset_connection(), no reply
                raise NarwalCommandError(f"{command} interrupted by a reconnect", timeout=True)
            return holder[0]

    async def _publish_no_response(self, command: str, request: bytes) -> None:
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")
        topic = f"{self.base_topic}/{command}"
        self._client.publish(
            topic, self._auth_frame() + request, qos=1,
            properties=self._build_publish_properties(topic),
        )

    @staticmethod
    def _active_request() -> bytes:
        return encode("ActiveRobotPublish_Request", {
            "topicsDuration": {
                "workStatusDuration": 2000,
                "displayMapDuration": 2000,
                "robotBaseStatusDuration": 2000,
            },
            "publishDuration": 60000,
        })

    async def notify_active(self) -> None:
        """Keepalive: re-register as an app client (no reply awaited).

        Keeps an awake robot pushing status every ~2 s. It does NOT wake a
        sleeping robot; use wake() for that.
        """
        try:
            await self._publish_no_response(TOPIC_CMD_ACTIVE_ROBOT, self._active_request())
        except Exception:  # noqa: BLE001 - best effort keepalive
            _LOGGER.debug("active_robot_publish failed", exc_info=True)

    # ---- status / info -------------------------------------------------

    async def request_status_update(self) -> None:
        resp = await self.send_command(TOPIC_CMD_GET_BASE_STATUS, retry=False, wake=False)
        base = resp.message.get("robotBaseStatus")
        if isinstance(base, dict):
            self.state.update_base_status(base)
            self.state.device_reachable = True
            self._notify_state_update()

    async def fetch_config(self) -> dict[str, Any]:
        resp = await self.send_command(TOPIC_CMD_GET_CONFIG, retry=False, wake=False)
        config = resp.message.get("config")
        if isinstance(config, dict):
            self.state.config = config
            self._notify_state_update()
        return self.state.config

    async def fetch_consumables(self) -> dict[str, list[str]]:
        resp = await self.send_command(TOPIC_CMD_GET_CONSUMABLE, check=False, retry=False, wake=False)
        info = resp.message.get("consumableInfo") or {}
        self.state.consumables_due = {
            "maintain": [i for i in info.get("maintainItems", []) if isinstance(i, str)],
            "replace": [i for i in info.get("replaceItems", []) if isinstance(i, str)],
        }
        self._notify_state_update()
        return self.state.consumables_due

    async def fetch_device_info(self) -> None:
        """Feature flags and firmware version (static; fetch once)."""
        try:
            resp = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST, check=False, retry=False, wake=False)
            self.state.features = {k: v for k, v in resp.message.items() if not k.startswith("#")}
        except NarwalCommandError:
            _LOGGER.debug("get_feature_list failed")
        try:
            resp = await self.send_command(TOPIC_CMD_GET_FIRMWARE_VERSION, check=False, retry=False, wake=False)
            text = resp.message.get("firmwareVersionText")
            if not text:
                resp = await self.send_command(TOPIC_CMD_GET_DEVICE_INFO, check=False, response_type="", retry=False, wake=False)
                text = _device_info_version(resp.raw)
            self.state.firmware_version = (text or "").strip()
        except NarwalCommandError:
            _LOGGER.debug("firmware version query failed")

    async def get_map(self, wake: bool = False) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_MAP, timeout=20.0, retry=wake, wake=wake)

    async def fetch_map(self) -> MapSnapshot | None:
        """Fetch the static map; updates rooms and map geometry in state."""
        resp = await self.get_map()
        static = resp.message.get("map")
        if not isinstance(static, dict):
            return None
        self.state.update_rooms_from_map(static)
        border = static.get("border") or {}
        grid = static.get("mapData") or b""
        if not grid or not static.get("width"):
            return self.state.map
        snapshot = MapSnapshot(
            map_id=int(static.get("mapId", 0) or 0),
            width=int(static["width"]),
            height=int(static.get("height", 0)),
            resolution_mm=int(static.get("resolution", 60) or 60),
            left=int(border.get("left", 0)),
            bottom=int(border.get("bottom", 0)),
            grid=grid,
            rotate_angle=int(static.get("rotateAngle", 0) or 0),
            station=Pose.from_msg(static.get("station")),
            robot=Pose.from_msg(static.get("robotPose")),
        )
        self.state.map = snapshot
        if snapshot.robot and not self.state.robot_pose:
            self.state.robot_pose = snapshot.robot
        self._notify_state_update()
        return snapshot

    async def fetch_rooms(self) -> None:
        try:
            await self.fetch_map()
        except NarwalCommandError:
            _LOGGER.warning("Failed to fetch rooms from map")

    # ---- control -------------------------------------------------------

    async def locate(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_YELL)

    async def pause(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RESUME)

    async def stop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_FORCE_END)

    async def return_to_base(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RECALL)

    async def wash_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_WASH_MOP)

    async def dry_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_DRY_MOP)

    async def wash_and_dry_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_WASH_AND_DRY_MOP)

    async def empty_dustbin(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_DUST_GATHERING)

    async def set_fan_speed(self, level: FanLevel) -> CommandResponse | None:
        """Change suction. The robot only accepts this during a clean, so
        while docked the choice is remembered and sent with the next clean."""
        self.state.preferred_fan_level = level
        resp = None
        if self.state.is_cleaning:
            request = encode("SetFanLevel_Request", {
                "level": level.value, "operationType": "OPERATION_TYPE_SET_VALUE",
            })
            resp = await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, request)
            self.state.base["fanLevel"] = {v: k for k, v in _FAN_LABELS.items()}[level]
        self._notify_state_update()
        return resp

    async def set_mop_humidity(self, level: MopHumidity) -> CommandResponse | None:
        """Change mop wetness; same docked/cleaning behaviour as suction."""
        self.state.preferred_mop_humidity = level
        resp = None
        if self.state.is_cleaning:
            request = encode("SetMopHumidity_Request", {
                "mopHumidity": level.value, "operationType": "OPERATION_TYPE_SET_VALUE",
            })
            resp = await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, request)
            self.state.base["mopHumidity"] = f"MOP_HUMIDITY_{level.name}"
        self._notify_state_update()
        return resp

    async def set_config(self, **values: Any) -> CommandResponse:
        """Change robot settings, e.g. ``set_config(childLockEnabled=True)``.

        Field names are SetConfig_Request's; on success the cached GetConfig
        values are refreshed from the robot.
        """
        request = encode("SetConfig_Request", values)
        resp = await self.send_command(TOPIC_CMD_SET_CONFIG, request)
        try:
            await self.fetch_config()
        except NarwalCommandError:
            self.state.config.update(values)
            self._notify_state_update()
        return resp

    async def reset_consumable(self, replace_item: str) -> CommandResponse:
        request = encode("ResetConsumableInfo_Request", {
            "consumableInfo": {"replaceItems": [replace_item]},
        })
        resp = await self.send_command(
            TOPIC_CMD_RESET_CONSUMABLE, request,
            response_type="ResetConsumableInfo_Response", check=False,
        )
        await self.fetch_consumables()
        return resp

    def build_clean_request(
        self,
        room_ids: list[int],
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> bytes:
        """Encode a StartClean_Request: one CleanItem per room, in order."""
        fan = fan_level or self.state.fan_level or FanLevel.NORMAL
        mop = mop_humidity or self.state.mop_humidity or MopHumidity.NORMAL
        vacuum = mode in (CleanMode.VACUUM_ONLY, CleanMode.VACUUM_AND_MOP, CleanMode.VACUUM_THEN_MOP)
        mopping = mode in (CleanMode.MOP_ONLY, CleanMode.VACUUM_AND_MOP, CleanMode.VACUUM_THEN_MOP)
        # Passes go in the field matching the mode (CleanTask.CleanParam).
        times: dict[str, int] = {
            CleanMode.VACUUM_ONLY: {"sweepTime": passes},
            CleanMode.MOP_ONLY: {"mopTime": passes},
            CleanMode.VACUUM_AND_MOP: {"sweepMopSyncTime": passes},
            CleanMode.VACUUM_THEN_MOP: {"sweepTime": passes, "mopTime": passes},
        }[mode]
        items = []
        for order, room_id in enumerate(room_ids, start=1):
            items.append({
                "zone": {"zoneType": "ZONE_TYPE_ROOM", "zoneId": room_id},
                "param": {
                    "mode": CLEAN_MODE_LABEL[mode],
                    "fanLevel": fan.value if vacuum else None,
                    "mopHumidity": mop.value if mopping else None,
                    **times,
                },
                "order": order,
            })
        task: dict[str, Any] = {"items": items, "taskType": CLEAN_MODE_TASK_TYPE[mode]}
        # The robot rejects a clean task without a map id (seen live). Prefer
        # the loaded map's id, else the one the robot reports in its status.
        map_id = self.state.current_map_id
        if map_id:
            task["mapId"] = map_id
        return encode("StartClean_Request", {"task": task})

    async def start_clean(
        self,
        room_ids: list[int] | None = None,
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> CommandResponse:
        """Start cleaning the given rooms (all known rooms if none given)."""
        if self.probably_asleep():
            await self.wake()
        if self.state.map is None or not self.state.rooms:
            # Map not loaded yet (e.g. the robot was asleep at startup): load it
            # now that the robot is awake, for room ids and the map id.
            await self.fetch_rooms()
        if not room_ids:
            room_ids = [
                r.room_id for r in self.state.rooms
                if r.room_id not in self.state.no_go_room_ids
            ]
        if not room_ids:
            raise NarwalCommandError("Cannot start: no rooms known yet (map not loaded)")
        request = self.build_clean_request(room_ids, mode, fan_level, mop_humidity, passes)
        return await self.send_command(TOPIC_CMD_START_CLEAN, request)

    # ------------------------------------------------------------------
    # Teardown / discovery
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Disconnect without flipping availability (planned reconnect/unload)."""
        client = self._client
        self._client = None
        self._connected.clear()
        if client:
            client.on_connect = None
            client.on_message = None
            client.on_disconnect = None
            client.on_subscribe = None
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._stop_mqtt_client, client), timeout=5.0,
                )
            except TimeoutError:
                _LOGGER.warning("MQTT disconnect timed out — forcing cleanup")
                try:
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _stop_mqtt_client(client: mqtt.Client) -> None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        client.loop_stop()

    @staticmethod
    def discover_devices_via_mqtt(
        product_key: str,
        user_uuid: str,
        mqtt_password: str,
        broker: str,
        port: int = MQTT_PORT,
        timeout: float = 10.0,
    ) -> list[str]:
        """Listen on /{product_key}/+/# and return device names seen (blocking)."""
        discovered: list[str] = []
        found_event = threading.Event()
        client = mqtt.Client(
            client_id=f"app_{user_uuid}_{uuid.uuid4()}",
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.username_pw_set(user_uuid, mqtt_password)
        client.tls_set_context(ssl.create_default_context())

        def on_connect(cl, ud, flags, rc, props=None):
            if str(rc) == "Success" or rc == 0:
                cl.subscribe(f"/{product_key}/+/#", qos=1)

        def on_message(cl, ud, msg):
            parts = msg.topic.split("/")
            if len(parts) >= 3 and parts[1] == product_key and parts[2] not in discovered:
                discovered.append(parts[2])
                found_event.set()

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(broker, port, keepalive=30)
            client.loop_start()
            found_event.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("MQTT discovery failed", exc_info=True)
        finally:
            client.loop_stop()
            client.disconnect()
        return discovered


_FAN_LABELS = {
    "FAN_LEVEL_MUTE": FanLevel.QUIET,
    "FAN_LEVEL_NORMAL": FanLevel.NORMAL,
    "FAN_LEVEL_STRONG": FanLevel.STRONG,
    "FAN_LEVEL_DEEP": FanLevel.DEEP,
    "FAN_LEVEL_SUPER": FanLevel.MAX,
}


def _device_info_version(body: bytes) -> str:
    """common/get_device_info reply: field 3 holds the firmware text."""
    from .proto import iter_fields

    try:
        for num, wire, value in iter_fields(body):
            if num == 3 and wire == 2:
                return value.decode(errors="replace")
    except ProtoError:
        pass
    return ""
