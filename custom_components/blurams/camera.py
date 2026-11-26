"""Support for Blurams cameras."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import BluramsCannotConnect, BluramsInvalidAuth
from .coordinator import BluramsConfigEntry, BluramsDataUpdateCoordinator
from .entity import BluramsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluramsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Blurams cameras."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_cameras() -> None:
        new_ids = set(coordinator.data) - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(BluramsCamera(coordinator, device_id) for device_id in new_ids)

    _async_add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_cameras))


class BluramsCamera(BluramsEntity, Camera):
    """A camera provided by Blurams (thumbnail snapshots only)."""

    _attr_name = None
    # Without this flag the frontend never starts the HLS player and only
    # refreshes the still snapshot every 10s.
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: BluramsDataUpdateCoordinator, device_id: str) -> None:
        """Initialize a Blurams camera."""
        BluramsEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self._attr_unique_id = device_id

    @property
    def available(self) -> bool:
        """Return whether the camera is online."""
        device = self.device_data
        return super().available and device is not None and self._is_online

    @property
    def _is_online(self) -> bool:
        """Return whether the cloud reports the camera as reachable."""
        # Browser captures show both "online" and "available" for a reachable
        # camera, so accept either.
        return self.device_data.get("onlineStatus") in ("online", "available")

    async def stream_source(self) -> str | None:
        """Return the live FLV bridge URL for the stream integration."""
        proxy = self.coordinator.flv_proxy
        if proxy is None or not self._is_online:
            return None
        return proxy.url_for(self._device_id)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest camera thumbnail."""
        device = self.device_data or {}
        thumbnails = device.get("thumbnailUrlList") or []
        url = thumbnails[0].get("url") if thumbnails else None
        if not url:
            return None
        try:
            return await self.coordinator.api.async_get_camera_image(url)
        except BluramsInvalidAuth:
            self.coordinator.config_entry.async_start_reauth(self.hass)
        except BluramsCannotConnect:
            pass
        return None
