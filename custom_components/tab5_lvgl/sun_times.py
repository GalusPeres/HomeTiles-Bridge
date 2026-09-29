"""Local sunrise and sunset per day for the weather icons on the panel.

Home Assistant reports conditions such as "partlycloudy" at night too. With
the sun times of each day the panel shows the moon variant of an icon at
night. The weather payload carries them as a top-level field:

  "sun":[{"d":"2026-09-29","r":432,"s":1145},{"d":"2026-12-21","up":false}]

d is the local date (Home Assistant's time zone), r and s are sunrise and
sunset in minutes after local midnight (0 <= r < s <= 1440). A date without
sunrise and sunset says instead whether the sun stays up ("up"). The panel
treats a date without an entry as day. Nothing here performs I/O.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

# The names of Home Assistant's SUN_EVENT_SUNRISE and SUN_EVENT_SUNSET.
SUNRISE = "sunrise"
SUNSET = "sunset"
MAX_DAYS = 8
DAY_MINUTES = 1440


def sun_days(today: date, forecast_days: Iterable[Any]) -> List[date]:
  """Local dates from today through the last forecast day, at most MAX_DAYS."""
  last = today
  for value in forecast_days:
    try:
      day = date.fromisoformat(value)
    except (TypeError, ValueError):
      continue
    last = max(last, day)
  count = min((last - today).days + 1, MAX_DAYS)
  return [today + timedelta(days=offset) for offset in range(count)]


def _minute(moment: datetime) -> int:
  return moment.hour * 60 + moment.minute


def _polar_day(latitude: float, day: date) -> bool:
  """Without sunrise and sunset, the sun stays up on the hemisphere it faces."""
  # Approximate solar declination; a polar day or night only needs its sign.
  declination = -23.44 * math.cos(math.radians(360 / 365 * (day.timetuple().tm_yday + 10)))
  return latitude * declination > 0


def sun_entries(
  days: List[date],
  event: Callable[[str, date], Optional[datetime]],
  latitude: float,
) -> List[Dict[str, Any]]:
  """One entry per day from ``event(kind, date)``, which returns a local time.

  Home Assistant computes the event of a UTC date, which can fall on the
  local date before or after. The neighbouring dates are asked too and every
  event counts for its local date.
  """
  if not days:
    return []
  found: Dict[tuple, List[datetime]] = {}
  for offset in range(-1, len(days) + 1):
    asked = days[0] + timedelta(days=offset)
    for kind in (SUNRISE, SUNSET):
      moment = event(kind, asked)
      if moment is not None:
        found.setdefault((kind, moment.date()), []).append(moment)

  entries: List[Dict[str, Any]] = []
  for day in days:
    rises = found.get((SUNRISE, day))
    sets = found.get((SUNSET, day))
    entry: Dict[str, Any] = {"d": day.isoformat()}
    if not rises and not sets:
      entry["up"] = _polar_day(latitude, day)
    else:
      # Without a sunset the sun is still up at midnight, without a sunrise
      # it was already up when the date began.
      rise = _minute(min(rises)) if rises else 0
      set_ = _minute(max(sets)) if sets else DAY_MINUTES
      if rise < set_:
        entry["r"] = rise
        entry["s"] = set_
      else:
        # The sun sets after midnight and rises again the same date; the
        # short night between cannot be told with one sunrise and sunset.
        entry["up"] = True
    entries.append(entry)
  return entries
