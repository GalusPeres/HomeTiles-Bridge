"""Bounded Recorder reads for the numeric Sensor graph.

The graph asks for up to 168 hours in buckets. A sensor that reports every
second would otherwise load 600,000 full state rows at once. The Recorder is
read newest first in pages of ``page_size`` rows, each page is folded into
the buckets and dropped, and at most ``max_rows`` rows are read. When that
cap is reached, the oldest buckets stay empty; the recent part of the graph,
which the popup shows first, is always complete.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Callable, List, Mapping, Optional, Tuple

NUMERIC_HISTORY_PAGE_SIZE = 1000
# One change every 30 seconds for 7 days.
NUMERIC_HISTORY_MAX_ROWS = 20160
NUMERIC_HISTORY_STATS = frozenset({"mean", "min", "max", "last"})


def _row_time(row: Any) -> Optional[datetime]:
  if isinstance(row, Mapping):
    value = row.get("last_updated") or row.get("last_changed")
  else:
    value = getattr(row, "last_updated", None) or getattr(row, "last_changed", None)
  return value if isinstance(value, datetime) else None


def _row_value(row: Any) -> Optional[float]:
  raw = row.get("state") if isinstance(row, Mapping) else getattr(row, "state", None)
  if raw is None or isinstance(raw, bool):
    return None
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return None
  return value if math.isfinite(value) else None


class NumericBuckets:
  """Folds state rows into ``points`` buckets of ``period_minutes``."""

  def __init__(self, start: datetime, points: int, period_minutes: int) -> None:
    self.start = start
    self.points = max(points, 0)
    self.bucket_seconds = max(period_minutes, 1) * 60
    self.sums = [0.0] * self.points
    self.counts = [0] * self.points
    self.mins: List[Optional[float]] = [None] * self.points
    self.maxs: List[Optional[float]] = [None] * self.points
    self.lasts: List[Optional[Tuple[datetime, float]]] = [None] * self.points

  def add(self, row: Any) -> Optional[datetime]:
    """Add one row; returns its time, or None when it has no usable time."""
    moment = _row_time(row)
    if moment is None or not self.points:
      return moment
    # floor, not int(): a row just before the start must not land in bucket 0.
    index = math.floor((moment - self.start).total_seconds() / self.bucket_seconds)
    if index < 0:
      return moment
    index = min(index, self.points - 1)
    value = _row_value(row)
    if value is None:
      return moment
    self.counts[index] += 1
    self.sums[index] += value
    if self.mins[index] is None or value < self.mins[index]:
      self.mins[index] = value
    if self.maxs[index] is None or value > self.maxs[index]:
      self.maxs[index] = value
    last = self.lasts[index]
    if last is None or moment >= last[0]:
      self.lasts[index] = (moment, value)
    return moment

  def values(self, stat: str) -> List[Optional[float]]:
    result: List[Optional[float]] = []
    for index in range(self.points):
      value: Optional[float] = None
      if self.counts[index]:
        if stat == "min":
          value = self.mins[index]
        elif stat == "max":
          value = self.maxs[index]
        elif stat == "last":
          value = self.lasts[index][1] if self.lasts[index] else None
        else:
          value = self.sums[index] / self.counts[index]
      result.append(round(value, 3) if value is not None else None)
    return result


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
  page_size: int = NUMERIC_HISTORY_PAGE_SIZE,
  max_rows: int = NUMERIC_HISTORY_MAX_ROWS,
) -> Tuple[List[Optional[float]], int, bool]:
  """Return (bucket values, rows read, whether the whole period was read)."""
  buckets = NumericBuckets(start, points, period_minutes)
  stat = stat if stat in NUMERIC_HISTORY_STATS else "mean"
  if buckets.points == 0:
    return [], 0, True

  rows_read = 0
  if state_changes_during_period is None:
    if get_last_state_changes is not None:
      recent = get_last_state_changes(hass, min(page_size, max_rows), entity_id)
      for row in (recent or {}).get(entity_id, [])[:max_rows]:
        buckets.add(row)
        rows_read += 1
    return buckets.values(stat), rows_read, False

  cursor = end
  complete = False
  while rows_read < max_rows:
    limit = min(page_size, max_rows - rows_read)
    try:
      page = state_changes_during_period(
        hass,
        start,
        cursor,
        entity_id,
        no_attributes=True,
        descending=True,
        limit=limit,
        include_start_time_state=False,
      )
    except TypeError:
      # A Recorder without paged queries: keep a bounded recent tail only.
      if rows_read == 0 and get_last_state_changes is not None:
        recent = get_last_state_changes(hass, min(page_size, max_rows), entity_id)
        for row in (recent or {}).get(entity_id, [])[:max_rows]:
          buckets.add(row)
          rows_read += 1
      break
    rows = list((page or {}).get(entity_id, []))[:limit]
    oldest: Optional[datetime] = None
    for row in rows:
      moment = buckets.add(row)
      if moment is not None and (oldest is None or moment < oldest):
        oldest = moment
    rows_read += len(rows)
    if len(rows) < limit:
      complete = True
      break
    if oldest is None or oldest >= cursor or oldest <= start:
      complete = oldest is not None and oldest <= start
      break
    cursor = oldest
  return buckets.values(stat), rows_read, complete
