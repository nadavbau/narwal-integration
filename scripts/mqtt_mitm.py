#!/usr/bin/env python3
"""MQTT Man-in-the-Middle proxy for Narwal device discovery.

Terminates TLS from the Narwal app, connects to the real MQTT broker,
relays traffic bidirectionally, and logs decrypted MQTT packets to
extract the product key and device name from topic paths.

Usage:
  1. Share internet from Mac (Ethernet → WiFi)
  2. Connect phone to Mac's hotspot
  3. Set up pfctl redirect:
       echo 'rdr on bridge100 proto tcp from 192.168.2.0/24 to any port 8883 -> 127.0.0.1 port 18883' | sudo pfctl -ef -
  4. Run this script:
       python3 mqtt_mitm.py
  5. Open the Narwal app on the phone
  6. The script logs all MQTT topics — look for /{productKey}/{deviceName}/...
"""

from __future__ import annotations

import os
import select
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

LISTEN_PORT = 18883
REAL_BROKER = "us-01.mqtt.narwaltech.com"
REAL_PORT = 8883


def generate_self_signed_cert() -> tuple[str, str]:
    """Generate a self-signed cert for the MQTT broker hostname."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, REAL_BROKER),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(REAL_BROKER)]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        tmpdir = tempfile.mkdtemp(prefix="mqtt_mitm_")
        cert_path = os.path.join(tmpdir, "cert.pem")
        key_path = os.path.join(tmpdir, "key.pem")

        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        return cert_path, key_path
    except ImportError:
        print("ERROR: 'cryptography' package required. Install with: pip3 install cryptography")
        sys.exit(1)


def parse_mqtt_packet(data: bytes, direction: str) -> None:
    """Parse and log MQTT packet details."""
    if not data:
        return

    ptype = (data[0] >> 4) & 0x0F
    type_names = {
        1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK",
        5: "PUBREC", 6: "PUBREL", 7: "PUBCOMP", 8: "SUBSCRIBE",
        9: "SUBACK", 10: "UNSUBSCRIBE", 11: "UNSUBACK",
        12: "PINGREQ", 13: "PINGRESP", 14: "DISCONNECT", 15: "AUTH",
    }
    name = type_names.get(ptype, f"UNKNOWN({ptype})")

    # Decode remaining length
    idx = 1
    remaining = 0
    multiplier = 1
    while idx < len(data):
        b = data[idx]
        idx += 1
        remaining += (b & 0x7F) * multiplier
        multiplier *= 128
        if not (b & 0x80):
            break

    payload = data[idx:idx + remaining] if idx + remaining <= len(data) else data[idx:]

    if ptype == 1:  # CONNECT
        _parse_connect(payload, direction)
    elif ptype == 3:  # PUBLISH
        _parse_publish(payload, data[0], direction)
    elif ptype == 8:  # SUBSCRIBE
        _parse_subscribe(payload, direction)
    else:
        print(f"  {direction} {name} ({len(data)} bytes)")


def _parse_connect(payload: bytes, direction: str) -> None:
    """Parse CONNECT packet to extract client_id, username, password."""
    try:
        idx = 0
        # Protocol name
        proto_len = struct.unpack("!H", payload[idx:idx+2])[0]
        idx += 2
        proto_name = payload[idx:idx+proto_len].decode("utf-8", errors="replace")
        idx += proto_len
        # Protocol version
        proto_ver = payload[idx]
        idx += 1
        # Connect flags
        flags = payload[idx]
        idx += 1
        has_username = bool(flags & 0x80)
        has_password = bool(flags & 0x40)
        # Keep alive
        keepalive = struct.unpack("!H", payload[idx:idx+2])[0]
        idx += 2

        # MQTT5: properties
        if proto_ver == 5:
            prop_len, bytes_consumed = _decode_varint(payload[idx:])
            idx += bytes_consumed + prop_len

        # Client ID
        cid_len = struct.unpack("!H", payload[idx:idx+2])[0]
        idx += 2
        client_id = payload[idx:idx+cid_len].decode("utf-8", errors="replace")
        idx += cid_len

        username = ""
        password_preview = ""
        if has_username:
            u_len = struct.unpack("!H", payload[idx:idx+2])[0]
            idx += 2
            username = payload[idx:idx+u_len].decode("utf-8", errors="replace")
            idx += u_len
        if has_password:
            p_len = struct.unpack("!H", payload[idx:idx+2])[0]
            idx += 2
            pw = payload[idx:idx+p_len]
            password_preview = pw[:30].decode("utf-8", errors="replace") + "..."
            idx += p_len

        print(f"\n{'='*60}")
        print(f"  {direction} CONNECT (MQTTv{proto_ver})")
        print(f"  Client ID:  {client_id}")
        print(f"  Username:   {username}")
        print(f"  Password:   {password_preview}")
        print(f"  Keep Alive: {keepalive}s")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"  {direction} CONNECT (parse error: {e})")


def _parse_publish(payload: bytes, hdr: int, direction: str) -> None:
    """Parse PUBLISH to extract topic and log it."""
    try:
        idx = 0
        topic_len = struct.unpack("!H", payload[idx:idx+2])[0]
        idx += 2
        topic = payload[idx:idx+topic_len].decode("utf-8", errors="replace")
        idx += topic_len

        parts = topic.strip("/").split("/")
        if len(parts) >= 2:
            product_key = parts[0]
            device_name = parts[1]
            command = "/".join(parts[2:])
            print(f"  {direction} PUBLISH  /{product_key}/{device_name}/{command}  ({len(payload)} bytes)")

            if len(device_name) == 32:
                print(f"\n{'*'*60}")
                print(f"  *** DEVICE FOUND ***")
                print(f"  Product Key:  {product_key}")
                print(f"  Device Name:  {device_name}")
                print(f"  Topic:        {topic}")
                print(f"{'*'*60}\n")

                # Save to file
                out = Path(__file__).parent / "discovered_device.txt"
                with open(out, "w") as f:
                    f.write(f"product_key={product_key}\n")
                    f.write(f"device_name={device_name}\n")
                    f.write(f"discovered_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                print(f"  Saved to {out}")
        else:
            print(f"  {direction} PUBLISH  {topic}  ({len(payload)} bytes)")

    except Exception as e:
        print(f"  {direction} PUBLISH (parse error: {e})")


def _parse_subscribe(payload: bytes, direction: str) -> None:
    """Parse SUBSCRIBE to show subscribed topics."""
    try:
        idx = 0
        # Packet ID
        idx += 2
        # MQTT5 properties
        if idx < len(payload):
            prop_len, consumed = _decode_varint(payload[idx:])
            idx += consumed + prop_len

        topics = []
        while idx + 2 < len(payload):
            t_len = struct.unpack("!H", payload[idx:idx+2])[0]
            idx += 2
            if idx + t_len > len(payload):
                break
            topic = payload[idx:idx+t_len].decode("utf-8", errors="replace")
            idx += t_len
            qos = payload[idx] if idx < len(payload) else 0
            idx += 1
            topics.append(f"{topic} (QoS {qos})")

        for t in topics:
            print(f"  {direction} SUBSCRIBE  {t}")

    except Exception as e:
        print(f"  {direction} SUBSCRIBE (parse error: {e})")


def _decode_varint(data: bytes) -> tuple[int, int]:
    """Decode MQTT variable-length integer. Returns (value, bytes_consumed)."""
    val = 0
    multiplier = 1
    idx = 0
    while idx < len(data):
        b = data[idx]
        idx += 1
        val += (b & 0x7F) * multiplier
        multiplier *= 128
        if not (b & 0x80):
            break
    return val, idx


def relay(src: ssl.SSLSocket, dst: ssl.SSLSocket, direction: str, stop: threading.Event):
    """Relay data between two TLS sockets, parsing MQTT packets."""
    try:
        while not stop.is_set():
            ready, _, _ = select.select([src], [], [], 1.0)
            if not ready:
                continue
            data = src.recv(8192)
            if not data:
                break
            try:
                parse_mqtt_packet(data, direction)
            except Exception:
                pass
            dst.sendall(data)
    except (ssl.SSLError, OSError, ConnectionResetError):
        pass
    finally:
        stop.set()


def handle_client(client_sock: socket.socket, cert_path: str, key_path: str):
    """Handle one proxied connection."""
    addr = client_sock.getpeername()
    print(f"\n[{time.strftime('%H:%M:%S')}] New connection from {addr[0]}:{addr[1]}")

    # TLS-wrap the client side (we're the "server")
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    client_ctx.load_cert_chain(cert_path, key_path)
    try:
        client_tls = client_ctx.wrap_socket(client_sock, server_side=True)
    except ssl.SSLError as e:
        print(f"  Client TLS handshake failed: {e}")
        client_sock.close()
        return

    print(f"  Client TLS handshake OK")

    # Connect to real broker
    server_sock = socket.create_connection((REAL_BROKER, REAL_PORT), timeout=10)
    server_ctx = ssl.create_default_context()
    try:
        server_tls = server_ctx.wrap_socket(server_sock, server_hostname=REAL_BROKER)
    except ssl.SSLError as e:
        print(f"  Server TLS handshake failed: {e}")
        client_tls.close()
        server_sock.close()
        return

    print(f"  Connected to real broker {REAL_BROKER}:{REAL_PORT}")

    stop = threading.Event()
    t1 = threading.Thread(target=relay, args=(client_tls, server_tls, "APP →", stop), daemon=True)
    t2 = threading.Thread(target=relay, args=(server_tls, client_tls, "← SRV", stop), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    client_tls.close()
    server_tls.close()
    print(f"  Connection from {addr[0]}:{addr[1]} closed")


def main():
    print("Generating self-signed certificate...")
    cert_path, key_path = generate_self_signed_cert()
    print(f"  Cert: {cert_path}")
    print(f"  Key:  {key_path}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    sock.listen(5)

    print(f"\nMQTT MITM listening on port {LISTEN_PORT}")
    print(f"Proxying to {REAL_BROKER}:{REAL_PORT}")
    print(f"\nSetup:")
    print(f"  1. Enable Internet Sharing (Ethernet → WiFi)")
    print(f"  2. Connect phone to Mac's hotspot")
    print(f"  3. Run: echo 'rdr on bridge100 proto tcp from 192.168.2.0/24 to any port 8883 -> 127.0.0.1 port {LISTEN_PORT}' | sudo pfctl -ef -")
    print(f"  4. Open Narwal app on phone")
    print(f"\nWaiting for connections...\n")

    try:
        while True:
            client, addr = sock.accept()
            threading.Thread(target=handle_client, args=(client, cert_path, key_path), daemon=True).start()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
