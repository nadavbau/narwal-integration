"""Config flow for Narwal integration.

1. Log in with the Narwal app's email, password and region.
2. The robots bound to the account are listed from the Narwal cloud; with
   one robot the entry is created right away, with several the user picks
   one. Manual entry of product key + device name is only a fallback when
   the list can't be fetched.
"""

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
from .narwal_client import NarwalAuthError, NarwalCloud, NarwalCloudError, NarwalDevice

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default="us"): vol.In(REGION_OPTIONS),
    }
)

DEFAULT_PRODUCT_KEY = "EHf6cRNRGT"  # Freo X Ultra


def _unique_id(product_key: str, device_name: str) -> str:
    return f"{product_key}_{device_name}"


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal."""

    VERSION = 2

    def __init__(self) -> None:
        self._cloud: NarwalCloud | None = None
        self._email = ""
        self._password = ""
        self._region = "us"
        self._devices: list[NarwalDevice] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: email, password and region."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            self._region = user_input[CONF_REGION]
            cloud = NarwalCloud(region=self._region)
            try:
                await self.hass.async_add_executor_job(cloud.login, self._email, self._password)
            except NarwalAuthError:
                errors["base"] = "invalid_auth"
            except NarwalCloudError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"
            else:
                self._cloud = cloud
                return await self._async_after_login()

        return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

    async def _async_after_login(self) -> ConfigFlowResult:
        assert self._cloud is not None
        try:
            devices = await self.hass.async_add_executor_job(self._cloud.get_devices)
        except NarwalCloudError as err:
            _LOGGER.warning("Could not list Narwal robots, falling back to manual entry: %s", err)
            return await self.async_step_manual()

        configured = self._async_current_ids(include_ignore=False)
        self._devices = [
            d for d in devices if _unique_id(d.product_key, d.device_name) not in configured
        ]
        if not devices:
            return self.async_abort(reason="no_devices")
        if not self._devices:
            return self.async_abort(reason="already_configured")
        if len(self._devices) == 1:
            return await self._async_create(self._devices[0])
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2 (several robots): choose one."""
        if user_input is not None:
            device = next(d for d in self._devices if d.device_name == user_input[CONF_DEVICE_NAME])
            return await self._async_create(device)

        options = {d.device_name: d.label for d in self._devices}
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_NAME): vol.In(options)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fallback: enter the product key and MQTT device name by hand."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_name = user_input[CONF_DEVICE_NAME].strip().lower()
            if len(device_name) != 32 or any(c not in "0123456789abcdef" for c in device_name):
                errors[CONF_DEVICE_NAME] = "invalid_device_name"
            else:
                return await self._async_create(NarwalDevice(
                    device_name=device_name,
                    product_key=user_input[CONF_PRODUCT_KEY].strip(),
                ))

        schema = vol.Schema(
            {
                vol.Required(CONF_PRODUCT_KEY, default=DEFAULT_PRODUCT_KEY): str,
                vol.Required(CONF_DEVICE_NAME): str,
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def _async_create(self, device: NarwalDevice) -> ConfigFlowResult:
        await self.async_set_unique_id(_unique_id(device.product_key, device.device_name))
        self._abort_if_unique_id_configured()
        session = self._cloud.session  # type: ignore[union-attr]
        return self.async_create_entry(
            title=device.name or device.model or f"Narwal {device.device_name[:8]}",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_REGION: self._region,
                CONF_PRODUCT_KEY: device.product_key,
                CONF_DEVICE_NAME: device.device_name,
                CONF_USER_UUID: session.user_uuid,
                CONF_ACCESS_TOKEN: session.access_token,
                CONF_REFRESH_TOKEN: session.refresh_token,
            },
        )
