"""Media player entity for the speaker built into a HomeTiles panel.

TTS (``tts.speak``), media-source sounds and ``media_player.play_media``
with an http(s) URL work. A panel that announces Bridge streams (firmware
from b502) gets the clip from the Bridge's FFmpeg as PCM (audio_stream.py),
any format, with seeking; older firmware downloads the URL itself
(speaker.py). A small player: title, position, length and pause for Home
Assistant's player bar, no queue. Only panels that announce the
``audio_output`` capability get the entity.
"""

from __future__ import annotations

import logging
from time import monotonic

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .capabilities import merged_capabilities_data, supports
from .command_channel import entry_pairing_key
from .const import DOMAIN
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic
from .local_camera import RateLimitedWarnings, parse_connected
from .speaker import (
    async_publish_audio_command,
    audio_status_topic,
    build_mute_command,
    build_pause_command,
    build_play_command,
    build_stop_command,
    build_volume_command,
    clamp_volume,
    media_title,
    new_request_id,
    parse_status,
    playable_url_error,
    speaker_unique_id,
    stream_rate,
)

_LOGGER = logging.getLogger(__name__)

_WARNING_INTERVAL_S = 300.0
_VOLUME_STEP = 0.05


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    # Codec detection lives on the panel; the Bridge only mirrors the
    # announcement and never probes panels that did not announce it.
    if supports(merged_capabilities_data(entry), "audio_output"):
        async_add_entities([HomeTilesSpeaker(entry, entry_base_topic(entry))])


class HomeTilesSpeaker(MediaPlayerEntity):
    """Plays clips by URL on the panel's own speaker."""

    _attr_has_entity_name = True
    _attr_translation_key = "speaker"
    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _BASE_FEATURES = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    )
    _attr_volume_step = _VOLUME_STEP

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry_id = entry.entry_id
        self._paired = entry_pairing_key(entry) is not None
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = speaker_unique_id(entry_device_id(entry))
        self._base = base_topic
        self._status: dict | None = None
        self._panel_online: bool | None = None
        self._last_request_id: str | None = None
        # Title of the clip this entity started, shown while that clip plays.
        self._title: tuple[str, str | None] | None = None
        # (url, title, rate) of the Bridge-decoded clip this entity started,
        # for seeking: a seek starts the same clip at the new position.
        self._stream_clip: tuple[str, str | None, int] | None = None
        self._warnings = RateLimitedWarnings(_WARNING_INTERVAL_S)
        self._subscriptions = []
        self._attr_available = False

    # State ---------------------------------------------------------------

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = self._BASE_FEATURES
        status = self._status
        if status is not None and status["pausable"]:
            features |= MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY
        if (status is not None and status["stream"] and status["duration_ms"]
                and self._stream_clip is not None):
            features |= MediaPlayerEntityFeature.SEEK
        return features

    def _refresh(self) -> None:
        status = self._status
        self._attr_available = self._panel_online is not False and status is not None
        if status is None:
            self._attr_state = None
            self._clear_media()
            return
        self._attr_state = {
            "playing": MediaPlayerState.PLAYING,
            "paused": MediaPlayerState.PAUSED,
        }.get(status["state"], MediaPlayerState.IDLE)
        self._attr_volume_level = status["volume"]
        self._attr_is_volume_muted = status["muted"]
        if status["state"] == "idle":
            self._clear_media()
            return
        # The panel reports the position with a status change only; Home
        # Assistant moves it on from the update time while playing.
        position = status["position_ms"]
        if position is not None:
            self._attr_media_position = position // 1000
            self._attr_media_position_updated_at = dt_util.utcnow()
        duration = status["duration_ms"]
        self._attr_media_duration = duration // 1000 if duration else None
        title = self._title
        self._attr_media_title = (title[1] if title is not None and status["id"] == title[0]
                                  else None)

    def _clear_media(self) -> None:
        self._attr_media_position = None
        self._attr_media_position_updated_at = None
        self._attr_media_duration = None
        self._attr_media_title = None

    @property
    def extra_state_attributes(self):
        if self._status is None or self._status["error"] is None:
            return None
        return {"last_error": self._status["error"]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                # A cleared retained status means the panel withdrew the speaker.
                self._status = None
            else:
                status = parse_status(msg.payload)
                if status is None:
                    if self._warnings.allow("invalid_status", monotonic()):
                        _LOGGER.warning("HomeTiles speaker status ignored for %s: invalid payload",
                                        self._base)
                    return
                if (status["error"] and status["id"] is not None
                        and status["id"] == self._last_request_id):
                    _LOGGER.warning("HomeTiles speaker on %s could not play the clip: %s",
                                    self._base, status["error"])
                    self._last_request_id = None
                self._status = status
            self._refresh()
            self.async_write_ha_state()

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            self._refresh()
            self.async_write_ha_state()

        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, audio_status_topic(self._base), handle_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, state_topic(self._base, "connected"), handle_connected, qos=0))

    async def async_will_remove_from_hass(self) -> None:
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    # Commands ------------------------------------------------------------

    def _bridge(self):
        data = getattr(self.hass, "data", None)
        domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        return domain.get("entries", {}).get(self._entry_id)

    async def _async_send(self, payload: str) -> None:
        # Commands, never retained state; sealed when the panel is paired.
        sent = await async_publish_audio_command(
            self.hass, self._bridge(), self._paired, self._base, payload, mqtt.async_publish)
        if not sent:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="speaker_not_connected")

    async def async_play_media(self, media_type, media_id: str, **kwargs) -> None:
        title = media_title(media_id, kwargs.get("extra"))
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = item.url
        # Relative /api/... URLs become absolute (internal URL first) and are
        # signed, so the panel can fetch them without a token of its own.
        url = async_process_play_media_url(self.hass, media_id)
        # The panel downloads https with the same unverified TLS client as its
        # screensaver image URL.
        error = playable_url_error(url, https_allowed=True)
        if error is not None:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key=f"speaker_{error}")
        await self._async_start(url, title, stream_rate(media_id, kwargs.get("announce")), 0)

    async def _async_start(self, url: str, title: str | None, rate: int, start_ms: int) -> None:
        """Play url from start_ms: decoded by the Bridge when the panel takes it."""
        bridge = self._bridge()
        create_stream = getattr(bridge, "async_create_audio_stream", None)
        play_url = url
        self._stream_clip = None
        if self._status is not None and self._status["stream"] and create_stream is not None:
            try:
                play_url = await create_stream(url, rate, start_ms)
                self._stream_clip = (url, title, rate)
            except ValueError as err:
                # No camera listener: the panel downloads the clip itself.
                _LOGGER.debug("HomeTiles speaker on %s plays without the Bridge stream: %s",
                              self._base, err)
        request_id = new_request_id()
        self._last_request_id = request_id
        self._title = (request_id, title)
        # A Bridge stream comes back by playing it again (async_media_play),
        # so the panel frees it while paused.
        await self._async_send(build_play_command(
            request_id, play_url, restart=self._stream_clip is not None))

    def _bridge_call(self, name: str, *args) -> None:
        method = getattr(self._bridge(), name, None)
        if method is not None:
            method(*args)

    async def async_media_stop(self) -> None:
        await self._async_send(build_stop_command())
        self._stream_clip = None
        self._bridge_call("stop_audio_stream")

    async def async_media_pause(self) -> None:
        await self._async_send(build_pause_command(True))
        self._bridge_call("set_audio_stream_paused", True)

    async def async_media_play(self) -> None:
        # A clip the panel paused by closing its stream plays again from the
        # paused point (user 2026-10-21: a pause holds nothing); otherwise the
        # panel resumes the clip it kept. It has no queue to start from.
        status = self._status
        clip = self._stream_clip
        if status is not None and status["released"] and clip is not None:
            url, title, rate = clip
            position_ms = status["position_ms"] or 0
            await self._async_start(url, title, rate, position_ms)
            self._attr_media_position = position_ms // 1000
            self._attr_media_position_updated_at = dt_util.utcnow()
            self.async_write_ha_state()
            return
        await self._async_send(build_pause_command(False))
        self._bridge_call("set_audio_stream_paused", False)

    async def async_media_seek(self, position: float) -> None:
        """Start the Bridge-decoded clip again at position (seconds)."""
        clip = self._stream_clip
        if clip is None:
            return
        url, title, rate = clip
        start_ms = max(0, int(position * 1000))
        await self._async_start(url, title, rate, start_ms)
        # The panel reports the new position once its new stream started;
        # until then Home Assistant moved the bar back to the old position.
        self._attr_media_position = start_ms // 1000
        self._attr_media_position_updated_at = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        level = clamp_volume(volume)
        await self._async_send(build_volume_command(level))
        # Optimistic until the retained status confirms it.
        self._attr_volume_level = level
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        await self._async_send(build_mute_command(mute))
        self._attr_is_volume_muted = bool(mute)
        self.async_write_ha_state()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
