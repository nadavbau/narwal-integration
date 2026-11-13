#!/usr/bin/env python3
"""Start a real room clean, confirm the robot is cleaning, then stop and dock.

Uses the integration's own narwal_client library, so it exercises exactly
the payloads Home Assistant sends.

Usage (credentials from scripts/.env or the environment):
  python3 test_clean_rooms.py                         # list rooms, then exit
  python3 test_clean_rooms.py --rooms 4               # vacuum + mop room 4
  python3 test_clean_rooms.py --rooms 4,2 --mode vacuum --fan strong --passes 2
  python3 test_clean_rooms.py --rooms 4 --mode mop --wetness wet --run 60

The robot drives. It is stopped after --run seconds (default 30) and sent
back to the dock; Ctrl-C also stops it and sends it home.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from _common import connect_client, load_narwal_client

lib = load_narwal_client()
CleanMode = lib.const.CleanMode
FanLevel = lib.const.FanLevel
MopHumidity = lib.const.MopHumidity
NarwalCommandError = lib.client.NarwalCommandError

MODES = {
    "vacuum-mop": CleanMode.VACUUM_AND_MOP,
    "vacuum-then-mop": CleanMode.VACUUM_THEN_MOP,
    "vacuum": CleanMode.VACUUM_ONLY,
    "mop": CleanMode.MOP_ONLY,
}
FANS = {"quiet": FanLevel.QUIET, "normal": FanLevel.NORMAL, "strong": FanLevel.STRONG, "max": FanLevel.MAX}
WETNESS = {"dry": MopHumidity.DRY, "normal": MopHumidity.NORMAL, "wet": MopHumidity.WET}


async def wait_for(client, predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(client.state):
            return True
        await asyncio.sleep(1)
    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live room-clean test")
    parser.add_argument("--rooms", default="", help="Comma-separated room ids (omit to list rooms)")
    parser.add_argument("--mode", choices=MODES, default="vacuum-mop")
    parser.add_argument("--fan", choices=FANS, default="normal")
    parser.add_argument("--wetness", choices=WETNESS, default="normal")
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--run", type=int, default=30, help="Seconds to let it clean before stopping")
    args = parser.parse_args()

    client = await connect_client()
    state = client.state
    try:
        await client.request_status_update()
        await client.fetch_map()
        print(f"Robot: {state.status_text} ({state.activity}), battery {state.battery_level}%")
        for room in state.rooms:
            flag = "  [no-go]" if room.room_id in state.no_go_room_ids else ""
            print(f"  room {room.room_id}: {room.display_name}{flag}")
        if not args.rooms:
            print("\nPass --rooms to start a clean.")
            return

        rooms = [int(r) for r in args.rooms.split(",")]
        print(f"\nStarting {args.mode} in rooms {rooms} (fan {args.fan}, wetness {args.wetness}, passes {args.passes})")
        try:
            resp = await client.start_clean(
                rooms, MODES[args.mode], FANS[args.fan], WETNESS[args.wetness], args.passes,
            )
        except NarwalCommandError as err:
            print(f"Rejected: {err}")
            return
        print(f"start_clean: {resp.result}")

        if await wait_for(client, lambda s: s.activity == "cleaning", 60):
            print(f"Cleaning: {state.status_text}")
            end = time.monotonic() + args.run
            while time.monotonic() < end:
                await asyncio.sleep(5)
                print(f"  {state.status_text} | fan {state.base.get('fanLevel')} | "
                      f"room {state.current_room_id} | area {state.cleaned_area} m²")
        else:
            print(f"Did not start cleaning within 60 s (status: {state.status_text})")
    finally:
        if state.is_cleaning or state.activity in ("paused", "returning"):
            print("Stopping and returning to the dock...")
            try:
                await client.stop()
                await asyncio.sleep(3)
                await client.return_to_base()
                docked = await wait_for(client, lambda s: s.activity == "docked", 240)
                print("Docked." if docked else f"Not docked yet: {state.status_text}")
            except NarwalCommandError as err:
                print(f"Stop/return failed: {err}")
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
