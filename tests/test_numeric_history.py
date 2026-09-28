"""The numeric Sensor graph reads statistics or bounded state rows."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import types
import unittest

from test_view_navigation import ROOT, load_module

NUMERIC = load_module("numeric_history")

START = datetime(2026, 9, 20, tzinfo=timezone.utc)
# A request at an arbitrary moment: the graph's buckets are not aligned with
# the Recorder's 5-minute and hourly statistics periods.
END = datetime(2026, 9, 28, 10, 7, 23, tzinfo=timezone.utc)
ENTITY = "sensor.power"
MEAN_META = {"statistic_id": ENTITY, "source": "recorder", "name": None, "mean_type": 1,
             "has_sum": False, "unit_class": "power", "unit_of_measurement": "W"}
SUM_META = {"statistic_id": ENTITY, "source": "recorder", "name": None, "mean_type": 0,
            "has_sum": True, "unit_class": "energy", "unit_of_measurement": "kWh"}


def at(minutes):
    return START + timedelta(minutes=minutes)


def row(minutes, value):
    return state_at(at(minutes), value)


def state_at(moment, value):
    return types.SimpleNamespace(state=value, last_updated=moment, last_changed=moment)


def period(start, minutes, **columns):
    return dict(start=start.timestamp(), end=(start + timedelta(minutes=minutes)).timestamp(), **columns)


def grid(moment, seconds):
    return datetime.fromtimestamp(math.floor(moment.timestamp() / seconds) * seconds, tz=timezone.utc)


class FakeRecorder:
    """state_changes_during_period as the Recorder runs it.

    The SQL query reads changes strictly between start and end, oldest first,
    and applies the limit to them; ``descending`` only reverses the result.
    The start time state is the newest row before start, reported at start.
    """

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda item: item.last_updated)
        self.calls = []

    def __call__(self, hass, start_time, end_time=None, entity_id=None, no_attributes=False,
                 descending=False, limit=None, include_start_time_state=True):
        self.calls.append(dict(start=start_time, end=end_time, no_attributes=no_attributes,
                               descending=descending, limit=limit,
                               include_start_time_state=include_start_time_state))
        selected = [item for item in self.rows
                    if start_time < item.last_updated and (end_time is None or item.last_updated < end_time)]
        if limit:
            selected = selected[:limit]
        before = [item for item in self.rows if item.last_updated < start_time]
        if include_start_time_state and before:
            selected.insert(0, state_at(start_time, before[-1].state))
        if descending:
            selected.reverse()
        return {entity_id: selected} if selected else {}


class FakeStatistics:
    """statistics_during_period, get_metadata and get_display_unit of the Recorder."""

    def __init__(self, metadata=None, short=(), hourly=()):
        self.metadata = metadata
        self.tables = {"5minute": list(short), "hour": list(hourly)}
        self.calls = []
        self.metadata_calls = 0

    def __call__(self, hass, start_time, end_time, statistic_ids, period_name, units, columns):
        self.calls.append((period_name, start_time, end_time, set(columns)))
        # Periods starting in [start_time, end_time), with the requested columns only.
        keep = set(columns) | {"start", "end"}
        rows = [{key: value for key, value in item.items() if key in keep}
                for item in self.tables[period_name]
                if start_time.timestamp() <= item["start"]
                and (end_time is None or item["start"] < end_time.timestamp())]
        return {statistic_id: rows for statistic_id in statistic_ids} if rows else {}

    def get_metadata(self, hass, *, statistic_ids=None, statistic_type=None, statistic_source=None):
        self.metadata_calls += 1
        if self.metadata is None:
            return {}
        return {statistic_id: (7, dict(self.metadata)) for statistic_id in statistic_ids}

    @staticmethod
    def get_display_unit(hass, statistic_id, unit_class, statistic_unit):
        return statistic_unit

    def kwargs(self, state_unit):
        return dict(statistics_during_period=self, get_metadata=self.get_metadata,
                    get_display_unit=self.get_display_unit, state_unit=state_unit)


class NumericBucketsTest(unittest.TestCase):
    def test_buckets_keep_the_legacy_statistics(self):
        buckets = NUMERIC.NumericBuckets(START, 3, 60)
        for item in (row(-5, "1"), row(10, "2"), row(50, "4"), row(70, "nan"), row(80, "x"),
                     row(90, "6"), row(500, "9"), {"state": "8", "last_updated": at(20)}):
            buckets.add(item)
        self.assertEqual(buckets.values("mean"), [4.667, 6.0, 9.0])
        self.assertEqual(buckets.values("min"), [2.0, 6.0, 9.0])
        self.assertEqual(buckets.values("max"), [8.0, 6.0, 9.0])
        self.assertEqual(buckets.values("last"), [4.0, 6.0, 9.0])

    def test_statistics_periods_find_their_buckets(self):
        buckets = NUMERIC.NumericBuckets(at(2), 24, 5)
        # A 5-minute period goes to the bucket of its middle.
        self.assertEqual(buckets.indices(at(0), at(5)), range(0, 1))
        self.assertEqual(buckets.indices(at(-5), at(0)), range(0))
        # An hour in 5-minute buckets fills every bucket whose middle it covers.
        self.assertEqual(buckets.indices(at(60), at(120)), range(12, 24))
        self.assertEqual(buckets.indices(at(0), at(60)), range(0, 12))


class StatisticsTest(unittest.TestCase):
    def fetch(self, stats, recorder, hours, period_minutes, stat, state_unit="W"):
        return NUMERIC.fetch_numeric_history_values(
            None, ENTITY, END - timedelta(hours=hours), END, hours * 60 // period_minutes,
            period_minutes, stat, state_changes_during_period=recorder, **stats.kwargs(state_unit))

    def test_measurement_sensor_uses_the_5_minute_statistics(self):
        first = grid(END - timedelta(hours=24), 300)
        # From the period around the graph's start to the last compiled one.
        short = [period(first + timedelta(minutes=5 * k), 5, mean=k, min=k - 0.5, max=k + 1)
                 for k in range(288)]
        stats = FakeStatistics(MEAN_META, short=short)
        recorder = FakeRecorder([state_at(END - timedelta(minutes=1), "999")])
        for stat, expected in (("mean", [float(k) for k in range(288)]),
                               ("min", [k - 0.5 for k in range(288)]),
                               ("max", [k + 1.0 for k in range(288)])):
            values, read, complete = self.fetch(stats, recorder, 24, 5, stat)
            self.assertEqual(values, expected, stat)
            self.assertEqual((read, complete), (1, True))
        self.assertEqual(stats.calls[0], ("5minute", first, END, {"mean", "min", "max"}))
        self.assertEqual({call[0] for call in stats.calls}, {"5minute"})
        # State rows only after the newest compiled period; its bucket keeps the statistics.
        self.assertEqual(recorder.calls[0]["start"], first + timedelta(hours=24))
        self.assertFalse(recorder.calls[0]["include_start_time_state"])

    def test_hourly_statistics_fill_in_for_purged_5_minute_ones(self):
        start = END - timedelta(hours=168)
        first_hour = grid(start, 3600)
        hourly = [period(first_hour + timedelta(hours=j), 60, mean=1000 + j, min=900 + j, max=1100 + j)
                  for j in range(168)]
        # purge_keep_days = 3: the 5-minute statistics of the last three days remain.
        kept_from = first_hour + timedelta(hours=96, minutes=5)
        short = [period(kept_from + timedelta(minutes=5 * k), 5, mean=k, min=k - 1, max=k + 2)
                 for k in range(864)]
        stats = FakeStatistics(MEAN_META, short=short, hourly=hourly)
        mean, _, complete = self.fetch(stats, FakeRecorder([]), 168, 60, "mean")
        self.assertTrue(complete)
        self.assertEqual(mean[:96], [1000.0 + j for j in range(96)])
        # Twelve 5-minute periods per hour bucket.
        self.assertEqual((mean[96], mean[-1]), (5.5, 857.5))
        low, _, _ = self.fetch(stats, FakeRecorder([]), 168, 60, "min")
        high, _, _ = self.fetch(stats, FakeRecorder([]), 168, 60, "max")
        self.assertEqual((low[0], low[95], low[96], low[-1]), (900.0, 995.0, -1.0, 851.0))
        self.assertEqual((high[0], high[95], high[96], high[-1]), (1100.0, 1195.0, 13.0, 865.0))
        self.assertEqual([call[:3] for call in stats.calls[:2]],
                         [("5minute", grid(start, 300), END), ("hour", first_hour, kept_from)])

    def test_total_increasing_sensor_graphs_its_state(self):
        first = grid(END - timedelta(hours=24), 300)
        short = [period(first + timedelta(minutes=5 * k), 5, state=100 + k / 10, sum=1000.0 * k)
                 for k in range(288)]
        stats = FakeStatistics(SUM_META, short=short)
        values = {stat: self.fetch(stats, FakeRecorder([]), 24, 60, stat, state_unit="kWh")[0]
                  for stat in ("mean", "min", "max", "last")}
        # Each period carries the meter reading at its end; an hour holds twelve.
        self.assertEqual((values["min"][0], values["last"][0], values["max"][0]), (100.0, 101.1, 101.1))
        self.assertEqual(values["mean"][1], 101.75)
        self.assertEqual(values["last"][23], 128.7)
        # The reading, never the statistics sum that starts at zero.
        self.assertEqual({frozenset(call[3]) for call in stats.calls}, {frozenset({"state"})})

    def test_state_rows_follow_where_the_statistics_end(self):
        start = END - timedelta(hours=24)
        first = grid(start, 300)
        # The sensor lost its state_class ten hours into the graph.
        short = [period(first + timedelta(minutes=5 * k), 5, mean=50.0, min=50.0, max=50.0) for k in range(120)]
        stats_end = first + timedelta(hours=10)
        # A change in the last bucket that has statistics, then one every 10 minutes.
        rows = [state_at(stats_end + timedelta(minutes=1), "9999")]
        rows += [state_at(stats_end + timedelta(minutes=10 * n + 5), str(n)) for n in range(1, 84)]
        values, read, complete = self.fetch(FakeStatistics(MEAN_META, short=short), FakeRecorder(rows),
                                            24, 60, "mean")
        self.assertEqual(values[:10], [50.0] * 10)
        reference = NUMERIC.NumericBuckets(start, 24, 60)
        for item in rows[1:]:
            reference.add(item)
        self.assertEqual(values[10:], reference.values("mean")[10:])
        self.assertNotIn(None, values)
        self.assertEqual((read, complete), (84, True))

    def test_state_rows_where_statistics_do_not_fit(self):
        start = END - timedelta(hours=1)
        rows = [state_at(start + timedelta(minutes=m, seconds=30), str(m)) for m in range(60)]
        short = [period(grid(start, 300) + timedelta(minutes=5 * k), 5, mean=12345, min=12345, max=12345)
                 for k in range(12)]
        cases = (
            ("last of a 5-minute mean", MEAN_META, "last", "W"),
            ("circular mean", dict(MEAN_META, mean_type=2), "mean", "W"),
            ("no statistics", None, "mean", "W"),
            ("statistics in another unit", MEAN_META, "mean", "kW"),
        )
        for name, metadata, stat, unit in cases:
            with self.subTest(name):
                stats = FakeStatistics(metadata, short=short)
                values, read, complete = NUMERIC.fetch_numeric_history_values(
                    None, ENTITY, start, END, 12, 5, stat,
                    state_changes_during_period=FakeRecorder(rows), **stats.kwargs(unit))
                self.assertEqual((stats.metadata_calls, stats.calls), (1, []))
                self.assertEqual((read, complete), (60, True))
                self.assertNotIn(12345.0, values)
                self.assertEqual(values[1], 7.0 if stat == "mean" else 9.0)

    def test_buckets_under_5_minutes_read_state_rows(self):
        start = END - timedelta(hours=1)
        rows = [state_at(start + timedelta(minutes=m, seconds=30), str(m)) for m in range(60)]
        stats = FakeStatistics(MEAN_META, short=[period(grid(start, 300), 5, mean=12345, min=1, max=1)])
        values, read, complete = NUMERIC.fetch_numeric_history_values(
            None, ENTITY, start, END, 60, 1, "mean",
            state_changes_during_period=FakeRecorder(rows), **stats.kwargs("W"))
        self.assertEqual(values, [float(m) for m in range(60)])
        self.assertEqual((read, complete), (60, True))
        # Statistics are not even looked up.
        self.assertEqual((stats.metadata_calls, stats.calls), (0, []))

    def test_metadata_before_mean_type_still_counts(self):
        legacy = {key: value for key, value in MEAN_META.items() if key != "mean_type"}
        first = grid(END - timedelta(hours=1), 300)
        short = [period(first + timedelta(minutes=5 * k), 5, mean=k, min=k, max=k) for k in range(12)]
        values, _, _ = self.fetch(FakeStatistics(dict(legacy, has_mean=True), short=short),
                                  FakeRecorder([]), 1, 5, "mean")
        self.assertEqual(values, [float(k) for k in range(12)])

    def test_failing_statistics_fall_back_to_state_rows(self):
        start = END - timedelta(hours=1)
        rows = [state_at(start + timedelta(minutes=m, seconds=30), str(m)) for m in range(60)]

        def broken(hass, **kwargs):
            raise RuntimeError("database is locked")

        stats = FakeStatistics(MEAN_META)
        with self.assertLogs("numeric_history", "DEBUG"):
            values, read, complete = NUMERIC.fetch_numeric_history_values(
                None, ENTITY, start, END, 12, 5, "mean", state_changes_during_period=FakeRecorder(rows),
                statistics_during_period=stats, get_metadata=broken,
                get_display_unit=stats.get_display_unit, state_unit="W")
        self.assertEqual((values[0], read, complete), (2.0, 60, True))


class StateRowsTest(unittest.TestCase):
    def fetch(self, recorder, stat, **kwargs):
        return NUMERIC.fetch_numeric_history_values(
            None, "sensor.power", START, at(600), 10, 60, stat,
            state_changes_during_period=recorder, **kwargs)

    def test_reads_ascending_pages_newest_range_first(self):
        rows = [row(minute + 0.5, str(minute)) for minute in range(600)]
        recorder = FakeRecorder(rows)
        values, read, complete = self.fetch(recorder, "max", page_size=100, max_rows=250)
        self.assertEqual(read, 250)
        self.assertFalse(complete)
        # Hours 9, 8 and 7 are complete. Hours 3 to 6 hit the cap after their
        # oldest 70 rows and are emptied again, so no hour is half drawn.
        self.assertEqual(values, [None] * 7 + [479.0, 539.0, 599.0])
        # The newest hour is read first.
        self.assertEqual(recorder.calls[0]["start"], at(540) - timedelta(microseconds=1))
        self.assertEqual(len(recorder.calls), 4)
        for call in recorder.calls:
            self.assertTrue(call["no_attributes"])
            self.assertFalse(call["descending"])
            self.assertLessEqual(call["limit"], 100)

        values, read, complete = self.fetch(FakeRecorder(rows), "mean", page_size=250, max_rows=5000)
        self.assertTrue(complete)
        self.assertEqual(read, 600)
        self.assertEqual(values[0], 29.5)
        self.assertEqual(values[-1], 569.5)

    def test_pv_sensor_stays_complete_for_a_week(self):
        # About 4,100 changes a day for 14 hours of daylight: 29,001 in 7 days.
        start = END - timedelta(hours=168)
        before = state_at(start - timedelta(hours=1), "0")
        rows = [before]
        for day in range(7):
            daylight = start + timedelta(days=day, hours=1)
            rows += [state_at(daylight + timedelta(seconds=n * 12.16), str(1000 + (n % 700) * 3.5))
                     for n in range(4143)]
        recorder = FakeRecorder(rows)
        values, read, complete = NUMERIC.fetch_numeric_history_values(
            None, ENTITY, start, END, 168, 60, "mean", state_changes_during_period=recorder)
        self.assertTrue(complete)
        self.assertEqual(read, 29001)
        self.assertGreater(read, 20160)  # The earlier cap left two of the seven days empty.
        reference = NUMERIC.NumericBuckets(start, 168, 60)
        reference.add(state_at(start, before.state))
        for item in rows[1:]:
            reference.add(item)
        self.assertEqual(values, reference.values("mean"))
        self.assertLessEqual(max(call["limit"] for call in recorder.calls), NUMERIC.NUMERIC_HISTORY_PAGE_SIZE)
        self.assertLess(len(recorder.calls), 30)

        # The last day in 5-minute buckets is complete as well.
        day_start = END - timedelta(hours=24)
        values, read, complete = NUMERIC.fetch_numeric_history_values(
            None, ENTITY, day_start, END, 288, 5, "mean", state_changes_during_period=FakeRecorder(rows))
        reference = NUMERIC.NumericBuckets(day_start, 288, 5)
        newest_before = [item for item in rows if item.last_updated < day_start][-1]
        reference.add(state_at(day_start, newest_before.state))
        in_day = [item for item in rows if day_start < item.last_updated < END]
        for item in in_day:
            reference.add(item)
        self.assertEqual((values, read, complete), (reference.values("mean"), len(in_day), True))

    def test_state_at_the_start_fills_the_first_bucket(self):
        start = END - timedelta(hours=24)
        rows = [state_at(start - timedelta(hours=10), "5"), state_at(start + timedelta(hours=20, minutes=30), "10")]
        recorder = FakeRecorder(rows)
        values, read, complete = NUMERIC.fetch_numeric_history_values(
            None, ENTITY, start, END, 24, 60, "mean", state_changes_during_period=recorder)
        # As before the paging: the panel carries 5 forward until the change to 10.
        self.assertEqual(values, [5.0] + [None] * 19 + [10.0] + [None] * 3)
        self.assertEqual((read, complete), (1, True))
        self.assertEqual([call["include_start_time_state"] for call in recorder.calls].count(True), 1)
        self.assertEqual(recorder.calls[-1]["start"], start)

    def test_change_on_a_range_boundary_is_read_once(self):
        # Every hour and half hour; hours 3, 7 and 9 start the ranges.
        rows = [row(minute, str(minute)) for minute in range(30, 600, 30)]
        values, read, complete = self.fetch(FakeRecorder(rows), "mean", page_size=100, max_rows=1000)
        self.assertEqual((read, complete), (19, True))
        self.assertEqual(values, [30.0] + [60.0 * hour + 15 for hour in range(1, 10)])

    def test_recorder_without_paging_falls_back_to_a_bounded_tail(self):
        def old_api(hass, start, end, entity_id, **kwargs):
            raise TypeError("unexpected keyword argument 'limit'")

        tail_calls = []

        def last_changes(hass, limit, entity_id):
            tail_calls.append(limit)
            return {entity_id: [row(599, "5")]}

        values, read, complete = self.fetch(
            old_api, "mean", get_last_state_changes=last_changes, page_size=100, max_rows=1000)
        self.assertEqual((values[-1], read, complete, tail_calls), (5.0, 1, False, [100]))


def history_handler(scope):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_handle_history_request"]
    nodes += [node for node in tree.body if getattr(node, "name", None) in {"_try_parse_json", "_coerce_int"}]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *nodes], type_ignores=[])
    namespace = dict(scope)
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), namespace)
    return namespace["_async_handle_history_request"]


class HistoryReplyTest(unittest.IsolatedAsyncioTestCase):
    """The panel gets the reply it always got, whatever the Bridge read."""

    TOPIC = "tab5_lvgl/config/A1B2C3D4E5F6/history/response"
    KEYS = ["entity_id", "hours", "period_minutes", "stat", "values", "unit", "name", "current"]

    def setUp(self):
        first = grid(END - timedelta(hours=168), 300)
        self.stats = FakeStatistics(MEAN_META, short=[
            period(first + timedelta(minutes=5 * k), 5, mean=k, min=k, max=k) for k in range(2016)])
        self.recorder = FakeRecorder([])
        self.published = []
        self.states = {ENTITY: types.SimpleNamespace(state="1234.5", name="PV DC1",
                                                     attributes={"unit_of_measurement": "W"})}

        async def publish(hass, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload, qos, retain))

        class Recorder:
            async def async_add_executor_job(self, target, *args):
                return target(*args)

        self.handler = history_handler({
            "json": json, "_LOGGER": logging.getLogger("test_numeric_history"),
            "STATE_HISTORY_KIND": "state", "BINARY_HISTORY_KIND": "binary",
            "timedelta": timedelta, "dt_util": types.SimpleNamespace(utcnow=lambda: END),
            "get_instance": lambda hass: Recorder(), "mqtt": types.SimpleNamespace(async_publish=publish),
            "fetch_numeric_history_values": NUMERIC.fetch_numeric_history_values,
            "state_changes_during_period": self.recorder, "get_last_state_changes": None,
            "statistics_during_period": self.stats, "get_statistics_metadata": self.stats.get_metadata,
            "get_statistics_display_unit": self.stats.get_display_unit,
        })
        self.bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(states=types.SimpleNamespace(get=self.states.get)),
            history_response_topic=self.TOPIC, sensors=[ENTITY], binary_sensors=[])

    async def ask(self, payload):
        self.published.clear()
        await self.handler(self.bridge, types.SimpleNamespace(payload=payload, retain=False))
        [(topic, text, qos, retain)] = self.published
        self.assertEqual((topic, qos, retain), (self.TOPIC, 0, False))
        reply = json.loads(text)
        self.assertEqual(list(reply), self.KEYS)
        return reply

    async def test_request_of_the_oldest_firmware(self):
        # Word for word what v0.1.0 sends.
        reply = await self.ask('{"entity_id":"sensor.power","hours":24,"period_minutes":5,"points":288,"stat":"mean"}')
        self.assertEqual({key: reply[key] for key in self.KEYS if key != "values"}, {
            "entity_id": ENTITY, "hours": 24, "period_minutes": 5, "stat": "mean",
            "unit": "W", "name": "PV DC1", "current": "1234.5"})
        self.assertEqual(reply["values"], [1728.0 + bucket for bucket in range(288)])

    async def test_minimal_request_gets_the_defaults(self):
        reply = await self.ask('{"entity_id":"sensor.power","hours":24}')
        self.assertEqual((reply["period_minutes"], reply["stat"], len(reply["values"])), (5, "mean", 288))

    async def test_seven_day_request_of_the_popup(self):
        reply = await self.ask('{"entity_id":"sensor.power","hours":168,"period_minutes":60,"points":168,"stat":"mean"}')
        self.assertEqual(reply["values"], [12 * bucket + 5.5 for bucket in range(168)])

    async def test_sensor_without_history_reports_its_current_value(self):
        self.stats.metadata = None
        reply = await self.ask('{"entity_id":"sensor.power","hours":24,"period_minutes":5,"points":288,"stat":"mean"}')
        self.assertEqual(reply["values"], [None] * 287 + [1234.5])


if __name__ == "__main__":
    unittest.main()
