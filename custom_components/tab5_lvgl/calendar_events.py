"""The retained `events` payload of a calendar entity for the Calendar tile.

Topic: ``{ha_prefix}/calendar/<object>/events`` (retained). The panel shows the
events of one Home Assistant calendar in a window around today:

  {"v":1,"state":"on","from":"2026-09-24","to":"2026-12-31",
   "e":[{"s":"2026-10-24","e":"2026-10-25","t":"Bio waste"},
        {"s":"2026-10-24T10:00","e":"2026-10-24T11:30","t":"Long ride","l":"Pool"}]}

Times are Home Assistant's local time without an offset; a start without a
time is an all-day event whose end date is exclusive, like Home Assistant's.
"from" and "to" are the days (inclusive) the list is complete for, so the
panel can tell an empty day from one it has no data for. The payload is
bounded (events, text bytes, total bytes); when it must drop events it drops
the latest ones and moves "to" back, so the panel never shows a dropped day
as free. Pure functions, no Home Assistant imports: tests run them directly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple

CALENDAR_DOMAIN = "calendar"
PAYLOAD_VERSION = 1
# How often the Bridge fetches the events again: edits of future events do
# not change the calendar's state, and the window moves at midnight.
REFRESH_S = 15 * 60
# Coalesces state changes and snapshots of the same moment.
DEBOUNCE_S = 2.0
MAX_EVENTS = 96
MAX_TITLE_BYTES = 63
MAX_LOCATION_BYTES = 47
MAX_PAYLOAD_BYTES = 16384
# The month grid starts up to six days before the 1st; the panel browses the
# current month and the two after it.
LEAD_DAYS = 7
MONTHS_AHEAD = 2


def fetch_window(today: date) -> Tuple[date, date]:
  """The days (inclusive) the Bridge asks Home Assistant for."""
  first = today.replace(day=1)
  start = first - timedelta(days=LEAD_DAYS)
  month_index = first.year * 12 + first.month - 1 + MONTHS_AHEAD + 1
  next_first = date(month_index // 12, month_index % 12 + 1, 1)
  end = next_first - timedelta(days=1) + timedelta(days=LEAD_DAYS)
  return start, end


def _clip_utf8(text: Any, limit: int) -> str:
  value = " ".join(str(text or "").split())
  raw = value.encode("utf-8")
  if len(raw) <= limit:
    return value
  return raw[:limit].decode("utf-8", errors="ignore").rstrip()


def _parse_point(value: Any, zone: Optional[tzinfo]) -> Optional[Tuple[date, Optional[str]]]:
  """(day, "HH:MM" or None for a date) in local time, None when unreadable."""
  if isinstance(value, datetime):
    moment = value
  elif isinstance(value, date):
    return value, None
  elif isinstance(value, str):
    text = value.strip()
    if len(text) == 10:
      try:
        return date.fromisoformat(text), None
      except ValueError:
        return None
    try:
      moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
      return None
  else:
    return None
  if moment.tzinfo is not None and zone is not None:
    moment = moment.astimezone(zone)
  return moment.date(), f"{moment.hour:02d}:{moment.minute:02d}"


def _stamp(day: date, clock: Optional[str]) -> str:
  return day.isoformat() if clock is None else f"{day.isoformat()}T{clock}"


def normalise_events(events: Iterable[Any], zone: Optional[tzinfo]) -> List[Dict[str, Any]]:
  """Home Assistant's calendar.get_events items as sorted payload entries.

  Each entry keeps its local start day under "_day" for windowing; build_payload
  removes it.
  """
  out: List[Dict[str, Any]] = []
  for item in events or ():
    if not isinstance(item, dict):
      continue
    start = _parse_point(item.get("start"), zone)
    end = _parse_point(item.get("end"), zone)
    if start is None:
      continue
    if end is None or (end[1] is None) != (start[1] is None):
      # A missing or mixed end: a single day, or a zero-length timed event.
      end = (start[0] + timedelta(days=1), None) if start[1] is None else start
    if (end[0], end[1] or "") < (start[0], start[1] or ""):
      end = start if start[1] is not None else (start[0] + timedelta(days=1), None)
    entry: Dict[str, Any] = {
      "s": _stamp(*start),
      "e": _stamp(*end),
      "t": _clip_utf8(item.get("summary"), MAX_TITLE_BYTES),
    }
    location = _clip_utf8(item.get("location"), MAX_LOCATION_BYTES)
    if location:
      entry["l"] = location
    entry["_day"] = start[0]
    out.append(entry)
  # All-day events first on their day, like the panel sorts them.
  out.sort(key=lambda e: (e["_day"], len(e["s"]) > 10, e["s"], e["t"]))
  return out


def build_payload(events: Iterable[Any], start: date, end: date, state: Optional[str],
                  zone: Optional[tzinfo]) -> str:
  """The retained payload for `events` fetched for start..end (inclusive)."""
  entries = [e for e in normalise_events(events, zone) if e["_day"] <= end]
  to_day = end
  if len(entries) > MAX_EVENTS:
    to_day = min(to_day, entries[MAX_EVENTS]["_day"] - timedelta(days=1))
    entries = [e for e in entries[:MAX_EVENTS] if e["_day"] <= to_day]

  def encode(items: List[Dict[str, Any]], last: date) -> str:
    body = {
      "v": PAYLOAD_VERSION,
      "state": state if isinstance(state, str) and state else "unknown",
      "from": start.isoformat(),
      "to": max(last, start).isoformat(),
      "e": [{k: v for k, v in item.items() if k != "_day"} for item in items],
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

  payload = encode(entries, to_day)
  while entries and len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
    dropped = entries.pop()
    to_day = min(to_day, dropped["_day"] - timedelta(days=1))
    # Drop every event of that day: the payload is complete up to "to".
    while entries and entries[-1]["_day"] > to_day:
      entries.pop()
    payload = encode(entries, to_day)
  return payload


def unavailable_payload(start: date, end: date) -> str:
  """A calendar Home Assistant does not have (or reports unavailable)."""
  return build_payload([], start, end, "unavailable", None)


def events_of_response(response: Any, entity_id: str) -> Optional[List[Any]]:
  """The events list of a calendar.get_events service response, None if missing."""
  if not isinstance(response, dict):
    return None
  result = response.get(entity_id)
  if not isinstance(result, dict):
    return None
  events = result.get("events")
  return events if isinstance(events, list) else None
