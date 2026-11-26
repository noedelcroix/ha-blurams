"""Browser-assisted Blurams authentication."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ContentTypeError, CookieJar, web
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from .api import BluramsApi, BluramsCannotConnect, BluramsInvalidAuth
from .const import (
    ALIYUN_API_SOURCE,
    ALIYUN_SDK_SOURCE,
    ALIYUN_SDK_SRI,
    ALIYUN_SDK_URL,
    API_URL,
    CONF_BLURAMS_COOKIES,
    DOMAIN,
    WEB_URL,
)

_LOGGER = logging.getLogger(__name__)
_SESSION_TTL = 600
_MAX_LOGIN_ATTEMPTS = 5
_CAPTCHA_CONFIG_URL = f"{API_URL}/aliyunCaptcha/config"


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
    hass.http.register_view(BluramsCaptchaConfigView(hass))
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


def build_login_page(flow_id: str, nonce: str) -> str:
    """Return the HTML of the sign-in page."""
    config_endpoint = f"/api/blurams/captcha-config/{flow_id}/{nonce}"
    auth_endpoint = f"/api/blurams/auth/{flow_id}/{nonce}"
    integrity = (
        f' integrity="{ALIYUN_SDK_SRI}" crossorigin="anonymous"' if ALIYUN_SDK_SRI else ""
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
    #captcha-element {{ min-height: 4rem; margin-top: 1rem; }}
    button {{ margin-top: 1rem; padding: .7rem 1rem; }}
    #status {{ min-height: 1.5rem; }}
  </style>
  <script src="{ALIYUN_SDK_URL}"{integrity} defer></script>
</head>
<body>
  <h1>Sign in to Blurams</h1>
  <p>Enter your Blurams credentials, complete the “I’m a human” check, then sign in.</p>
  <form id="login-form" data-config-endpoint="{config_endpoint}"
    data-auth-endpoint="{auth_endpoint}">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="username" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <div id="captcha-element"></div>
    <button id="submit" type="submit" disabled>Sign in</button>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
  <script src="/blurams/captcha.js" defer></script>
</body>
</html>"""


def build_csp() -> str:
    """Return the Content-Security-Policy of the sign-in page."""
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "form-action 'self'; "
        # Only Aliyun's CDN may load scripts; the SDK also pulls helper
        # scripts and stylesheets from the g. and x. alicdn.com subdomains.
        # 'unsafe-inline' is needed because the SDK runs helper code in a
        # srcdoc iframe, which cannot carry a nonce; 'unsafe-eval' is kept
        # because the SDK may compile code at runtime.
        f"script-src 'self' {ALIYUN_SDK_SOURCE} 'unsafe-inline' 'unsafe-eval'; "
        # The widget talks to the Aliyun Captcha API of Blurams' region, e.g.
        # https://<prefix>.captcha-open-southeast.aliyuncs.com for "sgp".
        # The data: source is fetched by the SDK's fingerprinting script.
        f"connect-src 'self' {ALIYUN_SDK_SOURCE} {ALIYUN_API_SOURCE} data:; "
        f"style-src 'self' {ALIYUN_SDK_SOURCE} 'unsafe-inline'; "
        f"img-src 'self' data: {ALIYUN_SDK_SOURCE} {ALIYUN_API_SOURCE}; "
        f"font-src 'self' {ALIYUN_SDK_SOURCE} data:; "
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

        return web.Response(
            text=build_login_page(flow_id, nonce),
            content_type="text/html",
            headers={
                "Content-Security-Policy": build_csp(),
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


class BluramsCaptchaConfigView(HomeAssistantView):
    """Serve the Blurams CAPTCHA configuration to the sign-in page."""

    url = "/api/blurams/captcha-config/{flow_id}/{nonce}"
    name = "api:blurams:captcha_config"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(
        self, request: web.Request, flow_id: str, nonce: str
    ) -> web.Response:
        """Fetch the CAPTCHA configuration with the flow's session."""
        captcha_session = _sessions(self.hass).get(nonce)
        if captcha_session is None or captcha_session.flow_id != flow_id:
            return _captcha_error(
                "This authentication page has expired.", HTTPStatus.GONE
            )

        # The request goes through the flow's own client so that the trace
        # cookies Blurams sets end up in the same cookie jar as the login.
        try:
            async with captcha_session.client.get(
                _CAPTCHA_CONFIG_URL,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": f"{WEB_URL}/login",
                    "X-Requested-With": "XMLHttpRequest",
                },
            ) as response:
                if response.status != HTTPStatus.OK:
                    _LOGGER.warning(
                        "Blurams CAPTCHA config request failed with status %s",
                        response.status,
                    )
                    return _captcha_error(
                        "Blurams did not return a CAPTCHA configuration.",
                        HTTPStatus.BAD_GATEWAY,
                    )
                config = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Unable to fetch Blurams CAPTCHA config: %s", err)
            return _captcha_error(
                "Home Assistant could not reach Blurams CAPTCHA services.",
                HTTPStatus.BAD_GATEWAY,
            )

        if not isinstance(config, dict):
            return _captcha_error(
                "Blurams returned an unexpected CAPTCHA configuration.",
                HTTPStatus.BAD_GATEWAY,
            )
        # Only forward the fields the sign-in page needs.
        return web.json_response(
            {
                key: config[key]
                for key in ("enabled", "configured", "region", "prefix", "sceneId")
                if key in config
            },
            headers={"Cache-Control": "no-store"},
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
const captchaElement = document.querySelector("#captcha-element");
const submit = document.querySelector("#submit");
const status = document.querySelector("#status");
let captchaToken = "";
let captchaInstance = null;
let captchaReady = false;

function setStatus(message) {
  status.textContent = message;
}

async function loadCaptcha() {
  captchaReady = false;
  submit.disabled = true;
  setStatus("Loading the human check…");
  try {
    const response = await fetch(form.dataset.configEndpoint, {
      headers: {Accept: "application/json"},
    });
    const config = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(config.error || "Could not load the human check.");
    if (!config.enabled || !config.configured || !config.sceneId) {
      throw new Error("Blurams does not currently offer its human check. Try again later.");
    }
    // The SDK reads the region and prefix from this global.
    window.AliyunCaptchaConfig = {region: config.region, prefix: config.prefix};
    if (!window.initAliyunCaptcha) {
      throw new Error("The human check did not load. Disable content blockers and retry.");
    }
    window.initAliyunCaptcha({
      SceneId: config.sceneId,
      language: "en",
      mode: "embed",
      element: "#captcha-element",
      button: "#submit",
      delayBeforeSuccess: false,
      success: (event) => {
        captchaToken = typeof event === "string"
          ? event
          : (event && (event.captchaVerifyParam || event.CaptchaVerifyParam)) || "";
        if (!captchaToken) {
          setStatus("The human check did not complete. Please try again.");
          return;
        }
        setStatus("Signing in to Blurams…");
        form.requestSubmit();
      },
      fail: () => {
        captchaToken = "";
        setStatus("The human check was not completed. Please try again.");
      },
      getInstance: (instance) => {
        captchaInstance = instance;
        captchaReady = true;
        submit.disabled = false;
        setStatus("");
      },
      onError: (error) => {
        captchaReady = false;
        captchaToken = "";
        submit.disabled = true;
        console.error("Blurams human check error", error);
        setStatus("The human check failed. Please reload this page and try again.");
      },
    });
  } catch (error) {
    submit.disabled = true;
    setStatus(error.message || "The human check failed to load. Please reload and try again.");
  }
}

async function resetCaptcha() {
  captchaToken = "";
  captchaReady = false;
  submit.disabled = true;
  if (captchaInstance && typeof captchaInstance.destroy === "function") {
    try {
      captchaInstance.destroy();
    } catch (_) {
      // The widget may already be gone; reload it regardless.
    }
  }
  captchaInstance = null;
  captchaElement.innerHTML = "";
  await loadCaptcha();
}

form.addEventListener("submit", async (event) => {
  // The Sign in button also triggers the human check; only sign in once the
  // check has produced a token.
  event.preventDefault();
  if (!captchaToken || !captchaReady) return;
  submit.disabled = true;
  setStatus("Signing in to Blurams…");
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
    setStatus("Blurams is connected. You can close this window.");
    form.hidden = true;
  } catch (error) {
    setStatus(error.message || "Sign-in failed. Please try again.");
    await resetCaptcha();
  }
});

loadCaptcha();
"""