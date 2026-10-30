"""Executable Bridge wiring for the Vacuum tile."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import types
import unittest

from test_announcement_guard import extract
from test_view_navigation import ROOT, load_module

VAC = load_module("vacuum_helpers")
CONTROL = load_module("control_helpers")
LOGGER_NAME = "test_vacuum_wiring"
METHODS = {
  "_detail_entities", "_detail_payload", "_async_publish_detail_state", "_async_handle_vacuum_command",
  "_resolve_target_entity", "_secure_log_due", "_owns_state_publish", "_ha_topic_for_entity",
  "_vacuum_areas", "_vacuum_extras", "_refresh_vacuum_companions", "_state_watch_entities",
}
FEATURES = 4 | 8 | 16 | 32 | 512 | 1024 | 4096 | 8192 | 16384


def _state(value, **attributes):
  return types.SimpleNamespace(state=value, attributes=attributes, name=None)


def _entry(entity_id, device_id=None, options=None, **extra):
  return types.SimpleNamespace(entity_id=entity_id, device_id=device_id, options=options or {},
                               device_class=extra.get("device_class"), original_device_class=None,
                               unit_of_measurement=extra.get("unit"), disabled_by=None)


class VacuumWiringTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.publishes: list[tuple[str, dict, bool]] = []
    self.calls: list[tuple] = []
    self.rechecks = 0
    self.states = {
      "vacuum.robo": _state("docked", supported_features=FEATURES, fan_speed="Turbo",
                            fan_speed_list=["Quiet", "Turbo", "Max"]),
      "sensor.robo_battery": _state("84", device_class="battery", unit_of_measurement="%"),
      "sensor.robo_cleaning_area": _state("24", unit_of_measurement="m²", state_class="measurement"),
      "sensor.robo_cleaning_time": _state("1080", device_class="duration", unit_of_measurement="s"),
      "image.robo_map": _state("2026-10-30T10:00:00"),
      "vacuum.old": _state("idle", supported_features=8192 | 4096, battery_level=40),
    }
    mapping = {"kitchen": ["1"], "hall": ["2"], "garage": []}
    self.entries = {
      "vacuum.robo": _entry("vacuum.robo", "dev1", {"vacuum": {"area_mapping": mapping}}),
      "sensor.robo_battery": _entry("sensor.robo_battery", "dev1"),
      "sensor.robo_cleaning_area": _entry("sensor.robo_cleaning_area", "dev1"),
      "sensor.robo_cleaning_time": _entry("sensor.robo_cleaning_time", "dev1"),
      "image.robo_map": _entry("image.robo_map", "dev1"),
      "vacuum.old": _entry("vacuum.old"),
    }
    registry = types.SimpleNamespace(async_get=lambda entity_id: self.entries.get(entity_id))

    def entries_for_device(_registry, device_id):
      return [entry for entry in self.entries.values() if entry.device_id == device_id]

    areas = {"kitchen": "Küche", "hall": "Flur"}
    area_registry = types.SimpleNamespace(
      async_get_area=lambda area_id: types.SimpleNamespace(name=areas[area_id]) if area_id in areas else None)

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.publishes.append((topic, json.loads(payload), retain))

    async def call(domain, service, data, blocking=False):
      self.calls.append((domain, service, dict(data), blocking))

    logger = logging.getLogger(LOGGER_NAME)
    scope: dict = {key: value for key, value in vars(VAC).items() if not key.startswith("__")}
    scope.update({
      "callback": lambda function: function,
      "DOMAIN": "tab5_lvgl",
      "DATA_IMAGES": "images",
      "FAN_DOMAIN": "fan",
      "json": json,
      "entity_domain": CONTROL.entity_domain,
      "resolve_control_entity": CONTROL.resolve_control_entity,
      "_unique_entities": lambda items: list(dict.fromkeys(item for item in items if item)),
      "mqtt": types.SimpleNamespace(async_publish=publish),
      "er": types.SimpleNamespace(async_get=lambda hass: registry, async_entries_for_device=entries_for_device),
      "ar": types.SimpleNamespace(async_get=lambda hass: area_registry),
      "monotonic": lambda: 0.0,
      "_LOGGER": logger,
    })
    extract(METHODS, scope)
    bridge_class = type("Bridge", (), {name: scope[name] for name in METHODS})
    images = types.SimpleNamespace(recheck=self._recheck)
    self.hass = types.SimpleNamespace(
      data={"tab5_lvgl": {"images": images}},
      states=types.SimpleNamespace(get=lambda entity_id: self.states.get(entity_id)),
      services=types.SimpleNamespace(async_call=call),
    )
    bridge = bridge_class()
    bridge.hass = self.hass
    bridge.entry = types.SimpleNamespace(entry_id="entry1", data={}, options={})
    bridge.base_topic = "hometiles"
    bridge.ha_prefix = "ha"
    bridge.locks = bridge.alarm_panels = bridge.fans = []
    bridge.vacuums = ["vacuum.robo", "vacuum.old"]
    bridge.tracked_entities = ["vacuum.robo", "vacuum.old"]
    bridge.vacuum_maps = []
    bridge._vacuum_companions = {}
    bridge._vacuum_of_companion = {}
    bridge._runtime_setup_complete = True
    bridge._secure_log_times = {}
    self.bridge = bridge

  def _recheck(self) -> None:
    self.rechecks += 1

  async def test_companions_are_found_on_the_device_and_watched(self) -> None:
    self.bridge._refresh_vacuum_companions()
    self.assertEqual(self.bridge.vacuum_maps, ["image.robo_map"])
    self.assertEqual(self.rechecks, 1)
    self.assertEqual(set(self.bridge._vacuum_of_companion), {
      "sensor.robo_battery", "sensor.robo_cleaning_area", "sensor.robo_cleaning_time"})
    self.assertIn("sensor.robo_battery", self.bridge._state_watch_entities())
    self.bridge._refresh_vacuum_companions()
    self.assertEqual(self.rechecks, 1)

  async def test_detail_state_carries_areas_values_and_map(self) -> None:
    self.bridge._refresh_vacuum_companions()
    await self.bridge._async_publish_detail_state("vacuum.robo", self.states["vacuum.robo"])
    topic, detail, retain = self.publishes[-1]
    self.assertEqual(topic, "ha/vacuum/robo/detail")
    self.assertTrue(retain)
    self.assertEqual(detail["areas"], [{"id": "kitchen", "name": "Küche"}, {"id": "hall", "name": "Flur"}])
    self.assertEqual((detail["battery"], detail["map"]), (84, "image.robo_map"))
    self.assertEqual(detail["cleaned_area"], {"value": 24.0, "unit": "m²"})
    self.assertEqual(detail["cleaning_time"], {"value": 18, "unit": "min"})
    self.assertEqual(detail["fan_speed_list"], ["Quiet", "Turbo", "Max"])
    await self.bridge._async_publish_detail_state("vacuum.old", self.states["vacuum.old"])
    detail = self.publishes[-1][1]
    self.assertEqual((detail["battery"], detail["areas"], detail["map"]), (40, [], None))
    await self.bridge._async_publish_detail_state("vacuum.gone", None)
    self.assertIsNone(self.publishes[-1][1]["state"])

  async def test_commands_are_validated_and_not_blocking(self) -> None:
    async def vacuum(payload, retain=False):
      message = types.SimpleNamespace(topic="hometiles/cmnd/vacuum", payload=json.dumps(payload), retain=retain)
      await self.bridge._async_handle_vacuum_command(message)

    await vacuum({"entity_id": "vacuum.robo", "action": "start"})
    await vacuum({"entity_id": "vacuum.robo", "action": "start"}, retain=True)
    await vacuum({"entity_id": "vacuum.robo", "action": "set_fan_speed", "fan_speed": "Max"})
    await vacuum({"entity_id": "vacuum.robo", "action": "set_fan_speed", "fan_speed": "Boost"})
    await vacuum({"entity_id": "vacuum.robo", "action": "clean_area", "areas": ["hall", "kitchen"]})
    await vacuum({"entity_id": "vacuum.robo", "action": "clean_area", "areas": ["garage"]})
    await vacuum({"entity_id": "vacuum.old", "action": "pause"})
    await vacuum({"entity_id": "vacuum.other", "action": "start"})
    await vacuum({"entity_id": "vacuum.robo", "action": "send_command", "command": "x"})
    self.assertEqual(self.calls, [
      ("vacuum", "start", {"entity_id": "vacuum.robo"}, False),
      ("vacuum", "set_fan_speed", {"entity_id": "vacuum.robo", "fan_speed": "Max"}, False),
      ("vacuum", "clean_area", {"entity_id": "vacuum.robo", "cleaning_area_id": ["hall", "kitchen"]}, False),
    ])


class SourceContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls) -> None:
    cls.source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    cls.tree = ast.parse(cls.source)

  def segment(self, name: str) -> str:
    node = next(node for node in ast.walk(self.tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    return ast.get_source_segment(self.source, node)

  def test_command_topic_handler_and_cleanup(self) -> None:
    self.assertIn('f"{self.base_topic}/cmnd/vacuum"', self.segment("_async_setup_plain_commands"))
    self.assertIn('"vacuum": self._async_handle_vacuum_command', self.segment("_command_handlers"))
    unload = self.segment("async_unload")
    for name in ("self._unsub_vacuum()", "self._unsub_vacuum_registry()", "self._vacuum_refresh_cancel()"):
      self.assertIn(name, unload)

  def test_config_payload_tracking_and_level(self) -> None:
    config = self.segment("async_publish_config_to_device")
    for key in ("CONF_VACUUMS", '"vacuum_meta"', '"entity_search": 6'):
      self.assertIn(key, config)
    refresh = self.segment("_refresh_runtime_entity_lists")
    self.assertIn("+ self.vacuums", refresh)
    self.assertIn("_refresh_vacuum_companions()", refresh)
    self.assertIn("vacuum_maps", self.source[self.source.index("def allowed(session"):][:600])

  def test_panel_list_and_options_form(self) -> None:
    self.assertEqual(CONTROL.panel_entity_list(["vacuum.robo"], "vacuums"), ["vacuum.robo"])
    with self.assertRaises(ValueError):
      CONTROL.panel_entity_list(["vacuum.robo", "fan.x"], "vacuums")
    flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    self.assertIn('vacuums = selected(CONF_VACUUMS, ("vacuum",))', flow)
    for filename in ["strings.json"] + [f"translations/{name}.json" for name in
                                        ("de", "en", "es", "fr", "it", "nl", "pl", "pt-BR", "ru", "sv")]:
      step = json.loads((ROOT / filename).read_text(encoding="utf-8"))["options"]["step"]["entities"]
      self.assertTrue(step["data"]["vacuums"].strip(), filename)


if __name__ == "__main__":
  unittest.main()
