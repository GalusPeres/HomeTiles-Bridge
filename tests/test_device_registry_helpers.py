"""Focused regressions for HomeTiles device-registry removal policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tab5_lvgl"
    / "device_registry_helpers.py"
)
SPEC = importlib.util.spec_from_file_location("device_registry_helpers", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
HELPERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPERS)


class DeviceRegistryHelpersTest(unittest.TestCase):
    def test_provisional_duplicate_is_removable(self) -> None:
        self.assertTrue(
            HELPERS.is_stale_device_entry(
                {("tab5_lvgl", "provisional-config-entry-id")},
                ("tab5_lvgl", "guition_jc1060p470c"),
            )
        )

    def test_active_device_is_not_removable(self) -> None:
        self.assertFalse(
            HELPERS.is_stale_device_entry(
                {("tab5_lvgl", "guition_jc1060p470c")},
                ("tab5_lvgl", "guition_jc1060p470c"),
            )
        )

    def test_active_device_with_legacy_alias_is_not_removable(self) -> None:
        self.assertFalse(
            HELPERS.is_stale_device_entry(
                {
                    ("tab5_lvgl", "provisional-config-entry-id"),
                    ("tab5_lvgl", "guition_jc1060p470c"),
                },
                ("tab5_lvgl", "guition_jc1060p470c"),
            )
        )


if __name__ == "__main__":
    unittest.main()
