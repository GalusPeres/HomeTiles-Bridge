"""Numeric Sensor graph values from Recorder statistics or state rows.

The panel asks for up to 168 hours in ``points`` buckets of ``period_minutes``
and plots the mean, min, max or last value of each bucket; it fills an empty
bucket with the value before it.

Buckets of 5 minutes or more come from the Recorder statistics when the sensor
has them: 5-minute statistics while the Recorder keeps them
(``purge_keep_days``), hourly statistics for older periods, and state rows after
the newest compiled period. A measurement sensor's statistics carry the mean,
min and max of each period. A ``total`` or ``total_increasing`` sensor's
statistics carry its state at the end of each period, the value its graph
shows; their ``sum`` starts at zero and is not used.

Everything else reads state changes without attributes. The Recorder sorts a
limited query oldest first, so the rows are read in ascending pages of
``page_size`` rows, in time ranges of 1, 2, 4, ... buckets from the newest
backwards, and at most ``max_rows`` rows. When that cap is reached, the older
buckets stay empty; the recent part of the graph, which the popup shows first,
is always complete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

NUMERIC_HISTORY_PAGE_SIZE = 5000
# One change every 10 seconds for 7 days. A PV inverter sensor with about
# 29,000 changes a week stays complete.
NUMERIC_HISTORY_MAX_ROWS = 60480
NUMERIC_HISTORY_STATS = frozenset({"mean", "min", "max", "last"})
# The shortest period the Recorder compiles statistics for.
STATISTICS_MIN_PERIOD_MINUTES = 5
SHORT_TERM_SECONDS = 300
HOUR_SECONDS = 3600
ONE_MICROSECOND = timedelta(microseconds=1)
# StatisticMeanType.ARITHMETIC. A circular mean (wind direction) keeps the
# arithmetic mean of state rows the graph always used.
ARITHMETIC_MEAN = 1


def _finite(raw: Any) -> Optional[float]:
  if raw is None or isinstance(raw, bool):
    return None
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return None
  return value if math.isfinite(value) else None


def _row_time(row: Any) -> Optional[datetime]:
  if isinstance(row, Mapping):
    value = row.get("last_updated") or row.get("last_changed")
  else:
    value = getattr(row, "last_updated", None) or getattr(row, "last_changed", None)
  return value if isinstance(value, datetime) else None


def _row_value(row: Any) -> Optional[float]:
  return _finite(row.get("state") if isinstance(row, Mapping) else getattr(row, "state", None))


def _stat_time(raw: Any) -> Optional[datetime]:
  # Statistics rows carry UTC timestamps.
  if isinstance(raw, datetime):
    return raw
  if isinstance(raw, (int, float)) and not isinstance(raw, bool):
    return datetime.fromtimestamp(raw, tz=timezone.utc)
  return None


def _floor(moment: datetime, seconds: int) -> datetime:
  return datetime.fromtimestamp(math.floor(moment.timestamp() / seconds) * seconds, tz=timezone.utc)


class NumericBuckets:
  """Folds values into ``points`` buckets of ``period_minutes``."""

  def __init__(self, start: datetime, points: int, period_minutes: int) -> None:
    self.start = start
    self.points = max(points, 0)
    self.bucket_seconds = max(period_minutes, 1) * 60
    self.sums = [0.0] * self.points
    self.weights = [0.0] * self.points
    self.mins: List[Optional[float]] = [None] * self.points
    self.maxs: List[Optional[float]] = [None] * self.points
    self.lasts: List[Optional[Tuple[datetime, float]]] = [None] * self.points

  def index(self, moment: datetime) -> Optional[int]:
    """Bucket of a moment: None before the start, the last one after the end."""
    if not self.points:
      return None
    # floor, not int(): a moment just before the start must not land in bucket 0.
    index = math.floor((moment - self.start).total_seconds() / self.bucket_seconds)
    if index < 0:
      return None
    return min(index, self.points - 1)

  def indices(self, period_start: datetime, period_end: datetime) -> range:
    """Buckets a statistics period belongs to.

    A period no longer than a bucket goes to the bucket of its middle, so
    5-minute statistics fill 5-minute buckets one to one. A longer period (an
    hour in 5-minute buckets) goes to every bucket whose middle it covers.
    """
    if (period_end - period_start).total_seconds() <= self.bucket_seconds:
      index = self.index(period_start + (period_end - period_start) / 2)
      return range(0) if index is None else range(index, index + 1)
    first = math.ceil((period_start - self.start).total_seconds() / self.bucket_seconds - 0.5)
    last = math.ceil((period_end - self.start).total_seconds() / self.bucket_seconds - 0.5) - 1
    return range(max(first, 0), min(last, self.points - 1) + 1)

  def bucket_start(self, index: int) -> datetime:
    return self.start + timedelta(seconds=index * self.bucket_seconds)

  def filled(self, index: int) -> bool:
    return self.weights[index] > 0

  def put(
    self,
    index: int,
    moment: datetime,
    value: float,
    low: Optional[float] = None,
    high: Optional[float] = None,
    weight: float = 1.0,
  ) -> None:
    self.sums[index] += value * weight
    self.weights[index] += weight
    low = value if low is None else low
    high = value if high is None else high
    if self.mins[index] is None or low < self.mins[index]:
      self.mins[index] = low
    if self.maxs[index] is None or high > self.maxs[index]:
      self.maxs[index] = high
    last = self.lasts[index]
    if last is None or moment >= last[0]:
      self.lasts[index] = (moment, value)

  def add(self, row: Any) -> Optional[datetime]:
    """Add one state row; returns its time, or None when it has no usable time."""
    moment = _row_time(row)
    if moment is None:
      return None
    index = self.index(moment)
    value = _row_value(row)
    if index is not None and value is not None:
      self.put(index, moment, value)
    return moment

  def clear(self, first: int, last: int) -> None:
    for index in range(first, last + 1):
      self.sums[index] = 0.0
      self.weights[index] = 0.0
      self.mins[index] = self.maxs[index] = self.lasts[index] = None

  def fill_gaps_from(self, other: NumericBuckets) -> None:
    """Take every bucket this one has no value for from ``other``."""
    for index in range(self.points):
      if not self.filled(index) and other.filled(index):
        self.sums[index] = other.sums[index]
        self.weights[index] = other.weights[index]
        self.mins[index] = other.mins[index]
        self.maxs[index] = other.maxs[index]
        self.lasts[index] = other.lasts[index]

  def values(self, stat: str) -> List[Optional[float]]:
    result: List[Optional[float]] = []
    for index in range(self.points):
      value: Optional[float] = None
      if self.filled(index):
        if stat == "min":
          value = self.mins[index]
        elif stat == "max":
          value = self.maxs[index]
        elif stat == "last":
          value = self.lasts[index][1] if self.lasts[index] else None
        else:
          value = self.sums[index] / self.weights[index]
      result.append(round(value, 3) if value is not None else None)
    return result


def statistics_kind(metadata: Mapping[str, Any]) -> Optional[str]:
  """``"mean"`` or ``"state"`` for statistics the graph can use, else None."""
  mean_type = metadata.get("mean_type")
  if mean_type is None:
    has_mean = bool(metadata.get("has_mean"))
  else:
    has_mean = mean_type == ARITHMETIC_MEAN
  if has_mean:
    return "mean"
  if metadata.get("has_sum"):
    return "state"
  return None


def _usable_statistics(
  hass: Any,
  entity_id: str,
  period_minutes: int,
  stat: str,
  state_unit: Optional[str],
  get_metadata: Optional[Callable[..., Any]],
  get_display_unit: Optional[Callable[..., Any]],
) -> Optional[str]:
  if period_minutes < STATISTICS_MIN_PERIOD_MINUTES or get_metadata is None or get_display_unit is None:
    return None
  found = (get_metadata(hass, statistic_ids={entity_id}) or {}).get(entity_id)
  if not found:
    return None
  metadata = found[1]
  kind = statistics_kind(metadata)
  # A 5-minute mean has no last value; state rows keep "last" exact.
  if kind is None or (kind == "mean" and stat == "last"):
    return None
  # Statistics shown in another unit than the state would rescale the graph.
  unit = get_display_unit(
    hass, entity_id, metadata.get("unit_class"), metadata.get("unit_of_measurement")
  )
  return kind if unit == state_unit else None


def _add_statistics_rows(
  buckets: NumericBuckets,
  rows: List[Mapping[str, Any]],
  kind: str,
  until: Optional[datetime] = None,
) -> Tuple[Optional[datetime], Optional[datetime]]:
  """Fold statistics rows in; returns (start of the oldest, end of the newest)."""
  oldest: Optional[datetime] = None
  newest: Optional[datetime] = None
  for row in rows:
    period_start = _stat_time(row.get("start"))
    period_end = _stat_time(row.get("end"))
    if period_start is None or period_end is None or period_end <= period_start:
      continue
    if until is not None and period_end > until:
      continue
    if kind == "state":
      value, low, high = _finite(row.get("state")), None, None
    else:
      value, low, high = _finite(row.get("mean")), _finite(row.get("min")), _finite(row.get("max"))
    if value is None:
      continue
    weight = (period_end - period_start).total_seconds()
    for index in buckets.indices(period_start, period_end):
      buckets.put(index, period_end, value, low, high, weight)
    if oldest is None or period_start < oldest:
      oldest = period_start
    if newest is None or period_end > newest:
      newest = period_end
  return oldest, newest


def _read_statistics(
  hass: Any,
  entity_id: str,
  buckets: NumericBuckets,
  end: datetime,
  kind: str,
  statistics_during_period: Callable[..., Any],
) -> Optional[datetime]:
  """Fold the statistics in; returns the end of the newest period, if any."""
  types = {"state"} if kind == "state" else {"mean", "min", "max"}
  # Periods start on the 5-minute grid; the one around the graph's start
  # belongs to bucket 0 when its middle is inside the graph.
  short_start = _floor(buckets.start, SHORT_TERM_SECONDS)
  short = statistics_during_period(hass, short_start, end, {entity_id}, "5minute", None, set(types))
  oldest, newest = _add_statistics_rows(buckets, (short or {}).get(entity_id, []), kind)
  covered_from = oldest or end
  if covered_from > short_start:
    # The Recorder purges 5-minute statistics after purge_keep_days and keeps
    # the hourly ones; they fill the older part up to the 5-minute ones.
    hourly = statistics_during_period(
      hass, _floor(buckets.start, HOUR_SECONDS), covered_from, {entity_id}, "hour", None, set(types)
    )
    _, hourly_newest = _add_statistics_rows(
      buckets, (hourly or {}).get(entity_id, []), kind, until=covered_from
    )
    newest = newest or hourly_newest
  return newest


class UnpagedRecorder(Exception):
  """The Recorder does not accept the paging arguments."""


def _ranges_newest_first(first: int, last: int) -> Iterator[Tuple[int, int]]:
  """Bucket ranges of 1, 2, 4, ... buckets from ``last`` back to ``first``."""
  high, size = last, 1
  while high >= first:
    low = max(first, high - size + 1)
    yield low, high
    high, size = low - 1, size * 2


def _read_state_rows(
  hass: Any,
  entity_id: str,
  buckets: NumericBuckets,
  since: datetime,
  end: datetime,
  *,
  state_changes_during_period: Callable[..., Any],
  page_size: int,
  max_rows: int,
  with_start_state: bool,
) -> Tuple[int, bool]:
  """Fold the state changes after ``since`` in, newest range first.

  Returns (changes read, whether every change up to ``end`` was read). A range
  cut off by ``max_rows`` is emptied again, so only complete ranges remain.
  """
  first = buckets.index(since) or 0
  last = buckets.points - 1
  rows_read = 0
  for low, high in _ranges_newest_first(first, last):
    range_end = end if high == last else buckets.bucket_start(high + 1)
    # The Recorder reads strictly after the cursor and before the end, so a
    # range starts 1 us before its first bucket: a change exactly on the
    # boundary is read once, by the range of its bucket.
    cursor = since if low == first else buckets.bucket_start(low) - ONE_MICROSECOND
    while True:
      limit = min(page_size, max_rows - rows_read)
      if limit <= 0:
        buckets.clear(low, high)
        return rows_read, False
      try:
        page = state_changes_during_period(
          hass,
          cursor,
          range_end,
          entity_id,
          no_attributes=True,
          limit=limit,
          # The state at the graph's start fills bucket 0, as it always did.
          include_start_time_state=with_start_state and cursor == buckets.start,
        )
      except TypeError as err:
        raise UnpagedRecorder from err
      changes = 0
      newest: Optional[datetime] = None
      for row in (page or {}).get(entity_id, []):
        moment = buckets.add(row)
        # The start state carries the cursor's time and is no change.
        if moment is not None and moment > cursor:
          changes += 1
          if newest is None or moment > newest:
            newest = moment
      rows_read += changes
      if changes < limit or newest is None:
        break
      cursor = newest
  return rows_read, True


def fetch_numeric_history_values(
  hass: Any,
  entity_id: str,
  start: datetime,
  end: datetime,
  points: int,
  period_minutes: int,
  stat: str,
  *,
  state_changes_during_period: Optional[Callable[..., Any]],
  get_last_state_changes: Optional[Callable[..., Any]] = None,
  statistics_during_period: Optional[Callable[..., Any]] = None,
  get_metadata: Optional[Callable[..., Any]] = None,
  get_display_unit: Optional[Callable[..., Any]] = None,
  state_unit: Optional[str] = None,
  page_size: int = NUMERIC_HISTORY_PAGE_SIZE,
  max_rows: int = NUMERIC_HISTORY_MAX_ROWS,
) -> Tuple[List[Optional[float]], int, bool]:
  """Return (bucket values, state changes read, whether the whole period was read)."""
  buckets = NumericBuckets(start, points, period_minutes)
  stat = stat if stat in NUMERIC_HISTORY_STATS else "mean"
  if buckets.points == 0:
    return [], 0, True

  def read_rows(target: NumericBuckets, since: datetime, with_start_state: bool) -> Tuple[int, bool]:
    return _read_state_rows(
      hass,
      entity_id,
      target,
      since,
      end,
      state_changes_during_period=state_changes_during_period,
      page_size=page_size,
      max_rows=max_rows,
      with_start_state=with_start_state,
    )

  stats_end: Optional[datetime] = None
  if statistics_during_period is not None:
    try:
      kind = _usable_statistics(
        hass, entity_id, period_minutes, stat, state_unit, get_metadata, get_display_unit
      )
      if kind:
        stats_end = _read_statistics(hass, entity_id, buckets, end, kind, statistics_during_period)
    except Exception:  # noqa: BLE001 - state rows still draw the graph
      _LOGGER.debug("HomeTiles statistics unavailable for %s", entity_id, exc_info=True)
      buckets = NumericBuckets(start, points, period_minutes)
      stats_end = None

  if stats_end is not None:
    if stats_end >= end or state_changes_during_period is None:
      return buckets.values(stat), 0, stats_end >= end
    # State rows after the newest compiled period fill the buckets that have
    # no statistics yet, or none any more when a sensor stopped having them.
    tail = NumericBuckets(start, points, period_minutes)
    try:
      rows_read, complete = read_rows(tail, stats_end, False)
    except UnpagedRecorder:
      rows_read, complete = 0, False
    buckets.fill_gaps_from(tail)
    return buckets.values(stat), rows_read, complete

  if state_changes_during_period is not None:
    try:
      rows_read, complete = read_rows(buckets, start, True)
      return buckets.values(stat), rows_read, complete
    except UnpagedRecorder:
      buckets = NumericBuckets(start, points, period_minutes)

  # A Recorder without paged queries: keep a bounded recent tail only.
  rows_read = 0
  if get_last_state_changes is not None:
    recent = get_last_state_changes(hass, min(page_size, max_rows), entity_id)
    for row in (recent or {}).get(entity_id, [])[:max_rows]:
      buckets.add(row)
      rows_read += 1
  return buckets.values(stat), rows_read, False
