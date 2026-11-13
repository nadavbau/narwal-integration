"""Config flow: robots are discovered from the account after login."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client import NarwalAuthError, NarwalCloudError
from custom_components.narwal.narwal_client.cloud import NarwalCloud, NarwalDevice

LOGIN = {"email": "user@example.com", "password": "secret", "region": "il"}
ROBOT_A = NarwalDevice("a" * 32, "EHf6cRNRGT", "Freo", "Narwal Freo X Ultra", "v01.09.01.00")
ROBOT_B = NarwalDevice("b" * 32, "EHf6cRNRGT", "Upstairs", "Narwal Freo X Ultra")

# A real-shaped getDeviceInfoList response (values replaced).
DEVICE_LIST_RESPONSE = {
    "code": 0,
    "msg": "ok",
    "result": {
        "deviceInfoList": [{
            "deviceId": "a" * 32,
            "productId": "EHf6cRNRGT",
            "robotName": "Freo",
            "firmwareVersion": "v01.09.01.00",
            "snNumber": "SN",
        }],
        "productInfoList": [{"productId": "EHf6cRNRGT", "productModel": "Narwal Freo X Ultra", "productGroup": "J4"}],
    },
}


def _fake_login(self: NarwalCloud, email: str, password: str):
    self.session.access_token = "access"
    self.session.refresh_token = "refresh"
    self.session.user_uuid = "0" * 32
    return self.session


@pytest.fixture(autouse=True)
def no_setup():
    """Don't actually set up the entry after the flow creates it."""
    with patch("custom_components.narwal.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant, devices=None, error=None):
    def get_devices(self):
        if error:
            raise error
        return devices

    with (
        patch.object(NarwalCloud, "login", _fake_login),
        patch.object(NarwalCloud, "get_devices", get_devices),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
        return await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)


def test_device_list_parsing() -> None:
    cloud = NarwalCloud(region="il")
    with patch.object(NarwalCloud, "_request", return_value=DEVICE_LIST_RESPONSE) as req:
        devices = cloud.get_devices()
    assert req.call_args.args[:2] == ("GET", "/user-device-platform-server/device-info/getDeviceInfoList")
    assert devices == [NarwalDevice("a" * 32, "EHf6cRNRGT", "Freo", "Narwal Freo X Ultra", "v01.09.01.00")]
    assert devices[0].label == "Freo (Narwal Freo X Ultra)"


async def test_single_robot_is_added_without_questions(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[ROBOT_A])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Freo"
    assert result["data"]["device_name"] == "a" * 32
    assert result["data"]["product_key"] == "EHf6cRNRGT"
    assert result["data"]["access_token"] == "access"
    assert result["result"].unique_id == "EHf6cRNRGT_" + "a" * 32


async def test_several_robots_pick_one(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_name": "b" * 32})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Upstairs"
    assert result["data"]["device_name"] == "b" * 32


async def test_already_added_robots_are_skipped(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="EHf6cRNRGT_" + "a" * 32).add_to_hass(hass)
    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    # Only robot B is left, so it is added directly.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["device_name"] == "b" * 32

    result = await _start(hass, devices=[ROBOT_A, ROBOT_B])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_no_robots_on_account(hass: HomeAssistant) -> None:
    result = await _start(hass, devices=[])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices"


async def test_device_list_failure_falls_back_to_manual(hass: HomeAssistant) -> None:
    result = await _start(hass, error=NarwalCloudError("boom"))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"

    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"product_key": "EHf6cRNRGT", "device_name": "not-hex"}
    )
    assert bad["errors"] == {"device_name": "invalid_device_name"}

    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"product_key": "EHf6cRNRGT", "device_name": "C" * 32}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"]["device_name"] == "c" * 32


async def test_wrong_password(hass: HomeAssistant) -> None:
    def bad_login(self, email, password):
        raise NarwalAuthError("Account or password is incorrect")

    with patch.object(NarwalCloud, "login", bad_login):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "invalid_auth"}


def test_wrong_password_reply_is_an_auth_error() -> None:
    """Narwal answers bad credentials with code -1 (seen live)."""
    cloud = NarwalCloud(region="il")
    reply = {"code": -1, "msg": "Account or password is incorrect, please check and try again"}
    with patch.object(NarwalCloud, "_request", return_value=reply), pytest.raises(NarwalAuthError):
        cloud.login("user@example.com", "wrong")
