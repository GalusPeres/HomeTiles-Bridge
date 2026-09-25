"""A panel announcement cannot add foreign domains to the shared lists."""

from __future__ import annotations

import unittest

from test_view_navigation import load_module

CONTROL = load_module("control_helpers")


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
