"""The panel's theme and rotation selects (user 2026-10-23).

The device page had no Dark / Light / Automatic choice, and its rotation was
the old Tab5 switch for 180 degrees while panels turn 0/90/180/270 or 0/180.
Panels that announce display_theme and display_rotation get two selects; the
rotation's steps come with the panel's retained state. The old switch stays
for panels without the capability and leaves the registry once a panel
offers its steps.
"""

from __future__ import annotations

import ast
import json
import types
import unittest

from test_view_navigation import ROOT, load_module

CAPABILITIES = load_module("capabilities")
BASE = "hometiles/panel"
LANGUAGES = ("en", "de", "es", "fr", "it", "nl", "pl", "pt-BR", "ru", "sv")


class RawMqtt:
    ReceiveMessage = types.SimpleNamespace

    def __init__(self):
        self.subscriptions = {}
        self.published = []

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscriptions[topic] = handler
        return lambda: None

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class FakeSelectEntity:
    _attr_current_option = None
    hass = None
    written = 0

    async def async_added_to_hass(self):
        return None

    async def async_will_remove_from_hass(self):
        return None

    def async_write_ha_state(self):
        self.written += 1


def select_scope(mqtt):
    """select.py's display parts with small stand-ins for Home Assistant."""
    tree = ast.parse((ROOT / "select.py").read_text(encoding="utf-8"))
    names = {"DISPLAY_THEMES", "DISPLAY_ROTATIONS", "theme_option_from_payload", "rotation_from_payload",
             "HomeTilesThemeSelect", "HomeTilesRotationSelect", "async_setup_entry"}
    body = [node for node in tree.body
            if getattr(node, "name", None) in names
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", None) in names for t in node.targets))]
    scope = {
        "json": json, "mqtt": mqtt, "SelectEntity": FakeSelectEntity, "ConfigEntry": object,
        "HomeAssistant": object, "EntityCategory": types.SimpleNamespace(CONFIG="config"),
        "command_topic": lambda base, leaf: f"{base}/cmnd/{leaf}",
        "state_topic": lambda base, leaf: f"{base}/stat/{leaf}",
        "entry_base_topic": lambda entry: BASE, "entry_device_id": lambda entry: "mac",
        "entry_device_info": lambda entry: {"identifiers": {("tab5_lvgl", "mac")}},
        "merged_capabilities_data": CAPABILITIES.merged_capabilities_data,
        "supports": CAPABILITIES.supports,
        "Tab5SleepSelect": lambda *args: "sleep", "HomeTilesViewSelect": lambda *args: "view",
        "HomeTilesWakeWordSelect": lambda *args: "wake_word", "_assist_selects": lambda hass, entry: [],
        "TOPIC_SLEEP_MAINS": "sleep_mains", "TOPIC_DISPLAY_THEME": "display_theme",
        "TOPIC_DISPLAY_ROTATION": "display_rotation",
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "select.py", "exec"), scope)
    return scope


def entry(capabilities):
    return types.SimpleNamespace(entry_id="e1", data={"capabilities": capabilities}, options={})


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.scope = select_scope(RawMqtt())

    def test_theme_ids(self):
        parse = self.scope["theme_option_from_payload"]
        self.assertEqual([parse(v) for v in ("dark", " LIGHT ", "auto")], ["dark", "light", "auto"])
        self.assertIsNone(parse("2"))
        self.assertIsNone(parse(None))

    def test_rotation_steps(self):
        parse = self.scope["rotation_from_payload"]
        self.assertEqual(parse('{"degrees":90,"options":[0,90,180,270]}'), ("90", ["0", "90", "180", "270"]))
        self.assertEqual(parse('{"degrees":180,"options":[0,180]}'), ("180", ["0", "180"]))
        for bad in ('{"degrees":90,"options":[0,180]}', '{"degrees":45,"options":[0,45]}',
                    '{"degrees":"90","options":[0,90]}', '{"degrees":0,"options":[]}',
                    '{"degrees":0,"options":[0,0]}', "ON", "", None, "[0,180]"):
            self.assertIsNone(parse(bad), bad)


class SelectEntityTest(unittest.IsolatedAsyncioTestCase):
    async def test_theme_select_follows_and_commands(self):
        mqtt = RawMqtt()
        scope = select_scope(mqtt)
        select = scope["HomeTilesThemeSelect"](entry({}), BASE)
        self.assertEqual(select._attr_unique_id, "mac_display_theme")
        self.assertEqual(select._attr_options, ["dark", "light", "auto"])
        await select.async_added_to_hass()
        await mqtt.subscriptions[f"{BASE}/stat/display_theme"](types.SimpleNamespace(payload="auto"))
        self.assertEqual(select._attr_current_option, "auto")
        await select.async_select_option("light")
        await select.async_select_option("sepia")
        self.assertEqual(mqtt.published, [(f"{BASE}/cmnd/display_theme", "light", False)])

    async def test_rotation_select_takes_the_panels_steps(self):
        mqtt = RawMqtt()
        scope = select_scope(mqtt)
        select = scope["HomeTilesRotationSelect"](entry({}), BASE)
        self.assertEqual(select._attr_unique_id, "mac_display_rotation")
        self.assertEqual(select._attr_options, ["0", "180"])
        await select.async_added_to_hass()
        state = mqtt.subscriptions[f"{BASE}/stat/display_rotation"]
        await state(types.SimpleNamespace(payload='{"degrees":270,"options":[0,90,180,270]}'))
        self.assertEqual((select._attr_current_option, select._attr_options), ("270", ["0", "90", "180", "270"]))
        await state(types.SimpleNamespace(payload="ON"))  # the old switch's payload changes nothing
        self.assertEqual(select._attr_current_option, "270")
        await select.async_select_option("90")
        await select.async_select_option("45")
        self.assertEqual(mqtt.published, [(f"{BASE}/cmnd/display_rotation", "90", False)])

    async def test_created_only_with_the_capability(self):
        scope = select_scope(RawMqtt())

        async def created(capabilities):
            entities = []
            await scope["async_setup_entry"](None, entry(capabilities), entities.extend)
            return [type(e).__name__ if not isinstance(e, str) else e for e in entities]

        self.assertEqual(await created({}), ["sleep"])
        self.assertEqual(await created({"display_theme": True, "display_rotation": True}),
                         ["sleep", "HomeTilesThemeSelect", "HomeTilesRotationSelect"])


class CapabilityTest(unittest.TestCase):
    def test_announced_never_inferred(self):
        norm = CAPABILITIES.normalise_capabilities({"display_theme": True, "display_rotation": False, "x": True})
        self.assertEqual(norm, {"display_theme": True, "display_rotation": False})
        self.assertFalse(CAPABILITIES.supports({}, "display_theme"))
        self.assertFalse(CAPABILITIES.supports({"model": "tab5"}, "display_rotation"))
        with self.assertRaises(ValueError):
            CAPABILITIES.normalise_capabilities({"display_theme": "yes"})

    def test_registry_follows_the_announcement(self):
        stale = CAPABILITIES.stale_display_controls
        new = {"capabilities": {"display_theme": True, "display_rotation": True}}
        self.assertTrue(stale("mac_display_rotate", "switch", new), "the old switch goes")
        self.assertFalse(stale("mac_display_rotate", "switch", {}), "older panels keep it")
        self.assertTrue(stale("mac_display_theme", "select", {}))
        self.assertTrue(stale("mac_display_rotation", "select", {}))
        self.assertFalse(stale("mac_display_theme", "select", new))
        self.assertFalse(stale("mac_display_rotation", "select", new))
        self.assertFalse(stale("mac_display_sleep", "switch", new))
        init = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('stale_display_controls(unique_id, entity.domain, merged)', init)

    def test_switch_only_without_the_rotation_select(self):
        tree = ast.parse((ROOT / "switch.py").read_text(encoding="utf-8"))
        setup = [node for node in tree.body if getattr(node, "name", None) == "async_setup_entry"]
        scope = {"HomeAssistant": object, "ConfigEntry": object, "entry_base_topic": lambda entry: BASE,
                 "Tab5RotateSwitch": lambda *args: "rotate", "Tab5DisplaySleepSwitch": lambda *args: "sleep",
                 "HomeTilesLocalRelay": lambda *args: "relay", "entry_local_io": lambda entry: [],
                 "LOCAL_IO_RELAY": "relay", "merged_capabilities_data": CAPABILITIES.merged_capabilities_data,
                 "supports": CAPABILITIES.supports}
        exec(compile(ast.fix_missing_locations(ast.Module(body=setup, type_ignores=[])), "switch.py", "exec"), scope)
        import asyncio

        def created(capabilities):
            entities = []
            asyncio.run(scope["async_setup_entry"](None, entry(capabilities), entities.extend))
            return entities

        self.assertEqual(created({}), ["rotate", "sleep"])
        self.assertEqual(created({"display_rotation": True}), ["sleep"])


class TranslationTest(unittest.TestCase):
    def test_every_language(self):
        for name in ["strings.json"] + [f"translations/{lang}.json" for lang in LANGUAGES]:
            data = json.loads((ROOT / name).read_text(encoding="utf-8"))
            selects = data["entity"]["select"]
            self.assertEqual(set(selects["display_theme"]["state"]), {"dark", "light", "auto"}, name)
            self.assertTrue(selects["display_theme"]["name"], name)
            self.assertEqual(selects["display_rotation"]["state"],
                             {"0": "0°", "90": "90°", "180": "180°", "270": "270°"}, name)
            self.assertEqual(selects["display_rotation"]["name"],
                             data["entity"]["switch"]["display_rotation"]["name"], name)


if __name__ == "__main__":
    unittest.main()
