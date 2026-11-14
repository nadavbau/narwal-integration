"""Protocol constants, enums, and MQTT topic definitions for Narwal vacuum.

Enum values match the official Narwal app's protobuf definitions (see
``schema.py``). Do not renumber them.
"""

from __future__ import annotations

from enum import IntEnum

MQTT_BROKER = "us-01.mqtt.narwaltech.com"
MQTT_PORT = 8883

COMMAND_RESPONSE_TIMEOUT = 10.0

# Waking a robot from deep sleep, as the official app does: send
# active_robot_publish as a request and wait for its reply, with these
# per-attempt timeouts (seconds), until the robot answers (~76 s in total).
WAKE_TIMEOUTS: tuple[float, ...] = (1, 1, 2, 2, 4, 4, 8, 8, 16, 30)
# No message from the robot for this long means it has probably gone to sleep
# (while awake and registered it pushes status every ~2 s).
ASLEEP_AFTER_SECONDS = 120

# --- Command topics (client -> robot) ---
TOPIC_CMD_YELL = "common/yell"
TOPIC_CMD_REBOOT = "common/reboot"
TOPIC_CMD_SHUTDOWN = "common/shutdown"
TOPIC_CMD_GET_DEVICE_INFO = "common/get_device_info"
TOPIC_CMD_GET_FEATURE_LIST = "common/get_feature_list"
TOPIC_CMD_GET_FIRMWARE_VERSION = "common/upgrade/get_firmware_version"
TOPIC_CMD_GET_BASE_STATUS = "status/get_device_base_status"
TOPIC_CMD_ACTIVE_ROBOT = "common/active_robot_publish"
TOPIC_CMD_NOTIFY_APP_EVENT = "common/notify_app_event"

# Task control
TOPIC_CMD_PAUSE = "task/pause"
TOPIC_CMD_RESUME = "task/resume"
TOPIC_CMD_FORCE_END = "task/force_end"
TOPIC_CMD_CANCEL = "task/cancel"

# Supply/dock
TOPIC_CMD_RECALL = "supply/recall"
TOPIC_CMD_WASH_MOP = "supply/wash_mop"
TOPIC_CMD_DRY_MOP = "supply/dry_mop"
TOPIC_CMD_WASH_AND_DRY_MOP = "supply/wash_and_dry_mop"
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"

# Cleaning
TOPIC_CMD_START_CLEAN = "clean/start_clean"
TOPIC_CMD_EASY_CLEAN = "clean/easy_clean/start"
TOPIC_CMD_SET_FAN_LEVEL = "clean/set_fan_level"
TOPIC_CMD_SET_MOP_HUMIDITY = "clean/set_mop_humidity"
TOPIC_CMD_GET_CURRENT_TASK = "clean/current_clean_task/get"

# Config
TOPIC_CMD_GET_CONFIG = "config/get"
TOPIC_CMD_SET_CONFIG = "config/set"
TOPIC_CMD_SET_VOLUME = "config/volume/set"

# Consumables
TOPIC_CMD_GET_CONSUMABLE = "consumable/get_consumable_info"
TOPIC_CMD_RESET_CONSUMABLE = "consumable/reset_consumable_info"

# Schedule
TOPIC_CMD_GET_SCHEDULE = "schedule/clean_schedule/get"

# Map
TOPIC_CMD_GET_MAP = "map/get_map"
TOPIC_CMD_GET_ALL_MAPS = "map/get_all_reduced_maps"

# --- Push topics (robot -> client) ---
TOPIC_WORKING_STATUS = "status/working_status"
TOPIC_ROBOT_BASE_STATUS = "status/robot_base_status"
TOPIC_UPGRADE_STATUS = "status/upgrade_status"
TOPIC_DISPLAY_MAP = "map/display_map"

BROADCAST_TOPICS = (
    TOPIC_ROBOT_BASE_STATUS,
    TOPIC_WORKING_STATUS,
    TOPIC_DISPLAY_MAP,
    TOPIC_UPGRADE_STATUS,
)


class FanLevel(IntEnum):
    """Suction level (protobuf ``FanLevel``)."""

    UNSPECIFIED = 0
    QUIET = 1  # FAN_LEVEL_MUTE
    NORMAL = 2
    STRONG = 3
    DEEP = 4
    MAX = 5  # FAN_LEVEL_SUPER


class MopHumidity(IntEnum):
    """Mop wetness (protobuf ``MopHumidity``)."""

    UNSPECIFIED = 0
    DRY = 1
    NORMAL = 2
    WET = 3


class CleanMode(IntEnum):
    """Clean mode choices offered in HA (sent as protobuf ``CleanMode`` labels)."""

    UNSPECIFIED = 0
    VACUUM_ONLY = 1  # CLEAN_MODE_SWEEP
    MOP_ONLY = 2  # CLEAN_MODE_MOP
    VACUUM_AND_MOP = 3  # CLEAN_MODE_SWEEP_MOP_SYNC
    VACUUM_THEN_MOP = 4  # CLEAN_MODE_SWEEP_THEN_MOP


CLEAN_MODE_LABEL: dict[CleanMode, str] = {
    CleanMode.VACUUM_ONLY: "CLEAN_MODE_SWEEP",
    CleanMode.MOP_ONLY: "CLEAN_MODE_MOP",
    CleanMode.VACUUM_AND_MOP: "CLEAN_MODE_SWEEP_MOP_SYNC",
    CleanMode.VACUUM_THEN_MOP: "CLEAN_MODE_SWEEP_THEN_MOP",
}

# ServiceResult labels that mean the command was accepted.
SUCCESS_RESULTS = frozenset({
    "SERVICE_RESULT_PERFORM_SUCCESS",
    "RESULT_TYPE_SUCCESS",
    "SERVICE_RESULT_LIDAR_CALIBRATION_SUCCESS",
})

# Human-readable reasons for ServiceResult failures.
SERVICE_RESULT_MESSAGES: dict[str, str] = {
    "SERVICE_RESULT_PERFORM_GENERAL_FAILED": "The robot rejected the command",
    "SERVICE_RESULT_ALREADY_EXECUTING": "The robot is already doing that",
    "SERVICE_RESULT_POWER_LOWER": "Battery too low",
    "SERVICE_RESULT_ROBOT_TRAPPED_IN_STATION": "The robot is stuck in the base station",
    "SERVICE_RESULT_ROBOT_IN_STATION_WHEN_RECALL": "The robot is already docked",
    "SERVICE_RESULT_ROBOT_NOT_CONTACT_WITH_STATION": "The robot is not seated on the base station",
    "SERVICE_RESULT_DUST_BOX_NOT_INSTALLED": "The dust bin is not installed",
    "SERVICE_RESULT_MOP_NOT_INSTALLED": "The mops are not installed",
    "SERVICE_RESULT_FORBIDDEN_TASK_IN_NET_CONFIG": "Not allowed during network setup",
    "SERVICE_RESULT_FORBIDDEN_TASK_IN_UPGRADE": "Not allowed during a firmware update",
    "SERVICE_RESULT_FORBIDDEN_TASK_IF_STATION_NOT_CONNECTED": "The base station is not connected",
    "SERVICE_RESULT_FORBIDDEN_TASK_IF_CAN_NOT_REACH_SPECIFIED_AREA": "The robot cannot reach that area",
    "SERVICE_RESULT_FORBIDDEN_TASK_WHEN_CHARGE": "Not allowed while charging",
    "SERVICE_RESULT_FORBIDDEN_RECALL_WHEN_NO_MAP": "Cannot return to base without a map",
    "SERVICE_RESULT_NOT_IN_MAIN_MAP": "The robot is not on the main map",
    "SERVICE_RESULT_NO_STATION_IN_MAP": "The map has no base station",
    "SERVICE_RESULT_UPPER_COVER_NOT_INSTALLED": "The robot's top cover is open",
    "SERVICE_RESULT_STATION_BAG_UNINSTALL": "The base station dust bag is missing",
    "SERVICE_RESULT_IN_TELECONTROL": "The robot is under remote control",
    "SERVICE_RESULT_FAILED_DURING_MOTOR_COOLING": "The motor is cooling down",
}

ROOM_SUB_TYPE_NAMES: dict[int, str] = {
    0: "Room",
    1: "Master Bedroom",
    2: "Bedroom",
    3: "Living Room",
    4: "Kitchen",
    5: "Bathroom",
    6: "Toilet",
    7: "Dining Room",
    8: "Dining Room",
    9: "Balcony",
    10: "Utility Room",
    11: "Study",
    12: "Nursery",
    13: "Recreation Room",
    14: "Storage Room",
    15: "Other",
}
