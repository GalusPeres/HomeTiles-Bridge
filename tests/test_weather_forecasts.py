"""Run the real weather service/cache/payload path without a running HA server."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from enum import IntFlag
import json
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"


class WeatherEntityFeature(IntFlag):
    # Home Assistant weather.const defines these stable feature bits.
    FORECAST_DAILY = 1
    FORECAST_HOURLY = 2
    FORECAST_TWICE_DAILY = 4


def helpers():
    names = {
        "_get_weather_forecast", "_fetch_weather_forecast", "_build_weather_payload",
        "_extract_weather_payload", "_sanitize_forecast_list", "_normalize_weather_value",
        "_extract_forecast_from_result", "_apply_forecast_icons", "_weather_number",
        "_forecast_entry_local_date", "_forecast_entry_local_datetime",
        "_merge_hourly_precip_into_daily", "_build_daily_forecast_from_hourly",
        "_build_daily_forecast_from_periods", "_compact_daily_forecast", "_compact_hourly_forecast",
        "_weather_started", "_sync_weather_subscriptions", "_subscribe_weather_forecast",
        "_queue_weather_update", "_async_process_weather_updates", "_async_stop_weather_updates",
        "_async_publish_weather_state",
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and (t.id.startswith("FORECAST_") or t.id == "_WEATHER_ICON_MAP")
        for t in n.targets)]
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    local_tz = timezone(timedelta(hours=3))

    def parse_datetime(value):
        try:
            return datetime.fromisoformat(value) if "T" in value else None
        except (ValueError, TypeError):
            return None

    scope = dict(json=json, asyncio=asyncio, callback=lambda fn: fn,
                 date=date, datetime=datetime, timedelta=timedelta,
                 WeatherEntityFeature=WeatherEntityFeature, WEATHER_DATA_COMPONENT="weather", async_get_forecasts=None,
                 _LOGGER=logging.getLogger(__name__), _weather_icon_from_state=lambda *args: None,
                 _weather_sun=lambda *args: None,
                 dt_util=SimpleNamespace(utcnow=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc),
                                         as_local=lambda dt: dt.astimezone(local_tz),
                                         as_utc=lambda dt: dt.astimezone(timezone.utc),
                                         parse_datetime=parse_datetime))
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *constants, *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), scope)
    return scope


def yandex_forecasts(days=7):
    # Yandex returns local midnight night periods and noon day periods, with high/low.
    periods = []
    for day in range(days):
        midnight = datetime(2026, 9, 8, tzinfo=timezone(timedelta(hours=3))) + timedelta(days=day)
        for daytime, high, low, condition in [(False, 12, 8, "clear-night"), (True, 23, 16, "sunny")]:
            stamp = midnight + timedelta(hours=12 if daytime else 0)
            periods.append(dict(datetime=stamp.astimezone(timezone.utc).isoformat(), is_daytime=daytime,
                                temperature=high + day, templow=low + day, condition=condition))
    hourly = [dict(datetime=(datetime(2026, 9, 8, tzinfo=timezone.utc) + timedelta(hours=h)).isoformat(),
                   temperature=10 + h / 10, condition="cloudy") for h in range(30)]
    return {"twice_daily": periods, "hourly": hourly}


class WeatherForecastTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, responses, features=6, attributes=None):
        scope = helpers()
        attrs = dict(attributes or {})
        if features is not None:
            attrs["supported_features"] = features
        state = SimpleNamespace(entity_id="weather.yandex", state="sunny", name="Yandex Pogoda", attributes=attrs)
        calls = []

        async def service(domain, name, data, **kwargs):
            self.assertEqual((domain, name), ("weather", "get_forecasts"))
            self.assertTrue(kwargs["return_response"])
            calls.append(data["type"])
            response = responses.get(data["type"])
            if isinstance(response, Exception):
                raise response
            return {state.entity_id: {"forecast": deepcopy(response)}}

        runtime = SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: state),
                                                       services=SimpleNamespace(async_call=service)),
                                  _forecast_cache={}, _weather_subscriptions={})
        for name in ("_get_weather_forecast", "_fetch_weather_forecast", "_build_weather_payload"):
            setattr(runtime, name, MethodType(scope[name], runtime))
        return runtime, state, calls

    async def test_yandex_week_is_not_limited_to_two_hourly_days(self):
        responses = yandex_forecasts()
        original = deepcopy(responses)
        runtime, state, calls = self.runtime(responses)
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(len(payload["forecast"]), 7)
        self.assertEqual(calls, ["twice_daily", "hourly"])
        for day, entry in enumerate(payload["forecast"]):
            self.assertEqual(entry["date_local"], f"2026-09-{8 + day:02}")
            self.assertEqual((entry["temperature"], entry["templow"]), (23 + day, 8 + day))
            self.assertEqual(entry["condition"], "sunny")
        self.assertEqual(len(payload["forecast_hourly"]), 30)
        self.assertEqual(payload["forecast_hourly"][0], dict(d="2026-09-08", h=3, t=10.0, c="cloudy"))
        await runtime._build_weather_payload(state.entity_id, state)
        self.assertEqual(calls, ["twice_daily", "hourly"], "Repeated state updates must use the cache")
        self.assertEqual(responses, original)

    async def test_supported_feature_combinations_and_daily_precedence(self):
        for features in range(8):
            with self.subTest(features=features):
                responses = yandex_forecasts()
                responses["daily"] = [dict(datetime="2026-09-08", temperature=30, templow=5, condition="rainy")]
                runtime, state, calls = self.runtime(responses, features)
                payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
                expected = (["daily"] if features & 1 else ["twice_daily"] if features & 4 else [])
                if features & 2:
                    expected.append("hourly")
                self.assertEqual(calls, expected)
                self.assertEqual("forecast_hourly" in payload, bool(features & 2))
                if features & 1:
                    self.assertEqual(payload["forecast"][0]["temperature"], 30)
                if features == 0:
                    self.assertNotIn("forecast", payload)

    async def test_missing_period_values_and_daytime_condition_are_preserved(self):
        entries = [dict(datetime="2026-09-08T22:00:00+03:00", is_daytime=True, condition="sunny", temperature=0),
                   dict(datetime="2026-09-08T12:00:00+03:00", is_daytime=False, condition="cloudy", templow=-4),
                   dict(datetime="2026-09-09T12:00:00+03:00", is_daytime=True, condition="rainy", temperature=None),
                   dict(datetime="not-a-date", temperature=999)]
        runtime, state, _ = self.runtime({"twice_daily": entries}, 4)
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(len(payload["forecast"]), 2)
        self.assertEqual(payload["forecast"][0]["temperature"], 0)
        self.assertEqual(payload["forecast"][0]["templow"], -4)
        self.assertEqual(payload["forecast"][0]["condition"], "sunny")
        self.assertNotIn("temperature", payload["forecast"][1])
        self.assertNotIn("templow", payload["forecast"][1])
        self.assertNotIn("forecast_hourly", payload)

    async def test_empty_daily_falls_back_to_twice_daily_and_keeps_precipitation(self):
        responses = yandex_forecasts(10)
        for entry in responses["twice_daily"]:
            entry.update(precipitation=2.5, precipitation_probability=60 if entry["is_daytime"] else 10)
        responses["hourly"][0].update(precipitation=0.1, precipitation_probability=20)
        runtime, state, _ = self.runtime(responses, 7)
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(len(payload["forecast"]), 8)
        self.assertEqual(payload["forecast"][0]["precipitation"], 5)
        self.assertEqual(payload["forecast"][0]["precipitation_probability"], 60)

    async def test_legacy_forecast_attribute_does_not_abort_publication(self):
        forecasts = [dict(datetime=f"2026-09-{8 + n:02}", temperature=20) for n in range(10)]
        runtime, state, calls = self.runtime({}, None, {"forecast": forecasts})
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(len(payload["forecast"]), 8)
        self.assertEqual(calls, ["hourly"])

    async def test_rain_dry_days_hourly_rain_and_current_weather_survive_together(self):
        responses = yandex_forecasts()
        rain = [2.4, 6.0, 0, 0, 1.2, 3.6, 0, 0, 4.8, 9.6, 0, 0, 2.4, 4.8]
        chance = [60, 85, 0, 0, 40, 70, 0, 0, 75, 95, 0, 0, 55, 80]
        for index, entry in enumerate(responses["twice_daily"]):
            entry.update(precipitation=rain[index], precipitation_probability=chance[index])
            if rain[index]:
                entry["condition"] = "rainy"
        midnight = datetime(2026, 9, 8, tzinfo=timezone(timedelta(hours=3)))
        for hour, entry in enumerate(responses["hourly"]):
            period = hour // 12
            entry.update(datetime=(midnight + timedelta(hours=hour)).isoformat(),
                         precipitation=rain[period] / 12,
                         precipitation_probability=chance[period])
        current = dict(temperature=21, apparent_temperature=20, dew_point=14,
                       humidity=65, pressure=1013, wind_speed=12, wind_gust_speed=22,
                       wind_bearing=225, visibility=12, cloud_coverage=70, uv_index=2,
                       temperature_unit="°C", pressure_unit="hPa", wind_speed_unit="km/h",
                       visibility_unit="km", precipitation_unit="mm")
        runtime, state, calls = self.runtime(responses, 6, current)
        state.state = "rainy"
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(calls, ["twice_daily", "hourly"])
        self.assertEqual(payload["state"], "rainy")
        for key, value in current.items():
            if not key.endswith("_unit"):
                self.assertEqual(payload[key], value)
        self.assertEqual(payload["units"], dict(temperature="°C", pressure="hPa",
                                               wind_speed="km/h", visibility="km", precipitation="mm"))
        daily = payload["forecast"]
        self.assertEqual(len(daily), 7)
        self.assertEqual([entry["precipitation"] for entry in daily], [8.4, 0, 4.8, 0, 14.4, 0, 7.2])
        self.assertEqual([entry["precipitation_probability"] for entry in daily], [85, 0, 70, 0, 95, 0, 80])
        self.assertEqual((daily[0]["temperature"], daily[0]["templow"]), (23, 8))
        self.assertEqual(daily[0]["condition"], "rainy")
        hourly = payload["forecast_hourly"]
        self.assertEqual(len(hourly), 30)
        self.assertAlmostEqual(sum(entry["p"] for entry in hourly[:24]), 8.4)
        self.assertEqual(hourly[12]["pp"], 85)
        self.assertEqual(hourly[24]["p"], 0)
        self.assertEqual(hourly[24]["pp"], 0)

    async def test_forecast_service_failure_does_not_remove_hourly_data(self):
        responses = yandex_forecasts()
        responses["twice_daily"] = RuntimeError("Provider unavailable")
        runtime, state, _ = self.runtime(responses)
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(len(payload["forecast"]), 2)
        self.assertEqual(len(payload["forecast_hourly"]), 30)


if __name__ == "__main__":
    unittest.main()
