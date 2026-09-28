"""Panel announcements, discovery cards and history requests stay bounded."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import logging
import time
import types
import unittest

from test_view_navigation import ROOT, load_module

GUARD = load_module("announcement_guard")
CHANNEL = load_module("command_channel")
LIMITS = load_module("request_limits")
CAPS = load_module("capabilities")
SELECTION = load_module("sensor_selection")

# Shared with the firmware test tools/tests/network/test-command-channel-core.mjs.
CODE = "ABCDE-FGHJK-MNPQR-STVWX-YZ012"
ANNOUNCE_KEY = "73fcf8b4dfdb0ea67138ebc100e63e0e25f93c342b1cbc4df27b76df17e0d321"
TOPIC = "tab5_lvgl/config/A1B2C3D4E5F6/bridge"
UNSIGNED = '{"device_id":"A1B2C3D4E5F6","base_topic":"hometiles","ha_prefix":"ha"}'
SIGNED = ('{"device_id":"A1B2C3D4E5F6","base_topic":"hometiles","ha_prefix":"ha",'
          '"sig":"8d87186d122e8b1d8e04b428db076c6ca09bffb81748a99e55abafdd974eb34c"}')


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class AnnouncementHelpersTest(unittest.TestCase):
    def test_topic_must_name_the_announced_device(self):
        self.assertEqual(GUARD.topic_device_id(TOPIC), "A1B2C3D4E5F6")
        self.assertTrue(GUARD.announcement_matches_topic(TOPIC, "A1B2C3D4E5F6"))
        self.assertFalse(GUARD.announcement_matches_topic(TOPIC, "FFFFFFFFFFFF"))
        self.assertFalse(GUARD.announcement_matches_topic(TOPIC, None))
        for topic in ("tab5_lvgl/config/A1B2/bridge/apply", "other/A1B2C3D4E5F6/bridge",
                      "tab5_lvgl/config//bridge", "tab5_lvgl/config/a+b/bridge", None):
            self.assertIsNone(GUARD.topic_device_id(topic), topic)
        self.assertTrue(GUARD.valid_device_id("tab5_lvgl_ABCD"))
        self.assertFalse(GUARD.valid_device_id("x" * 65))
        self.assertFalse(GUARD.valid_device_id("A1B2 C3"))

    def test_announce_key_and_signature_match_the_firmware_vector(self):
        keys = CHANNEL.Keys(CODE)
        self.assertEqual(keys.announce.hex(), ANNOUNCE_KEY)
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, SIGNED), GUARD.SIGNATURE_VALID)
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, SIGNED.encode()), GUARD.SIGNATURE_VALID)
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, UNSIGNED), GUARD.SIGNATURE_UNSIGNED)
        # Signed content is bound to its topic, its text and the pairing code.
        other_topic = "tab5_lvgl/config/FFFFFFFFFFFF/bridge"
        self.assertEqual(GUARD.check_signature(keys.announce, other_topic, SIGNED), GUARD.SIGNATURE_INVALID)
        tampered = SIGNED.replace('"hometiles"', '"attacker"')
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, tampered), GUARD.SIGNATURE_INVALID)
        other_code = CHANNEL.Keys("00000-00000-00000-00000-00000")
        self.assertEqual(GUARD.check_signature(other_code.announce, TOPIC, SIGNED), GUARD.SIGNATURE_INVALID)
        # Without a stored code the signature is irrelevant.
        self.assertEqual(GUARD.check_signature(None, TOPIC, SIGNED), GUARD.SIGNATURE_UNSIGNED)
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, None), GUARD.SIGNATURE_INVALID)
        self.assertEqual(GUARD.check_signature(keys.announce, TOPIC, b"\xff"), GUARD.SIGNATURE_INVALID)

    def test_discovery_cards_are_bounded(self):
        clock = Clock()
        limiter = GUARD.DiscoveryLimiter(clock)
        allowed = [limiter.allow(f"PANEL{i:07d}", pending=0) for i in range(8)]
        self.assertEqual(allowed, [True] * 5 + [False] * 3)
        # A panel that already got a card may announce again.
        self.assertTrue(limiter.allow("PANEL0000000", pending=0))
        clock.now += 600
        self.assertTrue(limiter.allow("NEWPANEL", pending=2))
        self.assertFalse(limiter.allow("OTHERPANEL", pending=3))


class RequestGateTest(unittest.TestCase):
    def test_concurrency_and_rate(self):
        clock = Clock()
        gate = LIMITS.RequestGate(clock, max_active=2, max_per_window=3, window_s=60)
        self.assertTrue(gate.try_acquire())
        self.assertTrue(gate.try_acquire())
        self.assertFalse(gate.try_acquire())  # Two already running.
        gate.release()
        self.assertTrue(gate.try_acquire())
        gate.release()
        gate.release()
        self.assertEqual(gate.active, 0)
        self.assertFalse(gate.try_acquire())  # Three in this minute.
        clock.now += 60
        self.assertTrue(gate.try_acquire())
        gate.release()
        gate.release()
        self.assertEqual(gate.active, 0)


class RequestLineTest(unittest.IsolatedAsyncioTestCase):
    async def test_graph_tiles_of_a_view_wait_in_line(self):
        # Every graph tile of a view asks at once, from old and new firmware.
        gate = LIMITS.RequestGate(Clock(), max_active=2, max_per_window=30)
        running, order, peak = [], [], []

        async def request(name):
            self.assertTrue(await gate.acquire())
            running.append(name)
            order.append(name)
            peak.append(len(running))
            await asyncio.sleep(0)
            running.remove(name)
            gate.release()

        names = [f"sensor.tile{index}" for index in range(8)]
        await asyncio.wait_for(asyncio.gather(*(request(name) for name in names)), 1)
        self.assertEqual(order, names)
        self.assertEqual(max(peak), 2)
        self.assertEqual((gate.active, gate.waiting), (0, 0))

    async def test_a_full_window_resumes_by_itself(self):
        gate = LIMITS.RequestGate(time.monotonic, max_active=5, max_per_window=2, window_s=0.05)
        for _ in range(2):
            self.assertTrue(await gate.acquire())
            gate.release()
        started = time.monotonic()
        self.assertTrue(await asyncio.wait_for(gate.acquire(), 1))
        self.assertGreaterEqual(time.monotonic() - started, 0.03)
        gate.release()


def extract(names, scope):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    nodes += [node for node in bridge.body if getattr(node, "name", None) in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
    return scope


class FakeFlows:
    def __init__(self):
        self.pending = []

    def async_progress_by_handler(self, handler, match_context=None):
        return [flow for flow in self.pending
                if not match_context or flow["context"].get("source") == match_context.get("source")]


class AnnouncementProcessingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scope = dict(vars(load_module("const")))
        scope.update(vars(load_module("editable_helpers")))
        scope.update(vars(load_module("control_helpers")))
        self.flows = []
        self.updates = []
        self.entries = []
        self.flow_manager = FakeFlows()

        def create_flow(hass, domain, context, data):
            self.flows.append(data)
            self.flow_manager.pending.append({"context": context})

        def update(item, **fields):
            self.updates.append(fields)
            for key, value in fields.items():
                setattr(item, key, value)

        self.hass = types.SimpleNamespace(
            data={}, config_entries=types.SimpleNamespace(
                async_update_entry=update, async_entries=lambda domain: self.entries,
                flow=self.flow_manager))
        self.clock = Clock()
        scope.update({
            "config_entries": types.SimpleNamespace(
                SOURCE_IGNORE="ignore", SOURCE_INTEGRATION_DISCOVERY="integration_discovery"),
            "CAPABILITIES": "capabilities",
            "normalise_capabilities": CAPS.normalise_capabilities,
            "normalise_local_io": lambda value: value,
            "_normalise_topic": lambda value, fallback: (value or fallback).rstrip("/"),
            "_unique_entities": lambda values: list(dict.fromkeys(values)),
            "_split_weather_entities": lambda values: ([], values),
            "split_binary_sensor_entities": lambda values: ([], values),
            "_runtime_managed_sensor_entity_ids": lambda *args: set(),
            "filter_runtime_sensor_entities": SELECTION.filter_runtime_sensor_entities,
            "clean_stored_sensor_selections": SELECTION.clean_stored_sensor_selections,
            "should_import_feedback_selection": SELECTION.should_import_feedback_selection,
            "entry_pairing_code": CHANNEL.entry_pairing_code,
            "CommandKeys": CHANNEL.Keys,
            "check_signature": GUARD.check_signature,
            "announcement_matches_topic": GUARD.announcement_matches_topic,
            "SIGNATURE_VALID": GUARD.SIGNATURE_VALID,
            "DiscoveryLimiter": GUARD.DiscoveryLimiter,
            "discovery_flow": types.SimpleNamespace(async_create_flow=create_flow),
            "monotonic": self.clock,
            "_LOGGER": logging.getLogger("test_announcement_guard"),
        })
        extract({"_payload_to_entry_data", "_async_process_bridge_config", "_announcement_trusted",
                 "_announcement_log_due", "_pending_discovery_flows", "_find_entry_by_device_id",
                 "_find_entry_by_base", "_may_adopt_entry", "_entry_title"}, scope)
        self.process = scope["_async_process_bridge_config"]

    def entry(self, **data):
        item = types.SimpleNamespace(entry_id=f"entry{len(self.entries)}", source="user", title="Kitchen",
                                     unique_id=data.get("device_id"), data=data, options={})
        self.entries.append(item)
        return item

    async def announce(self, payload, topic=None, raw=None):
        device_id = payload.get("device_id")
        topic = topic or f"tab5_lvgl/config/{device_id}/bridge"
        await self.process(self.hass, payload, topic=topic, raw_payload=raw or json.dumps(payload))

    async def test_foreign_topic_cannot_update_another_panel(self):
        entry = self.entry(device_id="A1B2C3D4E5F6", base_topic="hometiles")
        with self.assertLogs("test_announcement_guard", "WARNING"):
            await self.announce({"device_id": "A1B2C3D4E5F6", "base_topic": "hometiles",
                                 "local_io": [{"id": "relay", "type": "relay"}]},
                                topic="tab5_lvgl/config/EVIL00000000/bridge")
        self.assertEqual((self.updates, self.flows), ([], []))
        self.assertNotIn("local_io", entry.data)

    async def test_existing_panel_accepts_only_its_own_base_topic(self):
        entry = self.entry(device_id="A1B2C3D4E5F6", base_topic="hometiles")
        with self.assertLogs("test_announcement_guard", "WARNING") as logs:
            await self.announce({"device_id": "A1B2C3D4E5F6", "base_topic": "attacker",
                                 "local_io": [{"id": "relay", "type": "relay"}]})
        self.assertIn("base topic attacker", logs.output[0])
        self.assertEqual(self.updates, [])
        await self.announce({"device_id": "A1B2C3D4E5F6", "base_topic": "hometiles",
                             "local_io": [{"id": "relay", "type": "relay"}]})
        self.assertEqual(entry.data["local_io"], [{"id": "relay", "type": "relay"}])

    async def test_paired_panel_needs_a_valid_signature(self):
        entry = self.entry(device_id="A1B2C3D4E5F6", base_topic="hometiles", ha_prefix="ha",
                           command_pairing_code=CODE)
        payload = json.loads(UNSIGNED)
        with self.assertLogs("test_announcement_guard", "WARNING"):
            await self.announce(dict(payload, model="forged"), raw=UNSIGNED)
        self.assertEqual(self.updates, [])
        await self.announce(json.loads(SIGNED), raw=SIGNED)
        self.assertEqual(len(self.updates), 0)  # Nothing new to store, but accepted.
        # A valid signature from another code is rejected as well.
        other = CHANNEL.Keys("00000-00000-00000-00000-00000")
        body = '{"device_id":"A1B2C3D4E5F6","base_topic":"hometiles","model":"x"}'
        sig = hmac.new(other.announce, (TOPIC + "\n" + body).encode(), hashlib.sha256).hexdigest()
        forged = body[:-1] + f',"sig":"{sig}"}}'
        self.clock.now += 1000
        with self.assertLogs("test_announcement_guard", "WARNING"):
            await self.announce(json.loads(forged), raw=forged)
        self.assertNotIn("model", entry.data)
        good_body = '{"device_id":"A1B2C3D4E5F6","base_topic":"hometiles","model":"tab5"}'
        good_sig = hmac.new(bytes.fromhex(ANNOUNCE_KEY), (TOPIC + "\n" + good_body).encode(),
                            hashlib.sha256).hexdigest()
        good = good_body[:-1] + f',"sig":"{good_sig}"}}'
        await self.announce(json.loads(good), raw=good)
        self.assertEqual(entry.data["model"], "tab5")

    async def test_old_firmware_updates_an_entry_without_the_new_fields(self):
        # An entry from before the security work and a panel with firmware
        # v0.1.0: short device id, no signature, no pairing code, no local I/O.
        entry = self.entry(device_id="tab5_lvgl_1A2B", base_topic="tab5", ha_prefix="homeassistant")
        with self.assertNoLogs("test_announcement_guard", "WARNING"):
            await self.announce({"device_id": "tab5_lvgl_1A2B", "base_topic": "tab5",
                                 "ha_prefix": "homeassistant", "sensors": ["sensor.outdoor"]})
        self.assertEqual(entry.data["sensors"], ["sensor.outdoor"])
        self.assertNotIn("command_pairing_code", entry.data)
        self.assertEqual(self.flows, [])

    async def test_existing_manual_entry_is_linked_only_after_confirmation(self):
        entry = self.entry(base_topic="hometiles")
        await self.announce({"device_id": "A1B2C3D4E5F6", "base_topic": "hometiles"})
        self.assertEqual(self.updates, [])
        [flow] = self.flows
        self.assertEqual((flow["device_id"], flow["adopt_entry_id"]), ("A1B2C3D4E5F6", entry.entry_id))
        # An entry bound to another panel is never offered.
        self.entries.clear()
        self.flows.clear()
        self.entry(device_id="FFFFFFFFFFFF", base_topic="hometiles")
        await self.announce({"device_id": "A1B2C3D4E5F6", "base_topic": "hometiles"})
        self.assertEqual(self.flows, [])

    async def test_forged_announcements_open_a_bounded_number_of_cards(self):
        with self.assertLogs("test_announcement_guard", "WARNING") as logs:
            for index in range(20):
                await self.announce({"device_id": f"EVIL{index:08d}", "base_topic": f"evil{index}"})
        self.assertEqual(len(self.flows), 3)  # At most three cards wait at a time.
        self.assertEqual(len(logs.output), 1)  # And the warning is rate-limited.
        self.flow_manager.pending.clear()
        for index in range(20, 40):
            await self.announce({"device_id": f"EVIL{index:08d}", "base_topic": f"evil{index}"})
        self.assertEqual(len(self.flows), 5)  # Five new panels per ten minutes.


class HistoryGateWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scope = {"_LOGGER": logging.getLogger("test_announcement_guard"), "HISTORY_REQUEST_MAX_BYTES": 2048,
                 "monotonic": Clock()}
        extract({"_async_on_history_request", "_secure_log_due"}, scope)
        self.handled = []
        self.release = asyncio.Event()

        async def handle(msg):
            self.handled.append(msg.payload)
            await self.release.wait()

        self.bridge = types.SimpleNamespace(
            _history_gate=LIMITS.RequestGate(Clock(), max_active=2, max_per_window=30, max_waiting=1),
            _async_handle_history_request=handle, _secure_log_at={}, device_id="panel", base_topic="hometiles")
        self.bridge._secure_log_due = lambda reason, interval=60.0: scope["_secure_log_due"](
            self.bridge, reason, interval)
        self.on_request = lambda payload, retain=False: scope["_async_on_history_request"](
            self.bridge, types.SimpleNamespace(payload=payload, retain=retain))

    async def test_requests_are_bounded_before_the_recorder(self):
        await self.on_request('{"entity_id":"sensor.a"}', retain=True)
        await self.on_request("x" * 2049)
        self.assertEqual(self.handled, [])
        first = asyncio.create_task(self.on_request('{"entity_id":"sensor.a"}'))
        second = asyncio.create_task(self.on_request('{"entity_id":"sensor.b"}'))
        await asyncio.sleep(0)
        # A third graph tile waits in line instead of being dropped.
        third = asyncio.create_task(self.on_request('{"entity_id":"sensor.c"}'))
        await asyncio.sleep(0)
        self.assertEqual(self.bridge._history_gate.waiting, 1)
        # Only a full line drops requests.
        with self.assertLogs("test_announcement_guard", "WARNING"):
            await self.on_request('{"entity_id":"sensor.flood"}')
        self.assertEqual(self.handled, ['{"entity_id":"sensor.a"}', '{"entity_id":"sensor.b"}'])
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 1)
        self.assertEqual(self.handled[2], '{"entity_id":"sensor.c"}')
        self.assertEqual((self.bridge._history_gate.active, self.bridge._history_gate.waiting), (0, 0))
        await self.on_request('{"entity_id":"sensor.d"}')
        self.assertEqual(len(self.handled), 4)


class SourceContractTest(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def function(self, name):
        node = next(node for node in ast.walk(self.tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
        return ast.get_source_segment(self.source, node)

    def test_numeric_history_is_bounded_and_the_subscription_is_gated(self):
        handler = self.function("_async_handle_history_request")
        self.assertIn("fetch_numeric_history_values(", handler)
        self.assertNotIn("state_changes_during_period(", handler)
        self.assertIn("self._async_on_history_request", self.function("_async_setup_requests"))

    def test_announcements_carry_topic_and_raw_payload(self):
        setup = self.function("async_setup")
        self.assertIn("topic=msg.topic, raw_payload=raw_payload", setup)
        self.assertIn("MAX_ANNOUNCEMENT_BYTES", setup)
        self.assertNotIn("msg.payload)\n      return", setup)

    def test_adoption_waits_for_the_user(self):
        process = self.function("_async_process_bridge_config")
        self.assertNotIn("async_reload", process)
        self.assertIn("DISCOVERY_ADOPT_ENTRY", process)
        flow_source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        flow_tree = ast.parse(flow_source)
        step = next(node for node in ast.walk(flow_tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_adopt_confirm")
        segment = ast.get_source_segment(flow_source, step)
        self.assertLess(segment.index("if user_input is not None"), segment.index("async_update_entry"))
        self.assertIn("_set_confirm_only()", segment)
        for path in [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]:
            config = json.loads(path.read_text(encoding="utf-8"))["config"]
            self.assertIn("{entry}", config["step"]["adopt_confirm"]["description"], path)
            self.assertIn("panel_linked", config["abort"], path)
            self.assertIn("adopt_target_changed", config["abort"], path)


if __name__ == "__main__":
    unittest.main()
