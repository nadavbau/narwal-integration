"""Protocol constants, enums, and MQTT topic definitions for Narwal vacuum."""

from enum import IntEnum

MQTT_BROKER = "us-01.mqtt.narwaltech.com"
MQTT_PORT = 8883

COMMAND_RESPONSE_TIMEOUT = 10.0

# --- Command topics (client → robot) ---
TOPIC_CMD_YELL = "common/yell"
TOPIC_CMD_REBOOT = "common/reboot"
TOPIC_CMD_SHUTDOWN = "common/shutdown"
TOPIC_CMD_GET_DEVICE_INFO = "common/get_device_info"
TOPIC_CMD_GET_FEATURE_LIST = "common/get_feature_list"
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
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"

# Cleaning
TOPIC_CMD_START_CLEAN = "clean/start_clean"
TOPIC_CMD_START_PLAN = "clean/plan/start"
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

# --- Status topics (robot → client, pushed) ---
TOPIC_WORKING_STATUS = "status/working_status"
TOPIC_ROBOT_BASE_STATUS = "status/robot_base_status"
TOPIC_UPGRADE_STATUS = "status/upgrade_status"


class WorkingStatus(IntEnum):
    UNKNOWN = 0
    STANDBY = 1
    CLEANING = 4
    CLEANING_ALT = 5
    DOCKED = 10
    CHARGED = 14
    ERROR = 99


class FanLevel(IntEnum):
    QUIET = 0
    NORMAL = 1
    STRONG = 2
    MAX = 3


class MopHumidity(IntEnum):
    DRY = 0
    NORMAL = 1
    WET = 2


class CommandResult(IntEnum):
    SUCCESS = 1
    NOT_APPLICABLE = 2
    CONFLICT = 3
