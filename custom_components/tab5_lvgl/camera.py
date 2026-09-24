"""Camera entity for the camera built into a HomeTiles panel (still images)."""

from __future__ import annotations

import json
import logging
from time import monotonic

from homeassistant.components import mqtt
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .capabilities import merged_capabilities_data, supports
from .const import (
    LOCAL_CAMERA_FRAME_INTERVAL_S,
    LOCAL_CAMERA_MAX_BYTES,
    LOCAL_CAMERA_MIN_AGE_S,
    LOCAL_CAMERA_REQUEST_TIMEOUT_S,
    LOCAL_CAMERA_STALE_FALLBACK_S,
    LOCAL_CAMERA_WARNING_INTERVAL_S,
)
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic
from .local_camera import (
    LocalCameraSnapshots,
    RateLimitedWarnings,
    local_camera_command_topic,
    local_camera_error_prefix,
    local_camera_image_prefix,
    local_camera_status_topic,
    local_camera_unique_id,
    parse_connected,
    parse_error,
    parse_status,
    request_id_from_topic,
)

_LOGGER = logging.getLogger(__name__)

# Expected lifecycle endings; they are not failures worth a warning.
_QUIET_FAILURES = frozenset({"offline", "not_ready", "removed"})


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    entities = []
    # Consent and sensor detection live on the panel; the Bridge only mirrors
    # the announcement and never probes panels that did not announce it.
    if supports(merged_capabilities_data(entry), "local_camera"):
        entities.append(HomeTilesLocalCamera(entry, entry_base_topic(entry)))
    async_add_entities(entities)


class HomeTilesLocalCamera(Camera):
    """On-demand JPEG snapshots requested from the panel over MQTT."""

    _attr_has_entity_name = True
    _attr_translation_key = "local_camera"
    _attr_should_poll = False
    _attr_supported_features = CameraEntityFeature(0)
    _attr_frame_interval = LOCAL_CAMERA_FRAME_INTERVAL_S

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__()
        self.content_type = "image/jpeg"
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = local_camera_unique_id(entry_device_id(entry))
        self._base = base_topic
        self._image_prefix = local_camera_image_prefix(base_topic)
        self._error_prefix = local_camera_error_prefix(base_topic)
        self._snapshots = LocalCameraSnapshots(
            timeout_s=LOCAL_CAMERA_REQUEST_TIMEOUT_S,
            stale_fallback_s=LOCAL_CAMERA_STALE_FALLBACK_S,
        )
        self._warnings = RateLimitedWarnings(LOCAL_CAMERA_WARNING_INTERVAL_S)
        self._status: dict | None = None
        self._panel_online: bool | None = None
        self._subscriptions = []
        self._attr_available = False

    def _refresh_available(self) -> None:
        self._attr_available = (
            self._panel_online is not False
            and self._status is not None
            and self._status["state"] == "ready"
        )

    def _warn(self, reason: str, message: str, *args) -> None:
        if self._warnings.allow(reason, monotonic()):
            _LOGGER.warning(message, *args)

    @property
    def extra_state_attributes(self):
        if self._status is None:
            return None
        attributes = {"panel_camera_state": self._status["state"]}
        for key in ("width", "height", "sensor", "error"):
            if key in self._status:
                attributes[key] = self._status[key]
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                # A cleared retained status means the panel withdrew the camera.
                self._status = None
            else:
                status = parse_status(msg.payload, LOCAL_CAMERA_MAX_BYTES)
                if status is None:
                    self._warn("invalid_status",
                               "HomeTiles local camera status ignored for %s: invalid payload",
                               self._base)
                    return
                self._status = status
            if self._status is None or self._status["state"] != "ready":
                self._snapshots.fail_all("not_ready")
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            if not online:
                self._snapshots.fail_all("offline")
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_image(msg: mqtt.ReceiveMessage) -> None:
            request_id = request_id_from_topic(self._image_prefix, msg.topic)
            outcome = self._snapshots.resolve_image(request_id, msg.payload, monotonic())
            if outcome == "invalid":
                self._warn("invalid_image",
                           "HomeTiles local camera image rejected for %s: not a complete JPEG within %d bytes",
                           self._base, self._request_max_bytes())
            elif outcome == "unsolicited" and self._warnings.allow("unsolicited", monotonic()):
                _LOGGER.debug("HomeTiles local camera dropped an unsolicited image on %s", msg.topic)

        async def handle_error(msg: mqtt.ReceiveMessage) -> None:
            request_id = request_id_from_topic(self._error_prefix, msg.topic)
            self._snapshots.resolve_error(request_id, parse_error(msg.payload))

        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, local_camera_status_topic(self._base), handle_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, state_topic(self._base, "connected"), handle_connected, qos=0))
        # JPEG payloads are binary; encoding=None delivers them as bytes.
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, f"{self._image_prefix}/+", handle_image, qos=0, encoding=None))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, f"{self._error_prefix}/+", handle_error, qos=0))

    async def async_will_remove_from_hass(self) -> None:
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        self._snapshots.fail_all("removed")
        await super().async_will_remove_from_hass()

    def _request_max_bytes(self) -> int:
        if self._status is None:
            return LOCAL_CAMERA_MAX_BYTES
        return min(self._status["max_bytes"], LOCAL_CAMERA_MAX_BYTES)

    def _min_interval_s(self) -> float:
        device_interval = self._status["min_interval_s"] if self._status else 0.0
        return max(LOCAL_CAMERA_MIN_AGE_S, device_interval)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        # width/height are ignored: Home Assistant rescales the JPEG itself.
        if not self.available or not mqtt.is_connected(self.hass):
            return self._snapshots.fallback(monotonic())
        command_topic = local_camera_command_topic(self._base)

        async def publish(request: dict) -> None:
            await mqtt.async_publish(
                self.hass, command_topic, json.dumps(request, separators=(",", ":")),
                qos=0, retain=False)

        image, failure = await self._snapshots.async_fetch(
            publish,
            max_bytes=self._request_max_bytes(),
            min_interval_s=self._min_interval_s(),
        )
        if failure in _QUIET_FAILURES:
            _LOGGER.debug("HomeTiles local camera snapshot ended for %s: %s", self._base, failure)
        elif failure is not None:
            self._warn(failure,
                       "HomeTiles local camera snapshot failed for %s: %s%s",
                       self._base, failure,
                       " (serving cached frame)" if image is not None else "")
        return image
