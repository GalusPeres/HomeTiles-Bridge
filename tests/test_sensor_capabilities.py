"""Capability-gated creation and targeted existing-installation migration."""

from __future__ import annotations

import ast
import logging
import re
import types
import unittest

from test_view_navigation import ROOT, load_module

CAPS = load_module("capabilities")
SELECTION = load_module("sensor_selection")


def extract(filename, names, scope):
    tree = ast.parse((ROOT / filename).read_text())
    functions = [node for node in tree.body if getattr(node, "name", None) in names]
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), filename, "exec"), scope)


class SensorCapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    async def created(self, data):
        scope = {"entry_base_topic": lambda entry: "panel", "CONF_HA_PREFIX": "ha_prefix",
                 "DEFAULT_PREFIX": "ha/statestream", "normalise_topic": lambda value, fallback: value or fallback,
                 "merged_capabilities_data": CAPS.merged_capabilities_data, "supports": CAPS.supports,
                 "Tab5BatterySensor": lambda *args: "battery",
                 "Tab5ExternalTemperatureSensor": lambda *args: "legacy_temperature",
                 "HomeTilesLocalTemperatureSensor": lambda entry, base, descriptor: descriptor["id"],
                 "entry_local_io": lambda entry: entry.data.get("local_io", []),
                 "LOCAL_IO_TEMPERATURE": "temperature"}
        extract("sensor.py", {"async_setup_entry"}, scope)
        result = []
        await scope["async_setup_entry"](None, types.SimpleNamespace(data=data, options={}), result.extend)
        return result

    async def test_fresh_modern_and_existing_legacy_panels(self):
        for model in ["waveshare_touch_lcd_4_3", "waveshare_touch_lcd_8", "guition_esp32_4848s040"]:
            self.assertEqual(await self.created({"model": model, "local_io": []}), [])
            self.assertEqual(await self.created({"model": model}), [])
        self.assertEqual(await self.created({"model": "tab5"}), ["battery", "legacy_temperature"])
        self.assertEqual(await self.created({"model": "tab5", "local_io": [],
            "capabilities": {"battery_soc": False, "legacy_external_temperature": False}}), [])
        self.assertEqual(await self.created({"model": "future_board", "local_io": [
            {"id": "probe", "type": "temperature"}], "capabilities": {"battery_soc": True}}), ["battery", "probe"])

    def test_explicit_capabilities_validate_and_override_stale_options(self):
        for value in [None, [], {"battery_soc": 1}, {"view_navigation": "true"}]:
            with self.assertRaises(ValueError): CAPS.normalise_capabilities(value)
        self.assertEqual(CAPS.normalise_capabilities({"future": True}), {})
        entry = types.SimpleNamespace(data={"capabilities": {"battery_soc": False}, "local_io": []},
            options={"capabilities": {"battery_soc": True}, "local_io": [{"id": "ghost"}]})
        merged = CAPS.merged_capabilities_data(entry)
        self.assertFalse(CAPS.supports(merged, "battery_soc"))
        self.assertEqual(merged["local_io"], [])
        self.assertFalse(CAPS.supports({}, "view_navigation"))

    async def test_actual_configuration_feedback_upgrades_once_and_preserves_legacy_fields(self):
        constants = vars(load_module("const"))
        scope = dict(constants)
        scope.update(vars(load_module("editable_helpers")))
        scope.update(vars(load_module("control_helpers")))
        scope.update({
            "config_entries": types.SimpleNamespace(SOURCE_IGNORE="ignore"),
            "CAPABILITIES": "capabilities",
            "normalise_capabilities": CAPS.normalise_capabilities,
            "normalise_local_io": lambda value: value,
            "_normalise_topic": lambda value, fallback: value or fallback,
            "_unique_entities": lambda values: list(dict.fromkeys(values)),
            "_split_weather_entities": lambda values: ([], values),
            "split_binary_sensor_entities": lambda values: ([], values),
            "_runtime_managed_sensor_entity_ids": lambda *args: {
                "sensor.tab5_internal_battery_soc", "sensor.tab5_external_temperature"},
            "filter_runtime_sensor_entities": SELECTION.filter_runtime_sensor_entities,
            "clean_stored_sensor_selections": SELECTION.clean_stored_sensor_selections,
            "should_import_feedback_selection": SELECTION.should_import_feedback_selection,
            "_LOGGER": logging.getLogger(__name__),
        })
        entry = types.SimpleNamespace(entry_id="panel", source="user", data={"device_id": "mac", "sensors": [
            "sensor.tab5_internal_battery_soc", "sensor.room"]},
            options={"sensors": ["sensor.tab5_external_temperature", "sensor.room"]})
        updates = []
        def update(item, **fields):
            updates.append(fields)
            for key, value in fields.items(): setattr(item, key, value)
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=update))
        scope["_find_entry_by_device_id"] = lambda *args: entry
        extract("__init__.py", {"_payload_to_entry_data", "_async_process_bridge_config"}, scope)
        payload = {"device_id": "mac", "model": "waveshare_touch_lcd_4_3",
                   "sensors": ["sensor.tab5_internal_battery_soc", "sensor.tab5_external_temperature", "sensor.room"],
                   "configured_sensors": ["sensor.room"], "local_io": [],
                   "capabilities": {"battery_soc": False, "legacy_external_temperature": False, "view_navigation": True}}
        await scope["_async_process_bridge_config"](hass, payload)
        self.assertEqual(entry.data["sensors"], ["sensor.room"])
        self.assertEqual(entry.options["sensors"], ["sensor.room"])
        self.assertEqual(entry.data["capabilities"], payload["capabilities"])
        await scope["_async_process_bridge_config"](hass, payload)
        self.assertEqual(len(updates), 1)
        # Downgrading firmware or receiving an old retained announcement does
        # not discard the most recent positive/negative capability evidence.
        await scope["_async_process_bridge_config"](hass, {"device_id": "mac", "sensors": []})
        self.assertEqual(len(updates), 1)
        self.assertEqual(entry.data["local_io"], [])
        bad = dict(payload, capabilities={"battery_soc": "false"})
        await scope["_async_process_bridge_config"](hass, bad)
        self.assertEqual(len(updates), 1)

    def test_actual_registry_migration_is_owned_capability_based_and_idempotent(self):
        def entity(entity_id, unique_id, entry="this", platform="tab5_lvgl", domain="sensor"):
            return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                config_entry_id=entry, platform=platform, domain=domain)
        entries = [entity("sensor.renamed_battery", "oldmac_battery_soc"),
                   entity("sensor.tab5_external_temperature", "oldmac_external_temperature"),
                   entity("sensor.probe", "mac_local_io_probe"),
                   entity("sensor.tab5_living_room", "user", platform="template"),
                   entity("sensor.other_panel", "other_battery_soc", entry="other"),
                   entity("sensor.old_probe", "mac_local_io_old")]
        registry = types.SimpleNamespace(entities={item.entity_id: item for item in entries})
        removed = []
        def remove(entity_id):
            removed.append(entity_id)
            del registry.entities[entity_id]
        registry.async_remove = remove
        scope = {"er": types.SimpleNamespace(async_get=lambda hass: registry),
                 "entry_device_id": lambda entry: "mac", "CONF_LOCAL_IO": "local_io", "DOMAIN": "tab5_lvgl",
                 "merged_capabilities_data": CAPS.merged_capabilities_data,
                 "stale_internal_sensor": CAPS.stale_internal_sensor,
                 "entry_local_io": lambda entry: entry.data.get("local_io", []),
                 "local_io_unique_id": lambda device_id, descriptor: f"{device_id}_local_io_{descriptor['id']}",
                 "_LOGGER": logging.getLogger(__name__)}
        extract("__init__.py", {"_remove_stale_local_io_entities"}, scope)
        entry = types.SimpleNamespace(entry_id="this", data={"model": "waveshare_touch_lcd_4_3",
            "local_io": [{"id": "probe", "type": "temperature"}]}, options={})
        scope["_remove_stale_local_io_entities"](None, entry)
        self.assertEqual(set(removed), {"sensor.renamed_battery", "sensor.tab5_external_temperature", "sensor.old_probe"})
        scope["_remove_stale_local_io_entities"](None, entry)
        self.assertEqual(len(removed), 3)
        self.assertEqual(set(registry.entities), {"sensor.probe", "sensor.tab5_living_room", "sensor.other_panel"})

    def test_local_camera_capability_is_explicit_and_validated(self):
        self.assertEqual(CAPS.normalise_capabilities({"local_camera": True}), {"local_camera": True})
        self.assertEqual(CAPS.normalise_capabilities({"local_camera": False, "future": 1}),
                         {"local_camera": False})
        for value in [1, 0, "true", None, [], {}]:
            with self.assertRaises(ValueError):
                CAPS.normalise_capabilities({"local_camera": value})
        # Never inferred from the model, not even for the camera-equipped board.
        for data in [{}, {"model": "tab5"}, {"model": "guition_jc8012p4a1_v2"},
                     {"capabilities": {"view_navigation": True}}]:
            self.assertFalse(CAPS.supports(data, "local_camera"))
        self.assertTrue(CAPS.supports({"capabilities": {"local_camera": True}}, "local_camera"))
        self.assertTrue(CAPS.stale_local_camera("mac_local_camera", {"capabilities": {"local_camera": False}}))
        self.assertTrue(CAPS.stale_local_camera("mac_local_camera", {}))
        self.assertFalse(CAPS.stale_local_camera("mac_local_camera", {"capabilities": {"local_camera": True}}))
        self.assertFalse(CAPS.stale_local_camera("mac_view", {}))

    async def test_local_camera_capability_flip_updates_entry_once_per_change(self):
        constants = vars(load_module("const"))
        scope = dict(constants)
        scope.update(vars(load_module("editable_helpers")))
        scope.update(vars(load_module("control_helpers")))
        scope.update({
            "config_entries": types.SimpleNamespace(SOURCE_IGNORE="ignore"),
            "CAPABILITIES": "capabilities",
            "normalise_capabilities": CAPS.normalise_capabilities,
            "normalise_local_io": lambda value: value,
            "_normalise_topic": lambda value, fallback: value or fallback,
            "_unique_entities": lambda values: list(dict.fromkeys(values)),
            "_split_weather_entities": lambda values: ([], values),
            "split_binary_sensor_entities": lambda values: ([], values),
            "_runtime_managed_sensor_entity_ids": lambda *args: set(),
            "filter_runtime_sensor_entities": SELECTION.filter_runtime_sensor_entities,
            "clean_stored_sensor_selections": SELECTION.clean_stored_sensor_selections,
            "should_import_feedback_selection": SELECTION.should_import_feedback_selection,
            "_LOGGER": logging.getLogger(__name__),
        })
        entry = types.SimpleNamespace(entry_id="panel", source="user", data={"device_id": "mac"}, options={})
        updates = []
        def update(item, **fields):
            updates.append(fields)
            for key, value in fields.items(): setattr(item, key, value)
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=update))
        scope["_find_entry_by_device_id"] = lambda *args: entry
        extract("__init__.py", {"_payload_to_entry_data", "_async_process_bridge_config"}, scope)
        payload = {"device_id": "mac", "model": "guition_jc8012p4a1_v2", "sensors": [], "local_io": [],
                   "capabilities": {"battery_soc": False, "local_camera": True}}
        await scope["_async_process_bridge_config"](hass, payload)
        self.assertTrue(CAPS.supports(CAPS.merged_capabilities_data(entry), "local_camera"))
        await scope["_async_process_bridge_config"](hass, payload)
        self.assertEqual(len(updates), 1)
        withdrawn = dict(payload, capabilities={"battery_soc": False, "local_camera": False})
        await scope["_async_process_bridge_config"](hass, withdrawn)
        self.assertEqual(len(updates), 2)
        self.assertFalse(CAPS.supports(CAPS.merged_capabilities_data(entry), "local_camera"))
        await scope["_async_process_bridge_config"](hass, dict(payload, capabilities={"local_camera": "true"}))
        self.assertEqual(len(updates), 2)
        self.assertFalse(CAPS.supports(CAPS.merged_capabilities_data(entry), "local_camera"))

    def test_withdrawn_local_camera_entity_is_removed_from_registry(self):
        def entity(entity_id, unique_id, entry="this", domain="camera"):
            return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                config_entry_id=entry, platform="tab5_lvgl", domain=domain)
        entries = [entity("camera.panel_camera", "mac_local_camera"),
                   entity("camera.other_panel_camera", "other_local_camera", entry="other"),
                   entity("select.panel_view", "mac_view", domain="select")]
        registry = types.SimpleNamespace(entities={item.entity_id: item for item in entries})
        removed = []
        def remove(entity_id):
            removed.append(entity_id)
            del registry.entities[entity_id]
        registry.async_remove = remove
        scope = {"er": types.SimpleNamespace(async_get=lambda hass: registry),
                 "entry_device_id": lambda entry: "mac", "CONF_LOCAL_IO": "local_io", "DOMAIN": "tab5_lvgl",
                 "merged_capabilities_data": CAPS.merged_capabilities_data,
                 "stale_internal_sensor": CAPS.stale_internal_sensor,
                 "stale_local_camera": CAPS.stale_local_camera,
                 "entry_local_io": lambda entry: entry.data.get("local_io", []),
                 "local_io_unique_id": lambda device_id, descriptor: f"{device_id}_local_io_{descriptor['id']}",
                 "_LOGGER": logging.getLogger(__name__)}
        extract("__init__.py", {"_remove_stale_local_io_entities"}, scope)
        supported = types.SimpleNamespace(entry_id="this", options={}, data={
            "local_io": [], "capabilities": {"local_camera": True}})
        scope["_remove_stale_local_io_entities"](None, supported)
        self.assertEqual(removed, [])
        withdrawn = types.SimpleNamespace(entry_id="this", options={}, data={
            "local_io": [], "capabilities": {"local_camera": False}})
        scope["_remove_stale_local_io_entities"](None, withdrawn)
        self.assertEqual(removed, ["camera.panel_camera"])
        self.assertEqual(set(registry.entities), {"camera.other_panel_camera", "select.panel_view"})

    def test_actual_alias_resolution_preserves_registered_and_explicit_user_entities(self):
        user = types.SimpleNamespace(entity_id="sensor.tab5_external_temperature",
            config_entry_id="user", platform="template", domain="sensor")
        registry = types.SimpleNamespace(entities={user.entity_id: user}, async_get=lambda key: user if key == user.entity_id else None)
        entry = types.SimpleNamespace(entry_id="this", title="Waveshare Touch LCD 4.3",
            data={"user_sensor_selections": ["sensor.batterie_soc"]}, options={})
        scope = {"er": types.SimpleNamespace(async_get=lambda hass: registry),
                 "_preserved_user_sensor_ids": lambda hass, entry, incoming=None: set(entry.data.get("user_sensor_selections", [])),
                 "entry_device_name": lambda entry: entry.title, "entry_local_io": lambda entry: [],
                 "normalise_local_io": lambda value: value, "local_io_announced_entity_id": lambda value: value.get("entity_id"),
                 "_unique_entities": lambda values: list(dict.fromkeys(values)),
                 "slugify": lambda value: re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_"),
                 "runtime_sensor_entity_id_candidates": SELECTION.runtime_sensor_entity_id_candidates,
                 "DOMAIN": "tab5_lvgl", "LOCAL_IO_TEMPERATURE": "temperature",
                 "CONF_DEVICE_NAME": "device_name", "CONF_MODEL": "model", "CONF_DEVICE_ID": "device_id", "CONF_LOCAL_IO": "local_io"}
        extract("__init__.py", {"_runtime_managed_sensor_entity_ids"}, scope)
        runtime = scope["_runtime_managed_sensor_entity_ids"](None, entry)
        self.assertNotIn(user.entity_id, runtime)
        self.assertNotIn("sensor.batterie_soc", runtime)
        self.assertIn("sensor.waveshare_touch_lcd_4_3_batterie_soc", runtime)
        stored = ["sensor.waveshare_touch_lcd_4_3_batterie_soc", user.entity_id, "sensor.batterie_soc"]
        data, options, *_ = SELECTION.clean_stored_sensor_selections({"sensors": stored}, {"sensors": stored}, "sensors", runtime)
        self.assertEqual(data["sensors"], [user.entity_id, "sensor.batterie_soc"])
        self.assertEqual(data, options)
        self.assertFalse(SELECTION.should_import_feedback_selection(data, options, "sensors", stored))

    def test_explicit_selection_survives_except_removed_owned_capabilities(self):
        def entity(entity_id, unique_id, platform="tab5_lvgl"):
            return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                config_entry_id="this", platform=platform, domain="sensor")
        entities = [entity("sensor.battery", "mac_battery_soc"),
                    entity("sensor.probe", "mac_local_io_probe"),
                    entity("sensor.old_probe", "mac_local_io_old"),
                    entity("sensor.tab5_external_temperature", "user", "template")]
        registry = types.SimpleNamespace(async_get={item.entity_id: item for item in entities}.get)
        selected = [item.entity_id for item in entities] + ["sensor.batterie_soc"]
        entry = types.SimpleNamespace(entry_id="this", data={"sensors": selected,
            "user_sensor_selections": selected, "capabilities": {"battery_soc": False},
            "local_io": [{"id": "probe", "type": "temperature"}]},
            options={"sensors": selected, "user_sensor_selections": selected})
        def update(item, **fields):
            for key, value in fields.items(): setattr(item, key, value)
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
            async_entries=lambda domain: [entry], async_update_entry=update))
        scope = {"er": types.SimpleNamespace(async_get=lambda hass: registry), "DOMAIN": "tab5_lvgl",
                 "merged_capabilities_data": CAPS.merged_capabilities_data,
                 "entry_device_id": lambda entry: "mac", "entry_local_io": lambda entry: [],
                 "local_io_unique_id": lambda device_id, item: f"{device_id}_local_io_{item['id']}",
                 "stale_internal_sensor": CAPS.stale_internal_sensor, "CONF_LOCAL_IO": "local_io",
                 "CONF_SENSORS": "sensors", "_LOGGER": logging.getLogger(__name__),
                 "clean_stored_sensor_selections": SELECTION.clean_stored_sensor_selections,
                 "_runtime_managed_sensor_entity_ids": lambda *args: set(selected)}
        extract("__init__.py", {"_preserved_user_sensor_ids", "_cleanup_persisted_runtime_sensor_entities"}, scope)
        keep = {"sensor.probe", "sensor.tab5_external_temperature", "sensor.batterie_soc"}
        self.assertEqual(scope["_preserved_user_sensor_ids"](hass, entry), keep)
        self.assertIn("sensor.battery", scope["_preserved_user_sensor_ids"](
            hass, entry, {"capabilities": {"battery_soc": True}}))
        scope["_cleanup_persisted_runtime_sensor_entities"](hass, entry)
        for storage in (entry.data, entry.options):
            self.assertEqual(set(storage["sensors"]), keep)
            self.assertEqual(set(storage["user_sensor_selections"]), keep)
        scope["_cleanup_persisted_runtime_sensor_entities"](hass, entry)
        self.assertEqual(set(entry.data["sensors"]), keep)


if __name__ == "__main__":
    unittest.main()
