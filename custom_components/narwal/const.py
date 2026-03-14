"""Constants for the Narwal integration."""

from homeassistant.const import Platform

DOMAIN = "narwal"

PLATFORMS = [Platform.VACUUM, Platform.SENSOR, Platform.BUTTON, Platform.CAMERA, Platform.SELECT]

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

REGION_OPTIONS = {
    "us": "United States",
    "il": "Israel",
    "eu": "Europe",
    "cn": "China",
}

FAN_SPEED_LIST = ["Quiet", "Normal", "Strong", "Max"]
FAN_SPEED_MAP = {
    "Quiet": 0,
    "Normal": 1,
    "Strong": 2,
    "Max": 3,
}
FAN_SPEED_REVERSE = {v: k for k, v in FAN_SPEED_MAP.items()}

CLEAN_MODE_LIST = [
    "Vacuum & Mop",
    "Vacuum then Mop",
    "Vacuum Only",
    "Mop Only",
]
CLEAN_MODE_MAP = {
    "Vacuum & Mop": 1,
    "Vacuum then Mop": 2,
    "Vacuum Only": 3,
    "Mop Only": 4,
}
CLEAN_MODE_REVERSE = {v: k for k, v in CLEAN_MODE_MAP.items()}
