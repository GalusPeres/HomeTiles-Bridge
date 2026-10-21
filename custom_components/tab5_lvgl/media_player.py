"""Media player entity for the speaker built into a HomeTiles panel.

Home Assistant hands the panel a URL (speaker.py); the panel downloads the
clip and plays it, so TTS (``tts.speak``), media-source sounds and
``media_player.play_media`` with an http URL work. A small player: title,
position, length and pause for Home Assistant's player bar (firmware from
v0.9.4), no queue. Only panels that announce the ``audio_output``
capability get the entity.
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
        self._warnings = RateLimitedWarnings(_WARNING_INTERVAL_S)
        self._subscriptions = []
        self._attr_available = False

    # State ---------------------------------------------------------------

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = self._BASE_FEATURES
        if self._status is not None and self._status["pausable"]:
            features |= MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY
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

    async def _async_send(self, payload: str) -> None:
        # Commands, never retained state; sealed when the panel is paired.
        data = getattr(self.hass, "data", None)
        domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        bridge = domain.get("entries", {}).get(self._entry_id)
        sent = await async_publish_audio_command(
            self.hass, bridge, self._paired, self._base, payload, mqtt.async_publish)
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
        request_id = new_request_id()
        self._last_request_id = request_id
        self._title = (request_id, title)
        await self._async_send(build_play_command(request_id, url))

    async def async_media_stop(self) -> None:
        await self._async_send(build_stop_command())

    async def async_media_pause(self) -> None:
        await self._async_send(build_pause_command(True))

    async def async_media_play(self) -> None:
        # Resumes the paused clip; the panel has no queue to start from.
        await self._async_send(build_pause_command(False))

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
