"""Refresh, energy and weather requests stay bounded; the Bridge pushes meta."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import types
import unittest

from test_announcement_guard import Clock, extract
from test_view_navigation import ROOT, load_module

LIMITS = load_module("request_limits")
LOGGER = logging.getLogger("test_panel_request_limits")


def _message(payload="", retain=False):
  return types.SimpleNamespace(payload=payload, retain=retain)


class GatedRequestTest(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.clock = Clock()
    scope = {"_LOGGER": LOGGER, "monotonic": self.clock}
    extract({"_async_run_gated", "_async_on_refresh_request", "_async_on_energy_request",
             "_async_on_weather_request", "_secure_log_due"}, scope)
    self.handled = []
    self.release = asyncio.Event()

    def handler(kind, block=False):
      async def handle(msg):
        self.handled.append((kind, msg.payload))
        if block:
          await self.release.wait()
      return handle

    self.bridge = types.SimpleNamespace(
      _refresh_gate=LIMITS.RequestGate(self.clock, max_active=LIMITS.REFRESH_MAX_ACTIVE,
                                       max_per_window=LIMITS.REFRESH_MAX_PER_WINDOW,
                                       max_waiting=LIMITS.REFRESH_MAX_WAITING),
      _energy_gate=LIMITS.RequestGate(self.clock, max_active=LIMITS.ENERGY_MAX_ACTIVE,
                                      max_per_window=LIMITS.ENERGY_MAX_PER_WINDOW,
                                      max_waiting=LIMITS.ENERGY_MAX_WAITING),
      _weather_gate=LIMITS.RequestGate(self.clock, max_active=LIMITS.WEATHER_MAX_ACTIVE,
                                       max_per_window=LIMITS.WEATHER_MAX_PER_WINDOW,
                                       max_waiting=LIMITS.WEATHER_MAX_WAITING),
      _async_handle_request=handler("refresh", block=True),
      _async_handle_energy_request=handler("energy"),
      _async_handle_weather_request=handler("weather"),
      _secure_log_at={}, device_id="panel", base_topic="hometiles")
    self.bridge._secure_log_due = lambda reason, interval=60.0: scope["_secure_log_due"](
      self.bridge, reason, interval)
    self.bridge._async_run_gated = lambda gate, kind, handle, msg: scope["_async_run_gated"](
      self.bridge, gate, kind, handle, msg)
    self.on = {kind: (lambda msg, kind=kind: scope[f"_async_on_{kind}_request"](self.bridge, msg))
               for kind in ("refresh", "energy", "weather")}

  async def test_retained_requests_never_run(self):
    # A retained request would run again after every Home Assistant restart.
    for kind in ("refresh", "energy", "weather"):
      await self.on[kind](_message("force", retain=True))
    self.assertEqual(self.handled, [])

  async def test_a_refresh_flood_is_dropped_while_one_runs(self):
    first = asyncio.create_task(self.on["refresh"](_message("1")))
    await asyncio.sleep(0)
    second = asyncio.create_task(self.on["refresh"](_message("2")))
    await asyncio.sleep(0)
    with self.assertLogs(LOGGER, "WARNING"):
      await self.on["refresh"](_message("3"))
    # Further drops within a minute stay quiet.
    await self.on["refresh"](_message("4"))
    self.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    self.assertEqual(self.handled, [("refresh", "1"), ("refresh", "2")])
    self.assertEqual((self.bridge._refresh_gate.active, self.bridge._refresh_gate.waiting), (0, 0))

  async def test_energy_and_weather_have_a_budget_per_minute(self):
    self.release.set()
    for _ in range(LIMITS.ENERGY_MAX_PER_WINDOW):
      await self.on["energy"](_message('{"period":"day"}'))
    self.assertEqual(len(self.handled), LIMITS.ENERGY_MAX_PER_WINDOW)
    for _ in range(LIMITS.WEATHER_MAX_PER_WINDOW):
      await self.on["weather"](_message(""))
    self.assertEqual(len(self.handled), LIMITS.ENERGY_MAX_PER_WINDOW + LIMITS.WEATHER_MAX_PER_WINDOW)
    # The window ends by itself; a request after it runs again.
    self.clock.now += LIMITS.HISTORY_WINDOW_S
    await self.on["energy"](_message('{"period":"week"}'))
    self.assertEqual(self.handled[-1], ("energy", '{"period":"week"}'))


class EnergyWithoutDashboardTest(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.clock = Clock()
    self.published = []

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.published.append((topic, json.loads(payload), retain))

    self.manager = types.SimpleNamespace(data=None)

    async def get_manager(hass):
      return self.manager

    self.scope = {
      "_LOGGER": LOGGER, "monotonic": self.clock, "json": json,
      "mqtt": types.SimpleNamespace(async_publish=publish),
      "ENERGY_UNAVAILABLE_LOG_S": LIMITS.ENERGY_UNAVAILABLE_LOG_S,
      "async_get_energy_manager": get_manager, "statistics_during_period": object(),
    }
    extract({"_async_handle_energy_request", "_async_publish_energy_empty", "_try_parse_json",
             "_secure_log_due"}, self.scope)
    self.bridge = types.SimpleNamespace(
      hass=types.SimpleNamespace(data={}), energy_response_topic="hometiles/energy/response",
      _secure_log_at={})
    self.bridge._secure_log_due = lambda reason, interval=60.0: self.scope["_secure_log_due"](
      self.bridge, reason, interval)
    self.bridge._async_publish_energy_empty = lambda period: self.scope["_async_publish_energy_empty"](
      self.bridge, period)

  async def request(self, period):
    await self.scope["_async_handle_energy_request"](
      self.bridge, _message(json.dumps({"period": period})))

  async def test_every_request_gets_an_empty_answer_and_one_warning(self):
    # Without an answer the panel asked again every 15 s and each try logged.
    with self.assertLogs(LOGGER, "WARNING") as logs:
      await self.request("day")
      await self.request("week")
    self.assertEqual(len(logs.records), 1)
    self.assertEqual(self.published, [
      ("hometiles/energy/response", {"period": "day", "entries": []}, False),
      ("hometiles/energy/response", {"period": "week", "entries": []}, False),
    ])
    # An hour later the warning shows again.
    self.clock.now += LIMITS.ENERGY_UNAVAILABLE_LOG_S
    with self.assertLogs(LOGGER, "WARNING"):
      await self.request("month")
    self.assertEqual(self.published[-1][1], {"period": "month", "entries": []})

  async def test_an_energy_dashboard_without_statistics_also_answers(self):
    self.manager.data = {"energy_sources": [], "device_consumption": []}
    with self.assertLogs(LOGGER, "WARNING"):
      await self.request("day")
    self.assertEqual(self.published[-1][1], {"period": "day", "entries": []})

  async def test_missing_energy_component_answers(self):
    self.scope["async_get_energy_manager"] = None
    with self.assertLogs(LOGGER, "WARNING"):
      await self.request("day")
    self.assertEqual(self.published[-1][1], {"period": "day", "entries": []})


class MetaPushTest(unittest.TestCase):
  def setUp(self):
    self.scope = {"_is_weather_entity": lambda entity_id: entity_id.startswith("weather."),
                  "_extract_mdi_icon": lambda state, hass: "", "CONFIG_META_PUSH_DELAYS": (5.0,),
                  "callback": lambda function: function}
    extract({"_handle_state_event", "_config_meta_key"}, self.scope)
    self.refreshes = []
    self.bridge = types.SimpleNamespace(
      hass=types.SimpleNamespace(async_create_task=lambda coroutine: coroutine.close()),
      weathers=[], binary_sensors=[], switches=[], tracked_entities=["sensor.power"],
      _icon_cache={}, _config_meta_cache={},
      _schedule_icon_refresh=lambda: None,
      _schedule_config_refresh=lambda delays=None: self.refreshes.append(delays))

    async def publish(entity_id, state):
      return None

    self.bridge._async_publish_entity_state = publish

  def event(self, name, unit="W", entity_id="sensor.power", old=True):
    state = types.SimpleNamespace(name=name, attributes={"unit_of_measurement": unit})
    data = {"entity_id": entity_id, "new_state": state, "old_state": state if old else None}
    self.scope["_handle_state_event"](self.bridge, types.SimpleNamespace(data=data))

  def test_renames_and_new_units_republish_the_configuration_once(self):
    meta_key = self.scope["_config_meta_key"]
    # The configuration that was published already named the sensor.
    self.bridge._config_meta_cache["sensor.power"] = meta_key(
      types.SimpleNamespace(name="Power", attributes={"unit_of_measurement": "W"}))
    self.event("Power")
    self.event("Power")
    self.assertEqual(self.refreshes, [])
    self.event("Grid power")
    self.assertEqual(self.refreshes, [(5.0,)])
    self.event("Grid power", unit="kW")
    self.assertEqual(self.refreshes, [(5.0,), (5.0,)])
    self.event("Grid power", unit="kW")
    self.assertEqual(len(self.refreshes), 2)

  def test_an_entity_missing_from_the_last_configuration_republishes_it(self):
    self.event("Power", old=False)
    self.assertEqual(self.refreshes, [(5.0,)])

  def test_untracked_entities_are_ignored(self):
    self.event("Other", entity_id="sensor.other")
    self.assertEqual(self.refreshes, [])


class SourceContractTest(unittest.TestCase):
  def setUp(self):
    self.source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.tree = ast.parse(self.source)

  def function(self, name):
    node = next(node for node in ast.walk(self.tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    return ast.get_source_segment(self.source, node)

  def test_request_topics_are_subscribed_through_their_gates(self):
    setup = self.function("_async_setup_requests") if "_async_setup_requests" in self.source else self.source
    for gated in ("self._async_on_refresh_request", "self._async_on_energy_request",
                  "self._async_on_weather_request"):
      self.assertIn(gated, setup)
    for raw in ("        self._async_handle_request,\n", "        self._async_handle_energy_request,\n",
                "        self._async_handle_weather_request,\n"):
      self.assertNotIn(raw, self.source.replace("\r\n", "\n"))

  def test_configuration_announces_push_and_seeds_the_meta_cache(self):
    publish = self.function("async_publish_config_to_device")
    self.assertIn('"push": 1', publish)
    self.assertIn("self._config_meta_cache[entity_id] = _config_meta_key(state)", publish)


if __name__ == "__main__":
  unittest.main()
