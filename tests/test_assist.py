"""Assist contract (assist.py), the assist_satellite and button platforms against HA stubs."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
import enum
import importlib
import json
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT, load_module

ASSIST = load_module("assist")
CAPABILITIES = load_module("capabilities")
BASE = "hometiles/panel"
ID = "0123456789abcdef0123456789abcdef"
OTHER_ID = "fedcba9876543210"


def frame(session_id=ID, sequence=0, pcm=b"\x01\x00" * 4, last=False):
    return ASSIST.build_audio_frame(session_id, sequence, pcm, last)


class AssistContractTest(unittest.TestCase):
    def test_topics_and_unique_ids(self):
        self.assertEqual(ASSIST.assist_command_topic(BASE), f"{BASE}/cmnd/assist")
        self.assertEqual(ASSIST.assist_status_topic(BASE), f"{BASE}/stat/assist")
        self.assertEqual(ASSIST.assist_audio_topic(BASE), f"{BASE}/stat/assist_audio")
        self.assertEqual(ASSIST.assist_wake_topic(BASE), f"{BASE}/stat/assist_wake")
        self.assertEqual(ASSIST.assist_satellite_unique_id("mac"), "mac_assist_satellite")
        self.assertEqual(ASSIST.assist_button_unique_id("mac"), "mac_assist")
        # AssistPipelineSelect / VadSensitivitySelect append these suffixes.
        self.assertEqual(ASSIST.assist_select_prefix("mac"), "mac_assist")
        self.assertEqual(ASSIST.assist_pipeline_select_unique_id("mac"), "mac_assist-pipeline")
        self.assertEqual(ASSIST.assist_vad_select_unique_id("mac"),
                         "mac_assist-vad_sensitivity")

    def test_commands(self):
        self.assertEqual(json.loads(ASSIST.build_start_command(ID)),
                         {"v": 1, "op": "start", "id": ID, "max_ms": 15000})
        self.assertEqual(json.loads(ASSIST.build_start_command(ID, 99999))["max_ms"], 30000)
        self.assertEqual(json.loads(ASSIST.build_start_command(ID, 10))["max_ms"], 1000)
        self.assertEqual(json.loads(ASSIST.build_start_command(ID, True))["max_ms"], 15000)
        self.assertEqual(json.loads(ASSIST.build_stop_command(ID)),
                         {"v": 1, "op": "stop", "id": ID})
        for bad in ("ABCDEF0123456789", "0123", "g" * 16, "a" * 33, None):
            with self.assertRaises(ValueError):
                ASSIST.build_start_command(bad)
            with self.assertRaises(ValueError):
                ASSIST.build_stop_command(bad)
        session_id = ASSIST.new_session_id()
        self.assertTrue(ASSIST.valid_session_id(session_id))
        self.assertNotEqual(session_id, ASSIST.new_session_id())

    def test_frame_header(self):
        pcm = bytes(range(10))
        parsed = ASSIST.parse_audio_frame(frame(sequence=0x01020304, pcm=pcm))
        self.assertEqual(parsed, (ID, False, 0x01020304, pcm))
        raw = b"HTA1" + bytes([16]) + OTHER_ID.encode() + b"\x01" + b"\x07\x00\x00\x00"
        self.assertEqual(ASSIST.parse_audio_frame(bytearray(raw)), (OTHER_ID, True, 7, b""))
        self.assertEqual(ASSIST.parse_audio_frame(memoryview(raw)), (OTHER_ID, True, 7, b""))

    def test_frame_header_rejects_malformed(self):
        good = frame()
        cases = {
            "wrong magic": b"HTA2" + good[4:],
            "id too short": b"HTA1" + bytes([15]) + b"0" * 15 + good[21:],
            "id too long": b"HTA1" + bytes([33]) + b"0" * 33 + b"\x00" * 5,
            "uppercase id": b"HTA1" + bytes([16]) + b"ABCDEF0123456789" + b"\x00" * 5,
            "non-ascii id": b"HTA1" + bytes([16]) + b"\xff" * 16 + b"\x00" * 5,
            "short header": good[:5 + 32 + 3],
            "only magic": b"HTA1",
            "empty": b"",
            "odd pcm": frame(pcm=b"\x00\x01\x02"),
            "oversized": frame(pcm=b"\x00" * (ASSIST.FRAME_MAX_PCM_BYTES + 2)),
        }
        for name, payload in cases.items():
            self.assertIsNone(ASSIST.parse_audio_frame(payload), name)
        self.assertIsNone(ASSIST.parse_audio_frame("HTA1"))
        self.assertIsNone(ASSIST.parse_audio_frame(None))

    def test_status(self):
        self.assertEqual(ASSIST.parse_status(json.dumps({"v": 1, "state": "listening", "id": ID})),
                         {"state": "listening", "id": ID, "error": None,
                          "wake_words": None, "active_wake_words": None,
                          "mic_enabled": None})
        self.assertEqual(
            ASSIST.parse_status(json.dumps({"v": 1, "state": "idle", "id": ID,
                                            "error": "timeout"}).encode()),
            {"state": "idle", "id": ID, "error": "timeout",
             "wake_words": None, "active_wake_words": None,
                          "mic_enabled": None})
        self.assertEqual(ASSIST.parse_status(json.dumps({"v": 1, "state": "idle",
                                                         "error": "new_code"}))["error"],
                         "unknown")
        self.assertIsNone(ASSIST.parse_status(json.dumps({"v": 1, "state": "idle",
                                                          "id": "bad"}))["id"])
        for bad in ({"v": 2, "state": "idle"}, {"v": 1, "state": "speaking"}, {"v": 1}, []):
            self.assertIsNone(ASSIST.parse_status(json.dumps(bad)), bad)
        self.assertIsNone(ASSIST.parse_status("not json"))
        self.assertIsNone(ASSIST.parse_status(b"\xff"))
        self.assertIsNone(ASSIST.parse_status("{" + " " * 600 + "}"))

    def test_status_wake_word_fields(self):
        status = ASSIST.parse_status(json.dumps({
            "v": 1, "state": "wake", "wake_words": ["okay_nabu", "hey_jarvis"],
            "active_wake_words": ["okay_nabu"]}))
        self.assertEqual(status["state"], "wake")
        self.assertEqual(status["wake_words"], ["okay_nabu", "hey_jarvis"])
        self.assertEqual(status["active_wake_words"], ["okay_nabu"])
        # Reported but empty differs from not reported.
        status = ASSIST.parse_status(json.dumps({
            "v": 1, "state": "idle", "wake_words": [], "active_wake_words": []}))
        self.assertEqual((status["wake_words"], status["active_wake_words"]), ([], []))
        # Invalid and duplicate ids are dropped; active words must be available.
        status = ASSIST.parse_status(json.dumps({
            "v": 1, "state": "idle",
            "wake_words": ["okay_nabu", "Okay Nabu", 7, "okay_nabu", "x" * 33, "alexa"],
            "active_wake_words": ["alexa", "hey_jarvis"]}))
        self.assertEqual(status["wake_words"], ["okay_nabu", "alexa"])
        self.assertEqual(status["active_wake_words"], ["alexa"])
        status = ASSIST.parse_status(json.dumps({
            "v": 1, "state": "idle", "wake_words": "okay_nabu", "active_wake_words": {}}))
        self.assertEqual((status["wake_words"], status["active_wake_words"]), (None, None))
        many = ASSIST.parse_status(json.dumps({
            "v": 1, "state": "idle", "wake_words": [f"w{i}" for i in range(40)]}))
        self.assertEqual(len(many["wake_words"]), ASSIST.WAKE_WORDS_MAX)

    def test_mic_command_and_status(self):
        self.assertEqual(json.loads(ASSIST.build_mic_command(False)),
                         {"v": 1, "op": "mic", "enabled": False})
        self.assertEqual(json.loads(ASSIST.build_mic_command(True)),
                         {"v": 1, "op": "mic", "enabled": True})
        for bad in (None, 1, "false"):
            with self.assertRaises(ValueError):
                ASSIST.build_mic_command(bad)
        status = ASSIST.parse_status(json.dumps({"v": 1, "state": "idle", "error": "mic_off",
                                                 "mic_enabled": False}))
        self.assertEqual((status["error"], status["mic_enabled"]), ("mic_off", False))
        self.assertIsNone(ASSIST.parse_status(json.dumps(
            {"v": 1, "state": "idle", "mic_enabled": "no"}))["mic_enabled"])
        self.assertEqual(ASSIST.assist_microphone_switch_unique_id("mac"), "mac_assist_microphone")

    def test_wake_words_command(self):
        self.assertEqual(json.loads(ASSIST.build_wake_words_command(["okay_nabu"])),
                         {"v": 1, "op": "wake_words", "active": ["okay_nabu"]})
        self.assertEqual(json.loads(ASSIST.build_wake_words_command([])),
                         {"v": 1, "op": "wake_words", "active": []})
        self.assertEqual(json.loads(ASSIST.build_wake_words_command(("alexa", "alexa"))),
                         {"v": 1, "op": "wake_words", "active": ["alexa"]})
        for bad in (None, "okay_nabu", ["Okay Nabu"], [""], [1], ["okay_nabu", "alexa"]):
            with self.assertRaises(ValueError):
                ASSIST.build_wake_words_command(bad)

    def test_wake_word_names(self):
        self.assertEqual(ASSIST.wake_word_info("okay_nabu"), ("Okay Nabu", ["en"]))
        self.assertEqual(ASSIST.wake_word_info("hey_jarvis"), ("Hey Jarvis", ["en"]))
        self.assertEqual(ASSIST.wake_word_info("choo_choo_homie"), ("Choo Choo Homie", []))
        self.assertEqual(ASSIST.wake_word_phrase("okay_nabu"), "Okay Nabu")

    def test_wake_event(self):
        available = ["okay_nabu"]
        event = ASSIST.parse_wake_event(
            json.dumps({"v": 1, "word": "okay_nabu", "seq": 7}).encode(), available)
        self.assertEqual(event, ("okay_nabu", 7))
        self.assertEqual(ASSIST.parse_wake_event(
            json.dumps({"v": 1, "word": "okay_nabu", "seq": 0xFFFFFFFF}), available).sequence,
            0xFFFFFFFF)
        bad_cases = [
            {"v": 2, "word": "okay_nabu", "seq": 1},
            {"v": 1, "word": "hey_jarvis", "seq": 1},
            {"v": 1, "word": "Okay Nabu", "seq": 1},
            {"v": 1, "seq": 1},
            {"v": 1, "word": "okay_nabu"},
            {"v": 1, "word": "okay_nabu", "seq": "1"},
            {"v": 1, "word": "okay_nabu", "seq": True},
            {"v": 1, "word": "okay_nabu", "seq": 1.0},
            {"v": 1, "word": "okay_nabu", "seq": -1},
            {"v": 1, "word": "okay_nabu", "seq": 0x100000000},
            ["okay_nabu"],
        ]
        for bad in bad_cases:
            self.assertIsNone(ASSIST.parse_wake_event(json.dumps(bad), available), bad)
        good = json.dumps({"v": 1, "word": "okay_nabu", "seq": 1})
        self.assertIsNone(ASSIST.parse_wake_event(good, []))
        self.assertIsNone(ASSIST.parse_wake_event(good, None))
        self.assertIsNone(ASSIST.parse_wake_event("not json", available))
        self.assertIsNone(ASSIST.parse_wake_event(b"\xff", available))
        self.assertIsNone(ASSIST.parse_wake_event(None, available))
        self.assertIsNone(ASSIST.parse_wake_event("{" + " " * 300 + "}", available))

    def test_capability_is_never_inferred(self):
        self.assertEqual(CAPABILITIES.normalise_capabilities({"audio_input": True}),
                         {"audio_input": True})
        with self.assertRaises(ValueError):
            CAPABILITIES.normalise_capabilities({"audio_input": 1})
        self.assertFalse(CAPABILITIES.supports({"model": "tab5"}, "audio_input"))
        announced = {"capabilities": {"audio_input": True}}
        self.assertTrue(CAPABILITIES.supports(announced, "audio_input"))
        stale = CAPABILITIES.stale_assist
        self.assertTrue(stale("mac_assist_satellite", "assist_satellite", {}))
        self.assertTrue(stale("mac_assist", "button", {}))
        self.assertFalse(stale("mac_assist_satellite", "assist_satellite", announced))
        self.assertFalse(stale("mac_assist", "button", announced))
        self.assertFalse(stale("mac_other", "button", {}))
        self.assertFalse(stale("mac_assist", "switch", {}))
        self.assertTrue(stale("mac_assist_microphone", "switch", {}))
        self.assertFalse(stale("mac_assist_microphone", "switch", announced))
        self.assertTrue(stale("mac_assist-pipeline", "select", {}))
        self.assertTrue(stale("mac_assist-vad_sensitivity", "select", {}))
        self.assertFalse(stale("mac_assist-pipeline", "select", announced))
        self.assertTrue(stale("mac_assist_wake_word", "select", {}))
        self.assertFalse(stale("mac_view", "select", {}))

    def test_assist_is_sealed_data(self):
        self.assertIn("assist", load_module("command_channel").SEALED_DATA)

    def test_platform_registration(self):
        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        values = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                  for target in node.targets if isinstance(target, ast.Name)}
        self.assertIn("button", ast.literal_eval(values["PLATFORMS"]))
        self.assertNotIn("assist_satellite", ast.literal_eval(values["PLATFORMS"]))
        self.assertEqual(ast.literal_eval(values["ASSIST_PLATFORMS"]), ["assist_satellite"])
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("assist_satellite", manifest["after_dependencies"])
        self.assertIn("assist_pipeline", manifest["after_dependencies"])

    def test_translations(self):
        for name in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads((ROOT / name).read_text(encoding="utf-8"))
            self.assertIn("name", data["entity"]["assist_satellite"]["assist"], name)
            self.assertIn("name", data["entity"]["button"]["assist"], name)
            selects = data["entity"]["select"]
            self.assertTrue({"off", *ASSIST.KNOWN_WAKE_WORDS} <= set(selects["wake_word"]["state"]),
                            name)
            self.assertEqual(set(selects["pipeline"]["state"]), {"preferred"}, name)
            self.assertEqual(set(selects["vad_sensitivity"]["state"]),
                             {"default", "aggressive", "relaxed"}, name)
            self.assertIn("name", data["entity"]["switch"]["microphone"], name)
            for key in ("assist_unavailable", "assist_not_connected", "microphone_needs_pairing"):
                self.assertIn("message", data["exceptions"][key], name)


class AssistSelectsTest(unittest.IsolatedAsyncioTestCase):
    """The Assistant select is what lists the panel under Assist devices."""

    async def created(self, capabilities, components=("assist_pipeline",)):
        class Select:
            def __init__(self, *args):
                self.args = args

        class PipelineSelect(Select):
            pass

        class VadSelect(Select):
            pass

        pipeline = types.ModuleType("homeassistant.components.assist_pipeline")
        pipeline.AssistPipelineSelect = PipelineSelect
        pipeline.VadSensitivitySelect = VadSelect
        scope = {
            "DOMAIN": "tab5_lvgl", "entry_base_topic": lambda entry: BASE,
            "entry_device_id": lambda entry: "mac",
            "entry_device_info": lambda entry: {"identifiers": {("tab5_lvgl", "mac")}},
            "assist_select_prefix": ASSIST.assist_select_prefix,
            "merged_capabilities_data": CAPABILITIES.merged_capabilities_data,
            "supports": CAPABILITIES.supports,
            "Tab5SleepSelect": lambda *args: "sleep",
            "HomeTilesViewSelect": lambda *args: "view",
            "HomeTilesWakeWordSelect": lambda *args: "wake_word",
            "TOPIC_SLEEP_MAINS": "sleep_mains", "TOPIC_SLEEP_BATTERY": "sleep_battery",
        }
        tree = ast.parse((ROOT / "select.py").read_text(encoding="utf-8"))
        names = {"async_setup_entry", "_assist_selects"}
        tree = ast.Module(body=[node for node in tree.body if getattr(node, "name", None) in names],
                          type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), "select.py", "exec"), scope)
        hass = types.SimpleNamespace(config=types.SimpleNamespace(components=set(components)))
        created = []
        entry = types.SimpleNamespace(data={"capabilities": capabilities}, options={})
        with mock.patch.dict(sys.modules, {
                "homeassistant.components.assist_pipeline": pipeline}):
            await scope["async_setup_entry"](hass, entry, created.extend)
        return hass, created, PipelineSelect, VadSelect

    def test_select_module_names_resolve(self):
        tree = ast.parse((ROOT / "select.py").read_text(encoding="utf-8"))
        imported = {alias.asname or alias.name for node in tree.body
                    if isinstance(node, ast.ImportFrom) for alias in node.names}
        self.assertTrue({"DOMAIN", "assist_select_prefix", "entry_device_id",
                         "entry_device_info"} <= imported)

    async def test_selects_only_with_microphone(self):
        _, created, _, _ = await self.created({})
        self.assertEqual(created, ["sleep", "sleep"])

    async def test_no_selects_without_assist_pipeline(self):
        _, created, _, _ = await self.created({"audio_input": True}, components=())
        self.assertEqual(created, ["sleep", "sleep", "wake_word"])

    async def test_selects_join_the_panel_device(self):
        hass, created, PipelineSelect, VadSelect = await self.created({"audio_input": True})
        self.assertEqual(created[2], "wake_word")
        pipeline, vad = created[3:]
        self.assertIsInstance(pipeline, PipelineSelect)
        self.assertEqual(pipeline.args, (hass, "tab5_lvgl", "mac_assist"))
        self.assertIsInstance(vad, VadSelect)
        self.assertEqual(vad.args, (hass, "mac_assist"))
        for select in (pipeline, vad):
            self.assertTrue(select._attr_has_entity_name)
            self.assertEqual(select._attr_device_info, {"identifiers": {("tab5_lvgl", "mac")}})


class WakeWordSelectTest(unittest.IsolatedAsyncioTestCase):
    """The Wake word select mirrors the panel status and sets it via the satellite."""

    def make(self):
        class Entity:
            async def async_added_to_hass(self):
                pass

        class Category:
            CONFIG = "config"

        written = []
        scope = {"SelectEntity": Entity, "EntityCategory": Category,
                 "HomeAssistantError": FakeHomeAssistantError, "DOMAIN": "tab5_lvgl",
                 "ASSIST_SATELLITES_KEY": ASSIST.ASSIST_SATELLITES_KEY,
                 "assist_status_topic": ASSIST.assist_status_topic,
                 "assist_wake_word_select_unique_id": ASSIST.assist_wake_word_select_unique_id,
                 "parse_status": ASSIST.parse_status,
                 "entry_device_info": lambda entry: {}, "entry_device_id": lambda entry: "mac",
                 "ConfigEntry": object}
        tree = ast.parse((ROOT / "select.py").read_text(encoding="utf-8"))
        body = [node for node in tree.body
                if getattr(node, "name", None) == "HomeTilesWakeWordSelect"
                or (isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "WAKE_WORD_OFF" for t in node.targets))]
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                     "select.py", "exec"), scope)
        select = scope["HomeTilesWakeWordSelect"](types.SimpleNamespace(entry_id="e1"), BASE)
        select.async_write_ha_state = lambda: written.append(select.current_option)
        return select, written

    def status(self, **fields):
        payload = {"v": 1, "state": "idle", "wake_words": ["okay_nabu"],
                   "active_wake_words": []}
        payload.update(fields)
        return json.dumps(payload)

    def test_unavailable_until_the_panel_reports(self):
        select, _ = self.make()
        self.assertEqual(select._attr_unique_id, "mac_assist_wake_word")
        self.assertFalse(select.available)
        self.assertIsNone(select.current_option)
        self.assertTrue(select.apply_status(self.status()))
        self.assertTrue(select.available)
        self.assertEqual(select.options, ["okay_nabu", "off"])
        self.assertEqual(select.current_option, "off")
        self.assertTrue(select.apply_status(self.status(active_wake_words=["okay_nabu"])))
        self.assertEqual(select.current_option, "okay_nabu")
        self.assertFalse(select.apply_status("not json"))
        self.assertTrue(select.apply_status(""))
        self.assertFalse(select.available)

    async def test_select_sends_through_the_satellite(self):
        select, written = self.make()
        select.apply_status(self.status())
        sent = []

        class Satellite:
            async def async_set_active_wake_words(self, words):
                sent.append(words)

        select.hass = types.SimpleNamespace(data={"tab5_lvgl": {
            ASSIST.ASSIST_SATELLITES_KEY: {"e1": Satellite()}}})
        await select.async_select_option("okay_nabu")
        await select.async_select_option("off")
        self.assertEqual(sent, [["okay_nabu"], []])
        self.assertEqual(written, ["okay_nabu", "off"])
        with self.assertRaises(FakeHomeAssistantError):
            await select.async_select_option("alexa")

    async def test_select_without_satellite_fails(self):
        select, _ = self.make()
        select.apply_status(self.status())
        select.hass = types.SimpleNamespace(data={})
        with self.assertRaises(FakeHomeAssistantError):
            await select.async_select_option("okay_nabu")


class PublishHelperTest(unittest.IsolatedAsyncioTestCase):
    async def test_sealed_plain_and_paired_without_bridge(self):
        published = []

        async def publish(hass, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

        class Bridge:
            sent = []

            async def async_publish_assist_command(self, text):
                self.sent.append(text)

        bridge = Bridge()
        self.assertTrue(await ASSIST.async_publish_assist_command(None, bridge, True, BASE, "x", publish))
        self.assertEqual((bridge.sent, published), (["x"], []))
        self.assertFalse(await ASSIST.async_publish_assist_command(None, None, True, BASE, "x", publish))
        self.assertEqual(published, [])
        self.assertTrue(await ASSIST.async_publish_assist_command(None, None, False, BASE, "y", publish))
        self.assertEqual(published, [(f"{BASE}/cmnd/assist", "y", 0, False)])


async def collect(session):
    return [chunk async for chunk in session.stream()]


class AudioSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_ends_on_last_flag(self):
        session = ASSIST.AudioSession(ID)
        self.assertTrue(session.feed(ASSIST.parse_audio_frame(frame(sequence=0, pcm=b"\x01\x00"))))
        # Other sessions, duplicates and old frames are dropped.
        self.assertFalse(session.feed(ASSIST.parse_audio_frame(frame(OTHER_ID, 1, b"\x09\x00"))))
        self.assertFalse(session.feed(ASSIST.parse_audio_frame(frame(sequence=0, pcm=b"\x08\x00"))))
        self.assertTrue(session.feed(ASSIST.parse_audio_frame(frame(sequence=2, pcm=b"\x02\x00"))))
        self.assertFalse(session.feed(ASSIST.parse_audio_frame(frame(sequence=1, pcm=b"\x07\x00"))))
        self.assertTrue(session.feed(ASSIST.parse_audio_frame(frame(sequence=3, pcm=b"", last=True))))
        self.assertTrue(session.finished)
        self.assertFalse(session.feed(ASSIST.parse_audio_frame(frame(sequence=4))))
        self.assertEqual(await collect(session), [b"\x01\x00", b"\x02\x00"])
        self.assertEqual(session.dropped, 2)

    async def test_stream_ends_on_finish(self):
        session = ASSIST.AudioSession(ID)
        session.feed(ASSIST.parse_audio_frame(frame(sequence=0, pcm=b"\x01\x00")))
        task = asyncio.ensure_future(collect(session))
        await asyncio.sleep(0)
        session.finish()
        session.finish()
        self.assertEqual(await asyncio.wait_for(task, 1), [b"\x01\x00"])

    async def test_stream_ends_after_timeout_without_frames(self):
        session = ASSIST.AudioSession(ID, frame_timeout_s=0.05)
        session.feed(ASSIST.parse_audio_frame(frame(sequence=0, pcm=b"\x01\x00")))
        self.assertEqual(await asyncio.wait_for(collect(session), 1), [b"\x01\x00"])
        self.assertTrue(session.finished)
        self.assertEqual(ASSIST.FRAME_TIMEOUT_S, 2.0)

    async def test_queue_is_bounded(self):
        session = ASSIST.AudioSession(ID, max_frames=2)
        for sequence in range(4):
            session.feed(ASSIST.parse_audio_frame(frame(sequence=sequence)))
        session.finish()
        self.assertEqual(len(await collect(session)), 2)
        self.assertEqual(session.dropped, 2)


# Home Assistant stubs ---------------------------------------------------------


class FakeMqtt(types.ModuleType):
    def __init__(self):
        super().__init__("homeassistant.components.mqtt")
        self.subscriptions = {}
        self.encodings = {}
        self.published = []
        self.unsubscribed = 0
        self.ReceiveMessage = types.SimpleNamespace

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscriptions[topic] = handler
        self.encodings[topic] = encoding

        def unsubscribe():
            self.unsubscribed += 1
        return unsubscribe

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))


class EventType(enum.StrEnum):
    RUN_START = "run-start"
    RUN_END = "run-end"
    STT_START = "stt-start"
    STT_VAD_START = "stt-vad-start"
    STT_VAD_END = "stt-vad-end"
    STT_END = "stt-end"
    INTENT_START = "intent-start"
    INTENT_END = "intent-end"
    TTS_START = "tts-start"
    TTS_END = "tts-end"
    ERROR = "error"


class Stage(enum.StrEnum):
    WAKE_WORD = "wake_word"
    STT = "stt"
    INTENT = "intent"
    TTS = "tts"


@dataclass(frozen=True)
class Event:
    type: EventType
    data: dict | None = None


@dataclass(frozen=True)
class WakeWord:
    id: str
    wake_word: str
    trained_languages: list


@dataclass
class Configuration:
    available_wake_words: list
    active_wake_words: list
    max_active_wake_words: int


class FakeSatelliteEntity:
    """The parts of AssistSatelliteEntity the platform relies on."""

    hass = None
    written = 0
    tts_finished = 0
    pipeline = None
    _attr_tts_options = None

    @property
    def available(self):
        return self._attr_available

    @property
    def tts_options(self):
        return self._attr_tts_options

    def async_write_ha_state(self):
        self.written += 1

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass

    def tts_response_finished(self):
        self.tts_finished += 1

    async def async_accept_pipeline_from_satellite(self, audio_stream, start_stage=Stage.STT,
                                                   end_stage=Stage.TTS, wake_word_phrase=None):
        self.stages = (start_stage, end_stage)
        self.wake_word_phrase = wake_word_phrase
        await self.pipeline(self, audio_stream)


class FakeButtonEntity:
    hass = None


class FakeHomeAssistantError(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None, **kwargs):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key


def fake_process_url(hass, media_id):
    if media_id.startswith("/"):
        return f"{hass.internal}{media_id}?authSig=x"
    return media_id


def load_platforms(fake_mqtt):
    package_name = "_hometiles_assist_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    components.mqtt = fake_mqtt
    pipeline = types.ModuleType("homeassistant.components.assist_pipeline")
    pipeline.PipelineEvent = Event
    pipeline.PipelineEventType = EventType
    pipeline.PipelineStage = Stage
    satellite = types.ModuleType("homeassistant.components.assist_satellite")
    satellite.AssistSatelliteEntity = FakeSatelliteEntity
    satellite.AssistSatelliteConfiguration = Configuration
    satellite.AssistSatelliteWakeWord = WakeWord
    button = types.ModuleType("homeassistant.components.button")
    button.ButtonEntity = FakeButtonEntity
    media_player = types.ModuleType("homeassistant.components.media_player")
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
        "homeassistant.components.assist_pipeline": pipeline,
        "homeassistant.components.assist_satellite": satellite,
        "homeassistant.components.button": button,
        "homeassistant.components.media_player": media_player,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return (importlib.import_module(f"{package_name}.assist_satellite"),
                importlib.import_module(f"{package_name}.button"))


def entry(capabilities=None, **data):
    values = {"device_id": "mac", "base_topic": BASE, "model": "waveshare_touch_lcd_7b"}
    values.update(data)
    if capabilities is not None:
        values["capabilities"] = capabilities
    return types.SimpleNamespace(entry_id="e1", data=values, options={})


def message(topic, payload, retain=False):
    return types.SimpleNamespace(topic=topic, payload=payload, retain=retain)


class FakeHass:
    def __init__(self, bridge=None):
        self.internal = "http://192.168.1.2:8123"
        self.data = {"tab5_lvgl": {"entries": {"e1": bridge} if bridge else {}}}
        self.tasks = []

    def async_create_task(self, coro, name=None, eager_start=True):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    def async_create_background_task(self, coro, name, eager_start=True):
        return self.async_create_task(coro, name)


class SealingBridge:
    def __init__(self):
        self.assist = []
        self.audio = []

    async def async_publish_assist_command(self, text):
        self.assist.append(json.loads(text))

    async def async_publish_audio_command(self, text):
        self.audio.append(json.loads(text))


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class AssistSatellitePlatformTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.satellite_module, self.button_module = load_platforms(self.mqtt)
        self.chunks = []
        self.events = []
        self.pipeline_done = asyncio.Event()

    async def created(self, module, config_entry):
        result = []
        await module.async_setup_entry(None, config_entry, result.extend)
        return result

    async def start(self, bridge=None, pipeline=None):
        [satellite] = await self.created(self.satellite_module, entry({"audio_input": True}))
        satellite.hass = FakeHass(bridge)
        satellite.entity_id = "assist_satellite.panel_assist"
        satellite.pipeline = pipeline or self.vad_pipeline
        await satellite.async_added_to_hass()
        return satellite

    async def deliver(self, topic, payload):
        await self.mqtt.subscriptions[topic](message(topic, payload))

    async def send_frames(self, session_id, count, start=0):
        for sequence in range(start, start + count):
            await self.deliver(f"{BASE}/stat/assist_audio",
                               frame(session_id, sequence, bytes([sequence, 0])))

    def emit(self, satellite, event_type, data=None):
        self.events.append(event_type)
        satellite.on_pipeline_event(Event(event_type, data))

    async def vad_pipeline(self, satellite, stream):
        """Read two chunks, detect the end of speech, answer with TTS."""
        self.emit(satellite, EventType.RUN_START)
        self.emit(satellite, EventType.STT_START)
        async for chunk in stream:
            self.chunks.append(chunk)
            if len(self.chunks) == 2:
                break
        self.emit(satellite, EventType.STT_VAD_END)
        self.emit(satellite, EventType.STT_VAD_END)
        self.emit(satellite, EventType.STT_END, {"stt_output": {"text": "hi"}})
        self.emit(satellite, EventType.TTS_START, {"tts_input": "Hello"})
        self.emit(satellite, EventType.TTS_END, {"tts_output": {
            "media_id": "media-source://tts/x", "token": "abc.mp3",
            "url": "/api/tts_proxy/abc.mp3", "mime_type": "audio/mpeg"}})
        self.emit(satellite, EventType.RUN_END)
        self.pipeline_done.set()

    def assist_commands(self):
        return [payload for topic, payload, _, _ in self.mqtt.published
                if topic == f"{BASE}/cmnd/assist"]

    def audio_commands(self):
        return [payload for topic, payload, _, _ in self.mqtt.published
                if topic == f"{BASE}/cmnd/audio"]

    async def test_entities_only_for_announced_capability(self):
        for module in (self.satellite_module, self.button_module):
            self.assertEqual(await self.created(module, entry()), [])
            self.assertEqual(await self.created(module, entry({"audio_input": False})), [])
            self.assertEqual(await self.created(module, entry({"audio_output": True})), [])
            stale = entry({"audio_input": False})
            stale.options = {"capabilities": {"audio_input": True}}
            self.assertEqual(await self.created(module, stale), [])
        [satellite] = await self.created(self.satellite_module, entry({"audio_input": True}))
        self.assertEqual(satellite._attr_unique_id, "mac_assist_satellite")
        self.assertEqual(satellite._attr_translation_key, "assist")
        self.assertTrue(satellite._attr_has_entity_name)
        self.assertEqual(satellite.tts_options, {"preferred_format": "mp3"})
        self.assertFalse(hasattr(satellite, "_attr_supported_features"))
        [button] = await self.created(self.button_module, entry({"audio_input": True}))
        self.assertEqual(button._attr_unique_id, "mac_assist")
        self.assertEqual(button._attr_translation_key, "assist")

    async def announce_wake_words(self, available=("okay_nabu",), active=()):
        await self.deliver(f"{BASE}/stat/assist", json.dumps({
            "v": 1, "state": "idle", "wake_words": list(available),
            "active_wake_words": list(active)}))

    async def wake(self, word="okay_nabu", seq=1):
        await self.deliver(f"{BASE}/stat/assist_wake",
                           json.dumps({"v": 1, "word": word, "seq": seq}))

    async def test_configuration_without_announced_wake_words(self):
        satellite = await self.start()
        config = satellite.async_get_configuration()
        self.assertEqual((config.available_wake_words, config.active_wake_words,
                          config.max_active_wake_words), ([], [], 1))
        # Nothing is available, so the request turns detection off.
        await satellite.async_set_configuration(Configuration([], ["okay_nabu"], 1))
        self.assertEqual(self.assist_commands(), [{"v": 1, "op": "wake_words", "active": []}])

    async def test_configuration_maps_the_panel_wake_words(self):
        satellite = await self.start()
        await self.announce_wake_words(("okay_nabu", "choo_choo_homie"), ("okay_nabu",))
        config = satellite.async_get_configuration()
        self.assertEqual(config.available_wake_words, [
            WakeWord(id="okay_nabu", wake_word="Okay Nabu", trained_languages=["en"]),
            WakeWord(id="choo_choo_homie", wake_word="Choo Choo Homie", trained_languages=[]),
        ])
        self.assertEqual(config.active_wake_words, ["okay_nabu"])
        self.assertEqual(config.max_active_wake_words, 1)
        # A later status without the fields keeps the words (older payload shape).
        await self.deliver(f"{BASE}/stat/assist", json.dumps({"v": 1, "state": "listening"}))
        self.assertEqual(satellite.async_get_configuration().active_wake_words, ["okay_nabu"])
        # Turned off on the panel.
        await self.announce_wake_words(("okay_nabu", "choo_choo_homie"), ())
        self.assertEqual(satellite.async_get_configuration().active_wake_words, [])

    async def test_set_configuration_sends_the_wake_words_command(self):
        satellite = await self.start()
        await self.announce_wake_words(("okay_nabu", "hey_jarvis"))
        written = satellite.written
        await satellite.async_set_configuration(
            Configuration([], ["hey_jarvis", "unknown", "okay_nabu"], 1))
        self.assertEqual(self.assist_commands(),
                         [{"v": 1, "op": "wake_words", "active": ["hey_jarvis"]}])
        self.assertEqual(self.mqtt.published[0][2:], (0, False))
        self.assertEqual(satellite.async_get_configuration().active_wake_words, ["hey_jarvis"])
        self.assertEqual(satellite.written, written + 1)
        await satellite.async_set_configuration(Configuration([], [], 1))
        self.assertEqual(self.assist_commands()[-1], {"v": 1, "op": "wake_words", "active": []})
        self.assertEqual(satellite.async_get_configuration().active_wake_words, [])

    async def test_set_configuration_is_sealed_when_paired(self):
        bridge = SealingBridge()
        satellite = await self.start(bridge=bridge)
        satellite._paired = True
        await self.announce_wake_words()
        await satellite.async_set_configuration(Configuration([], ["okay_nabu"], 1))
        self.assertEqual(bridge.assist, [{"v": 1, "op": "wake_words", "active": ["okay_nabu"]}])
        self.assertEqual(self.mqtt.published, [])

    async def test_set_configuration_paired_without_bridge_fails(self):
        satellite = await self.start()
        satellite._paired = True
        await self.announce_wake_words()
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await satellite.async_set_configuration(Configuration([], ["okay_nabu"], 1))
        self.assertEqual(raised.exception.translation_key, "assist_not_connected")
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(satellite.async_get_configuration().active_wake_words, [])

    async def test_wake_word_starts_a_run_with_its_phrase(self):
        satellite = await self.start()
        await self.announce_wake_words(active=("okay_nabu",))
        await self.wake()
        [start] = self.assist_commands()
        self.assertEqual(start["op"], "start")
        self.assertEqual(start["max_ms"], 15000)
        await self.send_frames(start["id"], 2)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(satellite.stages, (Stage.STT, Stage.TTS))
        self.assertEqual(satellite.wake_word_phrase, "Okay Nabu")
        self.assertEqual(self.assist_commands()[1], {"v": 1, "op": "stop", "id": start["id"]})
        self.assertEqual(len(self.audio_commands()), 1)

    async def test_button_run_has_no_wake_word_phrase(self):
        satellite = await self.start()
        await satellite.async_start_tap_to_talk()
        await self.send_frames(self.assist_commands()[0]["id"], 2)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        self.assertIsNone(satellite.wake_word_phrase)

    async def test_wake_word_paired_uses_the_sealed_channel(self):
        bridge = SealingBridge()
        satellite = await self.start(bridge=bridge)
        satellite._paired = True
        await self.announce_wake_words()
        await self.wake()
        self.assertEqual([c["op"] for c in bridge.assist], ["start"])
        self.assertEqual(self.mqtt.published, [])
        await satellite.async_will_remove_from_hass()

    async def test_invalid_or_unknown_wake_events_are_ignored(self):
        satellite = await self.start()
        # Before the status announced any word, nothing is available.
        await self.wake()
        await self.announce_wake_words()
        await self.wake(word="hey_jarvis")
        for payload in ("not json", json.dumps({"v": 2, "word": "okay_nabu", "seq": 1}),
                        json.dumps({"v": 1, "word": "okay_nabu", "seq": "1"}), ""):
            await self.deliver(f"{BASE}/stat/assist_wake", payload)
        await settle()
        self.assertEqual(self.assist_commands(), [])
        self.assertIsNone(satellite._session)

    async def test_wake_events_during_a_run_are_ignored(self):
        blocked = asyncio.Event()

        async def pipeline(satellite, stream):
            blocked.set()
            await asyncio.Event().wait()

        satellite = await self.start(pipeline=pipeline)
        await self.announce_wake_words()
        await self.wake(seq=1)
        await asyncio.wait_for(blocked.wait(), 1)
        session = satellite._session
        await self.wake(seq=2)
        await settle()
        self.assertEqual([c["op"] for c in self.assist_commands()], ["start"])
        self.assertIs(satellite._session, session)
        # A button press still replaces the run.
        await satellite.async_start_tap_to_talk()
        self.assertEqual([c["op"] for c in self.assist_commands()], ["start", "stop", "start"])
        await satellite.async_will_remove_from_hass()

    async def test_wake_event_on_unavailable_panel_is_dropped(self):
        satellite = await self.start()
        await self.announce_wake_words()
        await self.deliver(f"{BASE}/stat/connected", "0")
        await self.wake()
        self.assertEqual(self.assist_commands(), [])
        self.assertIsNone(satellite._session)

    async def test_subscriptions(self):
        satellite = await self.start()
        self.assertEqual(set(self.mqtt.subscriptions), {
            f"{BASE}/stat/assist_audio", f"{BASE}/stat/assist", f"{BASE}/stat/assist_wake",
            f"{BASE}/stat/connected", f"{BASE}/stat/audio"})
        # Microphone frames are binary.
        self.assertIsNone(self.mqtt.encodings[f"{BASE}/stat/assist_audio"])
        self.assertEqual(self.mqtt.encodings[f"{BASE}/stat/assist"], "utf-8")
        await satellite.async_will_remove_from_hass()
        self.assertEqual(self.mqtt.unsubscribed, 5)

    async def test_tap_to_talk_run(self):
        satellite = await self.start()
        await satellite.async_start_tap_to_talk()
        [start] = self.assist_commands()
        session_id = start["id"]
        self.assertEqual(start, {"v": 1, "op": "start", "id": session_id, "max_ms": 15000})
        self.assertTrue(ASSIST.valid_session_id(session_id))
        # Frames of another session are ignored.
        await self.send_frames(OTHER_ID, 1, start=0)
        await self.send_frames(session_id, 3)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(satellite.stages, (Stage.STT, Stage.TTS))
        self.assertEqual(self.chunks, [b"\x00\x00", b"\x01\x00"])
        # Stop goes out exactly once although VAD end, STT end and run end all ask.
        self.assertEqual(self.assist_commands(),
                         [start, {"v": 1, "op": "stop", "id": session_id}])
        # The answer plays on the speaker with an absolute, signed URL.
        [play] = self.audio_commands()
        self.assertEqual(play["op"], "play")
        self.assertEqual(play["url"], "http://192.168.1.2:8123/api/tts_proxy/abc.mp3?authSig=x")
        self.assertTrue(ASSIST.valid_session_id(play["id"]))
        self.assertEqual(satellite.tts_finished, 0)
        # Another clip finishing does not end the answer; this one does.
        await self.deliver(f"{BASE}/stat/audio", json.dumps(
            {"v": 1, "state": "idle", "volume": 0.5, "id": "a" * 16}))
        self.assertEqual(satellite.tts_finished, 0)
        await self.deliver(f"{BASE}/stat/audio", json.dumps(
            {"v": 1, "state": "playing", "volume": 0.5, "id": play["id"]}))
        self.assertEqual(satellite.tts_finished, 0)
        await self.deliver(f"{BASE}/stat/audio", json.dumps(
            {"v": 1, "state": "idle", "volume": 0.5, "id": play["id"]}))
        self.assertEqual(satellite.tts_finished, 1)
        self.assertIsNone(satellite._tts_timer)
        self.assertIsNone(satellite._session)
        # Late frames of the finished session go nowhere.
        await self.send_frames(session_id, 1, start=5)

    async def test_paired_panel_uses_the_sealed_channel(self):
        bridge = SealingBridge()
        satellite = await self.start(bridge=bridge)
        await satellite.async_start_tap_to_talk()
        session_id = bridge.assist[0]["id"]
        await self.send_frames(session_id, 2)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual([c["op"] for c in bridge.assist], ["start", "stop"])
        self.assertEqual(bridge.audio[0]["url"],
                         "http://192.168.1.2:8123/api/tts_proxy/abc.mp3?authSig=x")

    async def test_paired_panel_without_bridge_never_gets_plaintext(self):
        satellite = await self.start()
        satellite._paired = True
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await satellite.async_start_tap_to_talk()
        self.assertEqual(raised.exception.translation_key, "assist_not_connected")
        self.assertEqual(self.mqtt.published, [])
        self.assertIsNone(satellite._session)

    async def test_stream_ends_after_last_frame(self):
        async def pipeline(satellite, stream):
            self.chunks.extend([chunk async for chunk in stream])
            self.emit(satellite, EventType.ERROR, {"code": "stt-no-text-recognized"})
            self.emit(satellite, EventType.RUN_END)
            self.pipeline_done.set()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        session_id = self.assist_commands()[0]["id"]
        await self.send_frames(session_id, 2)
        await self.deliver(f"{BASE}/stat/assist_audio", frame(session_id, 2, b"", last=True))
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(self.chunks, [b"\x00\x00", b"\x01\x00"])
        self.assertEqual([c["op"] for c in self.assist_commands()], ["start", "stop"])
        self.assertEqual(self.audio_commands(), [])
        # No answer was produced: nothing to finish.
        self.assertEqual(satellite.tts_finished, 0)

    async def test_stream_ends_when_panel_reports_idle(self):
        async def pipeline(satellite, stream):
            self.chunks.extend([chunk async for chunk in stream])
            self.emit(satellite, EventType.RUN_END)
            self.pipeline_done.set()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        session_id = self.assist_commands()[0]["id"]
        await self.send_frames(session_id, 1)
        await self.deliver(f"{BASE}/stat/assist",
                           json.dumps({"v": 1, "state": "idle", "id": OTHER_ID}))
        await settle()
        self.assertFalse(self.pipeline_done.is_set())
        await self.deliver(f"{BASE}/stat/assist", json.dumps(
            {"v": 1, "state": "idle", "id": session_id, "error": "timeout"}))
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        self.assertEqual(self.chunks, [b"\x00\x00"])

    async def test_stream_ends_after_two_seconds_without_frames(self):
        async def pipeline(satellite, stream):
            self.chunks.extend([chunk async for chunk in stream])
            self.pipeline_done.set()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        # FRAME_TIMEOUT_S (2 s) is asserted in AudioSessionTest; shortened here.
        satellite._session.frame_timeout_s = 0.05
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(self.chunks, [])
        self.assertEqual([c["op"] for c in self.assist_commands()], ["start", "stop"])

    async def test_tts_without_url_returns_to_idle(self):
        async def pipeline(satellite, stream):
            async for _ in stream:
                break
            self.emit(satellite, EventType.STT_VAD_END)
            self.emit(satellite, EventType.TTS_START, {"tts_input": "Hello"})
            self.emit(satellite, EventType.ERROR, {"code": "tts-failed"})
            self.emit(satellite, EventType.RUN_END)
            self.pipeline_done.set()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        await self.send_frames(self.assist_commands()[0]["id"], 1)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await settle()
        self.assertEqual(self.audio_commands(), [])
        self.assertEqual(satellite.tts_finished, 1)

    async def test_offline_panel_is_unavailable_and_ends_the_run(self):
        async def pipeline(satellite, stream):
            self.chunks.extend([chunk async for chunk in stream])
            self.pipeline_done.set()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        await self.deliver(f"{BASE}/stat/connected", "0")
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        self.assertFalse(satellite.available)
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await satellite.async_start_tap_to_talk()
        self.assertEqual(raised.exception.translation_key, "assist_unavailable")
        await self.deliver(f"{BASE}/stat/connected", "1")
        self.assertTrue(satellite.available)
        # A cleared retained status withdraws the microphone.
        await self.deliver(f"{BASE}/stat/assist", "")
        self.assertFalse(satellite.available)
        await self.deliver(f"{BASE}/stat/assist", json.dumps({"v": 1, "state": "idle"}))
        self.assertTrue(satellite.available)

    async def test_removal_stops_the_microphone(self):
        blocked = asyncio.Event()

        async def pipeline(satellite, stream):
            blocked.set()
            await asyncio.Event().wait()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        await asyncio.wait_for(blocked.wait(), 1)
        session_id = self.assist_commands()[0]["id"]
        await satellite.async_will_remove_from_hass()
        self.assertEqual(self.assist_commands()[1:], [{"v": 1, "op": "stop", "id": session_id}])
        self.assertEqual(satellite.hass.data["tab5_lvgl"]["assist_satellites"], {})

    async def test_new_press_stops_the_previous_session_first(self):
        blocked = asyncio.Event()

        async def pipeline(satellite, stream):
            blocked.set()
            await asyncio.Event().wait()

        satellite = await self.start(pipeline=pipeline)
        await satellite.async_start_tap_to_talk()
        await asyncio.wait_for(blocked.wait(), 1)
        await satellite.async_start_tap_to_talk()
        first, stop, second = self.assist_commands()
        self.assertEqual(stop, {"v": 1, "op": "stop", "id": first["id"]})
        self.assertEqual(second["op"], "start")
        self.assertNotEqual(second["id"], first["id"])
        await satellite.async_will_remove_from_hass()

    async def test_button_starts_the_satellite(self):
        satellite = await self.start()
        [button] = await self.created(self.button_module, entry({"audio_input": True}))
        button.hass = satellite.hass
        await button.async_press()
        self.assertEqual(self.assist_commands()[0]["op"], "start")
        session_id = self.assist_commands()[0]["id"]
        await self.send_frames(session_id, 2)
        await asyncio.wait_for(self.pipeline_done.wait(), 1)
        await satellite.async_will_remove_from_hass()
        with self.assertRaises(FakeHomeAssistantError) as raised:
            await button.async_press()
        self.assertEqual(raised.exception.translation_key, "assist_unavailable")


if __name__ == "__main__":
    unittest.main()


class MicrophoneSwitchTest(unittest.IsolatedAsyncioTestCase):
    """The Microphone switch mirrors the panel status and switches via the satellite."""

    def make(self):
        class Entity:
            async def async_added_to_hass(self):
                pass

        class Category:
            CONFIG = "config"

        written = []
        scope = {"SwitchEntity": Entity, "EntityCategory": Category,
                 "HomeAssistantError": FakeHomeAssistantError, "DOMAIN": "tab5_lvgl",
                 "ASSIST_SATELLITES_KEY": ASSIST.ASSIST_SATELLITES_KEY,
                 "assist_status_topic": ASSIST.assist_status_topic,
                 "assist_microphone_switch_unique_id": ASSIST.assist_microphone_switch_unique_id,
                 "parse_assist_status": ASSIST.parse_status,
                 "entry_device_info": lambda entry: {}, "entry_device_id": lambda entry: "mac",
                 "ConfigEntry": object}
        tree = ast.parse((ROOT / "switch.py").read_text(encoding="utf-8"))
        body = [node for node in tree.body
                if getattr(node, "name", None) == "HomeTilesMicrophoneSwitch"]
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                     "switch.py", "exec"), scope)
        switch = scope["HomeTilesMicrophoneSwitch"](types.SimpleNamespace(entry_id="e1"), BASE)
        switch.async_write_ha_state = lambda: written.append(switch._attr_is_on)
        return switch, written

    def test_mirrors_the_panel_status(self):
        switch, _ = self.make()
        self.assertEqual(switch._attr_unique_id, "mac_assist_microphone")
        self.assertFalse(switch._attr_available)
        # Firmware without the privacy switch leaves it unavailable.
        self.assertTrue(switch.apply_status(json.dumps({"v": 1, "state": "idle"})))
        self.assertFalse(switch._attr_available)
        self.assertTrue(switch.apply_status(json.dumps(
            {"v": 1, "state": "idle", "mic_enabled": True})))
        self.assertTrue(switch._attr_available)
        self.assertTrue(switch._attr_is_on)
        self.assertEqual(switch.icon, "mdi:microphone")
        self.assertTrue(switch.apply_status(json.dumps(
            {"v": 1, "state": "idle", "mic_enabled": False})))
        self.assertFalse(switch._attr_is_on)
        self.assertEqual(switch.icon, "mdi:microphone-off")
        self.assertFalse(switch.apply_status("not json"))
        self.assertTrue(switch.apply_status(""))
        self.assertFalse(switch._attr_available)

    async def test_switches_through_the_satellite(self):
        switch, written = self.make()
        sent = []

        class Satellite:
            async def async_set_microphone(self, enabled):
                sent.append(enabled)

        switch.hass = types.SimpleNamespace(data={"tab5_lvgl": {
            ASSIST.ASSIST_SATELLITES_KEY: {"e1": Satellite()}}})
        await switch.async_turn_off()
        await switch.async_turn_on()
        self.assertEqual(sent, [False, True])
        self.assertEqual(written, [False, True])
        switch.hass = types.SimpleNamespace(data={})
        with self.assertRaises(FakeHomeAssistantError):
            await switch.async_turn_off()

    def test_satellite_needs_pairing_to_turn_on(self):
        source = (ROOT / "assist_satellite.py").read_text(encoding="utf-8")
        self.assertIn('if enabled and not self._paired:', source)
        self.assertIn('translation_key="microphone_needs_pairing"', source)
        self.assertIn("build_mic_command(enabled)", source)
