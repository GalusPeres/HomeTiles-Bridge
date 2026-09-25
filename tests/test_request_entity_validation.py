"""Panel history and weather requests may only read configured entities."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import logging
import types
import unittest

from test_view_navigation import ROOT

HANDLERS = {"_async_handle_history_request", "_async_handle_weather_request"}
MODULE_FUNCTIONS = {"_try_parse_json", "_is_weather_entity", "_coerce_int"}


class ReachedHomeAssistant(Exception):
    """Raised by the fake state machine: the handler read Home Assistant data."""


def bridge_methods(scope):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HANDLERS
    ]
    for node in nodes:
        node.decorator_list = []
    nodes += [node for node in tree.body if getattr(node, "name", None) in MODULE_FUNCTIONS]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[],
    )
    namespace = dict(scope)
    exec(compile(ast.fix_missing_locations(module), "bridge-request-validation", "exec"), namespace)
    return namespace


class RequestEntityValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reads = []
        self.weather_publishes = []

        def get_state(entity_id):
            self.reads.append(entity_id)
            if self.stop_on_read:
                raise ReachedHomeAssistant(entity_id)
            return types.SimpleNamespace(entity_id=entity_id, state="sunny", attributes={})

        async def publish_weather(entity_id, state, retain=False):
            self.weather_publishes.append((entity_id, retain))

        self.stop_on_read = True
        self.methods = bridge_methods({
            "json": json,
            "_LOGGER": logging.getLogger(__name__),
            "STATE_HISTORY_KIND": "state",
            "BINARY_HISTORY_KIND": "binary",
            "timedelta": timedelta,
            "dt_util": types.SimpleNamespace(utcnow=lambda: datetime(2026, 9, 25, tzinfo=timezone.utc)),
        })
        self.bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(states=types.SimpleNamespace(get=get_state)),
            history_response_topic="tab5_lvgl/config/panel/bridge/history/response",
            sensors=["sensor.power", "sensor.panel_battery_soc"],
            binary_sensors=["binary_sensor.door"],
            weathers=["weather.home"],
            _async_publish_weather_state=publish_weather,
        )

    async def history(self, entity_id):
        payload = json.dumps({"entity_id": entity_id, "hours": 24})
        await self.methods["_async_handle_history_request"](
            self.bridge, types.SimpleNamespace(payload=payload, retain=False)
        )

    async def weather(self, payload):
        await self.methods["_async_handle_weather_request"](
            self.bridge, types.SimpleNamespace(payload=payload, retain=False)
        )

    async def test_numeric_history_reads_configured_entities(self):
        # Integration-owned sensors are part of self.sensors and stay allowed.
        for entity_id in ("sensor.power", "sensor.panel_battery_soc", "binary_sensor.door"):
            with self.assertRaises(ReachedHomeAssistant):
                await self.history(entity_id)
        self.assertEqual(
            self.reads, ["sensor.power", "sensor.panel_battery_soc", "binary_sensor.door"]
        )

    async def test_numeric_history_ignores_unconfigured_entities(self):
        for entity_id in ("person.owner", "lock.front_door", "sensor.unselected", "weather.home"):
            await self.history(entity_id)
        self.assertEqual(self.reads, [])

    async def test_weather_refresh_only_for_configured_weather(self):
        self.stop_on_read = False
        await self.weather(json.dumps({"entity_id": "weather.other"}))
        await self.weather(json.dumps({"entity_id": "sensor.power"}))
        self.assertEqual(self.reads, [])
        self.assertEqual(self.weather_publishes, [])

        await self.weather(json.dumps({"entity_id": "weather.home"}))
        self.assertEqual(self.weather_publishes, [("weather.home", True)])

    async def test_weather_refresh_without_entity_publishes_configured_weathers(self):
        self.stop_on_read = False
        await self.weather("")
        self.assertEqual(self.reads, ["weather.home"])
        self.assertEqual(self.weather_publishes, [("weather.home", True)])


if __name__ == "__main__":
    unittest.main()
