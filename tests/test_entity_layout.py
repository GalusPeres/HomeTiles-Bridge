"""The panel's device page in Home Assistant groups its entities by purpose.

Home Assistant files entities with EntityCategory.CONFIG under
Configuration and sorts every group by name. The Camera switch sat under
Controls while the Microphone switch sat under Configuration, the wake word
list mixed "Off" into the words, "Display Rotation" and "Display Sleep" were
hard-coded English names and a leftover battery auto-sleep select remained.
"""

from __future__ import annotations

import ast
import logging
import types
import unittest

from test_view_navigation import ROOT, load_module

# Settings of the panel itself: Configuration.
CONFIG = {
    ("switch", "HomeTilesMicrophoneSwitch"),
    ("switch", "HomeTilesWakeListenSwitch"),
    ("switch", "HomeTilesLocalCameraSwitch"),
    ("switch", "Tab5RotateSwitch"),
    ("switch", "Tab5DisplaySleepSwitch"),
    ("select", "Tab5SleepSelect"),
    ("select", "HomeTilesWakeWordSelect"),
    ("light", "Tab5DisplayLight"),
    ("light", "Tab5ScreensaverBrightnessLight"),
}
# Things to use or watch: Controls and Sensors.
NOT_CONFIG = {
    ("camera", "HomeTilesLocalCamera"),
    ("button", "HomeTilesAssistButton"),
    ("media_player", "HomeTilesSpeaker"),
    ("select", "HomeTilesViewSelect"),
}


def class_attributes(platform: str) -> dict[str, dict[str, str]]:
    tree = ast.parse((ROOT / f"{platform}.py").read_text(encoding="utf-8"))
    classes = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = {
                item.targets[0].id: ast.unparse(item.value)
                for item in node.body
                if isinstance(item, ast.Assign) and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
            }
    return classes


class EntityLayoutTest(unittest.TestCase):
    def test_panel_settings_are_configuration(self):
        for platform, name in CONFIG:
            attributes = class_attributes(platform)[name]
            self.assertEqual(attributes.get("_attr_entity_category"), "EntityCategory.CONFIG",
                             f"{platform}.{name}")
        for platform, name in NOT_CONFIG:
            attributes = class_attributes(platform)[name]
            self.assertNotIn("_attr_entity_category", attributes, f"{platform}.{name}")

    def test_display_switches_use_translation_keys(self):
        switches = class_attributes("switch")
        self.assertEqual(switches["Tab5RotateSwitch"].get("_attr_translation_key"),
                         "'display_rotation'")
        self.assertEqual(switches["Tab5DisplaySleepSwitch"].get("_attr_translation_key"),
                         "'display_standby'")
        for name, attributes in switches.items():
            self.assertNotIn("_attr_name", attributes, name)

    def test_battery_auto_sleep_is_gone(self):
        self.assertNotIn("sleep_battery", (ROOT / "select.py").read_text(encoding="utf-8"))
        self.assertNotIn("TOPIC_SLEEP_BATTERY", (ROOT / "const.py").read_text(encoding="utf-8"))


class StaleEntityCleanupTest(unittest.TestCase):
    """Registry entries of removed entities do not stay behind as unavailable."""

    def cleanup(self, capabilities):
        caps = load_module("capabilities")

        def entity(entity_id, unique_id, domain):
            return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                                         config_entry_id="this", platform="tab5_lvgl",
                                         domain=domain)

        entries = [entity("select.panel_sleep_battery", "mac_sleep_battery", "select"),
                   entity("select.panel_sleep_mains", "mac_sleep_mains", "select"),
                   entity("switch.panel_microphone", "mac_assist_microphone", "switch"),
                   entity("switch.panel_listening", "mac_assist_wake_listen", "switch"),
                   entity("switch.panel_display_sleep", "mac_display_sleep", "switch")]
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
                 "stale_assist": caps.stale_assist,
                 "stale_display_controls": caps.stale_display_controls,
                 "stale_speaker": caps.stale_speaker,
                 "entry_local_io": lambda entry: entry.data.get("local_io", []),
                 "local_io_unique_id": lambda device_id, item: f"{device_id}_local_io_{item['id']}",
                 "_LOGGER": logging.getLogger(__name__)}
        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body
                     if getattr(node, "name", None) == "_remove_stale_local_io_entities"]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.Module(body=[future, *functions], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
        panel = types.SimpleNamespace(entry_id="this", options={}, data={
            "capabilities": capabilities})
        scope["_remove_stale_local_io_entities"](None, panel)
        return removed

    def test_battery_select_is_removed(self):
        self.assertEqual(self.cleanup({"audio_input": True}), ["select.panel_sleep_battery"])

    def test_microphone_switches_leave_with_the_microphone(self):
        self.assertEqual(self.cleanup({"audio_input": False}),
                         ["select.panel_sleep_battery", "switch.panel_microphone",
                          "switch.panel_listening"])


if __name__ == "__main__":
    unittest.main()
