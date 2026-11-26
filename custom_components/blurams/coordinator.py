"""Data update coordinator for Blurams."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BluramsApi, BluramsCannotConnect, BluramsInvalidAuth
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type BluramsConfigEntry = ConfigEntry[BluramsDataUpdateCoordinator]

if TYPE_CHECKING:
    from .flv_proxy import FlvProxy


class BluramsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the Blurams cloud for the state of every camera."""

    config_entry: BluramsConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: BluramsConfigEntry, api: BluramsApi
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        # Set by the integration setup; re-serves live video for the cameras.
        self.flv_proxy: FlvProxy | None = None

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch the devices, keyed by device id."""
        try:
            devices = await self.api.async_get_devices()
        except BluramsInvalidAuth as err:
            raise ConfigEntryAuthFailed("Blurams authentication must be renewed") from err
        except BluramsCannotConnect as err:
            raise UpdateFailed(f"Error communicating with Blurams: {err}") from err
        return {device["deviceid"]: device for device in devices if "deviceid" in device}
