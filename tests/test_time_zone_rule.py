"""Home Assistant's time zone as a POSIX rule for the panels (issue #145).

Beside the exact rules of known zones, every zone of the time zone database is
checked hour by hour against an interpreter of the rule: the panel's clock must
show what Home Assistant's does.
"""

from __future__ import annotations

import calendar
import datetime
import re
import unittest
import zoneinfo

from test_view_navigation import ROOT, load_module

RULES = load_module("time_zone_rule")
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 4, 9, 15, tzinfo=UTC)


def _seconds(text: str) -> int:
  sign = -1 if text.startswith("-") else 1
  parts = [int(part) for part in text.lstrip("-").split(":")] + [0, 0]
  return sign * (parts[0] * 3600 + parts[1] * 60 + parts[2])


def _change_at(rule: str, year: int, offset: int) -> int:
  """The instant of an Mm.w.d/t date in a year, for the offset before it."""
  month, week, weekday, clock = re.fullmatch(r"M(\d+)\.(\d)\.(\d)/([\d:]+)", rule).groups()
  month, week, weekday = int(month), int(week), int(weekday)
  first_weekday = (calendar.monthrange(year, month)[0] + 1) % 7
  day = 1 + (weekday - first_weekday) % 7 + (week - 1) * 7
  while day > calendar.monthrange(year, month)[1]:
    day -= 7
  midnight = datetime.datetime(year, month, day, tzinfo=UTC).timestamp()
  return int(midnight) + _seconds(clock) - offset


def offset_of_rule(rule: str, stamp: int) -> int:
  """What a C library reads from the rule: the UTC offset at an instant."""
  match = re.fullmatch(r"STD(-?[\d:]+)(?:DST(-?[\d:]*),(M[^,]+),(M[^,]+))?", rule)
  assert match, rule
  standard = -_seconds(match.group(1))
  if not match.group(3):
    return standard
  summer = -_seconds(match.group(2)) if match.group(2) else standard + 3600
  year = datetime.datetime.fromtimestamp(stamp, UTC).year
  start = _change_at(match.group(3), year, standard)
  end = _change_at(match.group(4), year, summer)
  in_summer = start <= stamp < end if start < end else not end <= stamp < start
  return summer if in_summer else standard


class KnownZones(unittest.TestCase):
  def rule(self, key: str) -> str:
    return RULES.posix_rule(NOW.astimezone(zoneinfo.ZoneInfo(key)))

  def test_alaska_from_the_issue(self):
    self.assertEqual(self.rule("America/Anchorage"), "STD9DST,M3.2.0/2,M11.1.0/2")

  def test_summer_time_zones(self):
    self.assertEqual(self.rule("Europe/Berlin"), "STD-1DST,M3.5.0/2,M10.5.0/3")
    self.assertEqual(self.rule("America/New_York"), "STD5DST,M3.2.0/2,M11.1.0/2")
    self.assertEqual(self.rule("Europe/London"), "STD0DST,M3.5.0/1,M10.5.0/2")

  def test_the_southern_summer_runs_over_the_new_year(self):
    self.assertEqual(self.rule("Australia/Sydney"), "STD-10DST,M10.1.0/2,M4.1.0/3")
    self.assertEqual(self.rule("Pacific/Auckland"), "STD-12DST,M9.5.0/2,M4.1.0/3")

  def test_zones_without_summer_time(self):
    self.assertEqual(self.rule("Pacific/Honolulu"), "STD10")
    self.assertEqual(self.rule("America/Phoenix"), "STD7")
    self.assertEqual(self.rule("Asia/Tokyo"), "STD-9")
    self.assertEqual(self.rule("UTC"), "STD0")

  def test_offsets_with_minutes(self):
    self.assertEqual(self.rule("Asia/Kolkata"), "STD-5:30")
    self.assertEqual(self.rule("Asia/Kathmandu"), "STD-5:45")
    # Lord Howe changes by half an hour: the rule has to say so.
    self.assertEqual(self.rule("Australia/Lord_Howe"), "STD-10:30DST-11,M10.1.0/2,M4.1.0/2")

  def test_ireland_counts_its_winter_as_the_negative_summer(self):
    # The tz database calls Irish winter time negative daylight saving; the
    # offsets, which are all a clock needs, are the same as London's.
    self.assertEqual(self.rule("Europe/Dublin"), "STD0DST,M3.5.0/1,M10.5.0/2")

  def test_no_zone_no_rule(self):
    self.assertIsNone(RULES.posix_rule(datetime.datetime(2026, 11, 4, 9, 15)))

  def test_the_panel_accepts_what_this_writes(self):
    # The characters and the length the panel's time_zone_rule.h takes.
    for key in sorted(zoneinfo.available_timezones()):
      rule = self.rule(key)
      self.assertRegex(rule, r"^[A-Za-z][A-Za-z0-9+\-:,./]*$", key)
      self.assertLess(len(rule), 48, key)


class EveryZone(unittest.TestCase):
  def test_the_rule_shows_the_zones_clock_over_the_coming_year(self):
    start = int(NOW.timestamp())
    irregular = {}
    checked = 0
    for key in sorted(zoneinfo.available_timezones()):
      zone = zoneinfo.ZoneInfo(key)
      rule = RULES.posix_rule(NOW.astimezone(zone))
      wrong = sum(
        offset_of_rule(rule, stamp) != int(datetime.datetime.fromtimestamp(stamp, zone).utcoffset().total_seconds())
        for stamp in range(start, start + 366 * 86400, 3600)
      )
      checked += 1
      if wrong:
        irregular[key] = (rule, wrong)
    self.assertGreater(checked, 300)
    # A zone with no rule of this form may be off until its next change is
    # published; every other zone must match in every hour of the year.
    expected = {key for key in irregular if not re.search(r"DST", irregular[key][0])}
    self.assertEqual(set(irregular) - expected, set(), {key: irregular[key] for key in set(irregular) - expected})
    self.assertLess(len(irregular), 12, irregular)


class Publishing(unittest.TestCase):
  def test_the_configuration_carries_the_rule_near_its_start(self):
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.assertIn("from .time_zone_rule import posix_rule", source)
    build = source[source.index("async def async_publish_config_to_device"):]
    self.assertIn('"time_zone_rule": posix_rule(dt_util.now()),', build[:build.index('"sensors": self.sensors')])


if __name__ == "__main__":
  unittest.main()
