"""MQTT client for Narwal robot vacuum cloud communication."""

from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes

from .const import (
    COMMAND_RESPONSE_TIMEOUT,
    MQTT_BROKER,
    MQTT_PORT,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_START_PLAN,
    TOPIC_CMD_YELL,
    CleanMode,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, NarwalState

_LOGGER = logging.getLogger(__name__)


class NarwalConnectionError(Exception):
    """Raised when connection to the vacuum fails."""


class NarwalCommandError(Exception):
    """Raised when a command fails or times out."""


def _encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _make_protobuf_string(field_num: int, value: str | bytes) -> bytes:
    tag = (field_num << 3) | 2
    encoded = value.encode() if isinstance(value, str) else value
    return bytes([tag]) + _encode_varint(len(encoded)) + encoded


def _make_protobuf_varint(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return bytes([tag]) + _encode_varint(value)


class NarwalClient:
    """Async MQTT client for Narwal vacuum cloud communication."""

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
        self._mqtt_client_id = mqtt_client_id or f"app_{user_uuid}_{uuid.uuid4()}"

        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tls_insecure = False
        # Response matching: topic -> (Event, payload_holder)
        self._pending_responses: dict[str, tuple[threading.Event, list[bytes | None]]] = {}
        # SUBACK tracking: mid -> Event
        self._pending_subacks: dict[int, threading.Event] = {}
        # Per-command locks to prevent concurrent calls to the same command
        # from colliding in _pending_responses (same response topic key).
        self._command_locks: dict[str, threading.Lock] = {}

    @property
    def base_topic(self) -> str:
        return f"/{self.product_key}/{self.device_name}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _build_user_payload(self, extra_inner: bytes = b"") -> bytes:
        """Build the user auth protobuf wrapped in Narwal frame (0x01 + length + protobuf).

        extra_inner is appended INSIDE the 0x01 frame (after the user UUID fields)
        so the vacuum parses them as part of the same message.
        """
        inner = b""
        inner += _make_protobuf_string(1, self.user_uuid)
        inner += _make_protobuf_string(2, self.user_uuid)
        inner += extra_inner
        return b'\x01' + _encode_varint(len(inner)) + inner

    def _build_publish_properties(self, topic: str, request_id: str) -> Properties:
        """Build MQTT5 PUBLISH properties with correlation data and response topic."""
        response_topic = f"{topic}/response"
        timestamp_ms = int(time.time() * 1000)

        corr_data = b""
        corr_data += _make_protobuf_string(1, request_id)
        corr_data += _make_protobuf_varint(3, 0)
        corr_data += _make_protobuf_varint(4, timestamp_ms)

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = response_topic
        props.CorrelationData = corr_data

        return props

    def _extract_app_payload(self, raw_payload: bytes) -> bytes:
        """Extract the application payload, skipping the 0x01+length Narwal framing.

        The frame is: 0x01 + varint(inner_length) + inner_protobuf + extra_fields
        We return everything after the 0x01 byte as protobuf (the length prefix
        and inner message are part of a larger protobuf that may have trailing fields).
        """
        if len(raw_payload) < 2:
            return raw_payload

        # Skip 0x01 frame byte and the varint length, return ALL remaining bytes
        if raw_payload[0] == 0x01:
            idx = 1
            while idx < len(raw_payload) and raw_payload[idx] & 0x80:
                idx += 1
            idx += 1  # skip last byte of varint
            return raw_payload[idx:]

        return raw_payload

    async def connect(self) -> None:
        """Connect to MQTT broker.

        All blocking I/O runs in an executor to avoid blocking HA's event loop.
        Uses threading.Event instead of asyncio primitives to avoid
        cross-thread future resolution issues in HA.
        """
        self._loop = asyncio.get_running_loop()
        self._connected.clear()

        def _connect_blocking() -> None:
            self._setup_mqtt_client()
            if not self._connected.wait(timeout=15.0):
                if self._client:
                    self._client.loop_stop()
                raise NarwalConnectionError("MQTT connection timed out")

        await self._loop.run_in_executor(None, _connect_blocking)

        # Tell the vacuum an app client is active so it starts sending pushes
        await self.notify_active()

    def _setup_mqtt_client(self) -> None:
        """Create the paho-mqtt client, configure TLS, and initiate connection.

        Runs in an executor thread to avoid blocking the event loop.
        """
        _LOGGER.info(
            "Connecting to %s:%d as client_id=%s, base_topic=%s",
            self.broker,
            self.port,
            self._mqtt_client_id,
            self.base_topic,
        )

        self._client = mqtt.Client(
            client_id=self._mqtt_client_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.username_pw_set(self._mqtt_username, self._mqtt_password)
        _LOGGER.debug(
            "MQTT password (first 20 chars): %s...",
            self._mqtt_password[:20] if self._mqtt_password else "EMPTY",
        )

        ctx = ssl.create_default_context()
        self._client.tls_set_context(ctx)
        if self._tls_insecure:
            self._client.tls_insecure_set(True)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_log = self._on_log

        self._client.connect(self.broker, self.port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        _LOGGER.warning(
            "MQTT connected: %s | base_topic=%s | device_name=%s",
            reason_code, self.base_topic, self.device_name,
        )
        if str(reason_code) == "Success" or reason_code == 0:
            topic = f"{self.base_topic}/#"
            client.subscribe(topic, qos=1)
            _LOGGER.warning("Subscribed to %s", topic)
            self._connected.set()
        else:
            _LOGGER.error("MQTT connection REJECTED: %s", reason_code)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        _LOGGER.debug("MQTT SUBACK (mid=%s): %s", mid, reason_codes)
        evt = self._pending_subacks.pop(mid, None)
        if evt:
            evt.set()

    def _on_log(self, client, userdata, level, buf):
        """Forward paho-mqtt internal log messages."""
        _LOGGER.debug("PAHO: %s", buf)

    def _on_message(self, client, userdata, msg):
        """Handle all incoming messages: command responses and broadcasts."""
        topic_suffix = msg.topic.replace(self.base_topic, "").lstrip("/")
        _LOGGER.warning(
            "MQTT << %s (%d bytes) full_topic=%s pending=%s",
            topic_suffix, len(msg.payload), msg.topic,
            list(self._pending_responses.keys()),
        )

        # Check for pending command response
        if msg.topic in self._pending_responses:
            event, holder = self._pending_responses.pop(msg.topic)
            holder[0] = msg.payload
            event.set()
            return

        # Handle status broadcasts
        payload = self._extract_app_payload(msg.payload)

        if topic_suffix == "status/robot_base_status":
            self.state.update_base_status(payload)
            self._notify_state_update()
        elif topic_suffix == "status/working_status":
            self.state.update_working_status(payload)
            self._notify_state_update()

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        self._connected.clear()

    def _notify_state_update(self):
        """Schedule a state update callback on the event loop (if available)."""
        cb = self.on_state_update
        loop = self._loop
        if cb and loop:
            try:
                loop.call_soon_threadsafe(cb, self.state)
            except RuntimeError:
                pass

    async def send_command(
        self,
        command: str,
        extra_payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
        payload_override: bytes | None = None,
    ) -> CommandResponse:
        """Send a command and wait for response.

        The entire publish-and-wait runs in an executor thread using
        threading.Event + message_callback_add, avoiding cross-thread
        asyncio Future resolution which breaks in HA's event loop.
        """
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")

        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._send_command_blocking, command, extra_payload, timeout, payload_override
        )

    def _get_command_lock(self, command: str) -> threading.Lock:
        """Get or create a per-command lock to serialise concurrent calls."""
        if command not in self._command_locks:
            self._command_locks[command] = threading.Lock()
        return self._command_locks[command]

    def _send_command_blocking(
        self, command: str, extra_payload: bytes, timeout: float,
        payload_override: bytes | None = None,
    ) -> CommandResponse:
        """Publish a command and block until the response arrives (runs in executor).

        A per-command lock prevents concurrent calls to the same command from
        colliding -- they share the same response topic key in _pending_responses,
        so the second call would overwrite the first and both would timeout.
        """
        lock = self._get_command_lock(command)
        with lock:
            return self._send_command_locked(command, extra_payload, timeout, payload_override)

    def _send_command_locked(
        self, command: str, extra_payload: bytes, timeout: float,
        payload_override: bytes | None = None,
    ) -> CommandResponse:
        topic = f"{self.base_topic}/{command}"
        response_topic = f"{topic}/response"
        request_id = str(uuid.uuid1())

        response_event = threading.Event()
        response_holder: list[bytes | None] = [None]
        self._pending_responses[response_topic] = (response_event, response_holder)

        sub_event = threading.Event()
        sub_result = self._client.subscribe(response_topic, qos=1)
        sub_mid = sub_result[1]
        self._pending_subacks[sub_mid] = sub_event
        _LOGGER.debug("Subscribing to %s (mid=%s), waiting for SUBACK...", response_topic, sub_mid)

        if not sub_event.wait(timeout=5.0):
            _LOGGER.error("SUBACK timeout for %s (mid=%s)", response_topic, sub_mid)

        props = self._build_publish_properties(topic, request_id)
        if payload_override is not None:
            payload = payload_override
        else:
            payload = self._build_user_payload() + extra_payload
        result = self._client.publish(topic, payload, qos=1, properties=props)
        _LOGGER.warning(
            "Published >> %s | full_topic=%s | response_topic=%s | rc=%s mid=%s",
            command, topic, response_topic, result.rc, result.mid,
        )

        if not response_event.wait(timeout=timeout):
            self._pending_responses.pop(response_topic, None)
            _LOGGER.error("Command timeout: %s (%.0fs)", command, timeout)
            raise NarwalCommandError(f"Command {command} timed out")

        app_payload = self._extract_app_payload(response_holder[0])
        return CommandResponse.from_payload(app_payload)

    async def send_command_no_response(self, command: str, extra_payload: bytes = b"") -> None:
        """Send a command without waiting for a response."""
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")

        topic = f"{self.base_topic}/{command}"
        request_id = str(uuid.uuid1())
        props = self._build_publish_properties(topic, request_id)
        payload = self._build_user_payload() + extra_payload
        self._client.publish(topic, payload, qos=1, properties=props)

    # --- High-level commands ---

    async def notify_active(self) -> None:
        """Announce this client to the vacuum, triggering push status broadcasts."""
        try:
            await self.send_command_no_response(TOPIC_CMD_ACTIVE_ROBOT)
            _LOGGER.info("Sent active_robot notification")
        except Exception:
            _LOGGER.debug("active_robot notification failed", exc_info=True)

    async def locate(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_YELL)

    async def start(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_START_CLEAN)

    async def start_plan(
        self,
        mode: CleanMode | None = None,
        room_ids: list[int] | None = None,
    ) -> CommandResponse:
        """Start a cleaning plan.

        Args:
            mode: Cleaning mode (vacuum+mop, vacuum only, etc.).
                  None uses the vacuum's current default.
            room_ids: List of room IDs to clean.  None cleans all rooms.
        """
        extra_inner = b""
        if mode is not None:
            extra_inner += _make_protobuf_varint(3, mode.value)
        if room_ids:
            for rid in room_ids:
                extra_inner += _make_protobuf_varint(4, rid)

        if extra_inner:
            payload = self._build_user_payload(extra_inner)
            return await self.send_command(TOPIC_CMD_START_PLAN, payload_override=payload)
        return await self.send_command(TOPIC_CMD_START_PLAN)

    async def easy_clean(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_EASY_CLEAN)

    async def pause(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RESUME)

    async def stop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_FORCE_END)

    async def return_to_base(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RECALL)

    async def set_fan_speed(self, level: FanLevel) -> CommandResponse:
        extra = _make_protobuf_varint(1, level.value)
        return await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, extra)

    async def set_mop_humidity(self, level: MopHumidity) -> CommandResponse:
        extra = _make_protobuf_varint(1, level.value)
        return await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, extra)

    async def get_device_info(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_DEVICE_INFO)

    async def get_base_status(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_BASE_STATUS)

    async def get_config(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_CONFIG)

    async def get_consumable_info(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_CONSUMABLE)

    async def get_map(self) -> CommandResponse:
        """Fetch the current map from the vacuum (longer timeout for large data)."""
        return await self.send_command(TOPIC_CMD_GET_MAP, timeout=30.0)

    async def fetch_rooms(self) -> None:
        """Fetch the map and update the room list in self.state."""
        try:
            resp = await self.get_map()
            if resp.success and resp.data:
                self.state.update_rooms_from_map(resp.data)
                _LOGGER.info("Fetched %d rooms from map", len(self.state.rooms))
        except NarwalCommandError:
            _LOGGER.warning("Failed to fetch rooms from map")

    async def request_status_update(self) -> None:
        """Request a status update from the vacuum.

        The command also triggers the vacuum to send a push broadcast on
        status/robot_base_status, which _on_message handles separately.
        """
        try:
            resp = await self.send_command(TOPIC_CMD_GET_BASE_STATUS)
            _LOGGER.info(
                "Status response: success=%s, data=%d bytes, raw=%d bytes",
                resp.success,
                len(resp.data),
                len(resp.raw),
            )
            if resp.success and resp.data:
                self.state.update_base_status(resp.data)
                self._notify_state_update()
            elif resp.success and resp.raw:
                # Some firmware returns status fields inline in the response
                # rather than nested in a field-2 sub-message.
                self.state.update_base_status(resp.raw)
                self._notify_state_update()
                _LOGGER.info(
                    "Parsed inline status: battery=%.1f%%, status=%s",
                    self.state.battery_level,
                    self.state.working_status.name,
                )
        except NarwalCommandError:
            _LOGGER.warning("Status request timed out")
            raise

    async def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        client = self._client
        self._client = None
        self._connected.clear()
        if client:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._stop_mqtt_client, client)

    @staticmethod
    def _stop_mqtt_client(client: mqtt.Client) -> None:
        """Stop the paho-mqtt network loop and disconnect (blocking)."""
        client.loop_stop()
        client.disconnect()

    @staticmethod
    def discover_devices_via_mqtt(
        product_key: str,
        user_uuid: str,
        mqtt_password: str,
        broker: str,
        port: int = MQTT_PORT,
        timeout: float = 10.0,
    ) -> list[str]:
        """Connect to MQTT and discover device names by listening for broadcasts.

        Subscribes to /{product_key}/+/# and returns a list of unique
        device names seen in incoming message topics.  Blocking call.
        """
        discovered: list[str] = []
        found_event = threading.Event()

        client = mqtt.Client(
            client_id=f"app_{user_uuid}_{uuid.uuid4()}",
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.username_pw_set(user_uuid, mqtt_password)
        ctx = ssl.create_default_context()
        client.tls_set_context(ctx)

        def on_connect(cl, ud, flags, rc, props=None):
            if str(rc) == "Success" or rc == 0:
                cl.subscribe(f"/{product_key}/+/#", qos=1)

        def on_message(cl, ud, msg):
            parts = msg.topic.split("/")
            # Topic format: /{product_key}/{device_name}/...
            if len(parts) >= 3 and parts[1] == product_key:
                dev = parts[2]
                if dev and dev not in discovered:
                    discovered.append(dev)
                    _LOGGER.info("Discovered MQTT device: %s", dev)
                    found_event.set()

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(broker, port, keepalive=30)
            client.loop_start()
            found_event.wait(timeout=timeout)
        except Exception:
            _LOGGER.debug("MQTT discovery failed", exc_info=True)
        finally:
            client.loop_stop()
            client.disconnect()

        return discovered
