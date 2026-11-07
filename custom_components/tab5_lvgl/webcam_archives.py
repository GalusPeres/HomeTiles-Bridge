"""Webcam portals that keep their older pictures at addresses anyone can read.

A webcam from one of them gets its last day at once (webcam_source.py), so the time
bar and Play work from the start; any other webcam collects from when it is added.
A portal is data: how to know it from the address and how its archive names a
picture. Both name their pictures in Central European time (they are in the Alps).

  Foto-Webcam   every 10 minutes, .../webcam/<name>/2026/11/07/1010_lm.jpg (1200 wide)
  snow-online   every hour,       archive.snow-online.de/<id>/2026/11/07/10/archived_l.jpg

A missing picture (a night gap) is simply not there; the next one is asked.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Berlin")

_FOTO_WEBCAM = re.compile(r"^https://www\.foto-webcam\.eu/webcam/([a-z0-9][a-z0-9-]{0,40})/", re.IGNORECASE)
# snow-online names its pictures "<webcam>_n<id>-..." on cams.snow-online.de.
_SNOW_ONLINE_ID = re.compile(r"_n(\d{1,9})-")
_SNOW_ONLINE_HOSTS = ("www.snow-online.com", "www.snow-online.de", "cams.snow-online.de")


class Archive(NamedTuple):
  portal: str
  step_s: int
  # The address in the portal's local time (strftime).
  template: str

  def addresses(self, since: float, until: float) -> List[Tuple[int, str]]:
    """(time, address) of every picture from since to until, newest first."""
    result: List[Tuple[int, str]] = []
    first = math.ceil(since / self.step_s) * self.step_s
    when = int(until // self.step_s) * self.step_s
    while when >= first:
      local = datetime.fromtimestamp(when, timezone.utc).astimezone(ZONE)
      result.append((when, local.strftime(self.template)))
      when -= self.step_s
    return result


def archive_for(*addresses: Optional[str]) -> Optional[Archive]:
  """The archive behind a webcam's page or picture address, None for any other."""
  for address in addresses:
    if not address:
      continue
    found = _FOTO_WEBCAM.match(address)
    if found:
      name = found.group(1).lower()
      return Archive("foto-webcam", 600, f"https://www.foto-webcam.eu/webcam/{name}/%Y/%m/%d/%H%M_lm.jpg")
    if (urlsplit(address).hostname or "").lower() in _SNOW_ONLINE_HOSTS:
      found = _SNOW_ONLINE_ID.search(address)
      if found:
        return Archive("snow-online", 3600, f"https://archive.snow-online.de/{found.group(1)}/%Y/%m/%d/%H/archived_l.jpg")
  return None
