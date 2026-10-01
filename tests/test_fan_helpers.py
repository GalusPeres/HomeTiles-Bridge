"""Dependency-free tests for the Fan tile protocol."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


def _load():
  path = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl" / "fan_helpers.py"
  spec = importlib.util.spec_from_file_location("_hometiles_fan_helpers", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


FAN = _load()
ALL = 1 | 2 | 4 | 8 | 16 | 32


class FanHelpersTest(unittest.TestCase):
  def test_feature_bits_match_home_assistant(self) -> None:
    self.assertEqual(
      (FAN.FAN_SET_SPEED, FAN.FAN_OSCILLATE, FAN.FAN_DIRECTION, FAN.FAN_PRESET_MODE,
       FAN.FAN_TURN_OFF, FAN.FAN_TURN_ON),
      (1, 2, 4, 8, 16, 32),
    )

  def test_detail_keeps_missing_values_null(self) -> None:
    self.assertEqual(FAN.build_fan_detail(None, {}), {
      "state": None, "supported_features": 0, "percentage": None, "percentage_step": None,
      "preset_mode": None, "preset_modes": [], "oscillating": None, "direction": None,
    })

  def test_detail_values(self) -> None:
    detail = FAN.build_fan_detail("on", {
      "supported_features": ALL, "percentage": 66.6, "percentage_step": 100 / 3,
      "preset_mode": "auto", "preset_modes": ["auto", "sleep", "auto", "", 5, "x" * 40],
      "oscillating": True, "direction": "reverse",
    })
    self.assertEqual(detail["percentage"], 67)
    self.assertEqual(detail["percentage_step"], 33.33)
    self.assertEqual(detail["preset_modes"], ["auto", "sleep"])
    self.assertEqual((detail["oscillating"], detail["direction"]), (True, "reverse"))
    self.assertEqual(FAN.build_fan_detail("on", {"percentage": 250})["percentage"], 100)
    self.assertIsNone(FAN.build_fan_detail("on", {"percentage": True})["percentage"])
    self.assertIsNone(FAN.build_fan_detail("on", {"direction": "up"})["direction"])

  def test_preset_list_is_bounded(self) -> None:
    detail = FAN.build_fan_detail("on", {"preset_modes": [f"p{i}" for i in range(40)]})
    self.assertEqual(len(detail["preset_modes"]), 16)

  def call(self, features, **command):
    return FAN.build_fan_service_call(command, "on", {
      "supported_features": features, "preset_modes": ["auto", "sleep"],
    })

  def test_service_calls_need_their_feature(self) -> None:
    self.assertEqual(self.call(ALL, action="turn_on"), ("turn_on", {}))
    self.assertEqual(self.call(ALL, action="turn_off"), ("turn_off", {}))
    self.assertEqual(self.call(ALL, action="toggle"), ("toggle", {}))
    self.assertEqual(self.call(ALL, action="set_percentage", percentage=140), ("set_percentage", {"percentage": 100}))
    self.assertEqual(self.call(ALL, action="set_percentage", percentage=-3), ("set_percentage", {"percentage": 0}))
    self.assertEqual(self.call(ALL, action="set_preset_mode", preset_mode="sleep"), ("set_preset_mode", {"preset_mode": "sleep"}))
    self.assertEqual(self.call(ALL, action="oscillate", oscillating=False), ("oscillate", {"oscillating": False}))
    self.assertEqual(self.call(ALL, action="set_direction", direction="reverse"), ("set_direction", {"direction": "reverse"}))
    self.assertIsNone(self.call(ALL & ~32, action="turn_on"))
    self.assertIsNone(self.call(32, action="toggle"))
    self.assertIsNone(self.call(ALL & ~1, action="set_percentage", percentage=50))
    self.assertIsNone(self.call(ALL & ~8, action="set_preset_mode", preset_mode="auto"))
    self.assertIsNone(self.call(ALL & ~2, action="oscillate", oscillating=True))
    self.assertIsNone(self.call(ALL & ~4, action="set_direction", direction="forward"))

  def test_values_are_validated(self) -> None:
    self.assertIsNone(self.call(ALL, action="set_percentage", percentage="50"))
    self.assertIsNone(self.call(ALL, action="set_percentage", percentage=True))
    self.assertIsNone(self.call(ALL, action="set_preset_mode", preset_mode="turbo"))
    self.assertIsNone(self.call(ALL, action="oscillate", oscillating="yes"))
    self.assertIsNone(self.call(ALL, action="set_direction", direction="left"))
    self.assertIsNone(self.call(ALL, action="delete"))

  def test_unavailable_fan_gets_no_call(self) -> None:
    for state in (None, "unavailable"):
      self.assertIsNone(FAN.build_fan_service_call({"action": "turn_on"}, state, {"supported_features": ALL}))

  def test_parse(self) -> None:
    self.assertEqual(FAN.parse_fan_command(json.dumps({"action": "turn_on"})), {"action": "turn_on"})
    for payload in ("", "[]", "{", json.dumps({"action": 1}), "x" * 600, None):
      self.assertIsNone(FAN.parse_fan_command(payload))


if __name__ == "__main__":
  unittest.main()
