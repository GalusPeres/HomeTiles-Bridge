"""Sun times in the weather payload, so the panel shows moon icons at night."""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_weather_forecasts as forecasts
from test_view_navigation import load_module

SUN = load_module("sun_times")
SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"
DAY = date(2026, 9, 29)


def local(tz_hours, day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=timezone(timedelta(hours=tz_hours)))


def daily(tz_hours, first, days, rise, set_):
    """The same local sunrise and sunset on every date."""
    dates = [first + timedelta(days=offset) for offset in range(-2, days + 2)]
    return {SUN.SUNRISE: [local(tz_hours, d, *rise) for d in dates],
            SUN.SUNSET: [local(tz_hours, d, *set_) for d in dates]}


def home_assistant_like(events):
    """Like get_astral_event_date: the event on the asked UTC date."""
    def event(kind, asked):
        for moment in events.get(kind, []):
            if moment.astimezone(timezone.utc).date() == asked:
                return moment
        return None
    return event


class SunDaysTest(unittest.TestCase):
    def test_today_only_without_forecasts(self):
        self.assertEqual(SUN.sun_days(DAY, []), [DAY])

    def test_through_the_last_daily_or_hourly_forecast_date(self):
        days = SUN.sun_days(DAY, ["2026-09-28", "2026-10-02", None, "bad", 5, "2026-09-30"])
        self.assertEqual(days, [DAY + timedelta(days=offset) for offset in range(4)])

    def test_at_most_eight_dates(self):
        days = SUN.sun_days(DAY, ["2026-10-20"])
        self.assertEqual(len(days), 8)
        self.assertEqual(days[-1], date(2026, 10, 6))


class SunEntriesTest(unittest.TestCase):
    def entries(self, events, days=1, latitude=48.1, first=DAY):
        return SUN.sun_entries([first + timedelta(days=o) for o in range(days)],
                               home_assistant_like(events), latitude)

    def test_normal_day_in_minutes_after_local_midnight(self):
        entries = self.entries(daily(2, DAY, 2, (7, 12), (19, 5)), days=2)
        self.assertEqual(entries, [{"d": "2026-09-29", "r": 432, "s": 1145},
                                   {"d": "2026-09-30", "r": 432, "s": 1145}])

    def test_far_east_sunrise_on_the_previous_utc_date(self):
        # 06:00 at UTC+10 is 20:00 UTC the day before.
        entries = self.entries(daily(10, DAY, 3, (6, 0), (17, 50)), days=3)
        self.assertEqual([(e["d"], e["r"], e["s"]) for e in entries],
                         [("2026-09-29", 360, 1070), ("2026-09-30", 360, 1070),
                          ("2026-10-01", 360, 1070)])

    def test_far_west_sunset_on_the_next_utc_date(self):
        # 18:30 at UTC-10 is 04:30 UTC the day after.
        entries = self.entries(daily(-10, DAY, 3, (6, 20), (18, 30)), days=3)
        self.assertEqual([(e["r"], e["s"]) for e in entries], [(380, 1110)] * 3)

    def test_polar_night_and_polar_day(self):
        self.assertEqual(self.entries({}, latitude=69.65, first=date(2026, 12, 21)),
                         [{"d": "2026-12-21", "up": False}])
        self.assertEqual(self.entries({}, latitude=69.65, first=date(2026, 6, 21)),
                         [{"d": "2026-06-21", "up": True}])
        # Southern hemisphere: winter in June, summer in December.
        self.assertEqual(self.entries({}, latitude=-75.0, first=date(2026, 6, 21))[0]["up"], False)
        self.assertEqual(self.entries({}, latitude=-75.0, first=date(2026, 12, 21))[0]["up"], True)

    def test_sun_still_up_at_midnight_or_up_since_midnight(self):
        events = {SUN.SUNRISE: [local(2, DAY, 2, 10)],
                  SUN.SUNSET: [local(2, DAY + timedelta(days=1), 23, 40)]}
        self.assertEqual(self.entries(events, days=2, latitude=69.65),
                         [{"d": "2026-09-29", "r": 130, "s": 1440},
                          {"d": "2026-09-30", "r": 0, "s": 1420}])

    def test_sunset_after_midnight_before_the_next_sunrise(self):
        events = {SUN.SUNSET: [local(2, DAY, 0, 30)], SUN.SUNRISE: [local(2, DAY, 1, 50)]}
        self.assertEqual(self.entries(events, latitude=69.65), [{"d": "2026-09-29", "up": True}])

    def test_values_stay_in_range_and_small(self):
        entries = self.entries(daily(2, DAY, 8, (0, 0), (23, 59)), days=8)
        for entry in entries:
            self.assertTrue(0 <= entry["r"] < entry["s"] <= 1440)
        self.assertLess(len(json.dumps(entries, separators=(",", ":"))), 300)


def weather_sun():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_weather_sun")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              node], type_ignores=[])
    tz = timezone(timedelta(hours=2))
    asked = []

    def get_astral_event_date(hass, kind, day):
        asked.append((kind, day))
        # Sunrise 07:12 and sunset 19:05 local, returned in UTC like Home Assistant.
        hour, minute = (7, 12) if kind == "sunrise" else (19, 5)
        return local(2, day, hour, minute).astimezone(timezone.utc)

    scope = dict(date=date, datetime=datetime, _LOGGER=logging.getLogger(__name__),
                 get_astral_event_date=get_astral_event_date,
                 sun_days=SUN.sun_days, sun_entries=SUN.sun_entries,
                 dt_util=SimpleNamespace(now=lambda: datetime(2026, 9, 29, 21, 30, tzinfo=tz),
                                         as_local=lambda dt: dt.astimezone(tz)))
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), scope)
    return scope, asked


def hass(latitude=48.1, longitude=11.6):
    return SimpleNamespace(config=SimpleNamespace(latitude=latitude, longitude=longitude))


class WeatherSunTest(unittest.TestCase):
    def test_dates_match_the_forecasts(self):
        scope, asked = weather_sun()
        payload = {"forecast": [{"date_local": "2026-09-29"}, {"date_local": "2026-09-30"},
                                {"date_local": "2026-10-03"}],
                   "forecast_hourly": [{"d": "2026-09-29", "h": 22}, {"d": "2026-09-30", "h": 0}]}
        sun = scope["_weather_sun"](hass(), payload)
        self.assertEqual([entry["d"] for entry in sun],
                         ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(sun[0], {"d": "2026-09-29", "r": 432, "s": 1145})
        # The neighbouring UTC dates are asked too.
        self.assertEqual(min(day for _, day in asked), date(2026, 9, 28))
        self.assertEqual(max(day for _, day in asked), date(2026, 10, 4))

    def test_hourly_forecast_alone_sets_the_range(self):
        scope, _ = weather_sun()
        sun = scope["_weather_sun"](hass(), {"forecast_hourly": [{"d": "2026-09-30", "h": 5}]})
        self.assertEqual([entry["d"] for entry in sun], ["2026-09-29", "2026-09-30"])

    def test_today_without_forecasts(self):
        scope, _ = weather_sun()
        self.assertEqual(scope["_weather_sun"](hass(), {}), [{"d": "2026-09-29", "r": 432, "s": 1145}])

    def test_missing_location_omits_the_field(self):
        scope, asked = weather_sun()
        self.assertIsNone(scope["_weather_sun"](hass(latitude=None), {}))
        self.assertIsNone(scope["_weather_sun"](hass(longitude=None), {}))
        self.assertIsNone(scope["_weather_sun"](hass(0, 0), {}))
        self.assertEqual(asked, [])

    def test_failed_computation_omits_the_field(self):
        scope, _ = weather_sun()

        def broken(*_args):
            raise ValueError("no astral")

        scope["get_astral_event_date"] = broken
        with self.assertLogs(__name__, level="DEBUG"):
            self.assertIsNone(scope["_weather_sun"](hass(), {}))


class WeatherPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_sun_is_a_top_level_field_of_the_weather_payload(self):
        runtime, state, _ = forecasts.WeatherForecastTests().runtime(forecasts.yandex_forecasts())
        scope = runtime._build_weather_payload.__func__.__globals__
        seen = []

        def weather_sun_stub(hass, payload):
            seen.append(sorted(payload))
            return [{"d": "2026-09-08", "r": 400, "s": 1150}]

        scope["_weather_sun"] = weather_sun_stub
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertEqual(payload["sun"], [{"d": "2026-09-08", "r": 400, "s": 1150}])
        # The sun times see the finished forecasts, so their dates match.
        self.assertIn("forecast", seen[0])
        self.assertIn("forecast_hourly", seen[0])

    async def test_no_sun_field_without_sun_times(self):
        runtime, state, _ = forecasts.WeatherForecastTests().runtime(forecasts.yandex_forecasts())
        payload = json.loads(await runtime._build_weather_payload(state.entity_id, state))
        self.assertNotIn("sun", payload)


if __name__ == "__main__":
    unittest.main()
