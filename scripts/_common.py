"""Shared helpers for Narwal test scripts.

All credentials are read from environment variables:
  NARWAL_EMAIL      — Narwal account email
  NARWAL_PASSWORD   — Narwal account password
  NARWAL_REGION     — API region (us, il, eu, cn). Default: il
  NARWAL_DEVICE_ID  — Device name / ID (hex string from the MQTT topic)
  NARWAL_PRODUCT_KEY — Product key (found in MQTT topic path)
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import time
import uuid as uuid_mod
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes

# ---------------------------------------------------------------------------
# Credentials from environment
# ---------------------------------------------------------------------------

API_REGIONS = {
    "us": "us-app.narwaltech.com",
    "il": "il-app.narwaltech.com",
    "eu": "eu-app.narwaltech.com",
    "cn": "cn-app.narwaltech.com",
}

MQTT_REGIONS = {
    "us": "us-01.mqtt.narwaltech.com",
    "il": "us-01.mqtt.narwaltech.com",
    "eu": "eu-01.mqtt.narwaltech.com",
    "cn": "cn-mqtt.narwaltech.com",
}


def _require_env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default or "")
    if not val:
        print(f"ERROR: environment variable {name} is required.", file=sys.stderr)
        sys.exit(1)
    return val


def get_config() -> dict:
    region = os.environ.get("NARWAL_REGION", "il")
    return {
        "email": _require_env("NARWAL_EMAIL"),
        "password": _require_env("NARWAL_PASSWORD"),
        "region": region,
        "device_id": _require_env("NARWAL_DEVICE_ID"),
        "product_key": _require_env("NARWAL_PRODUCT_KEY"),
        "api_host": API_REGIONS.get(region, API_REGIONS["us"]),
        "mqtt_host": MQTT_REGIONS.get(region, MQTT_REGIONS["us"]),
    }


# ---------------------------------------------------------------------------
# Cloud login
# ---------------------------------------------------------------------------

def cloud_login(cfg: dict) -> tuple[str, str]:
    """Login to Narwal cloud. Returns (user_uuid, token)."""
    url = f"https://{cfg['api_host']}/user-authentication-server/v2/login/loginByEmail"
    req = Request(
        url,
        data=json.dumps({"email": cfg["email"], "password": cfg["password"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = json.loads(urlopen(req, context=ssl.create_default_context(), timeout=15).read())
    if resp.get("code") != 0:
        print(f"Login failed: {resp.get('msg', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    result = resp["result"]
    return result["uuid"], result["token"]


# ---------------------------------------------------------------------------
# Protobuf helpers
# ---------------------------------------------------------------------------

def encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append(value & 0x7F | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def pb_string(field: int, value: bytes | str) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    return bytes([(field << 3) | 2]) + encode_varint(len(encoded)) + encoded


def pb_varint(field: int, value: int) -> bytes:
    return bytes([(field << 3) | 0]) + encode_varint(value)


def narwal_frame(inner: bytes) -> bytes:
    return b"\x01" + encode_varint(len(inner)) + inner


def auth_payload(user_uuid: str) -> bytes:
    inner = pb_string(1, user_uuid) + pb_string(2, user_uuid)
    return narwal_frame(inner)


# ---------------------------------------------------------------------------
# Protobuf parser
# ---------------------------------------------------------------------------

def parse_protobuf_fields(data: bytes) -> dict[int, list]:
    """Parse top-level protobuf fields. Returns {field_num: [values]}."""
    fields: dict[int, list] = {}
    i = 0
    while i < len(data):
        tag = data[i]; i += 1
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            val = 0; shift = 0
            while i < len(data):
                b = data[i]; i += 1
                val |= (b & 0x7F) << shift; shift += 7
                if not (b & 0x80):
                    break
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 2:  # length-delimited
            length = 0; shift = 0
            while i < len(data):
                b = data[i]; i += 1
                length |= (b & 0x7F) << shift; shift += 7
                if not (b & 0x80):
                    break
            fields.setdefault(field_num, []).append(data[i:i + length])
            i += length
        elif wire_type == 5:  # 32-bit
            fields.setdefault(field_num, []).append(data[i:i + 4])
            i += 4
        elif wire_type == 1:  # 64-bit
            fields.setdefault(field_num, []).append(data[i:i + 8])
            i += 8
        else:
            break
    return fields


# ---------------------------------------------------------------------------
# MQTT client wrapper
# ---------------------------------------------------------------------------

class NarwalMQTT:
    """Thin wrapper around paho MQTT for test scripts."""

    def __init__(self, cfg: dict, user_uuid: str, token: str):
        self.cfg = cfg
        self.user_uuid = user_uuid
        self.token = token
        self.base = f"/{cfg['product_key']}/{cfg['device_id']}"
        self._connected = threading.Event()
        self._response_event = threading.Event()
        self._response_payload: bytes | None = None
        self._message_handlers: list = []

        cid = f"app_{user_uuid}_{uuid_mod.uuid4()}"
        self.client = mqtt.Client(
            client_id=cid, protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self.client.username_pw_set(user_uuid, token)
        self.client.tls_set_context(ssl.create_default_context())
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def add_message_handler(self, handler):
        self._message_handlers.append(handler)

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if str(rc) == "Success" or rc == 0:
            self._connected.set()

    def _on_message(self, client, userdata, msg):
        for handler in self._message_handlers:
            handler(msg)
        if "/response" in msg.topic:
            self._response_payload = msg.payload
            self._response_event.set()

    def connect(self, timeout: float = 15.0):
        self.client.connect(self.cfg["mqtt_host"], 8883, keepalive=30)
        self.client.loop_start()
        if not self._connected.wait(timeout=timeout):
            print("MQTT connection timed out", file=sys.stderr)
            sys.exit(1)

    def subscribe(self, topic_suffix: str):
        self.client.subscribe(f"{self.base}/{topic_suffix}", qos=1)

    def subscribe_full(self, full_topic: str):
        self.client.subscribe(full_topic, qos=1)

    def make_props(self, topic_suffix: str) -> Properties:
        resp_topic = f"{self.base}/{topic_suffix}/response"
        corr = (
            pb_string(1, str(uuid_mod.uuid1()))
            + pb_varint(3, 0)
            + pb_varint(4, int(time.time() * 1000))
        )
        p = Properties(PacketTypes.PUBLISH)
        p.ResponseTopic = resp_topic
        p.CorrelationData = corr
        return p

    def send_command(
        self, topic_suffix: str, payload: bytes, wait: float = 15.0,
    ) -> bytes | None:
        """Publish a command and wait for its response."""
        resp_topic = f"{self.base}/{topic_suffix}/response"
        self.client.subscribe(resp_topic, qos=1)
        time.sleep(0.5)

        self._response_event.clear()
        self._response_payload = None

        props = self.make_props(topic_suffix)
        self.client.publish(
            f"{self.base}/{topic_suffix}", payload, qos=1, properties=props,
        )

        if self._response_event.wait(timeout=wait):
            return self._response_payload
        return None

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


def parse_command_result(payload: bytes | None) -> int | None:
    """Extract the result code (field 1) from a command response payload."""
    if not payload:
        return None
    if payload[0] == 0x01:
        idx = 1
        while idx < len(payload) and payload[idx] & 0x80:
            idx += 1
        idx += 1
        inner = payload[idx:]
    else:
        inner = payload

    fields = parse_protobuf_fields(inner)
    vals = fields.get(1, [])
    return vals[0] if vals and isinstance(vals[0], int) else None


RESULT_NAMES = {1: "SUCCESS", 2: "NOT_APPLICABLE", 3: "CONFLICT"}
