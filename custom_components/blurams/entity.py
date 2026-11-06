"""Base entity for Blurams."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BluramsDataUpdateCoordinator


class BluramsEntity(CoordinatorEntity[BluramsDataUpdateCoordinator]):
    """Common behaviour for all Blurams entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BluramsDataUpdateCoordinator, device_id: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        device = coordinator.data[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device.get("devicename") or device_id,
            manufacturer="Blurams",
            model=device.get("cameraModelId"),
            serial_number=device.get("serialNumber"),
        )

    @property
    def device_data(self) -> dict[str, Any] | None:
        """Return the latest data for this camera, if it still exists."""
        return self.coordinator.data.get(self._device_id)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self.device_data is not None
