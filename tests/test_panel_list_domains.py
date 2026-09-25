"""A panel announcement cannot add foreign domains to the shared lists."""

from __future__ import annotations

import ast
import types
import unittest

from test_view_navigation import ROOT, load_module

CONTROL = load_module("control_helpers")


def may_adopt_entry():
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if getattr(item, "name", None) == "_may_adopt_entry")
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    scope = {"CONF_DEVICE_ID": "device_id"}
    exec(compile(ast.fix_missing_locations(module), "adopt", "exec"), scope)
    return scope["_may_adopt_entry"]


class PanelAdoptionTest(unittest.TestCase):
    def test_announce_cannot_take_over_another_panels_entry(self):
        adopt = may_adopt_entry()
        entry = lambda device_id=None, unique_id=None: types.SimpleNamespace(
            data={"device_id": device_id} if device_id else {}, unique_id=unique_id)
        # A manual entry without a panel waits for its first announcement.
        self.assertTrue(adopt(entry(), "A1B2C3D4E5F6"))
        # An entry bound to another panel is never taken over.
        self.assertFalse(adopt(entry("A1B2C3D4E5F6", "A1B2C3D4E5F6"), "FFFFFFFFFFFF"))
        # Firmware before v0.3.1 used tab5_lvgl_XXXX with the MAC suffix.
        self.assertTrue(adopt(entry("tab5_lvgl_ABCD"), "00112233ABCD"))
        self.assertFalse(adopt(entry("tab5_lvgl_ABCD"), "0011223344AE"))


class PanelListDomainTest(unittest.TestCase):
    def test_control_lists_accept_only_their_domains(self):
        cases = {
            "lights": ("light.kitchen", "script.unlock"),
            "media_players": ("media_player.tv", "lock.front_door"),
            "climates": ("climate.living", "switch.heater"),
            "cameras": ("camera.door", "script.open_gate"),
            "switches": ("fan.bedroom", "lock.front_door"),
        }
        for key, (valid, foreign) in cases.items():
            self.assertEqual(CONTROL.panel_entity_list([valid], key), [valid])
            with self.assertRaises(ValueError, msg=key):
                CONTROL.panel_entity_list([valid, foreign], key)
        self.assertEqual(CONTROL.panel_entity_list(None, "lights"), [])
        with self.assertRaises(ValueError):
            CONTROL.panel_entity_list("light.kitchen", "lights")


if __name__ == "__main__":
    unittest.main()
