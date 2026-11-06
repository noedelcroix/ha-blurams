"""The Blurams integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BluramsApi
from .const import CONF_BLURAMS_COOKIES
from .coordinator import BluramsConfigEntry, BluramsDataUpdateCoordinator

PLATFORMS = [Platform.CAMERA, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: BluramsConfigEntry) -> bool:
    """Set up Blurams from a config entry."""
    cookies = entry.data.get(CONF_BLURAMS_COOKIES)
    if not cookies:
        raise ConfigEntryAuthFailed("Blurams authentication must be renewed")

    api = BluramsApi(async_get_clientsession(hass))
    api.set_cookies(cookies)

    coordinator = BluramsDataUpdateCoordinator(hass, entry, api)
    # Raises ConfigEntryAuthFailed (starts reauth) or ConfigEntryNotReady (retry).
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BluramsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
