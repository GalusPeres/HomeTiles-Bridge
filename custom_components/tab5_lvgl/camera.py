"""Camera entity for the camera built into a HomeTiles panel.

Still images use MQTT snapshots (local_camera.py). Panels that announce
``local_camera_stream`` additionally deliver a live JPEG stream over the
acknowledged TCP upload described in local_camera_stream.py.
"""

from __future__ import annotations

import json
import logging
from time import monotonic

from homeassistant.components import mqtt
from homeassistant.components import network as ha_network
from homeassistant.components.camera import Camera, CameraEntityFeature, async_get_still_stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .capabilities import merged_capabilities_data, supports
from .const import (
    DOMAIN,
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
from .local_camera_stream import (
    LIVE_FRAME_FRESH_S,
    LIVE_FRAME_MAX_MISSES,
    LIVE_FRAME_WAIT_S,
    STREAM_FPS,
    LocalCameraLiveStream,
    valid_ipv4,
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
    data = merged_capabilities_data(entry)
    if supports(data, "local_camera"):
        entities.append(HomeTilesLocalCamera(
            entry, entry_base_topic(entry), live=supports(data, "local_camera_stream")))
    async_add_entities(entities)


class HomeTilesLocalCamera(Camera):
    """On-demand JPEG snapshots, plus a live MJPEG view when announced."""

    _attr_has_entity_name = True
    _attr_translation_key = "local_camera"
    _attr_should_poll = False
    _attr_supported_features = CameraEntityFeature(0)
    _attr_frame_interval = LOCAL_CAMERA_FRAME_INTERVAL_S

    def __init__(self, entry: ConfigEntry, base_topic: str, live: bool = False) -> None:
        super().__init__()
        self.content_type = "image/jpeg"
        self._entry_id = entry.entry_id
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
        # A capability change reloads the entry, so this is fixed per entity.
        self._live: LocalCameraLiveStream | None = None
        if live:
            # Polling interval for clients that bypass the MJPEG handler
            # below; the live handler itself is paced by arriving frames.
            self._attr_frame_interval = 1.0 / STREAM_FPS
            self._live = LocalCameraLiveStream(
                publish=self._async_publish_request,
                endpoint=self._async_live_endpoint,
                registry=self._upload_registry,
                ready=self._live_ready,
                log_name=base_topic,
            )

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
            self._poke_live()
            self.async_write_ha_state()

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            if not online:
                self._snapshots.fail_all("offline")
            self._refresh_available()
            self._poke_live()
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
        subscribe_connection = getattr(mqtt, "async_subscribe_connection_status", None)
        if self._live is not None and subscribe_connection is not None:
            # A broker disconnect suspends the stream without waiting for the
            # next keepalive tick; reconnecting resumes it for current viewers.
            self._subscriptions.append(subscribe_connection(
                self.hass, self._async_mqtt_connection_changed))

    async def async_will_remove_from_hass(self) -> None:
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        self._snapshots.fail_all("removed")
        if self._live is not None:
            # Closing also ends MJPEG responses that are still open, so no
            # viewer keeps a frozen frame of a removed or reloaded entity.
            await self._live.async_close()
        await super().async_will_remove_from_hass()

    async def _async_publish_request(self, request: dict) -> None:
        # Snapshot and stream requests are commands, never retained state.
        await mqtt.async_publish(
            self.hass, local_camera_command_topic(self._base),
            json.dumps(request, separators=(",", ":")), qos=0, retain=False)

    # Live stream ---------------------------------------------------------

    def _poke_live(self) -> None:
        if self._live is not None:
            self._live.poke()

    @callback
    def _async_mqtt_connection_changed(self, _connected: bool) -> None:
        # Must be a @callback: the dispatcher would otherwise run it in an
        # executor thread, where asyncio.Event.set() is not safe.
        self._poke_live()

    def _live_ready(self) -> bool:
        return self.available and mqtt.is_connected(self.hass)

    def _domain_data(self) -> dict:
        data = getattr(self.hass, "data", None)
        return data.get(DOMAIN, {}) if isinstance(data, dict) else {}

    def _stream_manager(self):
        return self._domain_data().get("camera_stream_manager")

    def _upload_registry(self):
        manager = self._stream_manager()
        if manager is None or getattr(manager, "tcp_port", None) is None:
            return None
        return getattr(manager, "local_camera_uploads", None)

    async def _async_live_endpoint(self) -> tuple[str, int] | None:
        """Return the Bridge IPv4 address and listener port the panel can reach."""
        manager = self._stream_manager()
        port = getattr(manager, "tcp_port", None)
        if port is None:
            return None
        bridge = self._domain_data().get("entries", {}).get(self._entry_id)
        device_ip = getattr(bridge, "_device_ip", None)
        if device_ip:
            host = await ha_network.async_get_source_ip(self.hass, target_ip=device_ip)
        else:
            host = await ha_network.async_get_source_ip(self.hass)
        return (host, port) if valid_ipv4(host) else None

    async def handle_async_mjpeg_stream(self, request):
        """Serve the live stream to one viewer; all viewers share one upload."""
        if self._live is None:
            return await super().handle_async_mjpeg_stream(request)
        live = self._live
        last: bytes | None = None
        misses = 0

        async def next_image() -> bytes | None:
            # Returning None ends the multipart response.
            nonlocal last, misses
            if live.closed:
                return None
            image = await live.async_next_frame(last, LIVE_FRAME_WAIT_S)
            if live.closed:
                return None
            if image is not None:
                misses = 0
            else:
                misses += 1
                if misses > LIVE_FRAME_MAX_MISSES:
                    # No live frame for too long (panel cannot upload): end
                    # instead of showing a frozen image forever.
                    _LOGGER.debug("HomeTiles local camera live view ended for %s: no frames",
                                  self._base)
                    return None
                # No live frame yet (starting, suspended or panel busy): keep
                # the previous image, else try one snapshot, else end.
                image = last if last is not None else await self.async_camera_image()
            last = image
            return image

        live.acquire()
        try:
            return await async_get_still_stream(request, next_image, self.content_type, 0.0)
        finally:
            live.release()

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
        if self._live is not None and (frame := self._live.latest(LIVE_FRAME_FRESH_S)) is not None:
            return frame
        if not self.available or not mqtt.is_connected(self.hass):
            return self._snapshots.fallback(monotonic())
        image, failure = await self._snapshots.async_fetch(
            self._async_publish_request,
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
