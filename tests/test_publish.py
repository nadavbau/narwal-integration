"""Publishes must not wait for broker acknowledgements."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandError

from .conftest import DEVICE, PRODUCT, USER


def _client() -> tuple[NarwalClient, list[dict]]:
    client = NarwalClient(PRODUCT, DEVICE, USER, "user", "pass")
    sent: list[dict] = []
    client._client = SimpleNamespace(publish=lambda *a, **k: sent.append(k))
    client._connected.set()
    client._ensure_subscribed = lambda topic: None
    return client, sent


async def test_publishes_use_qos_0() -> None:
    """Narwal's broker never PUBACKs QoS 1, and paho stops sending after 20
    unacknowledged messages, so every publish must be QoS 0."""
    client, sent = _client()
    with pytest.raises(NarwalCommandError):
        client._send_blocking("status/get_device_base_status", b"", timeout=0.01)
    await client._publish_no_response("common/notify_app_event", b"")
    assert [k["qos"] for k in sent] == [0, 0]
