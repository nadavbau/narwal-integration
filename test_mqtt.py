#!/usr/bin/env python3
"""Standalone MQTT test for the Narwal integration.

Tests the same code paths as Home Assistant without needing HA installed.
Run: python test_mqtt.py --email you@example.com --password secret --region il
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "custom_components/narwal")

from narwal_client import (
    NarwalClient,
    NarwalCloud,
    NarwalCloudError,
    NarwalCommandError,
    NarwalConnectionError,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("test_mqtt")


async def run(
    email: str,
    password: str,
    region: str,
    product_key: str | None = None,
    device_name: str | None = None,
) -> None:
    # ── 1. Cloud login ──────────────────────────────────────────────
    log.info("Logging in to Narwal cloud (region=%s)...", region)
    cloud = NarwalCloud(region=region)
    session = cloud.login(email, password)
    log.info("Login OK  uuid=%s  broker=%s", session.user_uuid, cloud.mqtt_broker)
    log.info("Token expires at %.0f (expired=%s)", session.token_expiry, session.is_token_expired)

    # ── 2. Device discovery ─────────────────────────────────────────
    log.info("Discovering devices...")
    try:
        devices = cloud.get_devices()
        for d in devices:
            log.info("  Device: id=%s  name=%s", d.device_id, d.name)
    except NarwalCloudError as e:
        log.warning("Device discovery failed: %s", e)

    if not product_key:
        product_key = input("\nProduct key [EHf6cRNRGT]: ").strip() or "EHf6cRNRGT"
    if not device_name:
        device_name = input("Device name: ").strip()
    if not device_name:
        log.error("Device name is required")
        return

    log.info("Using product_key=%s, device_name=%s", product_key, device_name)

    # ── 3. MQTT connect ─────────────────────────────────────────────
    client = NarwalClient(
        product_key=product_key,
        device_name=device_name,
        user_uuid=session.user_uuid,
        mqtt_username=session.user_uuid,
        mqtt_password=session.access_token,
        broker=cloud.mqtt_broker,
    )

    def on_state(state):
        log.info(
            "STATE UPDATE  battery=%.1f%%  status=%s  docked=%s  cleaning=%s",
            state.battery_level,
            state.working_status.name,
            state.is_docked,
            state.is_cleaning,
        )

    client.on_state_update = on_state

    log.info("Connecting to MQTT broker %s ...", cloud.mqtt_broker)
    await client.connect()
    log.info("Connected! client_id=%s", client._mqtt_client_id)

    # ── 4. Test commands ────────────────────────────────────────────
    log.info("─── Requesting status ───")
    await client.request_status_update()

    log.info("Waiting 5s for push broadcasts...")
    await asyncio.sleep(5)

    state = client.state
    log.info(
        "Current state: battery=%.1f%%  status=%s  docked=%s",
        state.battery_level,
        state.working_status.name,
        state.is_docked,
    )

    log.info("─── Sending YELL (locate) ───")
    try:
        resp = await client.locate()
        log.info("Yell response: success=%s  code=%s", resp.success, resp.result_code)
    except NarwalCommandError as e:
        log.error("Yell failed: %s", e)

    # ── 5. Listen for pushes ────────────────────────────────────────
    log.info("─── Listening for 30s (press Ctrl-C to stop) ───")
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass

    await client.disconnect()
    log.info("Disconnected.")


def main():
    p = argparse.ArgumentParser(description="Narwal MQTT integration test")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--region", default="il", choices=["us", "il", "eu", "cn"])
    p.add_argument("--product-key", default=None, help="MQTT product key")
    p.add_argument("--device-name", default=None, help="MQTT device name")
    args = p.parse_args()

    try:
        asyncio.run(run(args.email, args.password, args.region, args.product_key, args.device_name))
    except KeyboardInterrupt:
        log.info("Interrupted.")


if __name__ == "__main__":
    main()
