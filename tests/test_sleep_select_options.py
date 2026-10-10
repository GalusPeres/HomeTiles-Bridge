"""Auto-Sleep options are translated: English HA showed the panel's "Nie"."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT


def load_select_module(published):
    package_name = "_hometiles_select_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    mqtt = types.ModuleType("homeassistant.components.mqtt")

    async def async_publish(hass, topic, payload, qos=0, retain=False):
        published.append((topic, payload))
    mqtt.async_publish = async_publish
    mqtt.ReceiveMessage = types.SimpleNamespace
    components = types.ModuleType("homeassistant.components")
    components.mqtt = mqtt
    select = types.ModuleType("homeassistant.components.select")
    select.SelectEntity = type("SelectEntity", (), {"async_write_ha_state": lambda self: None})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *args, **kwargs: None
    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.EntityCategory = types.SimpleNamespace(CONFIG="config")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    stubs = {
        package_name: package,
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": components,
        "homeassistant.components.mqtt": mqtt,
        "homeassistant.components.select": select,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.entity": entity,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.select")


def _translations(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))["entity"]["select"]


class SleepSelectOptionTests(unittest.TestCase):
    def setUp(self):
        self.published = []
        self.module = load_select_module(self.published)

    def test_options_are_translation_keys_with_both_languages(self):
        options = self.module.SLEEP_OPTIONS
        self.assertIn("never", options)
        for option in options:
            # Home Assistant state translation keys: lowercase, no spaces.
            self.assertRegex(option, r"^[a-z0-9_-]+$")
        for name, never in (("strings.json", "Never"), ("translations/en.json", "Never"),
                            ("translations/de.json", "Nie")):
            select = _translations(name)
            for key in ("sleep_mains", "sleep_battery"):
                states = select[key]["state"]
                self.assertEqual(set(states), set(options), f"{name} {key}")
                self.assertEqual(states["never"], never)
                self.assertEqual(states["5min"], "5 min")

    def test_panel_labels_map_to_options(self):
        parse = self.module.sleep_option_from_payload
        self.assertEqual(parse("Nie"), "never")
        self.assertEqual(parse(" never "), "never")
        self.assertEqual(parse("off"), "never")
        self.assertEqual(parse("5 min"), "5min")
        self.assertEqual(parse("60 S"), "60s")
        self.assertEqual(parse("15min"), "15min")
        self.assertIsNone(parse("2 min"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))

    def test_commands_keep_the_panel_labels(self):
        entry = types.SimpleNamespace(entry_id="e1", data={"device_id": "mac", "base_topic": "ht/p"},
                                      options={})
        entity = self.module.Tab5SleepSelect(entry, "ht/p", "sleep_mains", "mac_sleep_mains",
                                             "sleep_mains", "mdi:power-sleep")
        entity.hass = object()
        asyncio.run(entity.async_select_option("never"))
        asyncio.run(entity.async_select_option("5min"))
        asyncio.run(entity.async_select_option("Nie"))  # Not an option key: ignored.
        self.assertEqual([payload for _, payload in self.published], ["Nie", "5 min"])
        self.assertEqual(entity._attr_current_option, "5min")


if __name__ == "__main__":
    unittest.main()
