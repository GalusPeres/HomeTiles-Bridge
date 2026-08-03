"""Dependency-free smoke tests for firmware-announced local hardware I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


def _load_local_io_module():
    """Load local_io without requiring a full Home Assistant installation."""
    package_name = "_hometiles_bridge_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package

    try:
        __import__("homeassistant.config_entries")
    except ModuleNotFoundError:
        homeassistant = types.ModuleType("homeassistant")
        config_entries = types.ModuleType("homeassistant.config_entries")
        config_entries.ConfigEntry = object
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.config_entries"] = config_entries

    const = types.ModuleType(f"{package_name}.const")
    const.CONF_LOCAL_IO = "local_io"
    sys.modules[const.__name__] = const

    device_helpers = types.ModuleType(f"{package_name}.device_helpers")
    device_helpers.command_topic = lambda base, leaf: f"{base}/cmnd/{leaf}"
    device_helpers.state_topic = lambda base, leaf: f"{base}/stat/{leaf}"
    sys.modules[device_helpers.__name__] = device_helpers

    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tab5_lvgl"
        / "local_io.py"
    )
    spec = importlib.util.spec_from_file_location(f"{package_name}.local_io", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LOCAL_IO = _load_local_io_module()


class LocalIoSmokeTest(unittest.TestCase):
    def test_normalises_current_and_legacy_entity_ids(self) -> None:
        channels = LOCAL_IO.normalise_local_io(
            [
                {
                    "id": "relay_1",
                    "type": "switch",
                    "name": "Desk Lamp",
                    "entity_id": "switch.panel_desk_lamp",
                    "legacy_entity_ids": [
                        "switch.panel_relay_1",
                        "switch.panel_relay_1",
                    ],
                },
                {
                    "id": "temperature_1",
                    "type": "temp",
                    "name": "Case",
                    "entity_id": "sensor.panel_case",
                    "legacy_entity_ids": ["sensor.panel_temperature_1"],
                    "unit": "celsius",
                    "precision": 9,
                },
            ]
        )

        self.assertEqual(channels[0]["type"], "relay")
        self.assertEqual(
            channels[0]["legacy_entity_ids"], ["switch.panel_relay_1"]
        )
        self.assertEqual(channels[1]["unit"], "\N{DEGREE SIGN}C")
        self.assertEqual(channels[1]["precision"], 3)

    def test_rejects_ambiguous_legacy_entity_ids(self) -> None:
        invalid_announcements = [
            [{"id": "relay_1", "type": "relay", "entity_id": "sensor.bad"}],
            [
                {
                    "id": "relay_1",
                    "type": "relay",
                    "entity_id": "switch.new",
                    "legacy_entity_ids": ["sensor.old"],
                }
            ],
            [
                {
                    "id": "a",
                    "type": "relay",
                    "entity_id": "switch.a",
                    "legacy_entity_ids": ["switch.b"],
                },
                {"id": "b", "type": "relay", "entity_id": "switch.b"},
            ],
            [
                {
                    "id": "a",
                    "type": "relay",
                    "legacy_entity_ids": ["switch.old"],
                },
                {
                    "id": "b",
                    "type": "relay",
                    "legacy_entity_ids": ["switch.old"],
                },
            ],
        ]

        for announcement in invalid_announcements:
            with self.subTest(announcement=announcement):
                with self.assertRaises(ValueError):
                    LOCAL_IO.normalise_local_io(announcement)

    def test_preserves_home_assistant_collision_suffix_during_migration(self) -> None:
        migrate = LOCAL_IO.local_io_migration_target_entity_id
        old = "switch.m5stacks_tab5_relay_1"
        new = "switch.m5stacks_tab5_desk_lamp"

        self.assertEqual(migrate(old, new, [old]), new)
        self.assertEqual(migrate(f"{old}_2", new, [old]), f"{new}_2")
        self.assertEqual(migrate(f"{old}_12", new, [old]), f"{new}_12")
        self.assertEqual(migrate(f"{old}_02", new, [old]), new)
        self.assertEqual(migrate("switch.user_choice", new, [old]), new)

    def test_local_topics_and_payload_parsers(self) -> None:
        self.assertEqual(
            LOCAL_IO.local_io_command_topic("home/panel", "relay_1"),
            "home/panel/cmnd/io/relay_1",
        )
        self.assertEqual(
            LOCAL_IO.local_io_state_topic("home/panel", "relay_1"),
            "home/panel/stat/io/relay_1",
        )
        self.assertIs(LOCAL_IO.parse_on_off_payload('{"state":"ON"}'), True)
        self.assertEqual(
            LOCAL_IO.parse_temperature_payload('{"temperature":21.25}'), 21.25
        )


if __name__ == "__main__":
    unittest.main()
