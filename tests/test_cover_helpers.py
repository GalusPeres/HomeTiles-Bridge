"""Dependency-free tests for the HomeTiles cover protocol contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


def _load_cover_helpers_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tab5_lvgl"
        / "cover_helpers.py"
    )
    spec = importlib.util.spec_from_file_location("_hometiles_cover_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COVERS = _load_cover_helpers_module()


class CoverHelpersTest(unittest.TestCase):
    def test_cover_icons_match_home_assistant_component_icons(self) -> None:
        expected = {
            ("blind", "open"): "mdi:blinds-horizontal",
            ("blind", "closed"): "mdi:blinds-horizontal-closed",
            ("curtain", "closing"): "mdi:arrow-collapse-horizontal",
            ("curtain", "opening"): "mdi:arrow-split-vertical",
            ("damper", "closed"): "mdi:circle-slice-8",
            ("garage", "opening"): "mdi:arrow-up-box",
            ("gate", "closing"): "mdi:arrow-right",
            ("shade", "closed"): "mdi:roller-shade-closed",
            ("shutter", "open"): "mdi:window-shutter-open",
            ("window", "closed"): "mdi:window-closed",
            ("awning", "open"): "mdi:window-open",
            (None, "closing"): "mdi:arrow-down-box",
        }
        for (device_class, state), icon in expected.items():
            with self.subTest(device_class=device_class, state=state):
                self.assertEqual(
                    COVERS.cover_component_icon(device_class, state), icon
                )

    def test_normalises_all_supported_cover_commands(self) -> None:
        expected = {
            "open": "open_cover",
            "cover.close_cover": "close_cover",
            "stop": "stop_cover",
            "set-position": "set_cover_position",
            "open tilt": "open_cover_tilt",
            "close_cover_tilt": "close_cover_tilt",
            "stop_tilt": "stop_cover_tilt",
            "set_tilt_position": "set_cover_tilt_position",
            "toggle_cover": "toggle",
            "toggle_tilt": "toggle_cover_tilt",
        }
        for command, service in expected.items():
            with self.subTest(command=command):
                self.assertEqual(COVERS.normalise_cover_command(command), service)

    def test_cover_commands_require_their_advertised_features(self) -> None:
        expected = {
            "open_cover": 1,
            "close_cover": 2,
            "set_cover_position": 4,
            "stop_cover": 8,
            "open_cover_tilt": 16,
            "close_cover_tilt": 32,
            "stop_cover_tilt": 64,
            "set_cover_tilt_position": 128,
            "toggle": 3,
            "toggle_cover_tilt": 48,
        }
        self.assertEqual(COVERS.COVER_COMMAND_FEATURES, expected)
        for command, required in expected.items():
            with self.subTest(command=command):
                self.assertTrue(COVERS.cover_command_supported(command, required))
                self.assertTrue(COVERS.cover_command_supported(command, 255))
                self.assertFalse(
                    COVERS.cover_command_supported(command, required & (required - 1))
                )

        self.assertFalse(COVERS.cover_command_supported("unknown", 255))
        for invalid_mask in (None, True, -1, 1.5, float("inf"), "invalid"):
            with self.subTest(invalid_mask=invalid_mask):
                self.assertFalse(
                    COVERS.cover_command_supported("open_cover", invalid_mask)
                )

    def test_command_positions_are_strict_integer_percentages(self) -> None:
        for value, expected in ((0, 0), (100, 100), ("25", 25), (25.0, 25)):
            with self.subTest(value=value):
                self.assertEqual(COVERS.parse_cover_position(value), expected)

        for value in (None, True, -1, 101, 25.5, "bad", float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(COVERS.parse_cover_position(value))

    def test_state_payload_preserves_cover_and_tilt_capabilities(self) -> None:
        payload = COVERS.build_cover_state_payload(
            "opening",
            {
                "current_position": 42,
                "current_tilt_position": "75",
                "supported_features": 255,
                "device_class": "blind",
                "assumed_state": True,
            },
        )

        self.assertEqual(
            payload,
            {
                "state": "opening",
                "available": True,
                "current_position": 42,
                "current_tilt_position": 75,
                "supported_features": 255,
                "device_class": "blind",
                "assumed_state": True,
            },
        )

    def test_unavailable_state_uses_nullable_feedback_values(self) -> None:
        payload = COVERS.build_cover_state_payload(
            "unavailable",
            {
                "current_position": "not-a-position",
                "current_tilt_position": 120,
                "supported_features": None,
            },
        )

        self.assertEqual(payload["state"], "unavailable")
        self.assertIs(payload["available"], False)
        self.assertIsNone(payload["current_position"])
        self.assertIsNone(payload["current_tilt_position"])
        self.assertEqual(payload["supported_features"], 0)
        self.assertIsNone(payload["device_class"])
        self.assertIs(payload["assumed_state"], False)

    def test_unknown_state_is_still_available(self) -> None:
        payload = COVERS.build_cover_state_payload(
            "unknown", {"supported_features": 3}
        )
        self.assertIs(payload["available"], True)
        self.assertEqual(payload["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
