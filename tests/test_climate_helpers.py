"""Dependency-free tests for the HomeTiles climate protocol contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


def _load_climate_helpers_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tab5_lvgl"
        / "climate_helpers.py"
    )
    spec = importlib.util.spec_from_file_location("_hometiles_climate_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CLIMATE = _load_climate_helpers_module()


class ClimateHelpersTest(unittest.TestCase):
    def test_feature_values_match_home_assistant_climate_entity_feature(self) -> None:
        self.assertEqual(
            (
                CLIMATE.CLIMATE_TARGET_TEMPERATURE,
                CLIMATE.CLIMATE_TARGET_TEMPERATURE_RANGE,
                CLIMATE.CLIMATE_TARGET_HUMIDITY,
                CLIMATE.CLIMATE_FAN_MODE,
                CLIMATE.CLIMATE_PRESET_MODE,
                CLIMATE.CLIMATE_SWING_MODE,
                CLIMATE.CLIMATE_TURN_OFF,
                CLIMATE.CLIMATE_TURN_ON,
                CLIMATE.CLIMATE_SWING_HORIZONTAL_MODE,
            ),
            (1, 2, 4, 8, 16, 32, 128, 256, 512),
        )

    def test_state_payload_preserves_availability_and_feature_mask(self) -> None:
        payload = CLIMATE.build_climate_state_payload(
            "heat",
            {
                "supported_features": 553,
                "temperature": 21.5,
                "humidity": 55,
                "target_humidity_step": 5,
                "fan_modes": ["auto", "high"],
                "swing_horizontal_mode": "center",
                "swing_horizontal_modes": ["left", "center", "right"],
            },
            "\N{DEGREE SIGN}C",
        )
        self.assertEqual(payload["state"], "heat")
        self.assertEqual(payload["hvac_mode"], "heat")
        self.assertIs(payload["available"], True)
        self.assertEqual(payload["supported_features"], 553)
        self.assertEqual(payload["temperature"], 21.5)
        self.assertEqual(payload["humidity"], 55)
        self.assertEqual(payload["target_humidity_step"], 5)
        self.assertEqual(payload["swing_horizontal_mode"], "center")
        self.assertEqual(payload["temperature_unit"], "\N{DEGREE SIGN}C")

    def test_unavailable_state_is_explicit(self) -> None:
        payload = CLIMATE.build_climate_state_payload(
            "unavailable", {}, "\N{DEGREE SIGN}C"
        )
        self.assertIs(payload["available"], False)
        self.assertNotIn("supported_features", payload)

    def test_explicit_empty_feature_mask_is_preserved(self) -> None:
        payload = CLIMATE.build_climate_state_payload(
            "off", {"supported_features": 0}
        )
        self.assertEqual(payload["supported_features"], 0)

    def test_validates_temperature_and_range_features(self) -> None:
        service, data = CLIMATE.build_climate_service_call(
            {"command": "set_temperature", "temperature": 22.5},
            {"supported_features": 1, "min_temp": 7, "max_temp": 35},
        )
        self.assertEqual((service, data), ("set_temperature", {"temperature": 22.5}))

        service, data = CLIMATE.build_climate_service_call(
            {"target_temp_low": 18, "target_temp_high": 24},
            {"supported_features": 2, "min_temp": 7, "max_temp": 35},
        )
        self.assertEqual(
            (service, data),
            ("set_temperature", {"target_temp_low": 18.0, "target_temp_high": 24.0}),
        )

        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"temperature": 22}, {"supported_features": 2}
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"target_temp_low": 25, "target_temp_high": 20},
                {"supported_features": 2},
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"temperature": 22, "target_temp_low": 18, "target_temp_high": 24},
                {"supported_features": 3},
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"target_temp_low": 18}, {"supported_features": 2}
            )

    def test_set_temperature_preserves_optional_hvac_mode(self) -> None:
        service, data = CLIMATE.build_climate_service_call(
            {
                "command": "set_temperature",
                "temperature": 22,
                "hvac_mode": "heat",
            },
            {
                "supported_features": 1,
                "hvac_modes": ["off", "heat"],
                "min_temp": 7,
                "max_temp": 35,
            },
        )
        self.assertEqual(
            (service, data),
            ("set_temperature", {"temperature": 22.0, "hvac_mode": "heat"}),
        )

        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {
                    "command": "set_temperature",
                    "temperature": 22,
                    "hvac_mode": "cool",
                },
                {"supported_features": 1, "hvac_modes": ["off", "heat"]},
            )

    def test_validates_option_features_and_membership(self) -> None:
        cases = (
            ("fan_mode", "high", "fan_modes", ["auto", "high"], 8),
            ("preset_mode", "eco", "preset_modes", ["none", "eco"], 16),
            ("swing_mode", "both", "swing_modes", ["off", "both"], 32),
            (
                "swing_horizontal_mode",
                "left",
                "swing_horizontal_modes",
                ["off", "left"],
                512,
            ),
        )
        for value_key, value, options_key, options, feature in cases:
            with self.subTest(value_key=value_key):
                service, data = CLIMATE.build_climate_service_call(
                    {value_key: value},
                    {"supported_features": feature, options_key: options},
                )
                self.assertEqual(service, f"set_{value_key}")
                self.assertEqual(data, {value_key: value})

        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"fan_mode": "turbo"},
                {"supported_features": 8, "fan_modes": ["auto", "high"]},
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"swing_horizontal_mode": "left"},
                {"supported_features": 32, "swing_horizontal_modes": ["left"]},
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"fan_mode": "high"}, {"supported_features": 8}
            )

    def test_validates_hvac_and_power_service_contract(self) -> None:
        service, data = CLIMATE.build_climate_service_call(
            {"command": "set_hvac_mode", "hvac_mode": "heat"},
            {"hvac_modes": ["off", "heat"]},
        )
        self.assertEqual((service, data), ("set_hvac_mode", {"hvac_mode": "heat"}))

        for command, features in (
            ("turn_off", 128),
            ("turn_on", 256),
            ("toggle", 128),
            ("toggle", 256),
        ):
            with self.subTest(command=command, features=features):
                self.assertEqual(
                    CLIMATE.build_climate_service_call(
                        {"command": command}, {"supported_features": features}
                    ),
                    (command, {}),
                )

        for command, features in (("turn_off", 256), ("turn_on", 128), ("toggle", 0)):
            with self.subTest(command=command, features=features):
                with self.assertRaises(ValueError):
                    CLIMATE.build_climate_service_call(
                        {"command": command}, {"supported_features": features}
                    )

    def test_humidity_is_feature_gated_bounded_and_integral(self) -> None:
        self.assertEqual(
            CLIMATE.build_climate_service_call(
                {"command": "set_humidity", "humidity": 55.0},
                {"supported_features": 4, "min_humidity": 30, "max_humidity": 99},
            ),
            ("set_humidity", {"humidity": 55}),
        )
        for humidity, features in ((29, 4), (100, 4), (55.5, 4), (55, 0)):
            with self.subTest(humidity=humidity, features=features):
                with self.assertRaises(ValueError):
                    CLIMATE.build_climate_service_call(
                        {"command": "set_humidity", "humidity": humidity},
                        {
                            "supported_features": features,
                            "min_humidity": 30,
                            "max_humidity": 99,
                        },
                    )

    def test_rejects_unknown_or_ambiguous_actions(self) -> None:
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"command": "run_arbitrary_service", "temperature": 22},
                {"supported_features": 1},
            )
        with self.assertRaises(ValueError):
            CLIMATE.build_climate_service_call(
                {"temperature": 22, "humidity": 50},
                {"supported_features": 5},
            )

    def test_legacy_payload_without_feature_mask_remains_compatible(self) -> None:
        service, data = CLIMATE.build_climate_service_call(
            {"humidity": 50}, {"min_humidity": 30, "max_humidity": 99}
        )
        self.assertEqual((service, data), ("set_humidity", {"humidity": 50}))


if __name__ == "__main__":
    unittest.main()
