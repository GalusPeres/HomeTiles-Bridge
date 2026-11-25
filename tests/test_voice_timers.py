"""Voice timers (voice_timers.py) and their wiring in the Assist satellite.

The panel's scanner (firmware src/audio/voice_timer_contract.h) reads the
keys in a fixed order, so the tests compare the raw JSON text, not only the
decoded values.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from test_assist import (
    BASE,
    FakeHass,
    FakeMqtt,
    SealingBridge,
    entry,
    load_platforms,
    message,
    settle,
)
from test_view_navigation import load_module

TIMERS = load_module("voice_timers")
TIMER_ID = "01JB8Z5Y0M3QK4V6W8X9Y0Z1A2"


class Timer(types.SimpleNamespace):
    def __init__(self, id=TIMER_ID, name=None, seconds=600, left=272, active=True, language="en"):
        super().__init__(id=id, name=name, created_seconds=seconds, seconds_left=left,
                         is_active=active, language=language)


class ContractTest(unittest.TestCase):
    def test_topic_and_unique_id(self):
        self.assertEqual(TIMERS.assist_timer_topic(BASE), f"{BASE}/stat/assist_timer")
        self.assertEqual(TIMERS.timer_sound_select_unique_id("mac"), "mac_timer_sound")

    def test_timer_list_keeps_the_panel_key_order(self):
        text = TIMERS.build_timers_command([Timer(name="Pasta"), Timer(id="B2", left=0, active=False)])
        self.assertEqual(text, '{"v":1,"op":"timers","timers":['
                               f'{{"id":"{TIMER_ID}","total":600,"left":272,"active":true,"name":"Pasta"}},'
                               '{"id":"B2","total":600,"left":0,"active":false}]}')
        self.assertEqual(TIMERS.build_timers_command([]), '{"v":1,"op":"timers","timers":[]}')

    def test_timer_list_is_bounded_and_skips_bad_ids(self):
        timers = [Timer(id=f"T{i}") for i in range(12)] + [Timer(id="bad-id")]
        entries = json.loads(TIMERS.build_timers_command(timers))["timers"]
        self.assertEqual([e["id"] for e in entries], [f"T{i}" for i in range(8)])
        # More than the panel holds: the soonest ones, paused ones last.
        timers = [Timer(id=f"T{i}", left=1000 - i) for i in range(10)] + [Timer(id="P", left=1, active=False)]
        entries = json.loads(TIMERS.build_timers_command(timers))["timers"]
        self.assertEqual([e["id"] for e in entries], [f"T{i}" for i in range(9, 1, -1)])

    def test_names_are_utf8_cut_and_clean(self):
        long_name = "Ä" * 40  # 80 bytes
        [timer] = json.loads(TIMERS.build_timers_command([Timer(name=long_name)]))["timers"]
        self.assertEqual(timer["name"], "Ä" * 24)
        self.assertLessEqual(len(timer["name"].encode()), 48)
        [timer] = json.loads(TIMERS.build_timers_command([Timer(name=" Tea\n\x07 ")]))["timers"]
        self.assertEqual(timer["name"], "Tea")
        # Raw UTF-8, not \\u escapes: the panel's byte limit stays honest.
        self.assertIn("Ä", TIMERS.build_timers_command([Timer(name="Ä")]))

    def test_seconds_are_clamped(self):
        [timer] = json.loads(TIMERS.build_timers_command([Timer(seconds=10**9, left=-3)]))["timers"]
        self.assertEqual((timer["total"], timer["left"]), (86400, 0))

    def test_done_and_sound_commands(self):
        self.assertEqual(TIMERS.build_done_command(Timer(name="Pasta")),
                         f'{{"v":1,"op":"timer_done","id":"{TIMER_ID}","total":600,"name":"Pasta"}}')
        self.assertEqual(TIMERS.build_done_command(Timer()),
                         f'{{"v":1,"op":"timer_done","id":"{TIMER_ID}","total":600}}')
        with self.assertRaises(ValueError):
            TIMERS.build_done_command(Timer(id=""))
        for sound in ("rising_three", "kitchen_beep", "soft_bell", "marimba", "westminster",
                      "off", "bridge"):
            self.assertEqual(json.loads(TIMERS.build_sound_command(sound)),
                             {"v": 1, "op": "timer_sound", "sound": sound, "volume": 60})
        self.assertEqual(json.loads(TIMERS.build_sound_command("marimba", 35.4))["volume"], 35)
        for bad, expected in ((150, 100), (-3, 0), (True, 60), (None, 60), (float("nan"), 60)):
            self.assertEqual(TIMERS.clamp_volume(bad), expected, bad)
        with self.assertRaises(ValueError):
            TIMERS.build_sound_command("alarm.mp3")

    def test_requests(self):
        parse = TIMERS.parse_request
        self.assertEqual(parse('{"v":1,"op":"start","seconds":300}'),
                         {"op": "start", "seconds": 300, "name": None})
        self.assertEqual(parse(b'{"v":1,"op":"start","seconds":60,"name":"Pasta"}'),
                         {"op": "start", "seconds": 60, "name": "Pasta"})
        # Control characters never reach Home Assistant's timer name.
        self.assertEqual(parse(json.dumps({"v": 1, "op": "start", "seconds": 60, "name": " Pa\nsta\u0007 "})),
                         {"op": "start", "seconds": 60, "name": "Pasta"})
        self.assertEqual(parse(json.dumps({"v": 1, "op": "start", "seconds": 60, "name": "\n"}))["name"], None)
        for op in ("pause", "resume", "cancel", "speak", "play_sound"):
            self.assertEqual(parse(json.dumps({"v": 1, "op": op, "id": TIMER_ID})),
                             {"op": op, "id": TIMER_ID})
        self.assertEqual(parse(json.dumps({"v": 1, "op": "add", "id": TIMER_ID, "seconds": 60})),
                         {"op": "add", "id": TIMER_ID, "seconds": 60})
        for bad in ('{"v":2,"op":"start","seconds":300}', '{"v":1,"op":"start","seconds":0}',
                    '{"v":1,"op":"start","seconds":86401}', '{"v":1,"op":"start","seconds":true}',
                    '{"v":1,"op":"start","seconds":1.5}', '{"v":1,"op":"start","seconds":5,"name":3}',
                    '{"v":1,"op":"pause","id":"a/b"}', '{"v":1,"op":"pause"}',
                    '{"v":1,"op":"add","id":"A1"}', '{"v":1,"op":"finish","id":"A1"}',
                    '[1]', 'nope', '', None, b"\xff", "x" * 300):
            self.assertIsNone(parse(bad), bad)

    def test_done_phrases(self):
        self.assertEqual(TIMERS.done_phrase("Pasta", "en"), "Pasta timer is done")
        self.assertEqual(TIMERS.done_phrase(None, "en"), "Your timer is done")
        self.assertEqual(TIMERS.done_phrase("Pasta", "de-CH"), "Der Timer Pasta ist abgelaufen")
        self.assertEqual(TIMERS.done_phrase(None, "pt-BR"), "O seu temporizador terminou")
        self.assertEqual(TIMERS.done_phrase(None, "xx"), "Your timer is done")
        self.assertEqual(TIMERS.done_phrase(None, None), "Your timer is done")

    def test_sound_options(self):
        self.assertEqual(TIMERS.panel_sound("marimba"), "marimba")
        self.assertEqual(TIMERS.panel_sound("off"), "off")
        self.assertEqual(TIMERS.panel_sound("alarm.mp3"), "bridge")
        self.assertEqual(TIMERS.panel_sound(None), "rising_three")
        self.assertEqual(TIMERS.panel_sound("../x.mp3"), "rising_three")

    def test_sound_files(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, size in (("b.mp3", 10), ("A.wav", 10), ("notes.txt", 10), (".hidden.mp3", 10),
                               ("empty.mp3", 0), ("huge.mp3", TIMERS.MAX_SOUND_FILE_BYTES + 1)):
                with open(os.path.join(directory, name), "wb") as handle:
                    handle.truncate(size)
            os.mkdir(os.path.join(directory, "dir.mp3"))
            self.assertEqual(TIMERS.list_sound_files(directory), ["A.wav", "b.mp3"])
        self.assertEqual(TIMERS.list_sound_files("/nonexistent/hometiles"), [])


class PanelTimersTest(unittest.TestCase):
    def test_events(self):
        timers = TIMERS.PanelTimers()
        timer = Timer(name="Pasta")
        self.assertEqual(timers.handle_event("started", timer), "list")
        self.assertTrue(timers.owns(TIMER_ID))
        self.assertEqual(timers.handle_event("updated", timer), "list")
        self.assertEqual(timers.timers(), [timer])
        self.assertEqual(timers.handle_event("finished", timer), "done")
        self.assertFalse(timers.owns(TIMER_ID))
        self.assertEqual(timers.finished(TIMER_ID), ("Pasta", "en"))
        self.assertIsNone(timers.handle_event("cancelled", timer))  # Already gone.
        other = Timer(id="B2")
        timers.handle_event("started", other)
        self.assertEqual(timers.handle_event("cancelled", other), "list")
        self.assertEqual(timers.timers(), [])
        self.assertIsNone(timers.handle_event("started", Timer(id="bad id")))
        self.assertIsNone(timers.handle_event("other", Timer(id="C3")))

    def test_finished_is_bounded(self):
        timers = TIMERS.PanelTimers()
        for i in range(10):
            timers.handle_event("finished", Timer(id=f"T{i}"))
        self.assertIsNone(timers.finished("T1"))
        self.assertIsNotNone(timers.finished("T9"))


class FakeTimerManager:
    def __init__(self):
        self.handlers = {}
        self.calls = []

    def register(self, hass, device_id, handler):
        self.handlers[device_id] = handler

        def unregister():
            self.handlers.pop(device_id)
        return unregister

    def start_timer(self, device_id, hours, minutes, seconds, language, name=None):
        self.calls.append(("start", device_id, hours, minutes, seconds, language, name))
        return "NEW"

    def pause_timer(self, timer_id):
        self.calls.append(("pause", timer_id))

    def unpause_timer(self, timer_id):
        self.calls.append(("resume", timer_id))

    def cancel_timer(self, timer_id):
        self.calls.append(("cancel", timer_id))

    def add_time(self, timer_id, seconds):
        self.calls.append(("add", timer_id, seconds))


class SatelliteTimersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.satellite_module, _ = load_platforms(self.mqtt)
        self.manager = FakeTimerManager()
        intent = types.ModuleType("homeassistant.components.intent")
        intent.TIMER_DATA = "intent.timer"
        intent.async_register_timer_handler = self.manager.register
        patcher = mock.patch.dict(sys.modules, {"homeassistant.components.intent": intent})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.satellite_module.TIMER_CONNECT_DELAY_S = 0
        self.bridge = SealingBridge()
        self.streams = []

        async def create_stream(url, rate, local=False):
            self.streams.append((url, rate, local))
            return "tcp://192.168.1.2:8141/token"
        self.bridge.async_create_audio_stream = create_stream

    async def start(self, sound=None):
        result = []
        await self.satellite_module.async_setup_entry(None, entry({"audio_input": True}), result.extend)
        [satellite] = result
        hass = FakeHass(self.bridge)
        hass.data["intent.timer"] = self.manager
        config_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(config_dir, ignore_errors=True))
        self.config_dir = config_dir
        hass.config = types.SimpleNamespace(language="en",
                                            path=lambda *parts: os.path.join(config_dir, *parts))

        async def executor(func, *args):
            return func(*args)
        hass.async_add_executor_job = executor
        if sound:
            hass.data["tab5_lvgl"]["timer_sounds"] = {"e1": sound}
        satellite.hass = hass
        satellite.registry_entry = types.SimpleNamespace(device_id="dev1")
        satellite.entity_id = "assist_satellite.panel_assist"
        satellite.announced = []

        async def internal_announce(message=None, preannounce=True, **kwargs):
            satellite.announced.append((message, preannounce))
        satellite.async_internal_announce = internal_announce
        await satellite.async_added_to_hass()
        return satellite

    async def deliver(self, topic, payload):
        await self.mqtt.subscriptions[topic](message(topic, payload))
        await settle()

    async def connect(self):
        await self.deliver(f"{BASE}/stat/connected", "1")

    async def request(self, payload):
        await self.deliver(f"{BASE}/stat/assist_timer", json.dumps(payload))

    def timer_ops(self):
        return [c for c in self.bridge.assist if c["op"] in ("timers", "timer_done", "timer_sound")]

    async def test_registers_and_unregisters(self):
        satellite = await self.start()
        self.assertIn("dev1", self.manager.handlers)
        await satellite.async_will_remove_from_hass()
        self.assertNotIn("dev1", self.manager.handlers)

    async def test_no_intent_integration(self):
        result = []
        await self.satellite_module.async_setup_entry(None, entry({"audio_input": True}), result.extend)
        [satellite] = result
        satellite.hass = FakeHass(self.bridge)
        satellite.registry_entry = types.SimpleNamespace(device_id="dev1")
        await satellite.async_added_to_hass()
        self.assertEqual(self.manager.handlers, {})
        await satellite.async_will_remove_from_hass()

    async def test_connect_sends_sound_and_timers(self):
        satellite = await self.start(sound="alarm.mp3")
        satellite.handle_timer_event("started", Timer(name="Pasta"))
        await settle()
        self.assertEqual(self.timer_ops(), [])  # Panel not connected yet.
        await self.connect()
        self.assertEqual([c["op"] for c in self.timer_ops()], ["timer_sound", "timers"])
        self.assertEqual(self.timer_ops()[0]["sound"], "bridge")
        self.assertEqual(self.timer_ops()[0]["volume"], 60)
        self.assertEqual(self.timer_ops()[1]["timers"][0]["name"], "Pasta")
        await satellite.async_will_remove_from_hass()

    async def test_timer_volume_goes_with_the_sound(self):
        satellite = await self.start(sound="soft_bell")
        satellite.hass.data["tab5_lvgl"]["timer_volumes"] = {"e1": 25}
        await self.connect()
        [sound, _] = self.timer_ops()
        self.assertEqual((sound["sound"], sound["volume"]), ("soft_bell", 25))
        await satellite.async_will_remove_from_hass()

    async def test_events_reach_the_panel(self):
        satellite = await self.start()
        await self.connect()
        self.bridge.assist.clear()
        timer = Timer(name="Pasta")
        satellite.handle_timer_event("started", timer)
        await settle()
        satellite.handle_timer_event("finished", timer)
        await settle()
        ops = self.timer_ops()
        self.assertEqual([c["op"] for c in ops], ["timers", "timer_done"])
        self.assertEqual(ops[1], {"v": 1, "op": "timer_done", "id": TIMER_ID, "total": 600,
                                  "name": "Pasta"})
        await satellite.async_will_remove_from_hass()

    async def test_panel_controls(self):
        satellite = await self.start()
        await self.connect()
        satellite.handle_timer_event("started", Timer())
        await self.request({"v": 1, "op": "start", "seconds": 300})
        await self.request({"v": 1, "op": "pause", "id": TIMER_ID})
        await self.request({"v": 1, "op": "resume", "id": TIMER_ID})
        await self.request({"v": 1, "op": "add", "id": TIMER_ID, "seconds": 60})
        await self.request({"v": 1, "op": "cancel", "id": TIMER_ID})
        # Another device's timer is never touched.
        await self.request({"v": 1, "op": "cancel", "id": "OTHER"})
        await self.request({"v": 1, "op": "cancel"})
        self.assertEqual(self.manager.calls, [
            ("start", "dev1", None, None, 300, "en", None),
            ("pause", TIMER_ID), ("resume", TIMER_ID), ("add", TIMER_ID, 60), ("cancel", TIMER_ID)])
        await satellite.async_will_remove_from_hass()

    async def test_failing_manager_does_not_break_the_topic(self):
        satellite = await self.start()
        satellite.handle_timer_event("started", Timer())

        def fail(timer_id):
            raise RuntimeError("timer_not_found")
        self.manager.pause_timer = fail
        await self.request({"v": 1, "op": "pause", "id": TIMER_ID})
        await self.request({"v": 1, "op": "cancel", "id": TIMER_ID})
        self.assertEqual(self.manager.calls, [("cancel", TIMER_ID)])
        await satellite.async_will_remove_from_hass()

    async def test_no_speech_without_announcements(self):
        # Without announcements on the satellite the panel's chime alone tells it.
        satellite = await self.start()
        satellite.handle_timer_event("finished", Timer(name="Pasta", language="de"))
        await self.request({"v": 1, "op": "speak", "id": TIMER_ID})
        self.assertEqual(satellite.announced, [])
        await satellite.async_will_remove_from_hass()

    async def test_speak_after_ringing(self):
        satellite = await self.start()

        async def announce(self, announcement):
            pass

        # A satellite with announcements (its own async_announce).
        satellite.__class__ = type("AnnouncingSatellite", (type(satellite),), {"async_announce": announce})
        satellite.handle_timer_event("finished", Timer(name="Pasta", language="de"))
        await self.request({"v": 1, "op": "speak", "id": TIMER_ID})
        await self.request({"v": 1, "op": "speak", "id": "UNKNOWN"})
        self.assertEqual(satellite.announced, [("Der Timer Pasta ist abgelaufen", False)])
        await satellite.async_will_remove_from_hass()

    def sound_file(self, name, size=1000):
        directory = os.path.join(self.config_dir, "hometiles", "sounds")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as file:
            file.write(b"\0" * size)
        return path

    async def test_own_sound_file(self):
        satellite = await self.start(sound="alarm.mp3")
        path = self.sound_file("alarm.mp3")
        await self.connect()
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        self.assertEqual(self.streams, [])  # The panel takes no Bridge streams yet.
        await self.deliver(f"{BASE}/stat/audio", json.dumps(
            {"v": 1, "state": "idle", "volume": 0.5, "stream": "tcp-ack-v1"}))
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        # A local file the Bridge chose: FFmpeg may read files for it.
        self.assertEqual(self.streams, [(path, 44100, True)])
        [play] = [c for c in self.bridge.audio if c.get("op") == "play"]
        self.assertEqual(play["url"], "tcp://192.168.1.2:8141/token")
        # At the timer volume, not the media volume.
        self.assertIs(play["timer_sound"], True)
        # The next round while the clip still plays: it keeps playing.
        self.bridge.audio_stream_url = lambda: path
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        self.assertEqual(len(self.streams), 1)
        # A file that is gone or too large is not streamed.
        self.bridge.audio_stream_url = lambda: None
        os.remove(path)
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        self.sound_file("alarm.mp3", TIMERS.MAX_SOUND_FILE_BYTES + 1)
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        self.assertEqual(len(self.streams), 1)
        await satellite.async_will_remove_from_hass()

    async def test_timers_from_before_a_reload(self):
        # Home Assistant still runs this panel's timers after a reload.
        mine = Timer(name="Pasta")
        mine.device_id = "dev1"
        other = Timer(id="OTHER")
        other.device_id = "dev2"
        self.manager.timers = {mine.id: mine, other.id: other}
        satellite = await self.start()
        await self.connect()
        [timers] = [c for c in self.timer_ops() if c["op"] == "timers"]
        self.assertEqual([t["id"] for t in timers["timers"]], [TIMER_ID])
        await self.request({"v": 1, "op": "pause", "id": TIMER_ID})
        self.assertEqual(self.manager.calls, [("pause", TIMER_ID)])
        await satellite.async_will_remove_from_hass()

    async def test_every_connect_sends_again(self):
        # A panel that restarted before its offline message: connected twice.
        satellite = await self.start()
        await self.connect()
        await self.connect()
        self.assertEqual([c["op"] for c in self.timer_ops()], ["timer_sound", "timers"] * 2)
        await satellite.async_will_remove_from_hass()

    async def test_pending_connect_send_ends_with_the_entity(self):
        self.satellite_module.TIMER_CONNECT_DELAY_S = 30
        satellite = await self.start()
        await self.deliver(f"{BASE}/stat/connected", "1")
        task = satellite._timer_connect_task
        await satellite.async_will_remove_from_hass()
        await settle()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.timer_ops(), [])

    async def test_built_in_sound_never_streams(self):
        satellite = await self.start(sound="marimba")
        await self.deliver(f"{BASE}/stat/audio", json.dumps(
            {"v": 1, "state": "idle", "volume": 0.5, "stream": "tcp-ack-v1"}))
        await self.request({"v": 1, "op": "play_sound", "id": TIMER_ID})
        self.assertEqual(self.streams, [])
        await satellite.async_will_remove_from_hass()


class TranslationsTest(unittest.TestCase):
    def test_every_language_names_the_sounds(self):
        from pathlib import Path

        root = Path(TIMERS.__file__).parent
        for path in [root / "strings.json", *sorted((root / "translations").glob("*.json"))]:
            select = json.loads(path.read_text(encoding="utf-8"))["entity"]["select"]["timer_sound"]
            self.assertEqual(set(select["state"]), {*TIMERS.BUILTIN_SOUNDS, "off"}, path.name)
            number = json.loads(path.read_text(encoding="utf-8"))["entity"]["number"]
            self.assertTrue(number["timer_volume"]["name"].strip(), path.name)


if __name__ == "__main__":
    unittest.main()
