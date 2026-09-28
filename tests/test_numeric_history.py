"""The numeric Sensor graph reads statistics or bounded state rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import types
import unittest

from test_view_navigation import load_module

NUMERIC = load_module("numeric_history")

START = datetime(2026, 9, 20, tzinfo=timezone.utc)


def at(minutes):
    return START + timedelta(minutes=minutes)


def row(minutes, value):
    return types.SimpleNamespace(state=value, last_updated=at(minutes), last_changed=at(minutes))


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
            selected.insert(0, types.SimpleNamespace(state=before[-1].state, last_updated=start_time,
                                                     last_changed=start_time))
        if descending:
            selected.reverse()
        return {entity_id: selected} if selected else {}


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


if __name__ == "__main__":
    unittest.main()
