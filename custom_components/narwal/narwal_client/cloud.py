"""Narwal Cloud REST API client for authentication and device discovery."""

from __future__ import annotations

import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)

API_REGIONS: dict[str, str] = {
    "us": "us-app.narwaltech.com",
    "il": "il-app.narwaltech.com",
    "eu": "eu-app.narwaltech.com",
    "cn": "cn-app.narwaltech.com",
}

MQTT_REGIONS: dict[str, str] = {
    "us": "us-01.mqtt.narwaltech.com",
    "il": "us-01.mqtt.narwaltech.com",
    "eu": "eu-01.mqtt.narwaltech.com",
    "cn": "cn-mqtt.narwaltech.com",
}

LOGIN_PATH = "/user-authentication-server/v2/login/loginByEmail"
REFRESH_PATH = "/user-authentication-server/v1/token/refresh"
USER_INFO_PATH = "/user-server/v2/user/getUserInfo"
DEVICE_MESSAGES_PATH = "/app-message-server/v1/device-message/listPage"


class NarwalCloudError(Exception):
    """General cloud API error."""


class NarwalAuthError(NarwalCloudError):
    """Authentication error (bad credentials)."""


@dataclass
class NarwalDevice:
    """A Narwal device discovered from the cloud."""

    device_id: str
    name: str
    product_pic: str = ""


@dataclass
class NarwalCloudSession:
    """Stores authentication tokens and session data."""

    access_token: str = ""
    refresh_token: str = ""
    user_uuid: str = ""
    email: str = ""
    region: str = "us"
    token_expiry: float = 0.0

    @property
    def is_token_expired(self) -> bool:
        return time.time() > self.token_expiry - 300  # 5 min buffer


class NarwalCloud:
    """Client for the Narwal cloud REST API."""

    def __init__(self, region: str = "us") -> None:
        self.region = region
        self.session = NarwalCloudSession(region=region)
        self._api_host = API_REGIONS.get(region, API_REGIONS["us"])

    @property
    def mqtt_broker(self) -> str:
        return MQTT_REGIONS.get(self.region, MQTT_REGIONS["us"])

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"https://{self._api_host}{path}"
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if auth and self.session.access_token:
            headers["Auth-Token"] = self.session.access_token

        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=headers, method=method)

        try:
            ctx = ssl.create_default_context()
            with urlopen(req, context=ctx, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            raise NarwalCloudError(
                f"HTTP {e.code} for {path}: {body_text}"
            ) from e
        except URLError as e:
            raise NarwalCloudError(f"Connection error for {path}: {e}") from e

    def login(self, email: str, password: str) -> NarwalCloudSession:
        """Log in with email and password. Returns the session."""
        resp = self._request(
            "POST",
            LOGIN_PATH,
            body={"email": email, "password": password},
            auth=False,
        )

        if resp.get("code") != 0:
            msg = resp.get("msg", "Login failed")
            err_code = resp.get("err_code", 0)
            if err_code in (100202400, 100202600):
                raise NarwalAuthError(msg)
            raise NarwalCloudError(f"Login error ({err_code}): {msg}")

        result = resp["result"]
        self.session.access_token = result["token"]
        # Login returns "refresh_token", refresh endpoint returns "refreshToken"
        self.session.refresh_token = (
            result.get("refresh_token") or result.get("refreshToken") or result["token"]
        )
        self.session.email = email
        self.session.region = self.region
        self._update_token_expiry()

        self.session.user_uuid = result.get("uuid", "")
        if not self.session.user_uuid:
            self.session.user_uuid = self._extract_uuid_from_jwt()

        _LOGGER.debug("Logged in as %s (uuid=%s)", email, self.session.user_uuid)
        return self.session

    def login_with_refresh_token(self, refresh_token: str) -> NarwalCloudSession:
        """Re-authenticate using a refresh token."""
        resp = self._request(
            "POST",
            REFRESH_PATH,
            body={"refreshToken": refresh_token},
            auth=False,
        )

        if resp.get("code") != 0:
            raise NarwalAuthError(
                f"Token refresh failed: {resp.get('msg', 'unknown')}"
            )

        result = resp["result"]
        self.session.access_token = result["token"]
        self.session.refresh_token = result["refreshToken"]
        self._update_token_expiry()

        if not self.session.user_uuid:
            self.session.user_uuid = self._extract_uuid_from_jwt()

        _LOGGER.debug("Token refreshed successfully")
        return self.session

    def refresh_token(self) -> NarwalCloudSession:
        """Refresh the access token using the current refresh token."""
        if not self.session.refresh_token:
            raise NarwalAuthError("No refresh token available")
        return self.login_with_refresh_token(self.session.refresh_token)

    def get_user_info(self) -> dict[str, Any]:
        """Get user profile information."""
        resp = self._request("GET", USER_INFO_PATH)
        if resp.get("code") != 0:
            raise NarwalCloudError(f"Get user info failed: {resp.get('msg')}")
        return resp.get("result", {})

    def get_devices(self) -> list[NarwalDevice]:
        """Discover devices by scanning recent device messages.

        The message API returns messages per device with device_id and device_name,
        which lets us find all bound devices without a dedicated device-list endpoint.
        """
        resp = self._request(
            "GET",
            f"{DEVICE_MESSAGES_PATH}?pageNum=1&pageSize=50",
        )
        if resp.get("code") != 0:
            _LOGGER.warning("Device message query failed: %s", resp.get("msg"))
            return []

        messages = resp.get("result", {}).get("message_list", [])
        seen: dict[str, NarwalDevice] = {}
        for msg in messages:
            did = msg.get("device_id") or msg.get("robot_id")
            if not did:
                continue
            did = str(did)
            if did not in seen:
                seen[did] = NarwalDevice(
                    device_id=did,
                    name=msg.get("robot_name", msg.get("device_name", did)),
                    product_pic=msg.get("product_pic", ""),
                )

        return list(seen.values())

    def ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self.session.is_token_expired:
            self.refresh_token()
        return self.session.access_token

    def _extract_uuid_from_jwt(self) -> str:
        """Extract uuid from the JWT access token payload."""
        import base64

        try:
            parts = self.session.access_token.split(".")
            if len(parts) != 3:
                return ""
            padding = "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            return payload.get("uuid", "")
        except Exception:
            return ""

    def _update_token_expiry(self) -> None:
        """Decode the JWT to extract the expiry timestamp."""
        import base64

        try:
            parts = self.session.access_token.split(".")
            if len(parts) != 3:
                self.session.token_expiry = time.time() + 3600
                return
            padding = "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            self.session.token_expiry = payload.get("exp", time.time() + 3600)
        except Exception:
            self.session.token_expiry = time.time() + 3600
