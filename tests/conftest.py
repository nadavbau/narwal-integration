"""Test fixtures: a fake Narwal robot behind the real client.

Only the transport is replaced: NarwalClient's encoding, decoding, state
model and every Home Assistant entity run for real. The fake robot answers
commands with protobuf messages built from the same schema the app uses.
"""

from __future__ import annotations

import time
import zlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.proto import encode, encode_varint

DEVICE = "abcdef1234567890abcdef1234567890"
PRODUCT = "EHf6cRNRGT"
USER = "00000000000000000000000000000000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def frame(message: bytes) -> bytes:
    """Wrap a response like the robot: 0x01 + varint(len) + auth + message."""
    auth = b"\x0a\x20" + USER.encode() + b"\x12\x20" + USER.encode()
    return b"\x01" + encode_varint(len(auth)) + auth + message


def _grid(width: int, height: int) -> bytes:
    """A tiny map: room 1 on the left half, room 2 on the right half."""
    pixels = []
    for y in range(height):
        for x in range(width):
            pixels.append(((1 if x < width // 2 else 2) << 8) | 0x01)
    packed = b"".join(encode_varint(p) for p in pixels)
    body = b"\x0a" + encode_varint(len(packed)) + packed
    return zlib.compress(body)


class FakeRobot:
    """Holds robot state and answers commands."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.base: dict[str, Any] = {
            "batteryPercentage": 87.0,
            "robotTaskStatus": {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_IN_STATION"},
            "stationContactType": "STATION_CONTACT_TYPE_CONTACTED",
            "fanLevel": "FAN_LEVEL_NORMAL",
            "mopHumidity": "MOP_HUMIDITY_NORMAL",
            "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_NORMAL",
            "sewageTankState": "SEWAGE_TANK_STATE_NORMAL",
            "dustBoxState": "DUST_BOX_STATE_NORMAL",
            "detergentState": "DETERGENT_STATE_INSTALLED_AND_EMPTY",
            "mapId": 1,
        }
        self.config: dict[str, Any] = {
            "volumePercentage": 16,
            "disturbMode": {"enabled": True, "beginTime": 21 * 3600, "endTime": 9 * 3600},
            "dustGatheringEnabled": True,
            "dryMopStrength": "DRY_MOP_STRENGTH_SMART",
            "cleanMopFreq": "CLEAN_MOP_FREQ_NORMAL",
            "carpetCleanOption": "CARPET_CLEAN_OPTION_IGNORE",
        }
        self.reject: set[str] = set()
        # Deep sleep: commands time out until this many active_robot_publish
        # requests have arrived (None = awake; float("inf") = never wakes).
        self.sleep_until_wake_requests: float | None = None
        self.wake_requests = 0
        # Stale connection: replies stop reaching us (pushes still do) until
        # the client reconnects.
        self.stale = False
        self.connects = 0

    def requests(self, command: str) -> list[bytes]:
        return [req for cmd, req in self.calls if cmd == command]

    @property
    def asleep(self) -> bool:
        return self.sleep_until_wake_requests is not None

    def answer(self, command: str, request: bytes) -> bytes:
        from custom_components.narwal.narwal_client.client import NarwalCommandError

        self.calls.append((command, request))
        if self.stale:
            raise NarwalCommandError(f"{command} timed out", timeout=True)
        if command == "common/active_robot_publish" and self.asleep:
            self.wake_requests += 1
            if self.wake_requests >= self.sleep_until_wake_requests:
                self.sleep_until_wake_requests = None  # woke up
            else:
                raise NarwalCommandError(f"{command} timed out", timeout=True)
        elif self.asleep:
            raise NarwalCommandError(f"{command} timed out", timeout=True)
        if command == "common/active_robot_publish":
            return frame(encode("ActiveRobotPublish_Response", {"publishDuration": 60000}))
        ok = {"result": "SERVICE_RESULT_PERFORM_SUCCESS"}
        if command in self.reject:
            return frame(encode("SetFanLevel_Response", {"result": "SERVICE_RESULT_PERFORM_GENERAL_FAILED"}))
        if command == "status/get_device_base_status":
            return frame(encode("GetDeviceBaseStatus_Response", {
                "result": "RESULT_TYPE_SUCCESS", "robotBaseStatus": self.base,
            }))
        if command == "config/get":
            return frame(encode("GetConfig_Response", {**ok, "config": self.config}))
        if command == "config/set":
            from custom_components.narwal.narwal_client.proto import decode

            self.config.update(decode("SetConfig_Request", request))
            return frame(encode("SetConfig_Response", ok))
        if command == "consumable/get_consumable_info":
            return frame(encode("GetConsumableInfo_Response", {
                "consumableInfo": {"replaceItems": ["CONSUMABLE_REPLACE_ITEM_DUST_BAG"]},
            }))
        if command == "common/get_feature_list":
            return frame(encode("GetFeatureList_Response", {"supportMultiMap": True}))
        if command == "common/upgrade/get_firmware_version":
            return frame(encode("GetFirmwareVersion_Response", {"firmwareVersionText": "v01.09.01.00"}))
        if command == "map/get_map":
            return frame(encode("GetMap_Response", {**ok, "map": {
                "mapId": 1, "resolution": 60, "width": 20, "height": 10,
                "border": {"left": -10, "bottom": -5, "right": 9, "top": 4},
                "station": {"location": {"x": -8.0, "y": 0.0}, "theta": 0.0},
                "robotPose": {"location": {"x": -8.0, "y": 0.0}, "theta": 0.0},
                "roomInfos": [
                    {"roomId": 1, "type": 3},
                    {"roomId": 2, "type": 4, "customName": "Galley"},
                    {"roomId": 3, "type": 6},
                ],
                "forbiddens": [{
                    "type": "FORBIDDEN_TYPE_SWEEP_MOP", "id": 1, "roomId": 3,
                    "mode": "FORBIDDEN_MODE_ROOM",
                }],
                "mapData": _grid(20, 10),
                "rotateAngle": 90,
            }}))
        return frame(encode("SetFanLevel_Response", ok))  # generic ServiceResult


@pytest.fixture
def robot() -> FakeRobot:
    return FakeRobot()


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DEVICE,
        data={
            "email": "user@example.com",
            "password": "secret",
            "region": "il",
            "product_key": PRODUCT,
            "device_name": DEVICE,
            "user_uuid": USER,
            "access_token": "not.a.jwt",
            "refresh_token": "refresh",
        },
    )


@pytest.fixture
def mock_robot(robot: FakeRobot):
    """Replace network I/O in NarwalClient/NarwalCloud with the fake robot."""

    async def fake_connect(self: NarwalClient) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()
        self._client = SimpleNamespace(
            subscribe=lambda *a, **k: (0, 1),
            publish=lambda *a, **k: None,
            disconnect=lambda *a, **k: None,
            loop_stop=lambda *a, **k: None,
        )
        self._connected.set()
        self.state.device_reachable = True
        robot.connects += 1
        robot.stale = False  # a fresh connection gets replies again

    def fake_send_blocking(self: NarwalClient, command: str, request: bytes, timeout: float) -> bytes:
        reply = robot.answer(command, request)
        self.last_message_time = time.time()  # like _on_message for a real reply
        return reply

    async def fake_publish(self: NarwalClient, command: str, request: bytes) -> None:
        robot.calls.append((command, request))

    async def fake_disconnect(self: NarwalClient) -> None:
        self._connected.clear()

    session = SimpleNamespace(access_token="new.token", refresh_token="new-refresh")
    with (
        patch.object(NarwalClient, "connect", fake_connect),
        patch.object(NarwalClient, "_send_blocking", fake_send_blocking),
        patch.object(NarwalClient, "_publish_no_response", fake_publish),
        patch.object(NarwalClient, "disconnect", fake_disconnect),
        patch("custom_components.narwal.coordinator.NarwalCloud.refresh_token", return_value=session),
        patch("custom_components.narwal._register_card"),
    ):
        yield robot


def push(hass, client: NarwalClient, topic: str, message: str, values: dict[str, Any]) -> None:
    """Deliver a robot push through the client's real MQTT message handler."""
    msg = SimpleNamespace(topic=f"{client.base_topic}/{topic}", payload=frame(encode(message, values)))
    client._on_message(None, None, msg)
