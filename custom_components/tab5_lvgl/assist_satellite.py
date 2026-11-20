"""Assist satellite entity for the microphone built into a HomeTiles panel.

Tap to talk: the Bridge picks a session id, asks the panel to capture
(assist.py), feeds the panel's PCM frames into Home Assistant's Assist
pipeline from the speech-to-text stage, asks the panel to stop once voice
activity ended, and plays the spoken answer on the panel's own speaker with
the existing speaker play command (speaker.py), decoded by the Bridge when the
panel takes Bridge streams (audio_stream.py). Only panels that announce the
``audio_input`` capability get the entity.

Wake word: the panel detects microWakeWord models itself and reports them on
``{base}/stat/assist_wake``; the Bridge then runs the same flow as a button
press and passes the word's phrase to the pipeline. The words come from the
panel's retained status; Home Assistant's wake word settings send the
``wake_words`` command.

Timers: the satellite registers the panel for Home Assistant's timer events,
so "set a timer" works on it, and passes the panel's timers on
(voice_timers.py). The panel's controls start, pause, resume, cancel and
extend them; once it rang, Home Assistant says which timer is done.
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
    AssistSatelliteWakeWord,
)
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .assist import (
    ASSIST_SATELLITES_KEY,
    MAX_ACTIVE_WAKE_WORDS,
    AudioSession,
    assist_audio_topic,
    assist_pipeline_select_unique_id,
    assist_satellite_unique_id,
    assist_status_topic,
    assist_vad_select_unique_id,
    assist_wake_topic,
    async_publish_assist_command,
    build_start_command,
    build_stop_command,
    build_mic_command,
    build_wake_words_command,
    new_session_id,
    parse_audio_frame,
    parse_status,
    parse_wake_event,
    wake_choice,
    wake_word_info,
    wake_word_phrase,
)
from .capabilities import merged_capabilities_data, supports
from .command_channel import entry_pairing_key
from .const import DOMAIN
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic
from .local_camera import RateLimitedWarnings, parse_connected
from . import speaker
from . import voice_timers

_LOGGER = logging.getLogger(__name__)

_WARNING_INTERVAL_S = 300.0
# The satellite returns to idle when the speaker never reports the answer
# finished (lost status, panel restart).
TTS_FINISH_TIMEOUT_S = 120.0
# Timers and their sound go to a panel this long after it connected.
TIMER_CONNECT_DELAY_S = 3.0
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
        self._device_id = entry_device_id(entry)
        self._attr_unique_id = assist_satellite_unique_id(self._device_id)
        self._base = base_topic
        self._session: AudioSession | None = None
        self._run_task: asyncio.Task | None = None
        self._panel_online: bool | None = None
        self._mic_withdrawn = False
        self._tts_started = False
        self._tts_request_id: str | None = None
        self._tts_timer: asyncio.TimerHandle | None = None
        # The panel's speaker takes Bridge-decoded clips (its retained status).
        self._speaker_streams = False
        self._warnings = RateLimitedWarnings(_WARNING_INTERVAL_S)
        self._subscriptions = []
        self._attr_available = True
        # Wake words from the panel's retained status (ids).
        self._available_wake_words: list[str] = []
        self._active_wake_words: list[str] = []
        # The chosen word stays while the listen switch is off.
        self._chosen_wake_words: list[str] = []
        self._wake_listen = False
        # This panel's Home Assistant timers (voice_timers.py).
        self._timers = voice_timers.PanelTimers()
        self._timer_unregister = None
        self._timer_device_id: str | None = None
        self._timer_connect_task: asyncio.Task | None = None

    # Assist selects ------------------------------------------------------

    # The select platform may load after this one, so the ids are looked up
    # per run. Without the selects Home Assistant uses its preferred pipeline
    # and default sensitivity.

    def _select_entity_id(self, unique_id: str) -> str | None:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(self.hass).async_get_entity_id("select", DOMAIN, unique_id)

    @property
    def pipeline_entity_id(self) -> str | None:
        return self._select_entity_id(assist_pipeline_select_unique_id(self._device_id))

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return self._select_entity_id(assist_vad_select_unique_id(self._device_id))

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
                if status["wake_words"] is not None:
                    self._available_wake_words = status["wake_words"]
                if status["active_wake_words"] is not None:
                    self._active_wake_words = [
                        word for word in status["active_wake_words"]
                        if word in self._available_wake_words]
                choice = wake_choice(status)
                if choice is not None:
                    self._chosen_wake_words, self._wake_listen = choice
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

        async def handle_wake(msg: mqtt.ReceiveMessage) -> None:
            event = parse_wake_event(msg.payload, self._available_wake_words)
            if event is None:
                if self._warnings.allow("invalid_wake", monotonic()):
                    _LOGGER.warning("HomeTiles Assist wake event ignored for %s: invalid or "
                                    "unknown wake word", self._base)
                return
            if self._run_active():
                _LOGGER.debug("HomeTiles Assist wake event %s on %s ignored: run active",
                              event.sequence, self._base)
                return
            try:
                await self._async_start_run(wake_word_phrase(event.word))
            except HomeAssistantError as err:
                if self._warnings.allow("wake_start_failed", monotonic()):
                    _LOGGER.warning("HomeTiles Assist wake word on %s not started: %s",
                                    self._base, err.translation_key or err)

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            if not online:
                self._end_session()
                self._finish_tts()
            else:
                # A panel keeps its timers in RAM: send them on every connect,
                # also one that restarted before its offline message arrived.
                self._schedule_timer_state()
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_timer_request(msg: mqtt.ReceiveMessage) -> None:
            request = voice_timers.parse_request(msg.payload)
            if request is None:
                if self._warnings.allow("invalid_timer_request", monotonic()):
                    _LOGGER.warning("HomeTiles timer request ignored for %s: invalid payload",
                                    self._base)
                return
            await self._async_handle_timer_request(request)

        async def handle_speaker_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                return
            status = speaker.parse_status(msg.payload)
            if status is not None:
                self._speaker_streams = status["stream"]
            if self._tts_request_id is None:
                return
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
            self.hass, assist_wake_topic(self._base), handle_wake, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, state_topic(self._base, "connected"), handle_connected, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, speaker.audio_status_topic(self._base), handle_speaker_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, voice_timers.assist_timer_topic(self._base), handle_timer_request, qos=0))
        self._register_timer_handler()

    async def async_will_remove_from_hass(self) -> None:
        satellites = self.hass.data.get(DOMAIN, {}).get(ASSIST_SATELLITES_KEY, {})
        if satellites.get(self._entry_id) is self:
            satellites.pop(self._entry_id, None)
        # Never leave the microphone running: the cancelled run sends stop.
        await self._async_cancel_run()
        self._cancel_tts_timer()
        self._tts_request_id = None
        if self._timer_unregister is not None:
            self._timer_unregister()
            self._timer_unregister = None
        if self._timer_connect_task is not None:
            self._timer_connect_task.cancel()
            self._timer_connect_task = None
        self._timers.clear()
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    # Configuration -------------------------------------------------------

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        available = []
        for word_id in self._available_wake_words:
            phrase, languages = wake_word_info(word_id)
            available.append(AssistSatelliteWakeWord(
                id=word_id, wake_word=phrase, trained_languages=languages))
        return AssistSatelliteConfiguration(
            available_wake_words=available,
            active_wake_words=list(self._active_wake_words),
            max_active_wake_words=MAX_ACTIVE_WAKE_WORDS)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        await self.async_set_active_wake_words(config.active_wake_words)

    async def async_set_active_wake_words(self, words: list[str]) -> None:
        """Home Assistant's own wake word setting: words listen, none stops."""
        active = [word for word in words
                  if word in self._available_wake_words][:MAX_ACTIVE_WAKE_WORDS]
        await self._async_send_wake_words(active)
        # The panel's retained status confirms it; until then show the request.
        self._active_wake_words = active
        if active:
            self._chosen_wake_words = active
        self._wake_listen = bool(active)
        self.async_write_ha_state()

    @property
    def wake_listen(self) -> bool:
        return self._wake_listen

    async def async_set_wake_listen(self, enabled: bool) -> None:
        """The Listening switch; the chosen word stays while it is off."""
        words: list[str] = []
        if enabled:
            words = (self._chosen_wake_words or self._available_wake_words)[:1]
        await self._async_send_wake_words(words, enabled)
        self._active_wake_words = words
        self._wake_listen = enabled
        self.async_write_ha_state()

    async def async_choose_wake_word(self, word: str) -> None:
        """The Wake word select: changes the word, not the listen switch."""
        if word not in self._available_wake_words:
            raise HomeAssistantError(f"Unknown wake word: {word}")
        await self._async_send_wake_words([word], self._wake_listen)
        self._chosen_wake_words = [word]
        if self._wake_listen:
            self._active_wake_words = [word]
        self.async_write_ha_state()

    async def _async_send_wake_words(self, words: list[str], listen: bool | None = None) -> None:
        try:
            sent = await self._async_send_assist(build_wake_words_command(words, listen))
        except Exception:  # noqa: BLE001 - reported as not connected below
            _LOGGER.debug("HomeTiles Assist wake words for %s failed", self._base,
                          exc_info=True)
            sent = False
        if not sent:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")

    async def async_set_microphone(self, enabled: bool) -> None:
        """Turn the panel's microphone privacy switch on or off.

        The panel accepts "on" only over the encrypted channel, so an
        unpaired panel can only be switched on at the panel itself.
        """
        if enabled and not self._paired:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="microphone_needs_pairing")
        try:
            sent = await self._async_send_assist(build_mic_command(enabled))
        except Exception:  # noqa: BLE001 - reported as not connected below
            _LOGGER.debug("HomeTiles microphone switch for %s failed", self._base,
                          exc_info=True)
            sent = False
        if not sent:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")

    # Timers --------------------------------------------------------------

    def _timer_manager(self):
        """Home Assistant's timer manager; None without the intent integration."""
        try:
            from homeassistant.components.intent import TIMER_DATA
        except ImportError:
            return None
        data = getattr(self.hass, "data", None)
        return data.get(TIMER_DATA) if isinstance(data, dict) else None

    @callback
    def _register_timer_handler(self) -> None:
        registry_entry = getattr(self, "registry_entry", None)
        device_id = getattr(registry_entry, "device_id", None)
        if not device_id or self._timer_manager() is None:
            _LOGGER.debug("HomeTiles timers for %s not registered: no intent integration or device",
                          self._base)
            return
        from homeassistant.components.intent import async_register_timer_handler

        self._timer_device_id = device_id
        self._timer_unregister = async_register_timer_handler(
            self.hass, device_id, self.handle_timer_event)
        # Timers this panel started before a reload or Bridge update.
        self._timers.seed(getattr(self._timer_manager(), "timers", None), device_id)

    @callback
    def handle_timer_event(self, event_type, timer) -> None:
        """Home Assistant's timer events for this panel (the intent integration)."""
        send = self._timers.handle_event(event_type, timer)
        if send == "list":
            self.hass.async_create_task(self._async_send_timers())
        elif send == "done":
            self.hass.async_create_task(self._async_send_timer_command(
                voice_timers.build_done_command(timer)))

    def timer_sound_option(self) -> str:
        sounds = self.hass.data.get(DOMAIN, {}).get(voice_timers.TIMER_SOUNDS_KEY, {})
        return sounds.get(self._entry_id) or voice_timers.DEFAULT_SOUND

    def timer_volume(self) -> int:
        volumes = self.hass.data.get(DOMAIN, {}).get(voice_timers.TIMER_VOLUMES_KEY, {})
        return voice_timers.clamp_volume(volumes.get(self._entry_id, voice_timers.DEFAULT_VOLUME))

    async def async_send_timer_sound(self) -> bool:
        """The finished-timer sound and volume (Timer sound, Timer volume)."""
        if self._panel_online is not True:
            return False  # Sent on the next connect.
        return await self._async_send_timer_command(voice_timers.build_sound_command(
            voice_timers.panel_sound(self.timer_sound_option()), self.timer_volume()))

    async def _async_send_timers(self) -> bool:
        if self._panel_online is not True:
            return False  # Sent on the next connect.
        return await self._async_send_timer_command(
            voice_timers.build_timers_command(self._timers.timers()))

    @callback
    def _schedule_timer_state(self) -> None:
        # One pending send per panel; a newer connect replaces it.
        if self._timer_connect_task is not None:
            self._timer_connect_task.cancel()
        self._timer_connect_task = self.hass.async_create_background_task(
            self._async_send_timer_state(), f"hometiles_timers_{self._entry_id}")

    async def _async_send_timer_state(self) -> None:
        # The panel subscribes to its commands right after it connected.
        await asyncio.sleep(TIMER_CONNECT_DELAY_S)
        if self._panel_online is not True:
            return
        await self.async_send_timer_sound()
        await self._async_send_timers()

    async def _async_send_timer_command(self, payload: str) -> bool:
        try:
            sent = await self._async_send_assist(payload)
        except Exception:  # noqa: BLE001 - the next change or connect sends again
            _LOGGER.debug("HomeTiles timers for %s not sent", self._base, exc_info=True)
            sent = False
        if not sent and self._warnings.allow("timers_not_sent", monotonic()):
            _LOGGER.warning("HomeTiles timers for %s not sent: panel not connected", self._base)
        return sent

    async def _async_handle_timer_request(self, request: dict) -> None:
        op = request["op"]
        manager = self._timer_manager()
        try:
            if op == "start":
                if manager is None or self._timer_device_id is None:
                    raise HomeAssistantError("timers unavailable")
                manager.start_timer(self._timer_device_id, None, None, request["seconds"],
                                    self.hass.config.language, name=request["name"])
                return
            timer_id = request["id"]
            if op == "speak":
                await self._async_speak_timer_done(timer_id)
                return
            if op == "play_sound":
                await self._async_play_timer_sound()
                return
            # Only this panel's own timers.
            if manager is None or not self._timers.owns(timer_id):
                _LOGGER.debug("HomeTiles timer %s on %s ignored: not this panel's",
                              timer_id, self._base)
                return
            if op == "pause":
                manager.pause_timer(timer_id)
            elif op == "resume":
                manager.unpause_timer(timer_id)
            elif op == "cancel":
                manager.cancel_timer(timer_id)
            elif op == "add":
                manager.add_time(timer_id, request["seconds"])
        except Exception as err:  # noqa: BLE001 - a lost timer must not break the topic
            if self._warnings.allow(f"timer_{op}", monotonic()):
                _LOGGER.warning("HomeTiles timer request %s on %s failed: %s",
                                op, self._base, err)

    async def _async_speak_timer_done(self, timer_id: str) -> None:
        finished = self._timers.finished(timer_id)
        if finished is None:
            return
        name, language = finished
        # Spoken through the satellite's announcements once it has them;
        # until then the panel's chime alone tells that a timer finished.
        announce = getattr(type(self), "async_announce", None)
        if announce is None or announce is getattr(AssistSatelliteEntity, "async_announce", None):
            return
        await self.async_internal_announce(
            message=voice_timers.done_phrase(name, language or self.hass.config.language),
            preannounce=False)

    async def _async_play_timer_sound(self) -> None:
        """The user's own sound file, decoded by the Bridge for the panel."""
        option = self.timer_sound_option()
        if voice_timers.panel_sound(option) != voice_timers.SOUND_BRIDGE:
            return
        bridge = self._bridge()
        create_stream = getattr(bridge, "async_create_audio_stream", None)
        if not self._speaker_streams or create_stream is None:
            if self._warnings.allow("timer_sound_stream", monotonic()):
                _LOGGER.warning("HomeTiles timer sound %s for %s not played: the panel takes "
                                "no Bridge streams", option, self._base)
            return
        path = self.hass.config.path(*voice_timers.SOUNDS_DIR, option)
        current_url = getattr(bridge, "audio_stream_url", None)
        if current_url is not None and current_url() == path:
            return  # Still playing from the previous round.
        if not await self.hass.async_add_executor_job(voice_timers.sound_file_usable, path):
            if self._warnings.allow("timer_sound_missing", monotonic()):
                _LOGGER.warning("HomeTiles timer sound %s for %s not played: the file is "
                                "missing or too large", option, self._base)
            return
        url = await create_stream(path, speaker.AUDIO_RATE_MUSIC, local=True)
        await speaker.async_publish_audio_command(
            self.hass, bridge, self._paired, self._base,
            speaker.build_play_command(speaker.new_request_id(), url, timer=True), mqtt.async_publish)

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

    def _run_active(self) -> bool:
        task = self._run_task
        return task is not None and not task.done()

    async def async_start_tap_to_talk(self) -> None:
        """Start one Assist run with the panel microphone."""
        await self._async_start_run(None)

    async def _async_start_run(self, phrase: str | None) -> None:
        """Start one Assist run; a wake word passes its phrase to the pipeline."""
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
            self._async_run(session, phrase), f"{DOMAIN}_assist_{self._entry_id}")

    async def _async_run(self, session: AudioSession, phrase: str | None = None) -> None:
        self._tts_started = False
        try:
            await self.async_accept_pipeline_from_satellite(
                session.stream(),
                start_stage=PipelineStage.STT,
                end_stage=PipelineStage.TTS,
                wake_word_phrase=phrase,
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
        self.hass.async_create_task(self._async_send_answer(request_id, url))

    async def _async_send_answer(self, request_id: str, url: str) -> None:
        bridge = self._bridge()
        try:
            create_stream = getattr(bridge, "async_create_audio_stream", None)
            if self._speaker_streams and create_stream is not None:
                try:
                    url = await create_stream(url, speaker.AUDIO_RATE_SPEECH)
                except ValueError as err:
                    # No camera listener: the panel downloads the answer itself.
                    _LOGGER.debug("HomeTiles Assist answer for %s without the Bridge stream: %s",
                                  self._base, err)
            sent = await speaker.async_publish_audio_command(
                self.hass, bridge, self._paired, self._base,
                speaker.build_play_command(request_id, url), mqtt.async_publish)
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
