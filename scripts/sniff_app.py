#!/usr/bin/env python3
"""Subscribe to all known MQTT topics and log traffic from the Narwal app.

Usage:
  export NARWAL_EMAIL=... NARWAL_PASSWORD=... NARWAL_DEVICE_ID=... NARWAL_REGION=il
  python3 sniff_app.py [--duration SECONDS]

The script listens for MQTT messages from the vacuum and the official app,
printing topic, size, raw hex, and the payload decoded with the
integration's schema (field and enum names from the official app).
Credentials come from scripts/.env or the environment.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from _common import NarwalMQTT, cloud_login, get_config, load_narwal_client

lib = load_narwal_client()
decode = lib.proto.decode
strip_frame = lib.models.strip_narwal_frame

# What each topic carries (requests are what the app sends).
REQUEST_TYPES = {
    "clean/start_clean": "StartClean_Request",
    "clean/set_fan_level": "SetFanLevel_Request",
    "clean/set_mop_humidity": "SetMopHumidity_Request",
    "config/set": "SetConfig_Request",
    "consumable/reset_consumable_info": "ResetConsumableInfo_Request",
    "common/active_robot_publish": "ActiveRobotPublish_Request",
    "map/get_map": "GetMap_Request",
}
RESPONSE_TYPES = lib.client.RESPONSE_TYPES
PUSH_TYPES = lib.client._PUSH_TYPES

# All known command and status topics to subscribe to
TOPICS = [
    "common/yell", "common/yell/response",
    "common/active_robot_publish", "common/active_robot_publish/response",
    "common/get_device_info", "common/get_device_info/response",
    "status/get_device_base_status", "status/get_device_base_status/response",
    "status/robot_base_status",
    "status/working_status",
    "status/upgrade_status",
    "map/display_map",
    "clean/start_clean", "clean/start_clean/response",
    "clean/plan/start", "clean/plan/start/response",
    "clean/easy_clean/start", "clean/easy_clean/start/response",
    "clean/set_fan_level", "clean/set_fan_level/response",
    "clean/set_mop_humidity", "clean/set_mop_humidity/response",
    "clean/current_clean_task/get", "clean/current_clean_task/get/response",
    "task/pause", "task/pause/response",
    "task/resume", "task/resume/response",
    "task/force_end", "task/force_end/response",
    "task/cancel", "task/cancel/response",
    "supply/recall", "supply/recall/response",
    "supply/wash_mop", "supply/wash_mop/response",
    "supply/dry_mop", "supply/dry_mop/response",
    "supply/dust_gathering", "supply/dust_gathering/response",
    "supply/wash_and_dry_mop", "supply/wash_and_dry_mop/response",
    "config/get", "config/get/response",
    "config/set", "config/set/response",
    "config/volume/set", "config/volume/set/response",
    "consumable/get_consumable_info", "consumable/get_consumable_info/response",
    "map/get_map", "map/get_map/response",
    "map/get_all_reduced_maps", "map/get_all_reduced_maps/response",
    "schedule/clean_schedule/get", "schedule/clean_schedule/get/response",
]

# Topics with high-frequency updates that we'll batch
STATUS_TOPICS = {"status/robot_base_status", "map/display_map"}

status_count = 0
last_status_flush = 0.0


def message_type(topic: str) -> str | None:
    if topic.endswith("/response"):
        return RESPONSE_TYPES.get(topic[: -len("/response")], "SetFanLevel_Response")
    return PUSH_TYPES.get(topic) or REQUEST_TYPES.get(topic)


def print_decoded(topic: str, payload: bytes) -> None:
    msg_type = message_type(topic)
    if not msg_type:
        return
    try:
        decoded = decode(msg_type, strip_frame(payload))
    except Exception as err:  # noqa: BLE001 - diagnostic tool
        print(f"       (decode as {msg_type} failed: {err})")
        return
    shown = {k: (f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) and len(v) > 32 else v)
             for k, v in decoded.items()}
    print(f"       {msg_type}: {shown}")


def on_message(msg):
    global status_count, last_status_flush

    short = msg.topic.rsplit("/", 1)[-1] if "/" in msg.topic else msg.topic
    for prefix in ["status/", "common/", "clean/", "task/", "supply/", "config/", "map/", "consumable/", "schedule/"]:
        idx = msg.topic.find(prefix)
        if idx >= 0:
            short = msg.topic[idx:]
            break

    if short in STATUS_TOPICS:
        status_count += 1
        now = time.time()
        if now - last_status_flush >= 10:
            if status_count > 1:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f" [{ts}] (base_status x{status_count})")
            status_count = 0
            last_status_flush = now
        return

    ts = datetime.now().strftime("%H:%M:%S")
    payload = msg.payload
    hex_str = payload.hex() if len(payload) <= 400 else payload[:400].hex() + f"... ({len(payload)}b total)"

    print(f"\n[{ts}] TOPIC: {short}")
    print(f"       Size: {len(payload)} bytes")
    print(f"       Hex:  {hex_str}")

    if hasattr(msg.properties, "ResponseTopic") and msg.properties.ResponseTopic:
        print(f"       ResponseTopic: {msg.properties.ResponseTopic}")

    print_decoded(short, payload)


def main():
    parser = argparse.ArgumentParser(description="Sniff Narwal MQTT traffic")
    parser.add_argument("--duration", type=int, default=300, help="Capture duration in seconds (default: 300)")
    args = parser.parse_args()

    cfg = get_config()
    user_uuid, token = cloud_login(cfg)

    mq = NarwalMQTT(cfg, user_uuid, token)
    mq.add_message_handler(on_message)
    mq.connect()

    for t in TOPICS:
        mq.subscribe(t)
    print(f"Subscribed to {len(TOPICS)} topics")
    print(f"\n{'=' * 60}")
    print(f"LISTENING for {args.duration} seconds...")
    print(f"Start an action from the Narwal app to capture traffic.")
    print(f"{'=' * 60}\n", flush=True)

    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        mq.disconnect()


if __name__ == "__main__":
    main()
