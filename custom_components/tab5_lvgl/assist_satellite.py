"""Assist satellite entity for the microphone built into a HomeTiles panel.

Tap to talk: the Bridge picks a session id, asks the panel to capture
(assist.py), feeds the panel's PCM frames into Home Assistant's Assist
pipeline from the speech-to-text stage, asks the panel to stop once voice
activity ended, and plays the spoken answer on the panel's own speaker with
the existing speaker play command (speaker.py). Only panels that announce the
``audio_input`` capability get the entity. Wake words are not supported.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from time import monotonic

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
)
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .assist import (
    ASSIST_SATELLITES_KEY,
    AudioSession,
    assist_audio_topic,
    assist_satellite_unique_id,
    assist_status_topic,
    async_publish_assist_command,
    build_start_command,
    build_stop_command,
    new_session_id,
    parse_audio_frame,
    parse_status,
)
from .capabilities import merged_capabilities_data, supports
from .command_channel import entry_pairing_key
from .const import DOMAIN
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic
from .local_camera import RateLimitedWarnings, parse_connected
from . import speaker

_LOGGER = logging.getLogger(__name__)

_WARNING_INTERVAL_S = 300.0
# The satellite returns to idle when the speaker never reports the answer
# finished (lost status, panel restart).
TTS_FINISH_TIMEOUT_S = 120.0
# Home Assistant converts the answer for the panel's decoder (mp3 or wav).
_TTS_OPTIONS = {"preferred_format": "mp3"}

_STOP_EVENTS = frozenset({
    PipelineEventType.STT_VAD_END,
    PipelineEventType.STT_END,
    PipelineEventType.ERROR,
    PipelineEventType.RUN_END,
})


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    # Microphone detection lives on the panel; the Bridge only mirrors the
    # announcement and never starts capture on panels that did not announce it.
    if supports(merged_capabilities_data(entry), "audio_input"):
        async_add_entities([HomeTilesAssistSatellite(entry, entry_base_topic(entry))])


def assist_satellite_for_entry(hass: HomeAssistant, entry_id: str):
    data = getattr(hass, "data", None)
    domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
    return domain.get(ASSIST_SATELLITES_KEY, {}).get(entry_id)


class HomeTilesAssistSatellite(AssistSatelliteEntity):
    """Streams the panel microphone into an Assist pipeline on demand."""

    _attr_has_entity_name = True
    _attr_translation_key = "assist"
    _attr_should_poll = False
    _attr_tts_options = _TTS_OPTIONS

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry_id = entry.entry_id
        self._paired = entry_pairing_key(entry) is not None
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = assist_satellite_unique_id(entry_device_id(entry))
        self._base = base_topic
        self._session: AudioSession | None = None
        self._run_task: asyncio.Task | None = None
        self._panel_online: bool | None = None
        self._mic_withdrawn = False
        self._tts_started = False
        self._tts_request_id: str | None = None
        self._tts_timer: asyncio.TimerHandle | None = None
        self._warnings = RateLimitedWarnings(_WARNING_INTERVAL_S)
        self._subscriptions = []
        self._attr_available = True

    # Lifecycle -----------------------------------------------------------

    def _refresh_available(self) -> None:
        # A cleared retained status means the panel withdrew its microphone.
        self._attr_available = self._panel_online is not False and not self._mic_withdrawn

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            ASSIST_SATELLITES_KEY, {})[self._entry_id] = self

        async def handle_audio(msg: mqtt.ReceiveMessage) -> None:
            frame = parse_audio_frame(msg.payload)
            if frame is None:
                if self._warnings.allow("invalid_frame", monotonic()):
                    _LOGGER.warning("HomeTiles Assist audio frame ignored for %s: invalid frame",
                                    self._base)
                return
            session = self._session
            if session is not None:
                session.feed(frame)

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                self._mic_withdrawn = True
                self._end_session()
            else:
                status = parse_status(msg.payload)
                if status is None:
                    if self._warnings.allow("invalid_status", monotonic()):
                        _LOGGER.warning("HomeTiles Assist status ignored for %s: invalid payload",
                                        self._base)
                    return
                self._mic_withdrawn = False
                session = self._session
                if (session is not None and status["id"] == session.session_id
                        and status["state"] == "idle"):
                    # The panel ended capture on its own (max_ms, error).
                    if status["error"] and self._warnings.allow(
                            f"session_{status['error']}", monotonic()):
                        _LOGGER.warning("HomeTiles Assist capture on %s ended: %s",
                                        self._base, status["error"])
                    session.finish()
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            if not online:
                self._end_session()
                self._finish_tts()
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_speaker_status(msg: mqtt.ReceiveMessage) -> None:
            if self._tts_request_id is None or not msg.payload:
                return
            status = speaker.parse_status(msg.payload)
            if (status is not None and status["id"] == self._tts_request_id
                    and status["state"] == "idle"):
                if status["error"]:
                    _LOGGER.warning("HomeTiles Assist answer on %s could not be played: %s",
                                    self._base, status["error"])
                self._finish_tts()

        # Microphone frames are binary; encoding=None delivers them as bytes.
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, assist_audio_topic(self._base), handle_audio, qos=0, encoding=None))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, assist_status_topic(self._base), handle_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, state_topic(self._base, "connected"), handle_connected, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, speaker.audio_status_topic(self._base), handle_speaker_status, qos=0))

    async def async_will_remove_from_hass(self) -> None:
        satellites = self.hass.data.get(DOMAIN, {}).get(ASSIST_SATELLITES_KEY, {})
        if satellites.get(self._entry_id) is self:
            satellites.pop(self._entry_id, None)
        # Never leave the microphone running: the cancelled run sends stop.
        await self._async_cancel_run()
        self._cancel_tts_timer()
        self._tts_request_id = None
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    # Configuration -------------------------------------------------------

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        # Tap to talk only: the panel has no wake word engine.
        return AssistSatelliteConfiguration(
            available_wake_words=[], active_wake_words=[], max_active_wake_words=0)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        if config.active_wake_words:
            _LOGGER.debug("HomeTiles Assist on %s ignores wake words: none are supported",
                          self._base)

    # Commands ------------------------------------------------------------

    def _bridge(self):
        data = getattr(self.hass, "data", None)
        domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        return domain.get("entries", {}).get(self._entry_id)

    async def _async_send_assist(self, payload: str) -> bool:
        # Commands, never retained state; sealed when the panel is paired.
        return await async_publish_assist_command(
            self.hass, self._bridge(), self._paired, self._base, payload, mqtt.async_publish)

    async def _async_publish_stop(self, session_id: str) -> None:
        try:
            await self._async_send_assist(build_stop_command(session_id))
        except Exception as err:  # noqa: BLE001 - a lost stop is covered by max_ms
            _LOGGER.debug("HomeTiles Assist stop for %s failed: %s", self._base, err)

    @callback
    def _request_stop(self, session: AudioSession | None) -> None:
        """Ask the panel to stop capturing, at most once per session."""
        if session is None or session.stop_sent:
            return
        session.stop_sent = True
        session.finish()
        self.hass.async_create_task(self._async_publish_stop(session.session_id))

    @callback
    def _end_session(self) -> None:
        if self._session is not None:
            self._session.finish()

    async def _async_cancel_run(self) -> None:
        task = self._run_task
        self._run_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def async_start_tap_to_talk(self) -> None:
        """Start one Assist run with the panel microphone."""
        if not self.available:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_unavailable")
        # A new press replaces the running conversation; its stop goes out
        # before the new start.
        await self._async_cancel_run()
        self._finish_tts()
        session = AudioSession(new_session_id())
        self._session = session
        try:
            sent = await self._async_send_assist(build_start_command(session.session_id))
        except Exception:
            sent = False
            _LOGGER.debug("HomeTiles Assist start for %s failed", self._base, exc_info=True)
        if not sent:
            session.stop_sent = True
            session.finish()
            if self._session is session:
                self._session = None
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")
        self._run_task = self.hass.async_create_background_task(
            self._async_run(session), f"{DOMAIN}_assist_{self._entry_id}")

    async def _async_run(self, session: AudioSession) -> None:
        self._tts_started = False
        try:
            await self.async_accept_pipeline_from_satellite(
                session.stream(),
                start_stage=PipelineStage.STT,
                end_stage=PipelineStage.TTS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - one failed run must not stick
            _LOGGER.warning("HomeTiles Assist run on %s failed: %s", self._base, err)
        finally:
            session.finish()
            if not session.stop_sent:
                session.stop_sent = True
                await self._async_publish_stop(session.session_id)
            if self._session is session:
                self._session = None

    # Pipeline ------------------------------------------------------------

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        event_type = event.type
        if event_type in _STOP_EVENTS:
            self._request_stop(self._session)
        if event_type is PipelineEventType.TTS_START:
            self._tts_started = True
        elif event_type is PipelineEventType.TTS_END:
            self._play_answer(event.data)
        elif event_type is PipelineEventType.RUN_END:
            if self._tts_started and self._tts_request_id is None:
                # TTS started but produced nothing playable.
                self._finish_tts()
            self._tts_started = False

    @callback
    def _play_answer(self, data) -> None:
        tts_output = (data or {}).get("tts_output") or {}
        path = tts_output.get("url")
        if not path:
            return
        # Relative /api/tts_proxy URLs become absolute and signed, so the
        # panel can fetch them without a token of its own.
        url = async_process_play_media_url(self.hass, path)
        error = speaker.playable_url_error(url, https_allowed=True)
        if error is not None:
            if self._warnings.allow(f"tts_{error}", monotonic()):
                _LOGGER.warning("HomeTiles Assist answer for %s not sent: %s", self._base, error)
            return
        request_id = speaker.new_request_id()
        self._tts_request_id = request_id
        self._arm_tts_timer(request_id)
        self.hass.async_create_task(
            self._async_send_answer(request_id, speaker.build_play_command(request_id, url)))

    async def _async_send_answer(self, request_id: str, payload: str) -> None:
        try:
            sent = await speaker.async_publish_audio_command(
                self.hass, self._bridge(), self._paired, self._base, payload, mqtt.async_publish)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("HomeTiles Assist answer for %s failed: %s", self._base, err)
            sent = False
        if not sent and self._tts_request_id == request_id:
            self._finish_tts()

    def _arm_tts_timer(self, request_id: str) -> None:
        self._cancel_tts_timer()

        def timed_out() -> None:
            self._tts_timer = None
            if self._tts_request_id == request_id:
                self._finish_tts()

        self._tts_timer = asyncio.get_running_loop().call_later(TTS_FINISH_TIMEOUT_S, timed_out)

    def _cancel_tts_timer(self) -> None:
        if self._tts_timer is not None:
            self._tts_timer.cancel()
            self._tts_timer = None

    @callback
    def _finish_tts(self) -> None:
        """The spoken answer ended (played, failed, timed out or replaced)."""
        had_answer = self._tts_request_id is not None or self._tts_started
        self._tts_request_id = None
        self._tts_started = False
        self._cancel_tts_timer()
        if had_answer and self.hass is not None:
            self.tts_response_finished()
