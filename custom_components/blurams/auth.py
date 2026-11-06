"""Browser-assisted Blurams authentication."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
import secrets
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, ClientSession, ContentTypeError, CookieJar, web
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from .api import BluramsApi, BluramsCannotConnect, BluramsInvalidAuth
from .const import (
    API_URL,
    CAP_CDN_SOURCE,
    CAP_WIDGET_SRI,
    CAP_WIDGET_URL,
    CONF_BLURAMS_COOKIES,
    DOMAIN,
    HOSTNAME,
    WEB_URL,
)

_LOGGER = logging.getLogger(__name__)
_SESSION_TTL = 600
_MAX_LOGIN_ATTEMPTS = 5
_CAPTCHA_BASE = f"{API_URL}/captcha/"


class BluramsCaptchaSession:
    """Track an isolated browser challenge session."""

    def __init__(self, flow_id: str) -> None:
        """Create an isolated HTTP session for a single config flow."""
        self.flow_id = flow_id
        self.client = ClientSession(cookie_jar=CookieJar())
        self.expiry_task: asyncio.Task[None] | None = None
        self.login_attempts = 0

    async def async_close(self) -> None:
        """Close the isolated HTTP session."""
        if self.expiry_task:
            self.expiry_task.cancel()
            self.expiry_task = None
        await self.client.close()


def _sessions(hass: HomeAssistant) -> dict[str, BluramsCaptchaSession]:
    """Return active CAPTCHA sessions."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("captcha_sessions", {})


def async_register_views(hass: HomeAssistant) -> None:
    """Register the CAPTCHA views (once) before a config entry exists."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("captcha_views_registered"):
        return

    hass.http.register_view(BluramsCaptchaPageView(hass))
    hass.http.register_view(BluramsCaptchaProxyView(hass))
    hass.http.register_view(BluramsCaptchaScriptView())

    async def close_all_sessions(_event: Any) -> None:
        for nonce in list(_sessions(hass)):
            await async_remove_captcha_session(hass, nonce)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_all_sessions)
    data["captcha_views_registered"] = True


async def async_create_captcha_session(
    hass: HomeAssistant, flow_id: str, nonce: str
) -> None:
    """Create a temporary CAPTCHA session and schedule its cleanup."""
    sessions = _sessions(hass)
    captcha_session = BluramsCaptchaSession(flow_id)
    sessions[nonce] = captcha_session

    async def expire_session() -> None:
        await asyncio.sleep(_SESSION_TTL)
        if sessions.pop(nonce, None) is captcha_session:
            captcha_session.expiry_task = None
            await captcha_session.async_close()

    captcha_session.expiry_task = hass.async_create_task(
        expire_session(), f"blurams-captcha-{flow_id}"
    )


async def async_remove_captcha_session(
    hass: HomeAssistant, nonce: str
) -> BluramsCaptchaSession | None:
    """Remove and close a temporary CAPTCHA session."""
    captcha_session = _sessions(hass).pop(nonce, None)
    if captcha_session:
        await captcha_session.async_close()
    return captcha_session


def _cookies_from_jar(cookie_jar: CookieJar) -> dict[str, str]:
    """Return Blurams cookies suitable for the API client."""
    return {
        name: morsel.value
        for name, morsel in cookie_jar.filter_cookies(URL(WEB_URL)).items()
    }


def _captcha_error(message: str, status: HTTPStatus) -> web.Response:
    """Return an error response for the browser page."""
    return web.json_response({"error": message}, status=status)


def build_captcha_url(path: str) -> str | None:
    """Return the upstream CAPTCHA URL for a proxied path, or None if unsafe.

    The proxy must only ever talk to the Blurams CAPTCHA endpoint. ``urljoin``
    happily returns a different host when ``path`` is an absolute URL, so the
    joined result is verified instead of trusting the input.
    """
    if path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        return None
    url = urljoin(_CAPTCHA_BASE, path)
    parsed = URL(url)
    if (
        parsed.scheme != "https"
        or parsed.host != HOSTNAME
        or not url.startswith(_CAPTCHA_BASE)
    ):
        return None
    return url


def build_login_page(flow_id: str, nonce: str, script_nonce: str) -> str:
    """Return the HTML of the sign-in page."""
    captcha_endpoint = f"/api/blurams/captcha/{flow_id}/{nonce}/"
    auth_endpoint = f"/api/blurams/auth/{flow_id}/{nonce}"
    integrity = (
        f' integrity="{CAP_WIDGET_SRI}" crossorigin="anonymous"' if CAP_WIDGET_SRI else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in to Blurams</title>
  <style>
    body {{ font: 16px system-ui, sans-serif; max-width: 28rem; margin: 3rem auto; padding: 0 1rem; }}
    label {{ display: block; margin: 1rem 0 .35rem; }}
    input {{ box-sizing: border-box; width: 100%; padding: .7rem; }}
    button {{ margin-top: 1rem; padding: .7rem 1rem; }}
    #status {{ min-height: 1.5rem; }}
  </style>
  <script type="module" src="{CAP_WIDGET_URL}"{integrity}></script>
</head>
<body>
  <h1>Sign in to Blurams</h1>
  <p>Enter your Blurams credentials, complete the “I’m a human” check, then sign in.</p>
  <form id="login-form" data-auth-endpoint="{auth_endpoint}">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="username" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <cap-widget id="captcha" data-cap-api-endpoint="{captcha_endpoint}"
      data-cap-lang="en" data-cap-hidden-field-name="captchaVerifyParam"></cap-widget>
    <button id="submit" type="submit" disabled>Sign in</button>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
  <script nonce="{script_nonce}">window.CAP_SCRIPT_NONCE = "{script_nonce}";</script>
  <script src="/blurams/captcha.js" defer></script>
</body>
</html>"""


def build_csp(script_nonce: str) -> str:
    """Return the Content-Security-Policy of the sign-in page."""
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "form-action 'self'; "
        # Only the pinned Cap packages may load, not the whole CDN.
        # 'unsafe-eval' is kept because the widget compiles WebAssembly; if you
        # confirm the widget works without it, try 'wasm-unsafe-eval' instead.
        f"script-src 'self' {CAP_CDN_SOURCE} 'nonce-{script_nonce}' 'unsafe-eval'; "
        f"connect-src 'self' {CAP_CDN_SOURCE}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "worker-src 'self' blob:; "
        "frame-src 'self' blob:"
    )


class BluramsCaptchaPageView(HomeAssistantView):
    """Serve the browser page for completing Blurams CAPTCHA."""

    url = "/api/blurams/auth/{flow_id}/{nonce}"
    name = "api:blurams:auth"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, flow_id: str, nonce: str) -> web.Response:
        """Serve the login page."""
        captcha_session = _sessions(self.hass).get(nonce)
        if captcha_session is None or captcha_session.flow_id != flow_id:
            return _captcha_error(
                "This authentication page has expired.", HTTPStatus.GONE
            )

        script_nonce = secrets.token_urlsafe(16)
        return web.Response(
            text=build_login_page(flow_id, nonce, script_nonce),
            content_type="text/html",
            headers={
                "Content-Security-Policy": build_csp(script_nonce),
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    async def post(
        self, request: web.Request, flow_id: str, nonce: str
    ) -> web.Response:
        """Authenticate with the submitted credentials and CAPTCHA token."""
        captcha_session = _sessions(self.hass).get(nonce)
        if captcha_session is None or captcha_session.flow_id != flow_id:
            return _captcha_error(
                "This authentication page has expired.", HTTPStatus.GONE
            )

        captcha_session.login_attempts += 1
        if captcha_session.login_attempts > _MAX_LOGIN_ATTEMPTS:
            await async_remove_captcha_session(self.hass, nonce)
            return _captcha_error(
                "Too many attempts. Start the setup again.",
                HTTPStatus.TOO_MANY_REQUESTS,
            )

        try:
            user_input: dict[str, Any] = await request.json()
        except (ValueError, web.HTTPBadRequest, ContentTypeError):
            return _captcha_error("Invalid sign-in request.", HTTPStatus.BAD_REQUEST)
        if not isinstance(user_input, dict):
            return _captcha_error("Invalid sign-in request.", HTTPStatus.BAD_REQUEST)

        email = user_input.get("email")
        password = user_input.get("password")
        captcha_token = user_input.get("captchaVerifyParam")
        if (
            not isinstance(email, str)
            or not email
            or not isinstance(password, str)
            or not password
            or not isinstance(captcha_token, str)
            or not captcha_token
        ):
            return _captcha_error(
                "Complete all fields and the human check.", HTTPStatus.BAD_REQUEST
            )

        api = BluramsApi(captcha_session.client)
        try:
            await api.async_login(email, password, captcha_verify_param=captcha_token)
            await api.async_get_devices()
        except BluramsInvalidAuth:
            return _captcha_error(
                "Blurams rejected these credentials or the CAPTCHA expired. Try again.",
                HTTPStatus.UNAUTHORIZED,
            )
        except BluramsCannotConnect:
            return _captcha_error(
                "Home Assistant could not connect to Blurams. Try again.",
                HTTPStatus.BAD_GATEWAY,
            )

        cookies = _cookies_from_jar(captcha_session.client.cookie_jar)
        if not cookies:
            return _captcha_error(
                "Blurams did not return an authenticated session.",
                HTTPStatus.BAD_GATEWAY,
            )

        # The password is only used above to sign in; it is never stored.
        try:
            result = await self.hass.config_entries.flow.async_configure(
                flow_id,
                {CONF_USERNAME: email, CONF_BLURAMS_COOKIES: cookies},
            )
        except UnknownFlow:
            return _captcha_error(
                "The setup flow is no longer active. Start setup again.",
                HTTPStatus.GONE,
            )
        if (
            result.get("type") is FlowResultType.EXTERNAL_STEP_DONE
            or result.get("type") is FlowResultType.CREATE_ENTRY
            or (
                result.get("type") is FlowResultType.ABORT
                and result.get("reason") == "reauth_successful"
            )
        ):
            await async_remove_captcha_session(self.hass, nonce)
            return web.json_response({"success": True})

        return _captcha_error(
            "The setup flow is no longer active. Start setup again.",
            HTTPStatus.GONE,
        )


class BluramsCaptchaProxyView(HomeAssistantView):
    """Proxy CAPTCHA requests while keeping vendor cookies on the server."""

    url = "/api/blurams/captcha/{flow_id}/{nonce}/{path:.*}"
    name = "api:blurams:captcha"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(
        self, request: web.Request, flow_id: str, nonce: str, path: str
    ) -> web.Response:
        """Proxy a CAPTCHA GET request."""
        return await self._async_proxy(request, flow_id, nonce, path)

    async def post(
        self, request: web.Request, flow_id: str, nonce: str, path: str
    ) -> web.Response:
        """Proxy a CAPTCHA POST request."""
        return await self._async_proxy(request, flow_id, nonce, path)

    async def _async_proxy(
        self, request: web.Request, flow_id: str, nonce: str, path: str
    ) -> web.Response:
        """Forward a browser CAPTCHA request to Blurams."""
        captcha_session = _sessions(self.hass).get(nonce)
        if captcha_session is None or captcha_session.flow_id != flow_id:
            return _captcha_error(
                "This authentication page has expired.", HTTPStatus.GONE
            )

        url = build_captcha_url(path)
        if url is None:
            return _captcha_error("Invalid CAPTCHA request.", HTTPStatus.BAD_REQUEST)

        headers = {
            "Origin": WEB_URL,
            "Referer": f"{WEB_URL}/login",
        }
        if content_type := request.headers.get("Content-Type"):
            headers["Content-Type"] = content_type
        if accept := request.headers.get("Accept"):
            headers["Accept"] = accept

        try:
            async with captcha_session.client.request(
                request.method,
                url,
                params=request.query,
                data=await request.read() if request.can_read_body else None,
                headers=headers,
            ) as response:
                body = await response.read()
                return web.Response(
                    body=body,
                    status=response.status,
                    content_type=response.content_type,
                )
        except ClientError:
            _LOGGER.warning("Unable to proxy Blurams CAPTCHA request")
            return _captcha_error(
                "Home Assistant could not reach Blurams CAPTCHA services.",
                HTTPStatus.BAD_GATEWAY,
            )


class BluramsCaptchaScriptView(HomeAssistantView):
    """Serve the small local CAPTCHA page script."""

    url = "/blurams/captcha.js"
    name = "api:blurams:captcha_script"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the local browser script."""
        return web.Response(
            text=_CAPTCHA_SCRIPT,
            content_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )


_CAPTCHA_SCRIPT = """
const form = document.querySelector("#login-form");
const captcha = document.querySelector("#captcha");
const submit = document.querySelector("#submit");
const status = document.querySelector("#status");
let captchaToken = "";

captcha.addEventListener("solve", (event) => {
  captchaToken = event.detail.token || "";
  submit.disabled = !captchaToken;
});
captcha.addEventListener("reset", () => {
  captchaToken = "";
  submit.disabled = true;
});
captcha.addEventListener("error", (event) => {
  captchaToken = "";
  submit.disabled = true;
  const detail = event.detail || {};
  status.textContent = detail.message
    ? `The human check failed (${detail.code || "unknown"}): ${detail.message}`
    : "The human check failed. Please complete it again.";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!captchaToken) return;
  submit.disabled = true;
  status.textContent = "Signing in to Blurams…";
  try {
    const response = await fetch(form.dataset.authEndpoint, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        email: form.elements.email.value,
        password: form.elements.password.value,
        captchaVerifyParam: captchaToken,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Sign-in failed.");
    status.textContent = "Blurams is connected. You can close this window.";
    form.hidden = true;
  } catch (error) {
    status.textContent = error.message || "Sign-in failed. Please try again.";
    captchaToken = "";
    submit.disabled = true;
    captcha.reset();
  }
});
"""
