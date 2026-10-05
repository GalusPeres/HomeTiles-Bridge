"""entity_search.py and its wiring: the picker's search and the entity
declaration of a paired panel.

With a Web Admin password on the panel (claimed in the sealed command, like
Lock and Alarm) the search covers every Home Assistant entity of the list's
domains and the declared entities are served like released ones; without it
only the released entities are found and a declaration serves nothing.
Answers and declarations fit the sealed size limit in parts. FlowTests run
the Bridge together with the panel's declaration rule through restarts and
changes: after one round nothing moves any more.
"""
from __future__ import annotations

import ast
import json
import unittest
import zlib
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

    def test_parse_declaration(self):
        parse = SEARCH.parse_declaration
        self.assertEqual(
            parse('{"v":7,"p":0,"n":1,"lists":{"switches":["light.desk","fan.bath","light.desk"],"sensors":[]},'
                  '"web_auth":true}'),
            {"v": 7, "p": 0, "n": 1, "lists": {"switches": ["fan.bath", "light.desk"]}, "web_auth": True},
            "Sorted, without duplicates and empty lists")
        self.assertFalse(parse('{"v":1,"p":0,"n":1,"lists":{},"web_auth":"true"}')["web_auth"])
        self.assertEqual(
            parse('{"v":1,"p":0,"n":1,"lists":{"switches":["lock.door","light.a"],"energy":["sensor.x"],'
                  '"sensors":"sensor.x","fans":[5]}}')["lists"],
            {"switches": ["light.a"]}, "Stray values are left out, the rest still counts")
        for bad in ('{"v":1,"p":0,"n":1,"lists":[]}', '{"v":1,"p":1,"n":1,"lists":{}}', '{"v":1,"p":0,"n":9,"lists":{}}',
                    '{"v":-1,"p":0,"n":1,"lists":{}}', '{"v":true,"p":0,"n":1,"lists":{}}',
                    '{"lists":{"switches":["light.desk"]}}', '{}', 'x', None):
            self.assertIsNone(parse(bad), bad)
        self.assertEqual(SEARCH.clean_lists({"sensors": ["sensor.b", "sensor.a", "sensor.b"]}),
                         {"sensors": ["sensor.a", "sensor.b"]})
        self.assertIsNone(SEARCH.clean_lists([]))

    def test_declaration_parts(self):
        parts = SEARCH.DeclarationParts()
        first = SEARCH.parse_declaration({"v": 5, "p": 0, "n": 2, "lists": {"sensors": ["sensor.a"]}, "web_auth": True})
        second = SEARCH.parse_declaration({"v": 5, "p": 1, "n": 2, "lists": {"sensors": ["sensor.b"],
                                                                            "fans": ["fan.bath"]}, "web_auth": True})
        self.assertIsNone(parts.add(first), "Waits for every part")
        self.assertEqual(parts.add(second), ({"sensors": ["sensor.a", "sensor.b"], "fans": ["fan.bath"]}, True))
        other = SEARCH.parse_declaration({"v": 6, "p": 1, "n": 2, "lists": {"sensors": ["sensor.c"]}, "web_auth": True})
        self.assertIsNone(parts.add(first))
        self.assertIsNone(parts.add(other), "A part of another version starts over")
        parts = SEARCH.DeclarationParts()
        parts.add(SEARCH.parse_declaration({"v": 8, "p": 0, "n": 2, "lists": {}, "web_auth": True}))
        lists, web_auth = parts.add(SEARCH.parse_declaration({"v": 8, "p": 1, "n": 2, "lists": {}, "web_auth": False}))
        self.assertFalse(web_auth, "The password counts only when every part claims it")
        many = [f"sensor.s{index}" for index in range(SEARCH.MAX_PANEL_ENTITIES + 1)]
        self.assertIsNone(SEARCH.DeclarationParts().add(
            SEARCH.parse_declaration({"v": 9, "p": 0, "n": 1, "lists": {"sensors": many}, "web_auth": True})))

    def test_sealed_only(self):
        self.assertTrue({"entities", "tiles"} <= CHANNEL.SEALED_ONLY_COMMANDS <= CHANNEL.SEALED_COMMANDS)
        self.assertTrue({"entities", "tiles"} <= CHANNEL.SEALED_DATA)
        self.assertFalse([topic for topic in CHANNEL.command_topics("b") if topic.endswith(("/entities", "/tiles"))])


def bridge_type():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    names = {"_released_for_list", "_search_entries", "_search_icon", "_async_handle_entities_command",
             "_async_handle_tiles_command", "_access_secured", "_secure_log_due",
             "_async_load_panel_entities", "_async_save_panel_entities"}
    functions = [node for node in bridge.body if getattr(node, "name", None) in names]
    opened = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_OpenedCommand")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              opened, *functions], type_ignores=[])
    return module, names


class FakeStore:
    """homeassistant.helpers.storage.Store over a dict that outlives a Bridge."""

    def __init__(self, disk):
        self.disk = disk

    async def async_load(self):
        return json.loads(self.disk["data"]) if "data" in self.disk else None

    async def async_save(self, data):
        self.disk["data"] = json.dumps(data)


class BridgeHarness(unittest.IsolatedAsyncioTestCase):
    def make(self, web_auth_ok=True, disk=None, key_id="k1"):
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
        bridge._command_channel = SimpleNamespace(removing=False, keys=SimpleNamespace(key_id=key_id))
        bridge.sensors, bridge.lights, bridge.switches = ["sensor.kitchen"], [], []
        bridge.tracked_entities = ["sensor.kitchen"]
        bridge._panel_entities = {}
        bridge._declaration_parts = SEARCH.DeclarationParts()
        self.disk = {} if disk is None else disk
        bridge._panel_entities_store = FakeStore(self.disk)
        self.sent, self.refreshed, self.configs, self.snapshots = [], [], [], []

        async def publish(kind, text):
            self.sent.append((kind, json.loads(text)))
            return True

        def refresh():
            # Released plus declared, like _refresh_runtime_entity_lists.
            self.refreshed.append(dict(bridge._panel_entities))
            declared = [item for items in bridge._panel_entities.values() for item in items]
            bridge.tracked_entities = list(dict.fromkeys(["sensor.kitchen"] + declared))

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

    def declaration(self, lists, version=1, web_auth=True, part=0, parts=1):
        return {"v": version, "p": part, "n": parts, "lists": lists, "web_auth": web_auth}

    def acks(self):
        return [body["v"] for kind, body in self.sent if kind == "tiles"]


class WiringTests(BridgeHarness):
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

    async def test_a_declaration_serves_its_entities_only_with_the_password(self):
        bridge = self.make()
        await bridge._async_handle_tiles_command(self.command(self.declaration({"switches": ["light.desk"]})))
        self.assertEqual(bridge._panel_entities, {"switches": ["light.desk"]})
        self.assertEqual((len(self.configs), self.snapshots, self.acks()), (1, [["light.desk"]], [1]))
        self.assertEqual(json.loads(self.disk["data"]), {"key_id": "k1", "lists": {"switches": ["light.desk"]}},
                         "Kept with the pairing's key id")
        await bridge._async_handle_tiles_command(self.command(self.declaration({"switches": ["light.desk"]})))
        self.assertEqual((len(self.configs), self.acks()), (1, [1, 1]), "Unchanged: acknowledged, nothing published")
        await bridge._async_handle_tiles_command(self.command({"v": 2, "p": 0, "n": 1, "lists": [], "web_auth": True}))
        self.assertEqual((bridge._panel_entities, self.acks()), ({"switches": ["light.desk"]}, [1, 1]),
                         "An invalid declaration is neither applied nor acknowledged")
        await bridge._async_handle_tiles_command(self.command(self.declaration({"switches": ["light.desk"]}, 3, False)))
        self.assertEqual((bridge._panel_entities, len(self.configs)), ({}, 2), "Without the password nothing is served")
        await bridge._async_handle_tiles_command(
            self.command(self.declaration({"switches": ["light.desk"]}, 4), sealed=False))
        self.assertEqual((bridge._panel_entities, self.acks()), ({}, [1, 1, 3]), "Never from an unencrypted message")

    async def test_a_restart_serves_the_last_declaration_of_the_same_pairing(self):
        disk = {"data": json.dumps({"key_id": "k1", "lists": {"selects": ["select.netzteil"]}})}
        bridge = self.make(disk=disk)
        await bridge._async_load_panel_entities()
        self.assertEqual(bridge._panel_entities, {"selects": ["select.netzteil"]})
        other = self.make(disk=disk, key_id="k2")
        await other._async_load_panel_entities()
        self.assertEqual(other._panel_entities, {}, "Another pairing starts empty")
        broken = self.make(disk={"data": json.dumps({"key_id": "k1", "lists": ["select.netzteil"]})})
        await broken._async_load_panel_entities()
        self.assertEqual(broken._panel_entities, {})
        unpaired = self.make(disk=disk)
        unpaired._command_channel = None
        await unpaired._async_load_panel_entities()
        self.assertEqual(unpaired._panel_entities, {}, "An unpaired panel gets nothing")

    def test_declared_entities_join_the_runtime_lists_and_the_capability_is_announced(self):
        text = SOURCE.read_text(encoding="utf-8")
        refresh = text[text.index("def _refresh_runtime_entity_lists"):text.index("def _refresh_runtime_entity_lists") + 4000]
        self.assertLess(refresh.index("extras = self._panel_entities"), refresh.index("self.tracked_entities = _unique_entities("))
        self.assertIn('"entity_search": 2,', text)
        self.assertIn('"entities": self._async_handle_entities_command,', text)
        self.assertIn('"tiles": self._async_handle_tiles_command,', text)
        setup = text[text.index("  async def async_setup(self) -> None:"):]
        self.assertLess(setup.index("await self._async_load_panel_entities()"),
                        setup.index("self._refresh_runtime_entity_lists()"), "Loaded before the first configuration")
        removal = text[text.index("async def async_remove_entry("):]
        self.assertIn("await Store(hass, 1, panel_entities_store_key(entry.entry_id)).async_remove()",
                      removal[:400], "A removed panel's declaration is deleted")


class PanelModel:
    """The panel side (entity_search.cpp): the declaration is built from the
    tiles and the password alone, versioned by its content, sent until the
    Bridge acknowledges that version, and again on every new session."""

    def __init__(self, tiles, web_auth=True):
        self.tiles = tiles
        self.web_auth = web_auth
        self.acked = None
        self.sent = 0

    def version(self):
        lists = {key: sorted(set(ids)) for key, ids in self.tiles.items() if ids}
        return zlib.crc32(json.dumps([lists, self.web_auth], sort_keys=True).encode()), lists

    async def service(self, test, bridge):
        version, lists = self.version()
        if version == self.acked:
            return
        self.sent += 1
        await bridge._async_handle_tiles_command(test.command(test.declaration(lists, version, self.web_auth)))
        if version in test.acks():
            self.acked = version


class FlowTests(BridgeHarness):
    async def rounds(self, panel, bridge, count=5):
        # Every configuration the Bridge publishes makes the panel look again.
        for _ in range(count):
            await panel.service(self, bridge)

    async def test_no_round_trip_moves_anything_twice(self):
        disk = {}
        bridge = self.make(disk=disk)
        panel = PanelModel({"selects": ["select.netzteil"], "sensors": ["sensor.kitchen"]})
        await self.rounds(panel, bridge)
        self.assertEqual((panel.sent, len(self.configs)), (1, 1), "One declaration, one configuration, then quiet")
        self.assertIn("select.netzteil", bridge.tracked_entities)

        panel = PanelModel(panel.tiles)  # panel restart: a new session
        await self.rounds(panel, bridge)
        self.assertEqual((panel.sent, len(self.configs)), (1, 1), "A panel restart publishes nothing")

        bridge = self.make(disk=disk)  # Home Assistant restart
        await bridge._async_load_panel_entities()
        self.assertEqual(bridge._panel_entities, {"selects": ["select.netzteil"], "sensors": ["sensor.kitchen"]},
                         "Served from the first configuration on")
        panel.acked = None  # the panel reconnects
        await self.rounds(panel, bridge)
        self.assertEqual(len(self.configs), 0, "The declaration after a restart changes nothing")

        panel.tiles = {"sensors": ["sensor.kitchen"]}  # the Select tile is deleted
        await self.rounds(panel, bridge)
        self.assertEqual(len(self.configs), 1)
        self.assertNotIn("select.netzteil", bridge.tracked_entities)

        panel.web_auth = False  # the Web Admin password is removed
        await self.rounds(panel, bridge)
        self.assertEqual((bridge._panel_entities, len(self.configs)), ({}, 2))
        panel.web_auth = True  # and set again
        await self.rounds(panel, bridge)
        self.assertEqual((bridge._panel_entities, len(self.configs)), ({"sensors": ["sensor.kitchen"]}, 3))

    async def test_parts_apply_together(self):
        bridge = self.make()
        lists = {"sensors": ["sensor.a"], "fans": ["fan.bath"]}
        await bridge._async_handle_tiles_command(self.command(self.declaration({"sensors": ["sensor.a"]}, 9, True, 0, 2)))
        self.assertEqual((bridge._panel_entities, self.acks(), len(self.configs)), ({}, [], 0))
        await bridge._async_handle_tiles_command(self.command(self.declaration({"fans": ["fan.bath"]}, 9, True, 1, 2)))
        self.assertEqual((bridge._panel_entities, self.acks(), len(self.configs)), (lists, [9], 1))


if __name__ == "__main__":
    unittest.main()
