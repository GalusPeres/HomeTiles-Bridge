"""The pause switch of a panel's own camera, run against small HA stubs."""

from __future__ import annotations

import ast
import importlib
import json
import logging
import sys
import types
import unittest
from unittest import mock

from test_local_camera_platform import BASE, READY, FakeMqtt, entry, message
from test_view_navigation import ROOT, load_module

PAUSED = '{"v":1,"state":"disabled","paused":true}'
STATUS = f"{BASE}/stat/local_camera"
CONNECTED = f"{BASE}/stat/connected"


class RawMqtt(FakeMqtt):
    """Record the exact published payload string, not only its JSON value."""

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class FakeSwitchEntity:
    _attr_available = True
    _attr_is_on = None
    written = 0

    @property
    def available(self):
        return self._attr_available

    @property
    def is_on(self):
        return self._attr_is_on

    def async_write_ha_state(self):
        self.written += 1

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


def load_switch_module(fake_mqtt):
    package_name = "_hometiles_switch_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    components.mqtt = fake_mqtt
    switch = types.ModuleType("homeassistant.components.switch")
    switch.SwitchEntity = FakeSwitchEntity
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = types.ModuleType("homeassistant.helpers")
    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.EntityCategory = types.SimpleNamespace(CONFIG="config")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    stubs = {
        package_name: package,
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.mqtt": fake_mqtt,
        "homeassistant.components.switch": switch,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity": entity,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.switch")


class LocalCameraSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = RawMqtt()
        self.module = load_switch_module(self.mqtt)

    async def created(self, config_entry):
        result = []
        await self.module.async_setup_entry(None, config_entry, result.extend)
        return [item for item in result
                if isinstance(item, self.module.HomeTilesLocalCameraSwitch)]

    async def start(self):
        [switch] = await self.created(entry({"local_camera": True}))
        switch.hass = object()
        await switch.async_added_to_hass()
        return switch

    async def deliver(self, topic, payload):
        handler, _ = self.mqtt.subscriptions[topic]
        await handler(message(topic, payload))

    async def test_switch_exists_exactly_with_the_camera_capability(self):
        self.assertEqual(await self.created(entry()), [])
        self.assertEqual(await self.created(entry({"local_camera": False})), [])
        self.assertEqual(await self.created(entry({"local_camera_stream": True})), [])
        # A stale option copy can never enable a camera the panel withdrew.
        stale = entry({"local_camera": False})
        stale.options = {"capabilities": {"local_camera": True}}
        self.assertEqual(await self.created(stale), [])
        [switch] = await self.created(entry({"local_camera": True}))
        self.assertEqual(switch._attr_unique_id, "mac_local_camera")
        self.assertEqual(switch._attr_translation_key, "local_camera")
        self.assertTrue(switch._attr_has_entity_name)
        self.assertEqual(switch._attr_icon, "mdi:camera")
        self.assertFalse(switch._attr_should_poll)
        self.assertIn(("tab5_lvgl", "mac"), switch.device_info["identifiers"])
        # The existing display switches are unchanged by the camera switch.
        result = []
        await self.module.async_setup_entry(None, entry({"local_camera": True}), result.extend)
        self.assertEqual([type(item).__name__ for item in result],
                         ["Tab5RotateSwitch", "Tab5DisplaySleepSwitch", "HomeTilesLocalCameraSwitch"])

    async def test_state_follows_the_retained_camera_status(self):
        switch = await self.start()
        self.assertEqual(set(self.mqtt.subscriptions), {STATUS, CONNECTED})
        self.assertEqual(self.mqtt.subscriptions[STATUS][1], "utf-8")
        # No status yet: unknown, but commands can be sent.
        self.assertIsNone(switch.is_on)
        self.assertTrue(switch.available)
        await self.deliver(STATUS, json.dumps(READY))
        self.assertIs(switch.is_on, True)
        await self.deliver(STATUS, PAUSED)
        self.assertIs(switch.is_on, False)
        self.assertTrue(switch.available)
        # An error keeps the last known pause state and stays available.
        await self.deliver(STATUS, '{"v":1,"state":"error","error":"sensor_unavailable"}')
        self.assertIs(switch.is_on, False)
        self.assertTrue(switch.available)
        await self.deliver(STATUS, json.dumps(READY))
        await self.deliver(STATUS, '{"v":1,"state":"error"}')
        self.assertIs(switch.is_on, True)
        # Malformed payloads, including a non-boolean paused, are ignored.
        for bad in ["{bad", '{"v":1,"state":"disabled","paused":"true"}',
                    '{"v":1,"state":"disabled","paused":1}']:
            await self.deliver(STATUS, bad)
            self.assertIs(switch.is_on, True)
            self.assertTrue(switch.available)
        # Disabled in the panel Web Admin (not paused): nothing to resume.
        await self.deliver(STATUS, '{"v":1,"state":"disabled"}')
        self.assertFalse(switch.available)
        await self.deliver(STATUS, PAUSED)
        self.assertTrue(switch.available)
        self.assertIs(switch.is_on, False)
        # A cleared retained status makes the state unknown again.
        await self.deliver(STATUS, "")
        self.assertIsNone(switch.is_on)
        self.assertTrue(switch.available)

    async def test_offline_panel_makes_the_switch_unavailable(self):
        switch = await self.start()
        await self.deliver(STATUS, PAUSED)
        await self.deliver(CONNECTED, "offline")
        self.assertFalse(switch.available)
        await self.deliver(CONNECTED, "garbage")
        self.assertFalse(switch.available)
        await self.deliver(CONNECTED, "1")
        self.assertTrue(switch.available)
        self.assertIs(switch.is_on, False)

    async def test_turn_off_pauses_and_turn_on_resumes(self):
        switch = await self.start()
        await self.deliver(STATUS, json.dumps(READY))
        written = switch.written
        await switch.async_turn_off()
        self.assertEqual(self.mqtt.published,
                         [(f"{BASE}/cmnd/local_camera", '{"v":1,"action":"pause"}', 0, False)])
        # Optimistic until the retained status confirms it.
        self.assertIs(switch.is_on, False)
        self.assertEqual(switch.written, written + 1)
        await self.deliver(STATUS, PAUSED)
        self.assertIs(switch.is_on, False)
        await switch.async_turn_on()
        self.assertEqual(self.mqtt.published[-1],
                         (f"{BASE}/cmnd/local_camera", '{"v":1,"action":"resume"}', 0, False))
        self.assertIs(switch.is_on, True)
        self.assertEqual(len(self.mqtt.published), 2)
        # A status that contradicts the optimistic state wins.
        await self.deliver(STATUS, PAUSED)
        self.assertIs(switch.is_on, False)

    async def test_removal_unsubscribes(self):
        switch = await self.start()
        await switch.async_will_remove_from_hass()
        self.assertEqual(self.mqtt.unsubscribed, 2)
        self.assertEqual(switch._subscriptions, [])


class WithdrawnCameraSwitchTest(unittest.TestCase):
    def test_registry_cleanup_covers_the_pause_switch_only(self):
        caps = load_module("capabilities")

        def entity(entity_id, unique_id, domain, entry_id="this"):
            return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                                         config_entry_id=entry_id, platform="tab5_lvgl", domain=domain)

        entries = [entity("camera.panel_camera", "mac_local_camera", "camera"),
                   entity("switch.panel_camera", "mac_local_camera", "switch"),
                   entity("switch.panel_display_sleep", "mac_display_sleep", "switch"),
                   entity("switch.other_panel_camera", "other_local_camera", "switch", "other"),
                   # A relay whose channel id happens to end like the camera.
                   entity("switch.panel_relay", "mac_local_io_relay_local_camera", "switch")]
        registry = types.SimpleNamespace(entities={item.entity_id: item for item in entries})
        removed = []

        def remove(entity_id):
            removed.append(entity_id)
            del registry.entities[entity_id]
        registry.async_remove = remove
        scope = {"er": types.SimpleNamespace(async_get=lambda hass: registry),
                 "entry_device_id": lambda entry: "mac", "CONF_LOCAL_IO": "local_io",
                 "DOMAIN": "tab5_lvgl",
                 "merged_capabilities_data": caps.merged_capabilities_data,
                 "stale_internal_sensor": caps.stale_internal_sensor,
                 "stale_local_camera": caps.stale_local_camera,
                 "entry_local_io": lambda entry: entry.data.get("local_io", []),
                 "local_io_unique_id": lambda device_id, item: f"{device_id}_local_io_{item['type']}_{item['id']}",
                 "_LOGGER": logging.getLogger(__name__)}
        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body
                     if getattr(node, "name", None) == "_remove_stale_local_io_entities"]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.Module(body=[future, *functions], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
        relay = {"id": "local_camera", "type": "relay"}
        supported = types.SimpleNamespace(entry_id="this", options={}, data={
            "local_io": [relay], "capabilities": {"local_camera": True}})
        scope["_remove_stale_local_io_entities"](None, supported)
        self.assertEqual(removed, [])
        withdrawn = types.SimpleNamespace(entry_id="this", options={}, data={
            "local_io": [relay], "capabilities": {"local_camera": False}})
        scope["_remove_stale_local_io_entities"](None, withdrawn)
        self.assertEqual(removed, ["camera.panel_camera", "switch.panel_camera"])


class SwitchTranslationTest(unittest.TestCase):
    def test_switch_name_is_translated_everywhere(self):
        files = [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]
        self.assertGreaterEqual(len(files), 3)
        names = {}
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            names[path.name] = data["entity"]["switch"]["local_camera"]["name"]
        self.assertEqual(names["strings.json"], "Camera")
        self.assertEqual(names["en.json"], "Camera")
        self.assertEqual(names["de.json"], "Kamera")


if __name__ == "__main__":
    unittest.main()
