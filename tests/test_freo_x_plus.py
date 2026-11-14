"""Protocol-level tests for the Freo X Plus differences (and that the Freo X
Ultra keeps its behaviour). No Home Assistant instance needed."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.const import (
    PRODUCT_KEY_FREO_X_PLUS,
    PRODUCT_KEY_FREO_X_ULTRA,
    CleanMode,
    FanLevel,
)
from custom_components.narwal.narwal_client.models import ACTIVITY_ERROR, ACTIVITY_PAUSED
from custom_components.narwal.narwal_client.proto import decode, iter_fields

DEVICE = "abcdef1234567890abcdef1234567890"
FAULT = {"identityCode": 0x02020042, "level": 1, "debugDetail": "...产生错误的原因:right mop uninstall"}
PAUSED_SWEEP = {"task": "TASK_TYPE_SWEEP", "pauseStatus": True, "sweepStatus": "SWEEP_STATUS_ROOM_CLEAN"}


def _client(product_key: str) -> NarwalClient:
    return NarwalClient(product_key, DEVICE, "user-uuid", "user-uuid", "token")


def _frame_fields(payload: bytes) -> dict[int, bytes]:
    length = shift = 0
    pos = 1
    while True:
        byte = payload[pos]
        pos += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return {num: value for num, _, value in iter_fields(payload[pos:pos + length])}


def test_publish_qos0_with_reply_routing_in_frame() -> None:
    client = _client(PRODUCT_KEY_FREO_X_ULTRA)
    client._client = MagicMock()
    client._ensure_subscribed = MagicMock()
    client._client.publish.side_effect = lambda *a, **k: client._pending_responses.popitem()[1][0].set()
    client._send_blocking("status/get_device_base_status", b"", 1.0)

    args, kwargs = client._client.publish.call_args
    assert kwargs["qos"] == 0
    topic = f"/{PRODUCT_KEY_FREO_X_ULTRA}/{DEVICE}/status/get_device_base_status"
    routing = {n: v for n, _, v in iter_fields(_frame_fields(args[1])[5])}
    assert routing[1] == f"{topic}/response".encode()
    assert routing[2] == kwargs["properties"].CorrelationData[2:2 + len(routing[2])]


def test_max_suction_is_deep_on_x_plus_only() -> None:
    for key, expected in ((PRODUCT_KEY_FREO_X_PLUS, "FAN_LEVEL_DEEP"), (PRODUCT_KEY_FREO_X_ULTRA, "FAN_LEVEL_SUPER")):
        request = _client(key).build_clean_request([3], CleanMode.VACUUM_ONLY, FanLevel.MAX)
        assert decode("StartClean_Request", request)["task"]["items"][0]["param"]["fanLevel"] == expected


def test_x_plus_holds_fault_while_paused() -> None:
    state = _client(PRODUCT_KEY_FREO_X_PLUS).state
    state.update_base_status({"errorCode": [FAULT], "robotTaskStatus": PAUSED_SWEEP})
    state.update_base_status({"errorCode": [{}], "robotTaskStatus": PAUSED_SWEEP})
    assert state.error_codes == [0x02020042]
    assert state.error_reasons == ["right mop uninstall"]
    assert state.activity == ACTIVITY_ERROR
    state.update_base_status({"robotTaskStatus": {"task": "TASK_TYPE_SWEEP"}})
    assert state.error_codes == []


def test_ultra_fault_follows_the_robot() -> None:
    state = _client(PRODUCT_KEY_FREO_X_ULTRA).state
    state.update_base_status({"errorCode": [FAULT], "robotTaskStatus": PAUSED_SWEEP})
    assert state.activity == ACTIVITY_PAUSED  # unchanged upstream behaviour
    state.update_base_status({"robotTaskStatus": PAUSED_SWEEP})
    assert state.error_codes == []


def test_x_plus_has_no_station_entities() -> None:
    from custom_components.narwal.entity import entity_supported

    def coordinator(key: str) -> MagicMock:
        return MagicMock(client=_client(key))

    for key in ("auto_empty", "wash_mop", "clean_water_tank", "detergent", "mop_wash_frequency"):
        assert not entity_supported(coordinator(PRODUCT_KEY_FREO_X_PLUS), key)
        assert entity_supported(coordinator(PRODUCT_KEY_FREO_X_ULTRA), key)
    assert entity_supported(coordinator(PRODUCT_KEY_FREO_X_PLUS), "dust_bin")  # the robot's own bin
