#!/usr/bin/env python3
"""Test cleaning individual and multiple rooms via MQTT.

Usage:
  export NARWAL_EMAIL=... NARWAL_PASSWORD=... NARWAL_DEVICE_ID=... NARWAL_REGION=il
  python tests/test_clean_rooms.py [--rooms 1,2,7] [--vacuum-only] [--passes 2]

By default, tests all rooms discovered from the map. Each room is started,
verified, then force-stopped before moving to the next.
"""

from __future__ import annotations

import argparse
import time

from _common import (
    RESULT_NAMES,
    NarwalMQTT,
    auth_payload,
    cloud_login,
    get_config,
    parse_command_result,
    parse_protobuf_fields,
    pb_string,
    pb_varint,
)


def build_clean_payload(
    user_uuid: str,
    room_ids: list[int],
    vacuum_on: bool = True,
    mop_on: bool = False,
    fan_level: int = 1,
    mop_humidity: int = 1,
    passes: int = 2,
) -> bytes:
    """Build a clean/start_clean payload with correct field mapping."""
    room_config = (
        pb_varint(1, passes) + pb_varint(2, passes)
        + pb_varint(3, 1 if vacuum_on else 0)
        + pb_varint(4, 1 if mop_on else 2)
        + pb_varint(5, fan_level) + pb_varint(6, mop_humidity)
        + pb_varint(7, 1) + pb_varint(8, 1) + pb_varint(9, 1) + pb_varint(10, 0)
    )

    room_list = b""
    for rid in room_ids:
        global_cfg = pb_varint(1, 1) + pb_varint(2, rid)
        room_list += pb_string(1, global_cfg)
        room_list += pb_string(2, room_config)
    room_list += pb_varint(3, 1)

    clean_config = (
        pb_varint(1, 1)
        + pb_string(2, room_list)
        + pb_varint(3, 1)
        + pb_string(4, pb_varint(1, 1) + pb_varint(5, 0))
        + pb_varint(5, 1)
    )

    frame = auth_payload(user_uuid)
    return frame + pb_string(1, clean_config)


def discover_rooms(mq: NarwalMQTT, user_uuid: str) -> list[tuple[int, str]]:
    """Fetch the map and extract room IDs and names."""
    from _common import parse_protobuf_fields

    ROOM_NAMES = {
        0: "Room", 1: "Master Bedroom", 2: "Bedroom", 3: "Living Room",
        4: "Kitchen", 5: "Bathroom", 6: "Toilet", 7: "Dining Room",
        8: "Dining Room", 9: "Balcony", 10: "Utility Room", 11: "Study",
        12: "Nursery", 13: "Recreation Room", 14: "Storage Room", 15: "Other",
    }

    payload = auth_payload(user_uuid) + pb_varint(1, 0) + pb_varint(2, 0)
    resp = mq.send_command("map/get_map", payload)
    if not resp:
        return []

    if resp[0] == 0x01:
        idx = 1
        while idx < len(resp) and resp[idx] & 0x80:
            idx += 1
        idx += 1
        inner = resp[idx:]
    else:
        inner = resp

    fields = parse_protobuf_fields(inner)
    map_data = fields.get(2, [b""])[0]
    if not isinstance(map_data, bytes):
        return []

    mf = parse_protobuf_fields(map_data)
    rooms = []
    for room_bytes in mf.get(16, []):
        if isinstance(room_bytes, bytes):
            rf = parse_protobuf_fields(room_bytes)
            room_id = rf.get(1, [0])[0]
            sub_type = rf.get(2, [15])[0]
            name = ROOM_NAMES.get(sub_type, f"Room-{sub_type}")
            rooms.append((room_id, name))
    return rooms


def main():
    parser = argparse.ArgumentParser(description="Test Narwal room cleaning")
    parser.add_argument("--rooms", type=str, default="", help="Comma-separated room IDs (default: all from map)")
    parser.add_argument("--vacuum-only", action="store_true", help="Vacuum only mode (no mop)")
    parser.add_argument("--mop-only", action="store_true", help="Mop only mode")
    parser.add_argument("--passes", type=int, default=2, help="Number of passes (default: 2)")
    parser.add_argument("--multi", type=str, default="", help="Multi-room test: comma-separated room IDs")
    args = parser.parse_args()

    cfg = get_config()
    user_uuid, token = cloud_login(cfg)
    print(f"Logged in as {user_uuid[:8]}...")

    mq = NarwalMQTT(cfg, user_uuid, token)
    mq.connect()
    mq.subscribe("status/robot_base_status")
    print("Connected to MQTT")

    vacuum_on = not args.mop_only
    mop_on = not args.vacuum_only

    if args.rooms:
        room_ids = [(int(r.strip()), f"Room-{r.strip()}") for r in args.rooms.split(",")]
    else:
        print("Discovering rooms from map...")
        room_ids = discover_rooms(mq, user_uuid)
        if not room_ids:
            print("No rooms found!")
            mq.disconnect()
            return
        print(f"Found {len(room_ids)} rooms: {room_ids}")

    print(f"\nForce-stopping any running task...")
    mq.send_command("task/force_end", auth_payload(user_uuid))
    time.sleep(5)

    for room_id, name in room_ids:
        print(f"\n{name} (id={room_id})... ", end="", flush=True)
        payload = build_clean_payload(
            user_uuid, [room_id],
            vacuum_on=vacuum_on, mop_on=mop_on, passes=args.passes,
        )
        resp = mq.send_command("clean/start_clean", payload)
        code = parse_command_result(resp)
        print(RESULT_NAMES.get(code, f"code={code}"), flush=True)

        if code == 1:
            time.sleep(3)
            mq.send_command("task/force_end", auth_payload(user_uuid))
            time.sleep(5)
        else:
            time.sleep(2)

    if args.multi:
        multi_ids = [int(r.strip()) for r in args.multi.split(",")]
        print(f"\nMulti-room test: {multi_ids}... ", end="", flush=True)
        payload = build_clean_payload(
            user_uuid, multi_ids,
            vacuum_on=vacuum_on, mop_on=mop_on, passes=args.passes,
        )
        resp = mq.send_command("clean/start_clean", payload)
        code = parse_command_result(resp)
        print(RESULT_NAMES.get(code, f"code={code}"), flush=True)
        if code == 1:
            time.sleep(3)
            mq.send_command("task/force_end", auth_payload(user_uuid))

    mq.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    main()
