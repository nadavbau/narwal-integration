#!/usr/bin/env python3
"""Try to discover the MQTT device_name through various MQTT-based approaches.

Approaches:
1. Subscribe to wildcard topics and see if messages arrive
2. Subscribe to $SYS topics for broker info
3. Try Aliyun IoT system topics for device discovery
"""

from __future__ import annotations

import ssl
import time
import threading
import uuid as uuid_mod

import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes

from _common import cloud_login, get_config, pb_string, pb_varint, encode_varint

found_topics = set()
message_count = 0


def on_message(client, userdata, msg):
    global message_count
    message_count += 1
    topic = msg.topic
    if topic not in found_topics:
        found_topics.add(topic)
        print(f"  FOUND: {topic} ({len(msg.payload)}b)", flush=True)


def main():
    cfg = get_config()
    user_uuid, token = cloud_login(cfg)
    product_key = cfg["product_key"]
    known_device = cfg["device_id"]

    print(f"Logged in: uuid={user_uuid}")
    print(f"Product key: {product_key}")
    print(f"Known device (for verification): {known_device}")

    connected = threading.Event()

    def on_connect(client, userdata, flags, rc, props=None):
        if str(rc) == "Success" or rc == 0:
            print("  Connected to MQTT broker")
            connected.set()
        else:
            print(f"  Connection failed: {rc}")

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        pass

    c = mqtt.Client(
        client_id=f"app_{user_uuid}_{uuid_mod.uuid4()}",
        protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    c.username_pw_set(user_uuid, token)
    c.tls_set_context(ssl.create_default_context())
    c.on_connect = on_connect
    c.on_message = on_message
    c.on_subscribe = on_subscribe
    c.connect(cfg["mqtt_host"], 8883, keepalive=30)
    c.loop_start()

    if not connected.wait(timeout=15):
        print("Connection timeout!")
        return

    print("\n=== Test 1: Wildcard subscriptions ===")
    wildcards = [
        f"/{product_key}/+/status/working_status",
        f"/{product_key}/+/status/robot_base_status",
        f"/{product_key}/+/status/#",
        f"/{product_key}/#",
        f"/{product_key}/+/#",
        "+/+/status/working_status",
        "#",
        "$SYS/#",
        "$SYS/broker/clients/connected",
        "$SYS/broker/clients/total",
    ]

    for topic in wildcards:
        c.subscribe(topic, qos=1)
        print(f"  Subscribed: {topic}")

    print("  Waiting 10s for wildcard messages...")
    time.sleep(10)
    print(f"  Messages received: {message_count}")

    print("\n=== Test 2: Aliyun IoT system topics ===")
    sys_topics = [
        f"/sys/{product_key}/+/thing/deviceinfo/get",
        f"/sys/{product_key}/+/thing/list/found",
        f"/ext/session/{product_key}/+/register",
        f"/ext/session/{product_key}/+/combine/login",
    ]

    for topic in sys_topics:
        c.subscribe(topic, qos=1)
        print(f"  Subscribed: {topic}")

    print("  Waiting 5s...")
    time.sleep(5)
    print(f"  Total messages: {message_count}")

    print("\n=== Test 3: Publish device discovery request ===")
    discovery_topics = [
        f"/sys/{product_key}/app/thing/deviceinfo/get",
        f"/sys/{product_key}/{user_uuid}/thing/deviceinfo/get",
    ]

    for topic in discovery_topics:
        resp_topic = f"{topic}_reply"
        c.subscribe(resp_topic, qos=1)

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = resp_topic
        corr = pb_string(1, str(uuid_mod.uuid1()))
        props.CorrelationData = corr

        payload = pb_string(1, user_uuid) + pb_string(2, user_uuid)
        c.publish(topic, payload, qos=1, properties=props)
        print(f"  Published to: {topic}")

    print("  Waiting 5s...")
    time.sleep(5)
    print(f"  Total messages: {message_count}")

    print(f"\n=== Test 4: Subscribe to known device to verify connectivity ===")
    c.subscribe(f"/{product_key}/{known_device}/status/working_status", qos=1)
    print(f"  Subscribed to known device topic")
    print(f"  Waiting 10s for status messages...")
    time.sleep(10)
    print(f"  Total messages: {message_count}")

    print(f"\n=== Summary ===")
    print(f"Total unique topics that delivered messages: {len(found_topics)}")
    for t in sorted(found_topics):
        print(f"  {t}")

    c.loop_stop()
    c.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    main()
