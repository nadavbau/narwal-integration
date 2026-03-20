#!/usr/bin/env python3
"""Subscribe to all known MQTT topics and log traffic from the Narwal app.

Usage:
  export NARWAL_EMAIL=... NARWAL_PASSWORD=... NARWAL_DEVICE_ID=... NARWAL_REGION=il
  python tests/sniff_app.py [--duration SECONDS]

The script listens for MQTT messages from the vacuum and the official app,
printing topic, size, raw hex, and decoded protobuf fields. It includes
a specialised decoder for clean/start_clean payloads.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from _common import (
    NarwalMQTT,
    auth_payload,
    cloud_login,
    get_config,
    parse_protobuf_fields,
)

# All known command and status topics to subscribe to
TOPICS = [
    "common/yell", "common/yell/response",
    "common/active_robot_publish", "common/active_robot_publish/response",
    "common/get_device_info", "common/get_device_info/response",
    "status/get_device_base_status", "status/get_device_base_status/response",
    "status/robot_base_status",
    "status/working_status",
    "status/upgrade_status",
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
    "config/get", "config/get/response",
    "config/set", "config/set/response",
    "config/volume/set", "config/volume/set/response",
    "consumable/get_consumable_info", "consumable/get_consumable_info/response",
    "map/get_map", "map/get_map/response",
    "map/get_all_reduced_maps", "map/get_all_reduced_maps/response",
    "schedule/clean_schedule/get", "schedule/clean_schedule/get/response",
]

# Topics with high-frequency updates that we'll batch
STATUS_TOPICS = {"status/robot_base_status"}

status_count = 0
last_status_flush = 0.0


def decode_clean_payload(raw: bytes) -> None:
    """Decode the extra clean configuration appended outside the Narwal frame."""
    if raw[0] != 0x01:
        return
    idx = 1
    while idx < len(raw) and raw[idx] & 0x80:
        idx += 1
    idx += 1
    frame_end = idx
    while frame_end < len(raw):
        tag = raw[frame_end]; frame_end += 1
        wt = tag & 7
        if wt == 0:
            while frame_end < len(raw) and raw[frame_end] & 0x80:
                frame_end += 1
            frame_end += 1
        elif wt == 2:
            length = 0; shift = 0
            while frame_end < len(raw):
                b = raw[frame_end]; frame_end += 1
                length |= (b & 0x7F) << shift; shift += 7
                if not (b & 0x80):
                    break
            frame_end += length
        elif wt == 5:
            frame_end += 4
        elif wt == 1:
            frame_end += 8
        else:
            break

    extra = raw[idx:]
    if not extra:
        return
    print(f"       Extra ({len(extra)}b): {extra.hex()}")
    print("       --- Decoded clean config ---")

    fields = parse_protobuf_fields(extra)
    for fn, vals in fields.items():
        for v in vals:
            if isinstance(v, bytes):
                cfg_fields = parse_protobuf_fields(v)
                print(f"       config.{fn} = sub({len(v)}b): {v.hex()}")
                for cfn, cvals in cfg_fields.items():
                    for cv in cvals:
                        if isinstance(cv, bytes):
                            sub = parse_protobuf_fields(cv)
                            sub_dict = {k: vs[0] for k, vs in sub.items()}
                            if cfn == 1:
                                print(f"         roomlist.{cfn} = {sub_dict}")
                            else:
                                print(f"         room entry: {sub_dict}")
                        else:
                            print(f"         roomlist.{cfn} = {cv}")
            else:
                print(f"       config.{fn} = {v}")


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

    if "start_clean" in short and "/response" not in short:
        try:
            decode_clean_payload(payload)
        except Exception as e:
            print(f"       (decode error: {e})")

    if "working_status" in short:
        try:
            fields = parse_protobuf_fields(payload)
            print(f"       Fields: {fields}")
        except Exception:
            pass


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
