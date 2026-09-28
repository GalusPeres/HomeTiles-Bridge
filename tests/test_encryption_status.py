"""Whether a panel is encrypted: diagnostic sensor and options menu, without HA."""

from __future__ import annotations

import ast
import importlib
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT, load_module

CC = load_module("command_channel")
KEY = "b925def556256ead767b0f1d14879e50d6bddbd0bb44dc0d2435e4af011b3e19"
KEY_ID = "20a8108ed11215c5"


class FakeBinarySensor:
    pass


def load_binary_sensor_module():
    package_name = "_hometiles_binary_sensor_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    components = types.ModuleType("homeassistant.components")
    components.mqtt = types.ModuleType("homeassistant.components.mqtt")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorEntity = FakeBinarySensor
    binary_sensor.BinarySensorDeviceClass = types.SimpleNamespace(CONNECTIVITY="connectivity")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    stubs = {
        package_name: package,
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": components,
        "homeassistant.components.mqtt": components.mqtt,
        "homeassistant.components.binary_sensor": binary_sensor,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.entity": entity,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.binary_sensor")


def entry(**data):
    values = {"device_id": "mac", "base_topic": "hometiles"}
    values.update(data)
    return types.SimpleNamespace(entry_id="e1", data=values, options={})


class EncryptionSensorTest(unittest.IsolatedAsyncioTestCase):
    async def created(self, config_entry):
        module = load_binary_sensor_module()
        result = []
        await module.async_setup_entry(None, config_entry, result.extend)
        return result

    async def test_paired_panel_shows_a_closed_shield(self):
        [connection, sensor] = await self.created(entry(command_pairing_key=KEY))
        self.assertEqual(connection._attr_unique_id, "mac_mqtt_connected")
        self.assertEqual(sensor._attr_unique_id, "mac_encryption")
        self.assertEqual(sensor._attr_translation_key, "encryption")
        self.assertEqual(sensor._attr_entity_category, "diagnostic")
        self.assertTrue(sensor._attr_has_entity_name)
        self.assertIs(sensor._attr_is_on, True)
        self.assertEqual(sensor._attr_icon, "mdi:shield-lock")
        self.assertEqual(sensor._attr_extra_state_attributes, {"key_id": KEY_ID})
        self.assertNotIn(KEY, repr(sensor.__dict__))

    async def test_unpaired_or_removing_panel_shows_an_open_shield(self):
        for data in ({}, {"command_pairing_removing": KEY}, {"command_pairing_code": "ABCDE"}):
            [_connection, sensor] = await self.created(entry(**data))
            self.assertIs(sensor._attr_is_on, False, data)
            self.assertEqual(sensor._attr_icon, "mdi:shield-off-outline")
            self.assertEqual(sensor._attr_extra_state_attributes, {})


def menu_helpers():
    source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [node for node in tree.body
             if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None)
                 in ("_PAIRING_CARD_TITLES", "_SECURITY_STATES"))
             or (isinstance(node, ast.FunctionDef) and node.name in ("_language", "_security_state"))]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *nodes], type_ignores=[])
    scope = {"entry_pairing_key": CC.entry_pairing_key, "entry_removing_key": CC.entry_removing_key}
    exec(compile(ast.fix_missing_locations(module), "config_flow.py", "exec"), scope)
    return source, scope


class SecurityMenuTest(unittest.TestCase):
    def test_menu_names_the_state_in_the_language_of_home_assistant(self):
        source, scope = menu_helpers()
        state = scope["_security_state"]
        for language, paired, removing, off in (
            ("en", "encrypted", "turning off", "not encrypted"),
            ("de", "verschlüsselt", "wird ausgeschaltet", "nicht verschlüsselt"),
            ("fr", "encrypted", "turning off", "not encrypted"),
        ):
            hass = types.SimpleNamespace(config=types.SimpleNamespace(language=language))
            self.assertEqual(state(hass, entry(command_pairing_key=KEY)), paired)
            self.assertEqual(state(hass, entry(command_pairing_removing=KEY)), removing)
            self.assertEqual(state(hass, entry()), off)
        tree = ast.parse(source)
        step = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_init")
        self.assertIn('description_placeholders={"security": _security_state(self.hass, self.config_entry)}',
                      ast.get_source_segment(source, step))


if __name__ == "__main__":
    unittest.main()
