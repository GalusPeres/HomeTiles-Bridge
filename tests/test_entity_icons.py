"""entity_icons.py: entity icons from Home Assistant's icon translations.

Home Assistant keeps most icons in the integrations' icons.json files, not
in the state attributes (the GitHub stars sensor, Kostal or YouTube
sensors, door binary sensors). Covers the resolution like the frontend
(state, numeric range, default), the cache lookups, loading per integration
with a broken integration, and the order in _extract_mdi_icon.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_link_protocol import PACKAGE, load_link_module

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"

GITHUB = {"github": {"sensor": {"stargazers_count": {"default": "mdi:star"},
                                "battery_state": {"default": "mdi:battery", "range": {"0": "mdi:battery-outline",
                                                                                      "50": "mdi:battery-50",
                                                                                      "90": "mdi:battery"}}}}}
COMPONENTS = {"binary_sensor": {"_": {"default": "mdi:radiobox-blank", "state": {"on": "mdi:checkbox-marked-circle"}},
                                "door": {"default": "mdi:door-closed", "state": {"on": "mdi:door-open"}}},
              "sensor": {"_": {"default": "mdi:eye"}}}


def load_icons(fake_get_icons=None):
    """Imports entity_icons with stubbed Home Assistant helpers."""
    registry = {}
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    icon = types.ModuleType("homeassistant.helpers.icon")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: SimpleNamespace(async_get=registry.get)
    icon.async_get_icons = fake_get_icons
    helpers.icon, helpers.entity_registry = icon, entity_registry
    stubs = {"homeassistant": ha, "homeassistant.helpers": helpers,
             "homeassistant.helpers.icon": icon, "homeassistant.helpers.entity_registry": entity_registry}
    load_link_module("link_protocol")  # the package
    sys.modules.pop(f"{PACKAGE}.entity_icons", None)
    patch = mock.patch.dict(sys.modules, stubs)
    patch.start()
    module = importlib.import_module(f"{PACKAGE}.entity_icons")
    return module, registry, patch


def state(entity_id, value, **attributes):
    return SimpleNamespace(entity_id=entity_id, state=value, attributes=attributes, name=entity_id)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.icons, _, self.patch = load_icons()

    def tearDown(self):
        self.patch.stop()
        sys.modules.pop(f"{PACKAGE}.entity_icons", None)

    def test_state_range_and_default_like_the_frontend(self):
        resolve = self.icons.resolve_icon
        door = COMPONENTS["binary_sensor"]["door"]
        self.assertEqual(resolve(door, "on"), "mdi:door-open")
        self.assertEqual(resolve(door, "off"), "mdi:door-closed")
        battery = GITHUB["github"]["sensor"]["battery_state"]
        self.assertEqual(resolve(battery, "72"), "mdi:battery-50")
        self.assertEqual(resolve(battery, "95.5"), "mdi:battery")
        self.assertEqual(resolve(battery, "3"), "mdi:battery-outline")
        self.assertEqual(resolve(battery, "unavailable"), "mdi:battery")
        self.assertEqual(resolve(battery, None), "mdi:battery")
        self.assertIsNone(resolve(None, "on"))
        self.assertIsNone(resolve({"range": {"x": "mdi:bad"}, "default": ""}, "5"))

    def test_cached_lookups(self):
        hass = SimpleNamespace(data={self.icons.DATA_ICON_TRANSLATIONS: {"entity": GITHUB, "component": COMPONENTS}})
        stars = state("sensor.galusperes_hometiles_sterne", "174")
        entry = SimpleNamespace(platform="github", translation_key="stargazers_count")
        self.assertEqual(self.icons.cached_entity_icon(hass, stars, entry), "mdi:star")
        self.assertIsNone(self.icons.cached_entity_icon(hass, stars, SimpleNamespace(platform="github", translation_key=None)))
        self.assertIsNone(self.icons.cached_entity_icon(hass, stars, None))
        door = state("binary_sensor.front", "on", device_class="door")
        self.assertEqual(self.icons.cached_component_icon(hass, door), "mdi:door-open")
        plain = state("binary_sensor.motion", "on", device_class="unknown_class")
        self.assertEqual(self.icons.cached_component_icon(hass, plain), "mdi:checkbox-marked-circle")
        self.assertIsNone(self.icons.cached_component_icon(hass, state("light.desk", "on")))
        # Media players keep the default icon, never a playback state.
        tv_icons = {"entity": {"cast": {"media_player": {"tv": {"default": "mdi:television", "state": {"playing": "mdi:play"}}}}}, "component": {}}
        tv = state("media_player.tv", "playing")
        self.assertEqual(self.icons.cached_entity_icon(SimpleNamespace(data={self.icons.DATA_ICON_TRANSLATIONS: tv_icons}), tv,
                                                       SimpleNamespace(platform="cast", translation_key="tv"), use_state=False),
                         "mdi:television")


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_each_integration_once_and_survives_a_broken_one(self):
        calls = []

        async def get_icons(hass, category, components):
            calls.append((category, set(components)))
            if "broken" in components:
                raise RuntimeError("no such integration")
            source = GITHUB if category == "entity" else COMPONENTS
            return {name: source[name] for name in components if name in source}

        icons, registry, patch = load_icons(get_icons)
        try:
            registry["sensor.stars"] = SimpleNamespace(platform="github", translation_key="stargazers_count")
            registry["sensor.other"] = SimpleNamespace(platform="broken", translation_key="value")
            registry["light.desk"] = SimpleNamespace(platform="hue", translation_key=None)
            hass = SimpleNamespace(data={})
            await icons.async_load_entity_icons(hass, ["sensor.stars", "sensor.other", "binary_sensor.door", "light.desk", "bad"])
            cache = hass.data[icons.DATA_ICON_TRANSLATIONS]
            self.assertEqual(cache["entity"]["github"], GITHUB["github"])
            self.assertEqual(cache["entity"]["broken"], {})
            self.assertNotIn("hue", cache["entity"])
            self.assertIn("door", cache["component"]["binary_sensor"])
            self.assertEqual(cache["component"]["light"], {})
            first = len(calls)
            await icons.async_load_entity_icons(hass, ["sensor.stars", "binary_sensor.door"])
            self.assertEqual(len(calls), first, "Loaded integrations are not fetched again")
        finally:
            patch.stop()
            sys.modules.pop(f"{PACKAGE}.entity_icons", None)

    async def test_without_icon_translations_nothing_happens(self):
        icons, _, patch = load_icons(None)
        try:
            hass = SimpleNamespace(data={})
            await icons.async_load_entity_icons(hass, ["sensor.stars"])
            self.assertEqual(hass.data, {})
        finally:
            patch.stop()
            sys.modules.pop(f"{PACKAGE}.entity_icons", None)


class ExtractOrderTests(unittest.TestCase):
    def test_home_assistant_order(self):
        icons, _, patch = load_icons()
        try:
            names = {"_extract_mdi_icon", "_normalize_mdi_icon_value", "_fallback_icon_from_state",
                     "_extract_media_player_mdi_icon"}
            nodes = [n for n in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
                     if isinstance(n, ast.FunctionDef) and n.name in names]
            entries = {}
            scope = {"entity_icons": icons, "cover_component_icon": lambda dc, st: "mdi:window-shutter",
                     "er": SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=entries.get))}
            tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
                              type_ignores=[])
            exec(compile(ast.fix_missing_locations(tree), str(SOURCE), "exec"), scope)
            extract = scope["_extract_mdi_icon"]
            hass = SimpleNamespace(data={icons.DATA_ICON_TRANSLATIONS: {"entity": GITHUB, "component": COMPONENTS}})
            stars = state("sensor.stars", "174")
            entries["sensor.stars"] = SimpleNamespace(icon=None, platform="github", translation_key="stargazers_count")
            self.assertEqual(extract(stars, hass), "mdi:star", "The integration's icon (it was missing before)")
            entries["sensor.stars"].icon = "mdi:heart"
            self.assertEqual(extract(stars, hass), "mdi:heart", "The user's icon in Home Assistant wins")
            entries["sensor.stars"].icon = None
            self.assertEqual(extract(state("sensor.stars", "1", icon="mdi:numeric"), hass), "mdi:numeric", "Then the icon attribute")
            battery = state("sensor.house_battery", "84", device_class="battery", unit_of_measurement="%")
            self.assertEqual(extract(battery, hass), "mdi:battery-80", "The Bridge's battery levels stay")
            self.assertEqual(extract(state("binary_sensor.front", "on", device_class="door"), hass), "mdi:door-open")
            self.assertEqual(extract(state("sensor.free_text", "x"), hass), "mdi:eye", "The domain default like the frontend")
            self.assertIsNone(extract(state("sensor.free_text", "x"), None))
            media = scope["_extract_media_player_mdi_icon"]
            entries["media_player.tv"] = SimpleNamespace(icon="mdi:television-classic", platform="cast", translation_key=None)
            self.assertEqual(media(state("media_player.tv", "playing"), hass), "mdi:television-classic")
        finally:
            patch.stop()
            sys.modules.pop(f"{PACKAGE}.entity_icons", None)

    def test_icons_load_before_they_are_published(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("icon_for_entity", text, "Home Assistant has no icon_for_entity")
        publish = text[text.index("async def _async_publish_icon_update"):]
        self.assertLess(publish.index("entity_icons.async_load_entity_icons(self.hass, self.tracked_entities)"),
                        publish.index("self._prime_icon_cache()"))
        config = text[text.index("async def async_publish_config_to_device"):]
        self.assertLess(config.index("entity_icons.async_load_entity_icons(self.hass, self.tracked_entities)"),
                        config.index('"sensor_meta": self._build_sensor_meta()'))


if __name__ == "__main__":
    unittest.main()
