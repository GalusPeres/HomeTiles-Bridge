"""Speaker contract (speaker.py) and the media_player platform against HA stubs."""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT, load_module

SPEAKER = load_module("speaker")
CAPABILITIES = load_module("capabilities")
BASE = "hometiles/panel"
ID = "0123456789abcdef"


def status(**changes):
    return dict({"v": 1, "state": "idle", "volume": 0.5, "muted": False,
                 "formats": ["mp3", "wav"], "max_bytes": 4194304}, **changes)


class SpeakerContractTest(unittest.TestCase):
    def test_topics_and_unique_id(self):
        self.assertEqual(SPEAKER.audio_command_topic(BASE), f"{BASE}/cmnd/audio")
        self.assertEqual(SPEAKER.audio_status_topic(BASE), f"{BASE}/stat/audio")
        self.assertEqual(SPEAKER.speaker_unique_id("mac"), "mac_speaker")
        self.assertTrue(SPEAKER.is_speaker_unique_id("mac_speaker"))
        self.assertFalse(SPEAKER.is_speaker_unique_id("mac_local_camera"))
        self.assertFalse(SPEAKER.is_speaker_unique_id(None))

    def test_commands(self):
        self.assertEqual(json.loads(SPEAKER.build_play_command(ID, "http://ha:8123/a.mp3")),
                         {"v": 1, "op": "play", "id": ID, "url": "http://ha:8123/a.mp3"})
        with self.assertRaises(ValueError):
            SPEAKER.build_play_command("XYZ", "http://ha/a.mp3")
        self.assertEqual(json.loads(SPEAKER.build_stop_command()), {"v": 1, "op": "stop"})
        self.assertEqual(json.loads(SPEAKER.build_volume_command(0.256)),
                         {"v": 1, "op": "volume", "volume": 0.256})
        self.assertEqual(json.loads(SPEAKER.build_volume_command(3))["volume"], 1.0)
        self.assertEqual(json.loads(SPEAKER.build_volume_command(-1))["volume"], 0.0)
        for bad in (None, "x", float("nan")):
            with self.assertRaises(ValueError):
                SPEAKER.build_volume_command(bad)
        self.assertEqual(json.loads(SPEAKER.build_mute_command(1)),
                         {"v": 1, "op": "mute", "muted": True})
        request_id = SPEAKER.new_request_id()
        self.assertTrue(SPEAKER.valid_request_id(request_id))
        self.assertNotEqual(request_id, SPEAKER.new_request_id())

    def test_playable_url(self):
        error = SPEAKER.playable_url_error
        self.assertIsNone(error("http://192.168.1.2:8123/api/tts_proxy/x.mp3?authSig=abc"))
        self.assertEqual(error("https://ha.example/api/tts_proxy/x.mp3"), "https_unsupported")
        self.assertIsNone(error("https://ha.example/x.mp3", https_allowed=True))
        for bad in ("/api/tts_proxy/x.mp3", "ftp://h/x.mp3", "http:///x.mp3", "http://h/a b",
                    "http://h/ä", "", None, "http://h/" + "a" * 1100):
            self.assertEqual(error(bad), "invalid_url", bad)

    def test_status(self):
        parsed = SPEAKER.parse_status(json.dumps(status()))
        self.assertEqual(parsed, {"state": "idle", "volume": 0.5, "muted": False,
                                  "max_bytes": 4194304, "formats": ["mp3", "wav"],
                                  "id": None, "error": None})
        parsed = SPEAKER.parse_status(json.dumps(status(
            state="playing", volume=1, id=ID, error="https_unsupported")).encode())
        self.assertEqual((parsed["state"], parsed["volume"], parsed["id"], parsed["error"]),
                         ("playing", 1.0, ID, "https_unsupported"))
        # Unknown errors from newer firmware are kept, but generic.
        self.assertEqual(SPEAKER.parse_status(json.dumps(status(error="new_code")))["error"],
                         "unknown")
        self.assertIsNone(SPEAKER.parse_status(json.dumps(status(id="bad")))["id"])
        for bad in (status(v=2), status(state="paused"), status(volume="x"),
                    status(volume=True), status(muted=1), status(max_bytes=0),
                    status(formats="mp3")):
            self.assertIsNone(SPEAKER.parse_status(json.dumps(bad)), bad)
        self.assertIsNone(SPEAKER.parse_status("not json"))
        self.assertIsNone(SPEAKER.parse_status("[]"))
        self.assertIsNone(SPEAKER.parse_status(b"\xff"))
        self.assertIsNone(SPEAKER.parse_status("{" + " " * 2000 + "}"))

    def test_capability_is_never_inferred(self):
        self.assertEqual(CAPABILITIES.normalise_capabilities({"audio_output": True}),
                         {"audio_output": True})
        with self.assertRaises(ValueError):
            CAPABILITIES.normalise_capabilities({"audio_output": "yes"})
        self.assertFalse(CAPABILITIES.supports({"model": "waveshare_touch_lcd_7b"}, "audio_output"))
        self.assertTrue(CAPABILITIES.supports({"capabilities": {"audio_output": True}},
                                              "audio_output"))
        self.assertTrue(CAPABILITIES.stale_speaker("mac_speaker", {}))
        self.assertFalse(CAPABILITIES.stale_speaker(
            "mac_speaker", {"capabilities": {"audio_output": True}}))
        self.assertFalse(CAPABILITIES.stale_speaker("mac_local_camera", {}))

    def test_audio_is_sealed_data(self):
        channel = load_module("command_channel")
        self.assertIn("audio", channel.SEALED_DATA)


class FakeMqtt(types.ModuleType):
    def __init__(self):
        super().__init__("homeassistant.components.mqtt")
        self.subscriptions = {}
        self.published = []
        self.unsubscribed = 0
        self.ReceiveMessage = types.SimpleNamespace

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscriptions[topic] = handler

        def unsubscribe():
            self.unsubscribed += 1
        return unsubscribe

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))


class FakeMediaSource(types.ModuleType):
    def __init__(self):
        super().__init__("homeassistant.components.media_source")
        self.browsed = []

    @staticmethod
    def is_media_source_id(media_id):
        return media_id.startswith("media-source://")

    async def async_resolve_media(self, hass, media_id, target):
        return types.SimpleNamespace(url="/api/tts_proxy/abc.mp3", mime_type="audio/mpeg")

    async def async_browse_media(self, hass, media_content_id, content_filter=None):
        self.browsed.append((media_content_id, content_filter))
        return "tree"


class FakeMediaPlayerEntity:
    # Home Assistant entities need no super().__init__(); neither does this.
    written = 0

    @property
    def available(self):
        return self._attr_available

    def async_write_ha_state(self):
        self.written += 1

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class FakeHomeAssistantError(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None, **kwargs):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key


class Feature:
    PLAY_MEDIA = 1
    STOP = 2
    VOLUME_SET = 4
    VOLUME_STEP = 8
    VOLUME_MUTE = 16
    BROWSE_MEDIA = 32
    MEDIA_ANNOUNCE = 64


def fake_process_url(hass, media_id):
    if media_id.startswith("/"):
        return f"{hass.internal}{media_id}?authSig=x"
    return media_id


def load_media_player_module(fake_mqtt, fake_media_source):
    package_name = "_hometiles_media_player_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    components.mqtt = fake_mqtt
    components.media_source = fake_media_source
    media_player = types.ModuleType("homeassistant.components.media_player")
    media_player.MediaPlayerEntity = FakeMediaPlayerEntity
    media_player.MediaPlayerEntityFeature = Feature
    media_player.MediaPlayerDeviceClass = types.SimpleNamespace(SPEAKER="speaker")
    media_player.MediaPlayerState = types.SimpleNamespace(PLAYING="playing", IDLE="idle")
    media_player.async_process_play_media_url = fake_process_url
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = FakeHomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    stubs = {
        package_name: package,
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.mqtt": fake_mqtt,
        "homeassistant.components.media_source": fake_media_source,
        "homeassistant.components.media_player": media_player,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.media_player")


def entry(capabilities=None, **data):
    values = {"device_id": "mac", "base_topic": BASE, "model": "waveshare_touch_lcd_7b"}
    values.update(data)
    if capabilities is not None:
        values["capabilities"] = capabilities
    return types.SimpleNamespace(entry_id="e1", data=values, options={})


def message(topic, payload):
    return types.SimpleNamespace(topic=topic, payload=payload, retain=True)


class SealingBridge:
    def __init__(self):
        self.sent = []

    async def async_publish_audio_command(self, text):
        self.sent.append(json.loads(text))


class MediaPlayerPlatformTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.media_source = FakeMediaSource()
        self.module = load_media_player_module(self.mqtt, self.media_source)

    async def created(self, config_entry):
        result = []
        await self.module.async_setup_entry(None, config_entry, result.extend)
        return result

    async def start(self, config_entry=None, bridge=None):
        [player] = await self.created(config_entry or entry({"audio_output": True}))
        player.hass = types.SimpleNamespace(
            internal="http://192.168.1.2:8123",
            data={"tab5_lvgl": {"entries": {"e1": bridge}} if bridge else {}})
        player.entity_id = "media_player.panel_speaker"
        await player.async_added_to_hass()
        return player

    async def deliver(self, topic, payload):
        await self.mqtt.subscriptions[topic](message(topic, payload))

    async def test_entity_only_for_announced_capability(self):
        self.assertEqual(await self.created(entry()), [])
        self.assertEqual(await self.created(entry({"audio_output": False})), [])
        stale = entry({"audio_output": False})
        stale.options = {"capabilities": {"audio_output": True}}
        self.assertEqual(await self.created(stale), [])
        [player] = await self.created(entry({"audio_output": True}))
        self.assertEqual(player._attr_unique_id, "mac_speaker")
        self.assertEqual(player._attr_translation_key, "speaker")
        self.assertTrue(player._attr_has_entity_name)
        self.assertEqual(player._attr_device_class, "speaker")
        self.assertEqual(player._attr_supported_features, 127)

    async def test_state_follows_status_and_connection(self):
        player = await self.start()
        self.assertEqual(set(self.mqtt.subscriptions),
                         {f"{BASE}/stat/audio", f"{BASE}/stat/connected"})
        self.assertFalse(player.available)
        await self.deliver(f"{BASE}/stat/audio", json.dumps(status(volume=0.3, muted=True)))
        self.assertTrue(player.available)
        self.assertEqual(player._attr_state, "idle")
        self.assertEqual(player._attr_volume_level, 0.3)
        self.assertTrue(player._attr_is_volume_muted)
        self.assertIsNone(player.extra_state_attributes)
        await self.deliver(f"{BASE}/stat/audio", json.dumps(status(state="playing")))
        self.assertEqual(player._attr_state, "playing")
        await self.deliver(f"{BASE}/stat/connected", "0")
        self.assertFalse(player.available)
        await self.deliver(f"{BASE}/stat/connected", "1")
        self.assertTrue(player.available)
        # An invalid status keeps the last good one.
        await self.deliver(f"{BASE}/stat/audio", "garbage")
        self.assertEqual(player._attr_state, "playing")
        await self.deliver(f"{BASE}/stat/audio", json.dumps(status(error="decode_failed")))
        self.assertEqual(player.extra_state_attributes, {"last_error": "decode_failed"})
        # A cleared retained status withdraws the speaker.
        await self.deliver(f"{BASE}/stat/audio", "")
        self.assertFalse(player.available)
        await player.async_will_remove_from_hass()
        self.assertEqual(self.mqtt.unsubscribed, 2)

    async def test_play_resolves_media_source_and_signs_url(self):
        player = await self.start()
        await player.async_play_media("music", "media-source://tts/cloud?message=hi")
        [(topic, payload, qos, retain)] = self.mqtt.published
        self.assertEqual((topic, qos, retain), (f"{BASE}/cmnd/audio", 0, False))
        self.assertEqual(payload["op"], "play")
        self.assertEqual(payload["url"], "http://192.168.1.2:8123/api/tts_proxy/abc.mp3?authSig=x")
        self.assertTrue(SPEAKER.valid_request_id(payload["id"]))
        self.assertEqual(player._last_request_id, payload["id"])

    async def test_play_sends_https_urls(self):
        player = await self.start()
        await player.async_play_media("music", "https://ha.example/clip.mp3")
        self.assertEqual(self.mqtt.published[0][1]["url"], "https://ha.example/clip.mp3")

    async def test_play_refuses_relative_urls_before_sending(self):
        player = await self.start()
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await player.async_play_media("music", "ftp://ha.example/clip.mp3")
        self.assertEqual(raised.exception.translation_key, "speaker_invalid_url")
        self.assertEqual(self.mqtt.published, [])

    async def test_volume_mute_stop(self):
        player = await self.start()
        await player.async_set_volume_level(0.42)
        await player.async_mute_volume(True)
        await player.async_media_stop()
        self.assertEqual([p for _, p, _, _ in self.mqtt.published], [
            {"v": 1, "op": "volume", "volume": 0.42},
            {"v": 1, "op": "mute", "muted": True},
            {"v": 1, "op": "stop"},
        ])
        self.assertEqual(player._attr_volume_level, 0.42)
        self.assertTrue(player._attr_is_volume_muted)

    async def test_paired_panel_uses_the_sealed_channel(self):
        bridge = SealingBridge()
        player = await self.start(entry({"audio_output": True}), bridge=bridge)
        await player.async_play_media("music", "http://192.168.1.2:8123/local/chime.mp3")
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(bridge.sent[0]["url"], "http://192.168.1.2:8123/local/chime.mp3")

    async def test_paired_panel_without_bridge_never_gets_plaintext(self):
        config_entry = entry({"audio_output": True})
        player = await self.start(config_entry)
        player._paired = True
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await player.async_play_media("music", "http://192.168.1.2:8123/local/chime.mp3")
        self.assertEqual(raised.exception.translation_key, "speaker_not_connected")
        self.assertEqual(self.mqtt.published, [])

    async def test_browse_filters_audio(self):
        player = await self.start()
        self.assertEqual(await player.async_browse_media(), "tree")
        [(content_id, content_filter)] = self.media_source.browsed
        self.assertIsNone(content_id)
        self.assertTrue(content_filter(types.SimpleNamespace(media_content_type="audio/mpeg")))
        self.assertFalse(content_filter(types.SimpleNamespace(media_content_type="image/png")))


if __name__ == "__main__":
    unittest.main()
