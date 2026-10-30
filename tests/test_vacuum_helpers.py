"""Dependency-free tests for the Vacuum tile protocol."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


def _load():
  path = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl" / "vacuum_helpers.py"
  spec = importlib.util.spec_from_file_location("_hometiles_vacuum_helpers", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


VAC = _load()
ALL = 4 | 8 | 16 | 32 | 512 | 1024 | 4096 | 8192 | 16384
SPEEDS = ["Quiet", "Balanced", "Turbo", "Max"]


class VacuumHelpersTest(unittest.TestCase):
  def test_feature_bits_match_home_assistant(self) -> None:
    self.assertEqual(
      (VAC.VACUUM_PAUSE, VAC.VACUUM_STOP, VAC.VACUUM_RETURN_HOME, VAC.VACUUM_FAN_SPEED,
       VAC.VACUUM_LOCATE, VAC.VACUUM_CLEAN_SPOT, VAC.VACUUM_START, VAC.VACUUM_CLEAN_AREA),
      (4, 8, 16, 32, 512, 1024, 8192, 16384),
    )

  def test_detail_keeps_missing_values_null(self) -> None:
    self.assertEqual(VAC.build_vacuum_detail(None, {}), {
      "state": None, "supported_features": 0, "fan_speed": None, "fan_speed_list": [],
      "battery": None, "areas": [], "map": None, "cleaned_area": None, "cleaning_time": None,
    })

  def test_detail_values_and_extras(self) -> None:
    areas = [{"id": "kitchen", "name": "Küche"}]
    detail = VAC.build_vacuum_detail("cleaning", {
      "supported_features": ALL, "fan_speed": "Turbo", "fan_speed_list": SPEEDS + ["Turbo", 7, ""],
      "battery_level": 12,
    }, {
      "battery": 84, "areas": areas, "map": "image.robo_map",
      "cleaned_area": {"value": 24.0, "unit": "m²"}, "cleaning_time": {"value": 18, "unit": "min"},
    })
    self.assertEqual(detail["fan_speed_list"], SPEEDS)
    self.assertEqual(detail["battery"], 84)
    self.assertEqual(detail["areas"], areas)
    self.assertEqual(detail["map"], "image.robo_map")
    self.assertEqual(detail["cleaned_area"], {"value": 24.0, "unit": "m²"})
    json.dumps(detail, ensure_ascii=False)

  def test_battery_falls_back_to_the_attribute(self) -> None:
    detail = VAC.build_vacuum_detail("docked", {"battery_level": 99.6})
    self.assertEqual(detail["battery"], 100)
    self.assertIsNone(VAC.build_vacuum_detail("docked", {"battery_level": "n/a"})["battery"])

  def test_lists_only_with_their_feature(self) -> None:
    detail = VAC.build_vacuum_detail("docked", {"supported_features": 8192, "fan_speed_list": SPEEDS},
                                     {"areas": [{"id": "a", "name": "A"}]})
    self.assertEqual(detail["fan_speed_list"], [])
    self.assertEqual(detail["areas"], [])

  def test_map_must_be_a_picture_entity(self) -> None:
    self.assertIsNone(VAC.build_vacuum_detail("idle", {}, {"map": "sensor.robo_map"})["map"])
    self.assertEqual(VAC.build_vacuum_detail("idle", {}, {"map": "camera.robo_map"})["map"], "camera.robo_map")

  def test_area_choices_follow_the_mapping(self) -> None:
    mapping = {"kitchen": ["1"], "hall": ["2", "3"], "garage": [], 5: ["4"], "bath": ["6"]}
    self.assertEqual(VAC.area_choices(mapping, {"kitchen": "Küche", "hall": "Flur"}), [
      {"id": "kitchen", "name": "Küche"}, {"id": "hall", "name": "Flur"}, {"id": "bath", "name": "bath"},
    ])
    self.assertEqual(VAC.area_choices(None, {}), [])
    many = {f"room_{i}": [str(i)] for i in range(30)}
    self.assertEqual(len(VAC.area_choices(many, {})), VAC.MAX_AREAS)

  def test_companions_from_the_device(self) -> None:
    rows = [
      {"entity_id": "sensor.robo_battery", "device_class": "battery", "unit": "%"},
      {"entity_id": "sensor.robo_total_cleaning_area", "unit": "m²", "state_class": "total_increasing"},
      {"entity_id": "sensor.robo_cleaning_area", "unit": "m²", "state_class": "measurement"},
      {"entity_id": "sensor.robo_main_brush_time_left", "device_class": "duration", "unit": "h"},
      {"entity_id": "sensor.robo_total_cleaning_time", "device_class": "duration", "unit": "min"},
      {"entity_id": "sensor.robo_cleaning_time", "device_class": "duration", "unit": "s"},
      {"entity_id": "camera.robo_map", "device_class": None},
      {"entity_id": "image.robo_floor_1", "device_class": None},
      {"entity_id": "image.robo_map_0", "device_class": None},
    ]
    self.assertEqual(VAC.pick_companions(rows), {
      "battery": "sensor.robo_battery", "cleaned_area": "sensor.robo_cleaning_area",
      "cleaning_time": "sensor.robo_cleaning_time", "map": "image.robo_map_0",
    })
    self.assertEqual(VAC.pick_companions([]), {"battery": None, "cleaned_area": None, "cleaning_time": None, "map": None})
    self.assertEqual(VAC.pick_companions([{"entity_id": "camera.valetudo_map"}])["map"], "camera.valetudo_map")

  def test_measures(self) -> None:
    self.assertEqual(VAC.area_measure("24.04", "m²"), {"value": 24.0, "unit": "m²"})
    self.assertEqual(VAC.area_measure("12", "ft2"), {"value": 12.0, "unit": "ft²"})
    self.assertIsNone(VAC.area_measure("unknown", "m²"))
    self.assertIsNone(VAC.area_measure("3", "km"))
    self.assertEqual(VAC.time_measure("1090", "s"), {"value": 18, "unit": "min"})
    self.assertEqual(VAC.time_measure("1.5", "h"), {"value": 90, "unit": "min"})
    self.assertIsNone(VAC.time_measure("unavailable", "min"))

  def test_command_parsing(self) -> None:
    self.assertEqual(VAC.parse_vacuum_command('{"action":"start","entity_id":"vacuum.robo"}')["action"], "start")
    for bad in (None, "", "[]", "{", '{"action":3}', "x" * 2000):
      self.assertIsNone(VAC.parse_vacuum_command(bad))

  def test_service_calls_need_their_feature(self) -> None:
    attrs = {"supported_features": ALL, "fan_speed_list": SPEEDS}
    for action, service in (("start", "start"), ("pause", "pause"), ("stop", "stop"),
                            ("return_to_base", "return_to_base"), ("locate", "locate"),
                            ("clean_spot", "clean_spot")):
      self.assertEqual(VAC.build_vacuum_service_call({"action": action}, "docked", attrs), (service, {}))
      self.assertIsNone(VAC.build_vacuum_service_call({"action": action}, "docked", {"supported_features": 0}))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "start"}, "unavailable", attrs))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "start"}, None, attrs))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "send_command"}, "docked", attrs))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "turn_on"}, "docked", attrs))

  def test_fan_speed_from_the_list_only(self) -> None:
    attrs = {"supported_features": ALL, "fan_speed_list": SPEEDS}
    self.assertEqual(VAC.build_vacuum_service_call({"action": "set_fan_speed", "fan_speed": "Max"}, "idle", attrs),
                     ("set_fan_speed", {"fan_speed": "Max"}))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "set_fan_speed", "fan_speed": "Boost"}, "idle", attrs))
    self.assertIsNone(VAC.build_vacuum_service_call({"action": "set_fan_speed", "fan_speed": 3}, "idle", attrs))

  def test_clean_area_from_the_mapping_only(self) -> None:
    attrs = {"supported_features": ALL}
    allowed = ["kitchen", "hall"]
    self.assertEqual(
      VAC.build_vacuum_service_call({"action": "clean_area", "areas": ["hall", "kitchen", "hall"]}, "docked", attrs, allowed),
      ("clean_area", {"cleaning_area_id": ["hall", "kitchen"]}),
    )
    for areas in ([], ["garage"], ["hall", 4], "hall", None, ["a"] * 17):
      self.assertIsNone(VAC.build_vacuum_service_call({"action": "clean_area", "areas": areas}, "docked", attrs, allowed))
    self.assertIsNone(VAC.build_vacuum_service_call(
      {"action": "clean_area", "areas": ["hall"]}, "docked", {"supported_features": 8192}, allowed))


if __name__ == "__main__":
  unittest.main()
