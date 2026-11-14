"""Captured Freo Z Ultra frames through the real client and entities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.core import HomeAssistant

from .conftest import frame
from .fixtures import zultra_payloads as zultra
from .test_integration import VACUUM, _eid


@pytest.fixture
async def setup(hass: HomeAssistant, config_entry, mock_robot):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry.runtime_data


def _push_raw(hass: HomeAssistant, client, payload: bytes) -> None:
    msg = SimpleNamespace(topic=f"{client.base_topic}/status/robot_base_status", payload=frame(payload))
    client._on_message(None, None, msg)


async def test_dust_bag_sensors(hass: HomeAssistant, setup) -> None:
    _push_raw(hass, setup.client, zultra.STANDBY_DOCKED)
    await hass.async_block_till_done()

    assert hass.states.get(VACUUM).state == VacuumActivity.DOCKED
    assert hass.states.get(_eid(hass, "sensor", "dust_bag")).state == "installed"
    health = hass.states.get(_eid(hass, "sensor", "dust_bag_health"))
    assert health.state == "69"
    assert health.attributes["unit_of_measurement"] == "%"


async def test_dust_bag_sensors_without_a_bag(hass: HomeAssistant, setup) -> None:
    """Bases without a dust bag don't report the fields: sensors stay unknown."""
    assert hass.states.get(_eid(hass, "sensor", "dust_bag")).state == "unknown"
    assert hass.states.get(_eid(hass, "sensor", "dust_bag_health")).state == "unknown"


async def test_zultra_activity(hass: HomeAssistant, setup) -> None:
    expected = [
        (zultra.COMBO_CLEANING, VacuumActivity.CLEANING),
        (zultra.COMBO_RETURNING_TO_WASH, VacuumActivity.RETURNING),
        (zultra.MOP_WASHING, VacuumActivity.DOCKED),
        (zultra.PAUSED_ON_DOCK_WITH_ERROR, VacuumActivity.PAUSED),
    ]
    for payload, activity in expected:
        _push_raw(hass, setup.client, payload)
        await hass.async_block_till_done()
        assert hass.states.get(VACUUM).state == activity

    # The sewage-tank fault from the last frame is surfaced as an error.
    assert hass.states.get(_eid(hass, "binary_sensor", "problem")).state == "on"
    error = hass.states.get(_eid(hass, "sensor", "error"))
    assert error.attributes["codes"] == ["0x01010036"]
