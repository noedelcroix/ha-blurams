"""Config flow for Blurams."""

from __future__ import annotations

from collections.abc import Mapping
import secrets
from typing import Any, override

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_USERNAME
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .auth import async_create_captcha_session, async_register_views
from .const import CONF_BLURAMS_COOKIES, DOMAIN


class BluramsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Blurams config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._captcha_nonce: str | None = None
        self._auth_data: dict[str, Any] | None = None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Open the browser page with Blurams' interactive CAPTCHA."""
        return await self._async_show_captcha()

    @override
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Reauthenticate by completing Blurams' browser challenge."""
        return await self._async_show_captcha()

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Complete the external CAPTCHA step."""
        if user_input is None:
            return self.async_abort(reason="cannot_connect")
        self._auth_data = user_input
        return self.async_external_step_done(next_step_id="finish_auth")

    async def async_step_finish_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the authenticated account and its session cookies."""
        if self._auth_data is None:
            return self.async_abort(reason="cannot_connect")

        username: str = self._auth_data[CONF_USERNAME]
        data = {
            CONF_USERNAME: username,
            CONF_BLURAMS_COOKIES: self._auth_data[CONF_BLURAMS_COOKIES],
        }
        await self.async_set_unique_id(username.strip().lower())

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=username, data=data)

    async def _async_show_captcha(self) -> ConfigFlowResult:
        """Open the browser page with Blurams' interactive CAPTCHA."""
        try:
            base_url = get_url(self.hass)
        except NoURLAvailableError:
            return self.async_abort(reason="no_url")

        async_register_views(self.hass)
        self._captcha_nonce = secrets.token_urlsafe(32)
        await async_create_captcha_session(self.hass, self.flow_id, self._captcha_nonce)
        return self.async_external_step(
            step_id="captcha",
            url=f"{base_url}/api/blurams/auth/{self.flow_id}/{self._captcha_nonce}",
        )
