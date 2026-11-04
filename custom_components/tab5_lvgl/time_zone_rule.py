"""Home Assistant's time zone as a POSIX rule for the panels.

A panel has no time zone database: its C library takes a POSIX TZ rule such as
"STD9DST,M3.2.0/2,M11.1.0/2". The rule is worked out from the offsets the zone
really has over the coming year, through the public datetime API only, so it
does not depend on how this Home Assistant stores its zone data. The panel gets
its time from NTP as before and only takes the zone from here (issue #145).

The names STD and DST are placeholders the C library needs: a panel never shows
them. A zone whose changes do not fit one rule (Ramadan shifts, a zone giving up
summer time) gets today's offset; the panel's regular refresh publishes the
configuration again when that changes.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone, tzinfo

_DAY_S = 86400
# More than a year: a change on "the first Sunday" can come 371 days after the
# last one, and the rule needs both changes of a year whatever the date today.
_SCAN_DAYS = 400
# A third change sooner than this is a zone with more than two changes a year.
_NEXT_YEAR_S = 350 * _DAY_S


def _offset_at(zone: tzinfo, stamp: int) -> int:
  return int(datetime.fromtimestamp(stamp, zone).utcoffset().total_seconds())


def _coming_changes(zone: tzinfo, start: int) -> list[tuple[int, int, int]]:
  """The zone's offset changes over the coming year: (instant, before, after)."""
  changes: list[tuple[int, int, int]] = []
  low = start
  before = _offset_at(zone, low)
  for _ in range(_SCAN_DAYS):
    high = low + _DAY_S
    after = _offset_at(zone, high)
    if after != before:
      first, last = low, high
      while last - first > 1:
        middle = (first + last) // 2
        if _offset_at(zone, middle) == before:
          first = middle
        else:
          last = middle
      changes.append((last, before, after))
    low, before = high, after
  return changes


def _clock(seconds: int) -> str:
  hours, rest = divmod(seconds, 3600)
  minutes, secs = divmod(rest, 60)
  if secs:
    return f"{hours}:{minutes:02d}:{secs:02d}"
  if minutes:
    return f"{hours}:{minutes:02d}"
  return str(hours)


def _posix_offset(offset: int) -> str:
  """POSIX counts west of Greenwich: UTC-9 is 9, UTC+5:30 is -5:30."""
  return f"-{_clock(offset)}" if offset > 0 else _clock(-offset)


def _posix_date(instant: int, before: int) -> str:
  """A change as month, week and weekday, at its wall clock time before it."""
  wall = datetime.fromtimestamp(instant + before, timezone.utc)
  days_in_month = calendar.monthrange(wall.year, wall.month)[1]
  # Week 5 is the month's last such weekday, whatever its number.
  week = 5 if wall.day + 7 > days_in_month else (wall.day - 1) // 7 + 1
  weekday = (wall.weekday() + 1) % 7  # POSIX counts from Sunday
  seconds = wall.hour * 3600 + wall.minute * 60 + wall.second
  return f"M{wall.month}.{week}.{weekday}/{_clock(seconds)}"


def posix_rule(now: datetime) -> str | None:
  """The rule of an aware datetime's zone; None without a zone."""
  zone = now.tzinfo
  if zone is None:
    return None
  start = int(now.timestamp())
  changes = _coming_changes(zone, start)
  standing = f"STD{_posix_offset(_offset_at(zone, start))}"
  if len(changes) < 2 or (len(changes) > 2 and changes[2][0] - changes[0][0] < _NEXT_YEAR_S):
    return standing
  first, second = changes[:2]
  if first[1] != second[2] or first[2] != second[1]:
    return standing
  to_summer, to_standard = (first, second) if first[2] > first[1] else (second, first)
  standard, summer = to_summer[1], to_summer[2]
  # One hour ahead is what the rule assumes without a second offset.
  summer_text = "" if summer - standard == 3600 else _posix_offset(summer)
  return (
    f"STD{_posix_offset(standard)}DST{summer_text},"
    f"{_posix_date(to_summer[0], to_summer[1])},{_posix_date(to_standard[0], to_standard[1])}"
  )
