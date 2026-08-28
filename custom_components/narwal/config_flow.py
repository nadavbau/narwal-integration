"""Config flow for Narwal integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PRODUCT_KEY,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_UUID,
    DOMAIN,
    REGION_OPTIONS,
)
from .narwal_client import (
    NarwalAuthError,
    NarwalCloud,
    NarwalCloudError,
    NarwalDeviceInfo,
)

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default="us"): vol.In(REGION_OPTIONS),
    }
)


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._cloud: NarwalCloud | None = None
        self._email: str = ""
        self._password: str = ""
        self._region: str = "us"
        self._cloud_devices: list[dict[str, str]] = []
        self._device_infos: list[NarwalDeviceInfo] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Enter email, password, and region."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            self._region = user_input[CONF_REGION]

            cloud = NarwalCloud(region=self._region)

            try:
                session = await self.hass.async_add_executor_job(
                    cloud.login, self._email, self._password
                )
            except NarwalAuthError:
                errors["base"] = "invalid_auth"
            except NarwalCloudError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"
            else:
                self._cloud = cloud

                # Discover bound devices with their product key and MQTT
                # device name so the next step can be pre-filled.
                try:
                    self._device_infos = await self.hass.async_add_executor_job(
                        cloud.get_device_info_list
                    )
                except Exception:
                    _LOGGER.debug("Cloud device info discovery failed", exc_info=True)
                    self._device_infos = []

                # Fall back to friendly names from device messages for display
                if not self._device_infos:
                    try:
                        devices = await self.hass.async_add_executor_job(
                            cloud.get_devices
                        )
                        self._cloud_devices = [
                            {"device_id": d.device_id, "name": d.name}
                            for d in devices
                        ]
                    except Exception:
                        _LOGGER.debug("Cloud device discovery failed")

                return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_LOGIN_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Select or enter device details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            product_key = user_input[CONF_PRODUCT_KEY]
            device_name = user_input[CONF_DEVICE_NAME]

            await self.async_set_unique_id(f"{product_key}_{device_name}")
            self._abort_if_unique_id_configured()

            session = self._cloud.session  # type: ignore[union-attr]
            return self.async_create_entry(
                title=f"Narwal {self._find_device_friendly_name(device_name)}",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_REGION: self._region,
                    CONF_PRODUCT_KEY: product_key,
                    CONF_DEVICE_NAME: device_name,
                    CONF_USER_UUID: session.user_uuid,
                    CONF_ACCESS_TOKEN: session.access_token,
                    CONF_REFRESH_TOKEN: session.refresh_token,
                },
            )

        # Pre-fill with the first discovered device; the fields stay
        # editable so a different device can still be entered manually.
        default_product_key = "EHf6cRNRGT"
        default_device_name: Any = vol.UNDEFINED
        if self._device_infos:
            default_product_key = self._device_infos[0].product_key
            default_device_name = self._device_infos[0].device_name

        schema = vol.Schema(
            {
                vol.Required(CONF_PRODUCT_KEY, default=default_product_key): str,
                vol.Required(CONF_DEVICE_NAME, default=default_device_name): str,
            }
        )

        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "discovered_devices": self._format_discovered_devices()
            },
        )

    def _find_device_friendly_name(self, mqtt_device_name: str) -> str:
        for info in self._device_infos:
            if info.device_name == mqtt_device_name and info.name:
                return info.name
        for d in self._cloud_devices:
            return d["name"]
        return mqtt_device_name[:8]

    def _format_discovered_devices(self) -> str:
        if self._device_infos:
            lines = ["Devices found on your account (pre-filled below):"]
            for info in self._device_infos:
                label = info.name or info.device_name[:8]
                details = (
                    f"product key `{info.product_key}`, "
                    f"device name `{info.device_name}`"
                )
                if info.serial:
                    details += f", S/N {info.serial}"
                if info.firmware:
                    details += f", firmware {info.firmware}"
                lines.append(f"- **{label}**: {details}")
            return "\n".join(lines)
        if self._cloud_devices:
            lines = ["Found devices:"]
            for d in self._cloud_devices:
                lines.append(f"- **{d['name']}** (cloud ID: {d['device_id']})")
            lines.append(
                "\nNote: The MQTT device name is a 32-character hex string, "
                "**not** the cloud ID above. Enter it manually; it can be found "
                "in the Narwal app's network traffic."
            )
            return "\n".join(lines)
        return (
            "No devices could be discovered on this account. Enter the MQTT "
            "device name (32-character hex string) manually; it can be found "
            "in the Narwal app's network traffic."
        )
