"""Blurams cloud API client."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .const import API_URL, HOSTNAME, WEB_URL
from .live import LiveParams

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = ClientTimeout(total=30)
_SUCCESS_CODES = (0, "0", 200, "200")
_AUTH_CODES = (401, "401", 403, "403")
_AUTH_MESSAGE_HINTS = ("login", "log in", "session", "token", "unauthor", "expired")


class BluramsApiError(Exception):
    """Base exception for Blurams API errors."""


class BluramsInvalidAuth(BluramsApiError):
    """Raised when Blurams rejects the credentials or the session."""


class BluramsCannotConnect(BluramsApiError):
    """Raised when Blurams cannot be reached or answers unexpectedly."""


def _cipher_key() -> bytes:
    """Return the AES key used by the Blurams web client."""
    return HOSTNAME.encode()[:32].ljust(32, b"\0")


def encrypt_payload(data: dict[str, Any]) -> str:
    """Encrypt a JSON payload for the Blurams API."""
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    # The Blurams web client uses a fixed zero IV; the server requires it.
    encryptor = Cipher(algorithms.AES(_cipher_key()), modes.CBC(b"\0" * 16)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode()


def decrypt_payload(value: str) -> dict[str, Any]:
    """Decrypt a JSON payload returned by the Blurams API."""
    decryptor = Cipher(algorithms.AES(_cipher_key()), modes.CBC(b"\0" * 16)).decryptor()
    padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plaintext)


def _is_success(response: dict[str, Any]) -> bool:
    """Return whether an API response reports success."""
    return response.get("code") in _SUCCESS_CODES


def _is_auth_failure(response: dict[str, Any]) -> bool:
    """Return whether a failed API response means the session is not valid."""
    if response.get("code") in _AUTH_CODES:
        return True
    message = str(response.get("msg", "")).lower()
    return any(hint in message for hint in _AUTH_MESSAGE_HINTS)


class BluramsApi:
    """Minimal client for the Blurams web API."""

    def __init__(self, session: ClientSession) -> None:
        """Initialize the API client."""
        self._session = session
        self._cookies: dict[str, str] | None = None

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Set cookies for API requests."""
        self._cookies = cookies

    async def async_login(
        self,
        email: str,
        password: str,
        captcha: str | None = None,
        captcha_verify_param: str | None = None,
    ) -> None:
        """Log in and retain the session cookie in the session's cookie jar."""
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "captcha": captcha or "",
            "remberMe": False,
        }
        if captcha_verify_param:
            payload["captchaVerifyParam"] = captcha_verify_param

        response = await self._async_request("login", payload)
        if not isinstance(response, dict) or not _is_success(response):
            message = response.get("msg") if isinstance(response, dict) else None
            raise BluramsInvalidAuth(message or "Login failed")

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the devices associated with the logged-in account."""
        response = await self._async_request("device/list", {"update": True})
        if not isinstance(response, dict):
            raise BluramsCannotConnect("Unexpected device list response")
        if not _is_success(response):
            message = str(response.get("msg", "Device list request failed"))
            if _is_auth_failure(response):
                raise BluramsInvalidAuth(message)
            raise BluramsCannotConnect(message)
        try:
            devices = response["data"]["devicelist"]
        except (KeyError, TypeError) as err:
            raise BluramsCannotConnect("Device list missing from response") from err
        if not isinstance(devices, list):
            raise BluramsCannotConnect("Device list has an unexpected format")
        return devices

    async def async_get_camera_image(self, path: str) -> bytes:
        """Download a camera thumbnail using the current session."""
        url = urljoin(WEB_URL, path)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise BluramsCannotConnect("Unsupported thumbnail URL")
        # Never send the Blurams session cookies to any other host.
        cookies = self._cookies if parsed.hostname == HOSTNAME else None
        try:
            async with self._session.get(
                url, cookies=cookies or {}, timeout=_REQUEST_TIMEOUT
            ) as response:
                if response.status in (401, 403):
                    raise BluramsInvalidAuth("Thumbnail request was rejected")
                response.raise_for_status()
                return await response.read()
        except (ClientError, TimeoutError) as err:
            raise BluramsCannotConnect from err

    async def async_set_camera_power(self, device_id: str, powered: bool) -> None:
        """Set the camera power state."""
        payload = {
            "deviceId": device_id,
            "path": "profile/general/status",
            "element": f"<status>{'On' if powered else 'OffByManual'}</status>",
        }
        try:
            async with self._session.post(
                f"{API_URL}/device/saveSetting",
                data={"__reqJSONStr": encrypt_payload(payload), "_csrf_header": ""},
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": (
                        f"{WEB_URL}/api/device/innerLive?"
                        f"gTime={time.time_ns() // 1_000_000}"
                    ),
                    "X-Requested-With": "XMLHttpRequest",
                },
                cookies=self._cookies or {},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                body = await response.json(content_type=None)
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise BluramsInvalidAuth from err
            raise BluramsCannotConnect from err
        except (ClientError, TimeoutError, ValueError) as err:
            raise BluramsCannotConnect from err

        if isinstance(body, dict) and "__respJSONStr" in body:
            try:
                result = decrypt_payload(body["__respJSONStr"])
            except (ValueError, TypeError) as err:
                raise BluramsCannotConnect from err
        else:
            result = body
        if not isinstance(result, dict):
            _LOGGER.warning(
                "Blurams camera power request returned an unexpected response type: %s",
                type(result).__name__,
            )
            raise BluramsCannotConnect
        if result.get("success") is True or _is_success(result):
            return

        _LOGGER.warning(
            "Blurams camera power request was not successful (response keys: %s)",
            tuple(result),
        )
        if _is_auth_failure(result):
            raise BluramsInvalidAuth
        raise BluramsCannotConnect

    async def async_get_live_params(self, device: dict[str, Any]) -> LiveParams:
        """Fetch the live stream parameters for a device.

        This mirrors the web player: it POSTs the live page form, and the
        returned HTML embeds the one-time stream token, the relay server and
        the player host that the WebSocket connects to.
        """
        device_id = device["deviceid"]
        form = {
            "deviceAddSource": device.get("deviceAddSource") or "",
            "cloudDid": str(device.get("cloudDid") or ""),
            "modelId": device.get("modelId") or "",
            "shareid": str(device.get("shareid") or ""),
            "did": str(device.get("did") or ""),
            "deviceId": device_id,
            "channelNo": "null",
        }
        referer = (
            f"{WEB_URL}/device/live?deviceId={device_id}"
            f"&did={form['did']}&shareid={form['shareid']}"
            f"&modelId={form['modelId']}&cloudDid={form['cloudDid']}"
        )
        try:
            async with self._session.post(
                f"{API_URL}/device/innerLive",
                params={"gTime": time.time_ns() // 1_000_000},
                data=form,
                headers={"Referer": referer},
                cookies=self._cookies or {},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                html = await response.text()
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise BluramsInvalidAuth from err
            raise BluramsCannotConnect from err
        except (ClientError, TimeoutError) as err:
            raise BluramsCannotConnect from err

        token = re.search(r"token\s*=\s*'([^']+)'", html)
        player_server = re.search(r"html5PlayerServer\s*:\s*'([^']+)'", html)
        relay_server = re.search(r"relayServer\s*:\s*'([^']*)'", html)
        if not token or not player_server:
            _LOGGER.debug(
                "Blurams live page for %s did not contain stream parameters",
                device_id,
            )
            raise BluramsCannotConnect("Live stream parameters not found")
        host = urlparse(player_server.group(1)).hostname or ""
        return LiveParams(
            vrs_host=host,
            token=token.group(1),
            device_id=device_id,
            product_key=str(device.get("productkey") or ""),
            relay_server=relay_server.group(1) if relay_server else "",
            has_audio=True,
        )

    async def _async_request(self, endpoint: str, payload: dict[str, Any]) -> Any:
        """Send an encrypted API request and decode its response."""
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            async with self._session.post(
                f"{API_URL}/{endpoint}",
                json={"__reqJSONStr": encrypt_payload(payload)},
                headers=headers,
                cookies=self._cookies or {},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                body = await response.json(content_type=None)
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise BluramsInvalidAuth from err
            raise BluramsCannotConnect from err
        except (ClientError, TimeoutError, ValueError) as err:
            raise BluramsCannotConnect from err

        if isinstance(body, dict) and "__respJSONStr" in body:
            try:
                return decrypt_payload(body["__respJSONStr"])
            except (ValueError, TypeError) as err:
                raise BluramsCannotConnect from err
        return body
