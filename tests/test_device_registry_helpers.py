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


class _ScopedRegistry:
    """Home Assistant 2026.8+ registry with config-entry-scoped lookups."""

    def __init__(self, device: object | None) -> None:
        self.device = device
        self.calls: list[tuple[tuple[str, str], str]] = []

    def async_get_device_by_identifier(
        self, identifier: tuple[str, str], config_entry_id: str
    ) -> object | None:
        self.calls.append((identifier, config_entry_id))
        return self.device

    def async_get_device(self, **_kwargs: object) -> object | None:
        raise AssertionError("deprecated unscoped lookup must not be used")


class _LegacyRegistry:
    """Pre-2026.8 registry that only provides the unscoped lookup."""

    def __init__(self, device: object | None) -> None:
        self.device = device
        self.calls: list[dict[str, object]] = []

    def async_get_device(self, **kwargs: object) -> object | None:
        self.calls.append(kwargs)
        return self.device


class FindEntryDeviceTest(unittest.TestCase):
    def test_scoped_lookup_is_preferred(self) -> None:
        device = object()
        registry = _ScopedRegistry(device)

        result = HELPERS.find_entry_device(
            registry, ("tab5_lvgl", "guition_jc8012p4a1_v2"), "entry-1"
        )

        self.assertIs(result, device)
        self.assertEqual(
            registry.calls, [(("tab5_lvgl", "guition_jc8012p4a1_v2"), "entry-1")]
        )

    def test_scoped_lookup_reports_missing_device(self) -> None:
        registry = _ScopedRegistry(None)

        self.assertIsNone(
            HELPERS.find_entry_device(registry, ("tab5_lvgl", "tab5"), "entry-1")
        )

    def test_legacy_registry_uses_unscoped_lookup(self) -> None:
        device = object()
        registry = _LegacyRegistry(device)

        result = HELPERS.find_entry_device(
            registry, ("tab5_lvgl", "waveshare_8"), "entry-2"
        )

        self.assertIs(result, device)
        self.assertEqual(
            registry.calls, [{"identifiers": {("tab5_lvgl", "waveshare_8")}}]
        )


if __name__ == "__main__":
    unittest.main()
