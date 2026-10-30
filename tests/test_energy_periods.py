"""Energy popup periods: today by the hour, the week from the panel's first
weekday and the month by the day, the year by the month (user 2026-10-29:
fixed periods Year, Month, Week, Today instead of 7D). Each statistics row
lands in its own bar, so a period the statistics lack stays empty."""

from __future__ import annotations

import datetime
import json
import logging
import types
import unittest

from test_announcement_guard import Clock, extract
from test_view_navigation import load_module

LIMITS = load_module("request_limits")
LOGGER = logging.getLogger("test_energy_periods")
ZONE = datetime.timezone(datetime.timedelta(hours=1))
# Thursday 2026-10-29, 14:30 local time.
NOW = datetime.datetime(2026, 10, 29, 14, 30, tzinfo=ZONE)


def _message(payload: str):
  return types.SimpleNamespace(payload=payload, retain=False)


class EnergyPeriodTest(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.clock = Clock()
    self.published = []
    self.queries = []
    self.rows = None

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.published.append(json.loads(payload))

    async def get_manager(hass):
      return types.SimpleNamespace(data={
        "energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv"}],
        "device_consumption": [],
      })

    def statistics(hass, start, end, ids, period, units, kinds):
      self.queries.append((start, end, period))
      if isinstance(self.rows, dict):
        rows = self.rows.get(period, [])
      elif period == "5minute":
        rows = []
      else:
        rows = self.rows if self.rows is not None else [{"change": 1.0, "sum": 1.0}]
      return {stat_id: rows for stat_id in ids}

    async def run_job(job):
      return job()

    self.scope = {
      "_LOGGER": LOGGER, "monotonic": self.clock, "json": json, "timedelta": datetime.timedelta,
      "datetime": datetime.datetime,
      "mqtt": types.SimpleNamespace(async_publish=publish),
      "ENERGY_UNAVAILABLE_LOG_S": LIMITS.ENERGY_UNAVAILABLE_LOG_S,
      "async_get_energy_manager": get_manager,
      "statistics_during_period": statistics,
      "get_instance": lambda hass: types.SimpleNamespace(async_add_executor_job=run_job),
      "dt_util": types.SimpleNamespace(now=lambda: NOW, as_utc=lambda d: d.astimezone(datetime.timezone.utc)),
      "CONF_ENERGY_ELECTRICITY": "energy_electricity", "CONF_ENERGY_GAS": "energy_gas",
      "CONF_ENERGY_WATER": "energy_water",
    }
    extract({"_async_handle_energy_request", "_async_publish_energy_empty", "_try_parse_json",
             "_secure_log_due", "_panel_secured", "_energy_categories"}, self.scope)
    self.bridge = types.SimpleNamespace(
      hass=types.SimpleNamespace(data={}, states=types.SimpleNamespace(get=lambda entity_id: None),
                                 config=types.SimpleNamespace(currency="EUR")),
      energy_response_topic="hometiles/energy/response", _secure_log_at={},
      _panel_declaration={"lists": {}, "secured": True, "own": True}, entry=types.SimpleNamespace(data={}))
    for name in ("_secure_log_due", "_async_publish_energy_empty", "_panel_secured", "_energy_categories"):
      setattr(self.bridge, name, lambda *args, name=name: self.scope[name](self.bridge, *args))

  async def request(self, payload: dict):
    await self.scope["_async_handle_energy_request"](self.bridge, _message(json.dumps(payload)))
    return self.published[-1]

  def local(self, *args) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=ZONE)

  async def test_today_by_the_hour_from_midnight(self):
    answer = await self.request({"period": "day"})
    self.assertEqual(answer["period"], "day")
    self.assertEqual(self.queries[0][0], self.local(2026, 10, 29).astimezone(datetime.timezone.utc))
    self.assertEqual(self.queries[0][2], "hour")

  async def test_the_running_hour_from_the_five_minute_statistics(self):
    # User 2026-10-30: the tile's bars stood still. The Recorder has written
    # the hours up to 12:00; 13:00 is over but not compiled yet and 14:00 is
    # running (14:30): both come from the 5-minute statistics after 13:00.
    hours = [{"start": self.local(2026, 10, 29, hour), "change": 1.0, "sum": hour + 1.0} for hour in range(13)]
    short = [{"start": self.local(2026, 10, 29, 12, 55), "change": 9.0, "sum": 13.0}]
    short += [{"start": self.local(2026, 10, 29, 13 + minute // 60, minute % 60), "change": 0.25,
               "sum": 13.0 + 0.25 * (index + 1)} for index, minute in enumerate(range(0, 90, 5))]
    self.rows = {"hour": hours, "5minute": short}
    answer = await self.request({"period": "day"})
    self.assertEqual([query[2] for query in self.queries], ["hour", "5minute"])
    self.assertEqual(self.queries[1][:2], (self.local(2026, 10, 29, 13).astimezone(datetime.timezone.utc),
                                           NOW.astimezone(datetime.timezone.utc)))
    pv = next(entry for entry in answer["entries"] if entry["id"] == "sensor.pv")
    # The 12:55 row belongs to an hour the hourly statistics already hold.
    self.assertEqual(pv["values"], [1.0] * 13 + [3.0, 1.5])
    self.assertEqual(pv["total"], 17.5)

  async def test_the_five_minute_statistics_only_for_today(self):
    # The week, month and year keep their own statistics alone.
    for period in ("week", "month", "year"):
      self.queries.clear()
      await self.request({"period": period})
      self.assertNotIn("5minute", [query[2] for query in self.queries], period)

  async def test_the_week_starts_on_the_panels_first_weekday(self):
    # tm_wday: Monday 1, Sunday 0, Saturday 6; today is a Thursday.
    for week_start, first in ((1, 26), (0, 25), (6, 24), (4, 29)):
      answer = await self.request({"period": "week", "week_start": week_start})
      self.assertEqual(answer["period"], "week")
      self.assertEqual(answer["start"], self.local(2026, 10, first).isoformat(), week_start)
      self.assertEqual(self.queries[-1][2], "day")

  async def test_a_week_without_first_weekday_stays_the_last_seven_days(self):
    # Firmware without fixed periods sends no week_start.
    for payload in ({"period": "week"}, {"period": "week", "week_start": 7},
                    {"period": "week", "week_start": "1"}, {"period": "week", "week_start": True}):
      answer = await self.request(payload)
      self.assertEqual(answer["start"], self.local(2026, 10, 23).isoformat(), payload)

  async def test_month_by_the_day_and_year_by_the_month(self):
    answer = await self.request({"period": "month"})
    self.assertEqual((answer["period"], answer["start"], self.queries[-1][2]),
                     ("month", self.local(2026, 10, 1).isoformat(), "day"))
    answer = await self.request({"period": "year"})
    self.assertEqual((answer["period"], answer["start"], self.queries[-1][2]),
                     ("year", self.local(2026, 1, 1).isoformat(), "month"))

  async def test_an_unknown_period_answers_as_today(self):
    answer = await self.request({"period": "decade"})
    self.assertEqual(answer["period"], "day")

  async def test_each_month_lands_in_its_own_bar(self):
    # A sensor added in March: its first row is March, January and February
    # stay empty instead of March showing under January.
    def month_start(month):
      return self.local(2026, month, 1).astimezone(datetime.timezone.utc).timestamp()
    self.rows = [{"start": month_start(3), "change": 4.0, "sum": 4.0},
                 {"start": month_start(4), "change": 6.0, "sum": 10.0},
                 {"start": month_start(6), "change": 5.0, "sum": 15.0}]
    answer = await self.request({"period": "year"})
    pv = next(entry for entry in answer["entries"] if entry["id"] == "sensor.pv")
    self.assertEqual(pv["values"], [None, None, 4.0, 6.0, None, 5.0])
    self.assertEqual(pv["total"], 15.0)

  async def test_hours_by_the_clock(self):
    # Datetime starts too; the hour before midnight belongs to no bar.
    self.rows = {"hour": [{"start": self.local(2026, 10, 28, 23), "change": 9.0, "sum": 9.0},
                          {"start": self.local(2026, 10, 29, 0), "change": 0.5, "sum": 9.5},
                          {"start": self.local(2026, 10, 29, 2), "change": 0.25, "sum": 9.75}]}
    answer = await self.request({"period": "day"})
    pv = next(entry for entry in answer["entries"] if entry["id"] == "sensor.pv")
    self.assertEqual(pv["values"], [0.5, None, 0.25])

  async def test_rows_without_a_start_keep_their_order(self):
    self.rows = [{"change": 1.0, "sum": 1.0}, {"change": 2.0, "sum": 3.0}]
    answer = await self.request({"period": "month"})
    pv = next(entry for entry in answer["entries"] if entry["id"] == "sensor.pv")
    self.assertEqual(pv["values"], [1.0, 2.0])


if __name__ == "__main__":
  unittest.main()
