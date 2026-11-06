"""Support for Blurams camera switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import BluramsCannotConnect, BluramsInvalidAuth
from .coordinator import BluramsConfigEntry, BluramsDataUpdateCoordinator
from .entity import BluramsEntity

_STATUS_OFF = "OffByManual"
_STATUS_ON = "On"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluramsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Blurams camera power switches."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_switches() -> None:
        new_ids = set(coordinator.data) - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            BluramsCameraPowerSwitch(coordinator, device_id) for device_id in new_ids
        )

    _async_add_new_switches()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_switches))


class BluramsCameraPowerSwitch(BluramsEntity, SwitchEntity):
    """Control the power state of a Blurams camera."""

    _attr_translation_key = "power"

    def __init__(self, coordinator: BluramsDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the camera power switch."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_power"

    @property
    def is_on(self) -> bool | None:
        """Return whether the camera is powered on."""
        device = self.device_data
        if device is None:
            return None
        return device.get("deviceStatus") != _STATUS_OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power on the camera."""
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off the camera."""
        await self._async_set_power(False)

    async def _async_set_power(self, powered: bool) -> None:
        """Send the power command and publish the new state immediately."""
        try:
            await self.coordinator.api.async_set_camera_power(self._device_id, powered)
        except BluramsInvalidAuth as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Blurams authentication expired; re-authenticate the integration"
            ) from err
        except BluramsCannotConnect as err:
            raise HomeAssistantError(f"Could not change camera power: {err}") from err

        # Update the cached data right away; the next poll confirms it.
        device = self.device_data
        if device is not None:
            device["deviceStatus"] = _STATUS_ON if powered else _STATUS_OFF
            self.coordinator.async_set_updated_data(self.coordinator.data)
