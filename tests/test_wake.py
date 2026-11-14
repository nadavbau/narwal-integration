"""Waking a robot from deep sleep before sending it commands."""

from __future__ import annotations

import time

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.narwal_client.const import WAKE_TIMEOUTS
from custom_components.narwal.narwal_client.proto import decode

VACUUM = "vacuum.narwal_ehf6crnrgt_narwal_vacuum"
ASLEEP = {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_LOW_POWER_IN_STATION"}


@pytest.fixture
async def setup(hass: HomeAssistant, config_entry, mock_robot):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry.runtime_data


def _put_to_sleep(coordinator, robot, wake_after: float) -> None:
    robot.sleep_until_wake_requests = wake_after
    robot.wake_requests = 0
    robot.calls.clear()
    coordinator.client.state.base["robotTaskStatus"] = dict(ASLEEP)


async def test_command_wakes_sleeping_robot_first(hass: HomeAssistant, setup, robot) -> None:
    _put_to_sleep(setup, robot, wake_after=4)

    await hass.services.async_call(
        "vacuum", "send_command",
        {"entity_id": VACUUM, "command": "clean_rooms", "params": {"rooms": [2]}},
        blocking=True,
    )

    sent = [cmd for cmd, _ in robot.calls]
    # The app's sequence: "entered device page", then active_robot_publish
    # until the robot answers, and only then the actual command (once).
    assert sent[0] == "common/notify_app_event"
    assert decode("NotifyAppEvent_Request", robot.calls[0][1]) == {"appEvent": "APP_EVENT_ENTER_DEVICE_PAGE"}
    assert sent[1:5] == ["common/active_robot_publish"] * 4
    assert sent.count("clean/start_clean") == 1
    assert sent.index("clean/start_clean") > 4
    assert not robot.asleep


async def test_timeout_triggers_wake_and_single_retry(hass: HomeAssistant, setup, robot) -> None:
    """Robot fell asleep without telling us: the command times out, we wake it, retry once."""
    robot.sleep_until_wake_requests = 2
    robot.wake_requests = 0
    robot.calls.clear()
    setup.client.last_message_time = time.time()  # looks awake

    await hass.services.async_call("vacuum", "locate", {"entity_id": VACUUM}, blocking=True)

    sent = [cmd for cmd, _ in robot.calls]
    assert sent == [
        "common/yell",                    # times out (asleep)
        "common/notify_app_event",
        "common/active_robot_publish",    # no answer
        "common/active_robot_publish",    # answers
        "common/yell",                    # retried once
    ]


async def test_robot_that_never_wakes(hass: HomeAssistant, setup, robot) -> None:
    _put_to_sleep(setup, robot, wake_after=float("inf"))

    with pytest.raises(HomeAssistantError, match="did not respond"):
        await hass.services.async_call("vacuum", "locate", {"entity_id": VACUUM}, blocking=True)

    sent = [cmd for cmd, _ in robot.calls]
    # One full wake schedule before the command, the command, a second full
    # schedule after its timeout, and no retry of the command.
    assert sent.count("common/active_robot_publish") == 2 * len(WAKE_TIMEOUTS)
    assert sent.count("common/yell") == 1
    assert setup.client.needs_reconnect


async def test_polls_do_not_wake_or_reconnect_while_asleep(hass: HomeAssistant, setup, robot) -> None:
    _put_to_sleep(setup, robot, wake_after=float("inf"))

    for _ in range(5):
        await setup._async_update_data()

    sent = [cmd for cmd, _ in robot.calls]
    assert "common/active_robot_publish" not in sent
    assert "common/notify_app_event" not in sent
    assert setup._consecutive_failures == 0  # sleeping isn't a connection problem
