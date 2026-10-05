"""entity_search.py and its wiring: the picker's search and the tiles' own
entities of a paired panel.

With a Web Admin password on the panel (claimed in the sealed command, like
Lock and Alarm) the search covers every Home Assistant entity of the list's
domains and the tiles' reported entities are served like released ones;
without it only the released entities are found and reports are dropped.
Answers fit the sealed size limit in parts.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_link_protocol import load_link_module

SEARCH = load_link_module("entity_search")
CHANNEL = load_link_module("command_channel")
SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"


class SearchModuleTests(unittest.TestCase):
    def test_parse_search(self):
        parse = SEARCH.parse_search
        self.assertEqual(parse('{"id":7,"q":"  küche ","list":"sensors","web_auth":true}'),
                         {"id": 7, "list": "sensors", "q": "küche", "o": 0, "web_auth": True})
        self.assertEqual(parse('{"id":7,"list":"sensors","o":60}')["o"], 60, "The picker's next page")
        self.assertFalse(parse('{"id":7,"list":"switches","web_auth":"true"}')["web_auth"], "Only a real true counts")
        self.assertEqual(len(parse(json.dumps({"id": 1, "list": "covers", "q": "x" * 500}))["q"]), SEARCH.MAX_QUERY)
        for bad in ('{"id":-1,"list":"sensors"}', '{"id":true,"list":"sensors"}', '{"id":1,"list":"energy"}',
                    '{"id":1,"list":"sensors","q":5}', '{"id":1,"list":"sensors","o":-1}',
                    '{"id":1,"list":"sensors","o":5001}', '{"id":1,"list":"sensors","o":"60"}', '[]', 'nope', None):
            self.assertIsNone(parse(bad), bad)

    def test_search_matches_every_word_in_name_id_area_and_device(self):
        entries = [{"v": "sensor.a", "t": "Temperatur", "a": "OG Küche", "d": "Heizung"},
                   {"v": "sensor.b", "t": "Außentemperatur", "a": "Außen", "d": "Wetterstation"},
                   {"v": "sensor.c", "t": "Download", "a": "EG Büro", "d": "FRITZ!Box"}]
        found, more = SEARCH.search(entries, "temp")
        self.assertEqual([item["v"] for item in found], ["sensor.b", "sensor.a"], "Sorted by name")
        self.assertFalse(more)
        self.assertEqual([item["v"] for item in SEARCH.search(entries, "KÜCHE heiz")[0]], ["sensor.a"])
        self.assertEqual([item["v"] for item in SEARCH.search(entries, "fritz")[0]], ["sensor.c"])
        self.assertEqual(len(SEARCH.search(entries, "")[0]), 3)
        found, more = SEARCH.search(entries, "", limit=2)
        self.assertEqual((len(found), more), (2, True))
        found, more = SEARCH.search(entries, "", limit=2, offset=2)
        self.assertEqual(([item["v"] for item in found], more), (["sensor.a"], False), "The last page")

    def test_answer_parts_fit_the_sealed_limit(self):
        results = [{"v": f"sensor.wohnzimmer_temperatur_{index}", "t": f"Wohnzimmer Temperatur Ä{index}",
                    "i": "mdi:thermometer", "a": "OG Wohnbereich", "d": "Heizung OG Wohnzimmer"} for index in range(60)]
        parts = SEARCH.pack_answer(9, results, True, False)
        self.assertGreater(len(parts), 1)
        decoded = [json.loads(part) for part in parts]
        self.assertTrue(all(len(part.encode("utf-8")) <= SEARCH.MAX_PART_BYTES for part in parts))
        self.assertTrue(all(len(part.encode("utf-8")) <= CHANNEL.MAX_BODY for part in parts))
        self.assertEqual([item["p"] for item in decoded], list(range(len(parts))))
        self.assertTrue(all(item["n"] == len(parts) and item["id"] == 9 and item["full"] for item in decoded))
        self.assertEqual([entry["v"] for item in decoded for entry in item["r"]], [entry["v"] for entry in results])
        self.assertIn("Ä0", parts[0], "Names stay readable UTF-8")
        empty = json.loads(SEARCH.pack_answer(3, [], False, False)[0])
        self.assertEqual((empty["n"], empty["r"], empty["full"]), (1, [], False))
        many = [dict(results[0], v=f"sensor.x{index}", t="N" * 300) for index in range(200)]
        capped = [json.loads(part) for part in SEARCH.pack_answer(1, many, True, False)]
        self.assertEqual(len(capped), SEARCH.MAX_PARTS)
        self.assertTrue(capped[-1]["more"], "A capped answer says there are more")
        self.assertEqual(SEARCH.compact({"v": "a.b", "t": "", "i": None, "a": "X"}), {"v": "a.b", "a": "X"})

    def test_parse_tiles(self):
        parse = SEARCH.parse_tiles
        self.assertEqual(parse('{"lists":{"switches":["light.desk","fan.bath","light.desk"],"sensors":[]}}'),
                         {"switches": ["light.desk", "fan.bath"]})
        self.assertEqual(parse({"lists": {}}), {})
        for bad in ('{"lists":{"switches":["lock.door"]}}', '{"lists":{"energy":["sensor.x"]}}',
                    '{"lists":{"sensors":"sensor.x"}}', '{"lists":[]}', '{}', 'x'):
            self.assertIsNone(parse(bad), bad)
        too_many = {"lists": {"sensors": [f"sensor.s{index}" for index in range(SEARCH.MAX_TILE_ENTITIES + 1)]}}
        self.assertIsNone(parse(too_many))

    def test_sealed_only(self):
        self.assertTrue({"entities", "tiles"} <= CHANNEL.SEALED_ONLY_COMMANDS <= CHANNEL.SEALED_COMMANDS)
        self.assertIn("entities", CHANNEL.SEALED_DATA)
        self.assertFalse([topic for topic in CHANNEL.command_topics("b") if topic.endswith(("/entities", "/tiles"))])


def bridge_type():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    names = {"_released_for_list", "_search_entries", "_search_icon", "_async_handle_entities_command",
             "_async_handle_tiles_command", "_access_secured", "_secure_log_due"}
    functions = [node for node in bridge.body if getattr(node, "name", None) in names]
    opened = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_OpenedCommand")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              opened, *functions], type_ignores=[])
    return module, names


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def make(self, web_auth_ok=True):
        module, names = bridge_type()
        self.loaded_icons = []

        async def load_icons(hass, ids):
            self.loaded_icons.append(list(ids))

        states = {
            "sensor.kitchen": SimpleNamespace(entity_id="sensor.kitchen", name="Temperatur", state="21"),
            "sensor.stars": SimpleNamespace(entity_id="sensor.stars", name="Sterne", state="174"),
            "light.desk": SimpleNamespace(entity_id="light.desk", name="Schreibtisch", state="on"),
            "fan.bath": SimpleNamespace(entity_id="fan.bath", name="Lüfter", state="off"),
        }
        entities = {"sensor.kitchen": SimpleNamespace(device_id="dev1", area_id=None),
                    "sensor.stars": SimpleNamespace(device_id=None, area_id="garden")}
        devices = {"dev1": SimpleNamespace(name="Heizung", name_by_user=None, area_id="kitchen")}
        areas = {"kitchen": SimpleNamespace(name="OG Küche"), "garden": SimpleNamespace(name="Garten")}
        scope = {
            "json": json, "entity_search": SEARCH, "_LOGGER": SimpleNamespace(warning=lambda *a: None),
            "monotonic": lambda: 0.0, "_unique_entities": lambda items: list(dict.fromkeys(items)),
            "er": SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=entities.get)),
            "dr": SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=devices.get)),
            "ar": SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get_area=areas.get)),
            "entity_icons": SimpleNamespace(async_load_entity_icons=load_icons),
            "_extract_mdi_icon": lambda state, hass: {"sensor.stars": "mdi:star"}.get(state.entity_id, "mdi:eye"),
            "_extract_media_player_mdi_icon": lambda state, hass: "mdi:speaker",
            "_is_weather_entity": lambda entity: entity.startswith("weather."),
            "_weather_icon_from_state": lambda state, hass: "mdi:weather-sunny",
        }
        exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
        Bridge = type("Bridge", (), {name: scope[name] for name in names})
        bridge = Bridge()
        bridge.hass = SimpleNamespace(states=SimpleNamespace(
            get=states.get,
            async_all=lambda domains: [state for state in states.values() if state.entity_id.split(".")[0] in domains]))
        bridge.base_topic = "hometiles/test"
        bridge._secure_log_at = {}
        bridge._command_channel = SimpleNamespace(removing=False)
        bridge.sensors, bridge.lights, bridge.switches = ["sensor.kitchen"], [], []
        bridge.tracked_entities = ["sensor.kitchen"]
        bridge._panel_extras = {}
        self.sent, self.refreshed, self.configs, self.snapshots = [], [], [], []

        async def publish(kind, text):
            self.sent.append((kind, json.loads(text)))
            return True

        def refresh():
            self.refreshed.append(dict(bridge._panel_extras))
            bridge.lights = [item for item in bridge._panel_extras.get("switches", []) if item.startswith("light.")]
            bridge.tracked_entities = ["sensor.kitchen"] + bridge.lights

        async def config():
            self.configs.append(True)

        async def snapshot(ids=None):
            self.snapshots.append(list(ids))

        bridge._async_publish_sealed_data = publish
        bridge._refresh_runtime_entity_lists = refresh
        bridge.async_publish_config_to_device = config
        bridge.async_publish_snapshot = snapshot
        self.opened = scope["_OpenedCommand"]
        return bridge

    def command(self, body, sealed=True):
        if sealed:
            return self.opened("hometiles/test/cmnd/x", json.dumps(body))
        return SimpleNamespace(topic="hometiles/test/cmnd/x", payload=json.dumps(body), qos=0, retain=False)

    async def test_full_search_needs_the_password_claim(self):
        bridge = self.make()
        await bridge._async_handle_entities_command(self.command({"id": 1, "q": "", "list": "sensors", "web_auth": True}))
        [(kind, answer)] = self.sent
        self.assertEqual(kind, "entities")
        self.assertTrue(answer["full"])
        self.assertEqual(answer["r"], [{"v": "sensor.stars", "t": "Sterne", "i": "mdi:star", "a": "Garten"},
                                       {"v": "sensor.kitchen", "t": "Temperatur", "i": "mdi:eye", "a": "OG Küche",
                                        "d": "Heizung"}])
        self.assertEqual(self.loaded_icons, [["sensor.stars", "sensor.kitchen"]], "Icons load only for the results")
        self.sent.clear()
        await bridge._async_handle_entities_command(self.command({"id": 2, "q": "", "list": "sensors", "web_auth": False}))
        [(_, answer)] = self.sent
        self.assertFalse(answer["full"])
        self.assertEqual([item["v"] for item in answer["r"]], ["sensor.kitchen"], "Without a password: released only")
        self.sent.clear()
        await bridge._async_handle_entities_command(self.command({"id": 3, "q": "", "list": "sensors", "web_auth": True}, sealed=False))
        self.assertEqual(self.sent, [], "Never unencrypted")
        bridge._command_channel.removing = True
        await bridge._async_handle_entities_command(self.command({"id": 4, "q": "", "list": "sensors", "web_auth": True}))
        self.assertFalse(self.sent[-1][1]["full"], "A pairing being removed grants nothing")

    async def test_next_page(self):
        bridge = self.make()
        await bridge._async_handle_entities_command(
            self.command({"id": 6, "q": "", "list": "sensors", "o": 1, "web_auth": True}))
        [(_, answer)] = self.sent
        self.assertEqual([item["v"] for item in answer["r"]], ["sensor.kitchen"])
        self.assertFalse(answer["more"])
        self.assertEqual(self.loaded_icons, [["sensor.kitchen"]], "Icons only for the page")

    async def test_search_by_area(self):
        bridge = self.make()
        await bridge._async_handle_entities_command(self.command({"id": 5, "q": "küche", "list": "sensors", "web_auth": True}))
        self.assertEqual([item["v"] for item in self.sent[0][1]["r"]], ["sensor.kitchen"])

    async def test_tiles_add_entities_only_with_the_password(self):
        bridge = self.make()
        await bridge._async_handle_tiles_command(self.command({"lists": {"switches": ["light.desk"]}, "web_auth": True}))
        self.assertEqual(bridge._panel_extras, {"switches": ["light.desk"]})
        self.assertEqual((len(self.configs), self.snapshots), (1, [["light.desk"]]))
        await bridge._async_handle_tiles_command(self.command({"lists": {"switches": ["light.desk"]}, "web_auth": True}))
        self.assertEqual(len(self.configs), 1, "An unchanged list changes nothing")
        await bridge._async_handle_tiles_command(self.command({"lists": {"switches": ["lock.door"]}, "web_auth": True}))
        self.assertEqual(bridge._panel_extras, {"switches": ["light.desk"]}, "An invalid list is ignored")
        await bridge._async_handle_tiles_command(self.command({"lists": {"switches": ["light.desk"]}, "web_auth": False}))
        self.assertEqual(bridge._panel_extras, {}, "Without the password the entities are dropped")
        await bridge._async_handle_tiles_command(self.command({"lists": {"switches": ["light.desk"]}, "web_auth": True}, sealed=False))
        self.assertEqual(bridge._panel_extras, {}, "Never from an unencrypted message")

    def test_extras_join_the_runtime_lists_and_the_capability_is_announced(self):
        text = SOURCE.read_text(encoding="utf-8")
        refresh = text[text.index("def _refresh_runtime_entity_lists"):text.index("def _refresh_runtime_entity_lists") + 4000]
        self.assertLess(refresh.index("extras = self._panel_extras"), refresh.index("self.tracked_entities = _unique_entities("))
        self.assertIn('"entity_search": 1,', text)
        self.assertIn('"entities": self._async_handle_entities_command,', text)
        self.assertIn('"tiles": self._async_handle_tiles_command,', text)


if __name__ == "__main__":
    unittest.main()
