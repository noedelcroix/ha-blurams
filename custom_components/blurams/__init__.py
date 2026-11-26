"""The Blurams integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BluramsApi
from .const import CONF_BLURAMS_COOKIES, LIVE_BRIDGE_HOST, LIVE_BRIDGE_PORT
from .coordinator import BluramsConfigEntry, BluramsDataUpdateCoordinator
from .flv_proxy import FlvProxy

PLATFORMS = [Platform.CAMERA, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: BluramsConfigEntry) -> bool:
    """Set up Blurams from a config entry."""
    cookies = entry.data.get(CONF_BLURAMS_COOKIES)
    if not cookies:
        raise ConfigEntryAuthFailed("Blurams authentication must be renewed")

    session = async_get_clientsession(hass)
    api = BluramsApi(session)
    api.set_cookies(cookies)

    coordinator = BluramsDataUpdateCoordinator(hass, entry, api)
    # Raises ConfigEntryAuthFailed (starts reauth) or ConfigEntryNotReady (retry).
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    proxy = FlvProxy(
        hass,
        session,
        _live_params_provider(coordinator),
        host=LIVE_BRIDGE_HOST,
        port=LIVE_BRIDGE_PORT,
    )
    await proxy.start()
    entry.async_on_unload(proxy.stop)
    coordinator.flv_proxy = proxy

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _live_params_provider(coordinator: BluramsDataUpdateCoordinator):
    """Return a callable that fetches live params for a device by id."""

    async def _get(device_id: str):
        device = coordinator.data.get(device_id)
        if device is None:
            return None
        return await coordinator.api.async_get_live_params(device)

    return _get


async def async_unload_entry(hass: HomeAssistant, entry: BluramsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
