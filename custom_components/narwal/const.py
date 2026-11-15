"""Constants for the Narwal integration."""

from homeassistant.const import Platform

from .narwal_client.const import PRODUCT_KEY_FREO_X_PLUS, CleanMode, FanLevel, MopHumidity

DOMAIN = "narwal"

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Config entry data keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_PRODUCT_KEY = "product_key"
CONF_DEVICE_NAME = "device_name"
CONF_USER_UUID = "user_uuid"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"

# Legacy keys (kept for migration from manual config)
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_CLIENT_ID = "mqtt_client_id"

# Cached room list so start_clean works while the vacuum sleeps.
CONF_ROOMS_CACHE = "rooms_cache"

REGION_OPTIONS = {
    "us": "United States",
    "il": "Israel",
    "eu": "Europe",
    "cn": "China",
}

# Entities for base-station features. The Freo X Plus base only charges the
# robot (no auto-empty, mop washing/drying, water tanks or detergent), although
# its firmware still reports Ultra-style values for all of them.
STATION_ENTITY_KEYS = frozenset({
    "auto_empty", "quiet_auto_empty", "empty_dustbin",
    "wash_mop", "dry_mop", "wash_and_dry_mop", "mop_dry_strength", "mop_wash_frequency",
    "hot_water_wash", "clean_water_tank", "dirty_water_tank",
    "detergent", "auto_detergent",
})
# Entity keys not created for a product key.
UNSUPPORTED_ENTITY_KEYS: dict[str, frozenset[str]] = {
    PRODUCT_KEY_FREO_X_PLUS: STATION_ENTITY_KEYS,
}

# Fan speed names shown in HA (order = app order).
FAN_SPEED_MAP: dict[str, FanLevel] = {
    "Quiet": FanLevel.QUIET,
    "Normal": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Max": FanLevel.MAX,
}
FAN_SPEED_LIST = list(FAN_SPEED_MAP)
FAN_SPEED_REVERSE = {v: k for k, v in FAN_SPEED_MAP.items()}

# Clean mode names are also used by the Lovelace card and send_command.
CLEAN_MODE_MAP: dict[str, CleanMode] = {
    "Vacuum & Mop": CleanMode.VACUUM_AND_MOP,
    "Vacuum then Mop": CleanMode.VACUUM_THEN_MOP,
    "Vacuum Only": CleanMode.VACUUM_ONLY,
    "Mop Only": CleanMode.MOP_ONLY,
}
CLEAN_MODE_LIST = list(CLEAN_MODE_MAP)
CLEAN_MODE_REVERSE = {v: k for k, v in CLEAN_MODE_MAP.items()}

MOP_HUMIDITY_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
