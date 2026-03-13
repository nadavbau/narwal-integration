"""MQTT client for Narwal robot vacuum cloud communication."""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
import uuid
from collections.abc import Callable
from typing import Any

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
    TOPIC_ROBOT_BASE_STATUS,
    TOPIC_WORKING_STATUS,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, NarwalState, parse_protobuf_fields

_LOGGER = logging.getLogger(__name__)
_PAHO_LOGGER = logging.getLogger(f"{__name__}.paho")


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
        self._connected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_responses: dict[str, asyncio.Future[bytes]] = {}
        self._tls_insecure = False

    @property
    def base_topic(self) -> str:
        return f"/{self.product_key}/{self.device_name}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _build_user_payload(self) -> bytes:
        """Build the user auth protobuf wrapped in Narwal frame (0x01 + length + protobuf)."""
        inner = b""
        inner += _make_protobuf_string(1, self.user_uuid)
        inner += _make_protobuf_string(2, self.user_uuid)
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

        All blocking I/O (SSL context, TCP connect) runs in an executor
        so we don't block the HA event loop.
        """
        self._loop = asyncio.get_running_loop()
        self._connected.clear()

        await self._loop.run_in_executor(None, self._setup_mqtt_client)

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            if self._client:
                self._client.loop_stop()
            raise NarwalConnectionError("MQTT connection timed out")

        # Tell the vacuum an app client is active so it starts sending pushes
        await self.notify_active()

    def _setup_mqtt_client(self) -> None:
        """Create the paho-mqtt client, configure TLS, and initiate connection.

        Runs in an executor thread to avoid blocking the event loop.
        """
        _LOGGER.warning(
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
        self._client.enable_logger(_PAHO_LOGGER)
        self._client.username_pw_set(self._mqtt_username, self._mqtt_password)

        ctx = ssl.create_default_context()
        self._client.tls_set_context(ctx)
        if self._tls_insecure:
            self._client.tls_insecure_set(True)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe

        self._client.connect(self.broker, self.port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        _LOGGER.warning("MQTT connected: %s", reason_code)
        if str(reason_code) == "Success" or reason_code == 0:
            topic = f"{self.base_topic}/#"
            client.subscribe(topic, qos=1)
            _LOGGER.warning("Subscribing to %s", topic)
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        else:
            _LOGGER.error("MQTT connection failed: %s", reason_code)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        _LOGGER.warning("MQTT subscription result (mid=%s): %s", mid, reason_codes)

    def _on_message(self, client, userdata, msg):
        topic_suffix = msg.topic.replace(self.base_topic, "").lstrip("/")
        _LOGGER.warning(
            "MQTT << %s [full: %s] (%d bytes) pending=%s",
            topic_suffix,
            msg.topic,
            len(msg.payload),
            list(self._pending_responses.keys()),
        )

        # Check if this is a response to a pending command
        if msg.topic in self._pending_responses:
            fut = self._pending_responses.pop(msg.topic)
            if self._loop and not fut.done():
                self._loop.call_soon_threadsafe(fut.set_result, msg.payload)
            return

        # Handle status broadcasts
        payload = self._extract_app_payload(msg.payload)

        if topic_suffix == "status/robot_base_status":
            _LOGGER.debug("Base status broadcast received (%d bytes)", len(payload))
            self.state.update_base_status(payload)
            _LOGGER.debug(
                "State after base status: battery=%.1f%%, status=%s",
                self.state.battery_level,
                self.state.working_status.name,
            )
            self._notify_state_update()
        elif topic_suffix == "status/working_status":
            _LOGGER.debug("Working status broadcast received (%d bytes)", len(payload))
            self.state.update_working_status(payload)
            self._notify_state_update()

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected.clear)

    def _notify_state_update(self):
        if self.on_state_update and self._loop:
            self._loop.call_soon_threadsafe(self.on_state_update, self.state)

    async def send_command(
        self,
        command: str,
        extra_payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
    ) -> CommandResponse:
        """Send a command and wait for response."""
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")

        topic = f"{self.base_topic}/{command}"
        response_topic = f"{topic}/response"
        request_id = str(uuid.uuid1())

        # Subscribe to response topic
        self._client.subscribe(response_topic, qos=1)

        # Create future for response
        fut: asyncio.Future[bytes] = self._loop.create_future()
        self._pending_responses[response_topic] = fut

        # Build and send
        props = self._build_publish_properties(topic, request_id)
        payload = self._build_user_payload() + extra_payload

        result = self._client.publish(topic, payload, qos=1, properties=props)
        _LOGGER.warning(
            "Published >> %s (%d bytes) rc=%s mid=%s, waiting on %s",
            topic,
            len(payload),
            result.rc,
            result.mid,
            response_topic,
        )

        try:
            response_data = await asyncio.wait_for(fut, timeout=timeout)
            app_payload = self._extract_app_payload(response_data)
            return CommandResponse.from_payload(app_payload)
        except asyncio.TimeoutError:
            self._pending_responses.pop(response_topic, None)
            _LOGGER.warning("Command timeout: %s", command)
            raise NarwalCommandError(f"Command {command} timed out")

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

    async def start_plan(self) -> CommandResponse:
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
