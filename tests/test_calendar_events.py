"""The Calendar tile's retained `events` payload and the Bridge wiring that publishes it."""

from __future__ import annotations

import asyncio
import json
import logging
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from test_announcement_guard import extract
from test_view_navigation import ROOT, load_module

CAL = load_module("calendar_events")
CONTROL = load_module("control_helpers")
SEARCH_SOURCE = (ROOT / "entity_search.py").read_text(encoding="utf-8")
BERLIN_SUMMER = timezone(timedelta(hours=2))
BERLIN = ZoneInfo("Europe/Berlin")


class PayloadTest(unittest.TestCase):
  def test_window_covers_the_month_grid_and_two_months_ahead(self):
    self.assertEqual(CAL.fetch_window(date(2026, 10, 24)), (date(2026, 9, 24), date(2027, 1, 7)))
    self.assertEqual(CAL.fetch_window(date(2026, 12, 1)), (date(2026, 11, 24), date(2027, 3, 7)))
    start, end = CAL.fetch_window(date(2026, 2, 28))
    self.assertLess((end - start).days, 400, "the panel rejects longer windows")

  def test_events_become_local_naive_stamps_sorted_all_day_first(self):
    events = [
      {"start": "2026-10-24T08:00:00+00:00", "end": "2026-10-24T09:30:00+00:00",
       "summary": "Long  ride", "location": "Pool"},
      {"start": "2026-10-24", "end": "2026-10-25", "summary": "Bio waste"},
      {"start": "2026-10-23T22:30:00Z", "end": "2026-10-24T00:15:00Z", "summary": "Night"},
    ]
    payload = json.loads(CAL.build_payload(events, date(2026, 9, 24), date(2027, 1, 7), "off", BERLIN_SUMMER))
    self.assertEqual(payload["v"], 1)
    self.assertEqual((payload["from"], payload["to"], payload["state"]), ("2026-09-24", "2027-01-07", "off"))
    self.assertEqual(payload["e"], [
      {"s": "2026-10-24", "e": "2026-10-25", "t": "Bio waste"},
      {"s": "2026-10-24T00:30", "e": "2026-10-24T02:15", "t": "Night"},
      {"s": "2026-10-24T10:00", "e": "2026-10-24T11:30", "t": "Long ride", "l": "Pool"},
    ])

  def test_live_local_calendar_response_across_the_dst_change(self):
    # Shape of a real local_calendar get_events response (2026-10-24, titles and
    # places replaced): offsets follow the event's own date, extra keys are ignored.
    response = {"calendar.waste": {"events": [
      {"start": "2026-10-23", "end": "2026-10-24", "summary": "Paper", "description": "",
       "location": "Musterstraße 1", "status": "confirmed"},
      {"start": "2026-10-24T08:00:00+02:00", "end": "2026-10-24T10:00:00+02:00", "summary": "Mobile collection",
       "description": "x", "location": "Market square", "status": "confirmed"},
      {"start": "2026-11-19T08:00:00+01:00", "end": "2026-11-19T14:15:00+01:00", "summary": "Mobile collection",
       "description": "x", "location": "Market square", "status": "confirmed"},
      {"start": "2026-12-23", "end": "2026-12-27", "summary": "Holidays", "description": "", "location": "",
       "status": "confirmed"},
      {"start": "2026-10-24T23:30:00+02:00", "end": "2026-10-25T01:30:00+02:00", "summary": "Night",
       "description": "", "location": "", "status": "confirmed"},
    ]}}
    events = CAL.events_of_response(response, "calendar.waste")
    payload = json.loads(CAL.build_payload(events, date(2026, 9, 24), date(2027, 1, 7), "off", BERLIN))
    self.assertEqual(payload["e"], [
      {"s": "2026-10-23", "e": "2026-10-24", "t": "Paper", "l": "Musterstraße 1"},
      {"s": "2026-10-24T08:00", "e": "2026-10-24T10:00", "t": "Mobile collection", "l": "Market square"},
      {"s": "2026-10-24T23:30", "e": "2026-10-25T01:30", "t": "Night"},
      {"s": "2026-11-19T08:00", "e": "2026-11-19T14:15", "t": "Mobile collection", "l": "Market square"},
      {"s": "2026-12-23", "e": "2026-12-27", "t": "Holidays"},
    ])

  def test_text_is_clipped_by_utf8_bytes_without_breaking_characters(self):
    title = "Ä" * 40  # 80 bytes
    payload = json.loads(CAL.build_payload(
      [{"start": "2026-10-24", "end": "2026-10-25", "summary": title, "location": "ö" * 30}],
      date(2026, 10, 1), date(2026, 10, 31), "on", None))
    event = payload["e"][0]
    self.assertLessEqual(len(event["t"].encode("utf-8")), CAL.MAX_TITLE_BYTES)
    self.assertEqual(event["t"], "Ä" * 31)
    self.assertLessEqual(len(event["l"].encode("utf-8")), CAL.MAX_LOCATION_BYTES)

  def test_broken_or_mixed_items_are_skipped_or_repaired(self):
    events = [
      "nope", {"summary": "no start"}, {"start": "tomorrow", "summary": "bad"},
      {"start": "2026-10-24", "summary": "no end"},
      {"start": "2026-10-24T10:00:00+02:00", "end": "2026-10-24T09:00:00+02:00", "summary": "backwards"},
      {"start": "2026-10-25", "end": "2026-10-25T10:00:00+02:00", "summary": "mixed"},
    ]
    payload = json.loads(CAL.build_payload(events, date(2026, 10, 1), date(2026, 10, 31), "on", BERLIN_SUMMER))
    self.assertEqual(payload["e"], [
      {"s": "2026-10-24", "e": "2026-10-25", "t": "no end"},
      {"s": "2026-10-24T10:00", "e": "2026-10-24T10:00", "t": "backwards"},
      {"s": "2026-10-25", "e": "2026-10-26", "t": "mixed"},
    ])

  def test_too_many_events_move_the_end_back_instead_of_hiding_days(self):
    events = [{"start": (date(2026, 10, 1) + timedelta(days=i // 2)).isoformat(),
               "end": (date(2026, 10, 2) + timedelta(days=i // 2)).isoformat(),
               "summary": f"Event {i}"} for i in range(150)]
    payload = json.loads(CAL.build_payload(events, date(2026, 10, 1), date(2027, 1, 7), "on", None))
    self.assertLessEqual(len(payload["e"]), CAL.MAX_EVENTS)
    last = date.fromisoformat(payload["to"])
    self.assertTrue(all(date.fromisoformat(e["s"]) <= last for e in payload["e"]))
    # Every event up to "to" is there: two per day.
    self.assertEqual(len(payload["e"]), 2 * ((last - date(2026, 10, 1)).days + 1))

  def test_payload_stays_below_the_panel_limit(self):
    events = [{"start": f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:00:00+00:00",
               "end": f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:30:00+00:00",
               "summary": "x" * 200, "location": "y" * 200} for i in range(400)]
    text = CAL.build_payload(events, date(2026, 10, 1), date(2026, 10, 31), "on", timezone.utc)
    self.assertLessEqual(len(text.encode("utf-8")), CAL.MAX_PAYLOAD_BYTES)
    payload = json.loads(text)
    last = date.fromisoformat(payload["to"])
    self.assertTrue(all(date.fromisoformat(e["s"][:10]) <= last for e in payload["e"]))

  def test_unavailable_and_service_response_shapes(self):
    payload = json.loads(CAL.unavailable_payload(date(2026, 10, 1), date(2026, 10, 31)))
    self.assertEqual((payload["state"], payload["e"]), ("unavailable", []))
    response = {"calendar.home": {"events": [{"start": "2026-10-24"}]}}
    self.assertEqual(CAL.events_of_response(response, "calendar.home"), [{"start": "2026-10-24"}])
    self.assertIsNone(CAL.events_of_response(response, "calendar.work"))
    self.assertIsNone(CAL.events_of_response(None, "calendar.home"))


class ListWiringTest(unittest.TestCase):
  def test_calendars_are_a_picker_and_panel_list(self):
    self.assertEqual(CONTROL.PANEL_LIST_DOMAINS["calendars"], ("calendar",))
    self.assertIn('"calendars": ("calendar",),', SEARCH_SOURCE)
    self.assertIn('"calendars": ("calendars",),', SEARCH_SOURCE)
    with self.assertRaises(ValueError):
      CONTROL.panel_entity_list(["calendar.home", "script.x"], "calendars")
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.assertIn('"calendar_meta": self._build_entity_meta(self.calendars),', source)
    self.assertIn('"entity_search": 5,', source)
    self.assertIn("+ self.calendars\n", source, "calendar state changes reach the Bridge")
    flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    self.assertIn('calendars = selected(CONF_CALENDARS, ("calendar",))', flow)


METHODS = {
  "_schedule_calendar_refresh", "_calendar_interval_tick", "_async_process_calendar_updates",
  "_async_publish_calendar_events", "_stop_calendar_updates", "_owns_state_publish", "_ha_topic_for_entity",
}


class PublisherWiringTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.publishes: list[tuple[str, dict, bool]] = []
    self.calls: list[tuple] = []
    self.timers: list = []
    self.intervals: list = []
    self.response: dict = {"calendar.home": {"events": [
      {"start": "2026-10-24T10:00:00+02:00", "end": "2026-10-24T11:00:00+02:00", "summary": "Dentist"}]}}
    self.states = {"calendar.home": types.SimpleNamespace(state="off", attributes={})}

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.publishes.append((topic, json.loads(payload), retain))

    async def call(domain, service, data, blocking=False, return_response=False):
      self.calls.append((domain, service, dict(data), blocking, return_response))
      if isinstance(self.response, Exception):
        raise self.response
      return self.response

    def call_later(hass, delay, action):
      self.timers.append((delay, action))
      return lambda: self.timers.remove((delay, action)) if (delay, action) in self.timers else None

    def interval(hass, action, period):
      self.intervals.append(period)
      return lambda: self.intervals.remove(period)

    scope: dict = {key: value for key, value in vars(CAL).items() if not key.startswith("__")}
    scope.update({
      "calendar_events": CAL, "CALENDAR_DOMAIN": CAL.CALENDAR_DOMAIN, "CALENDAR_REFRESH_S": CAL.REFRESH_S,
      "callback": lambda function: function, "entity_domain": CONTROL.entity_domain,
      "mqtt": types.SimpleNamespace(async_publish=publish), "async_call_later": call_later,
      "async_track_time_interval": interval, "DOMAIN": "tab5_lvgl",
      "datetime": datetime, "timedelta": timedelta,
      "dt_util": types.SimpleNamespace(now=lambda: datetime(2026, 10, 24, 9, 0, tzinfo=BERLIN_SUMMER),
                                       get_default_time_zone=lambda: BERLIN_SUMMER),
      "_LOGGER": logging.getLogger("test_calendar_events"),
    })
    extract(METHODS, scope)
    bridge = type("Bridge", (), {name: scope[name] for name in METHODS})()
    bridge.hass = types.SimpleNamespace(
      data={"tab5_lvgl": {}}, states=types.SimpleNamespace(get=lambda e: self.states.get(e)),
      services=types.SimpleNamespace(async_call=call),
      async_create_task=lambda coro: asyncio.ensure_future(coro),
    )
    bridge.entry = types.SimpleNamespace(entry_id="entry1")
    bridge.ha_prefix = "ha"
    bridge.calendars = ["calendar.home"]
    bridge._runtime_setup_complete = True
    bridge._calendar_pending = set()
    bridge._calendar_refresh_handle = None
    bridge._calendar_task = None
    bridge._unsub_calendar_interval = None
    bridge._calendar_last_payload = {}
    self.bridge = bridge

  async def run_due(self) -> None:
    for _delay, action in list(self.timers):
      self.timers.remove((_delay, action))
      action(None)
    for _ in range(5):
      await asyncio.sleep(0)

  async def test_refresh_is_coalesced_and_publishes_retained_events(self):
    self.bridge._schedule_calendar_refresh(["calendar.home", "calendar.unknown"])
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    self.assertEqual(len(self.timers), 1, "one debounce timer")
    self.assertEqual(self.intervals, [timedelta(seconds=CAL.REFRESH_S)], "periodic refresh started")
    await self.run_due()
    self.assertEqual(len(self.calls), 1)
    domain, service, data, blocking, response = self.calls[0]
    self.assertEqual((domain, service, blocking, response), ("calendar", "get_events", True, True))
    self.assertEqual(data["start_date_time"], "2026-09-24T00:00:00+02:00")
    self.assertEqual(data["end_date_time"], "2027-01-08T00:00:00+02:00")
    topic, payload, retain = self.publishes[0]
    self.assertEqual((topic, retain), ("ha/calendar/home/events", True))
    self.assertEqual(payload["e"], [{"s": "2026-10-24T10:00", "e": "2026-10-24T11:00", "t": "Dentist"}])
    # The same events again publish nothing.
    self.bridge._calendar_interval_tick()
    await self.run_due()
    self.assertEqual(len(self.calls), 2)
    self.assertEqual(len(self.publishes), 1)

  async def test_failures_keep_the_retained_events_and_missing_calendars_say_so(self):
    self.response = RuntimeError("calendar offline")
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    await self.run_due()
    self.assertEqual(self.publishes, [])
    self.states.clear()
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    await self.run_due()
    self.assertEqual(self.publishes[0][1]["state"], "unavailable")

  async def test_another_entry_owning_the_calendar_and_unload(self):
    self.bridge.hass.data["tab5_lvgl"]["state_owners"] = {("ha", "calendar.home"): "entry2"}
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    await self.run_due()
    self.assertEqual((self.calls, self.publishes), ([], []))
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    self.bridge._stop_calendar_updates()
    self.assertEqual((self.timers, self.intervals), ([], []))
    self.bridge._runtime_setup_complete = False
    self.bridge._schedule_calendar_refresh(["calendar.home"])
    self.assertEqual(self.timers, [])


if __name__ == "__main__":
  unittest.main()
