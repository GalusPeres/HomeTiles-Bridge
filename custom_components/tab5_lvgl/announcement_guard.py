"""Checks for panel announcements on tab5_lvgl/config/{id}/bridge.

Anyone on the MQTT broker can publish there. These pure helpers keep such
messages from binding, changing or flooding config entries:

* the topic id must be the announced device id, in the firmware's format;
* a paired panel (command_channel.py) signs its announcement, and the Bridge
  accepts an announcement for a paired entry only with a valid signature;
* new-panel discovery cards are limited in number and rate.

Signature: the panel appends ``,"sig":"<64 hex>"`` before the final ``}``,
where sig = HMAC-SHA256(announce key, topic + "\\n" + unsigned payload) and the
unsigned payload is the exact JSON text without that member. The announce
key is HKDF-SHA256 of the pairing code with the info label "announce". See
docs-dev/command-encryption.md in the HomeTiles firmware repository.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Callable, Dict, Optional

CONFIG_TOPIC_PREFIX = "tab5_lvgl/config/"
CONFIG_TOPIC_SUFFIX = "/bridge"
# Current firmware: 12 upper-case hex digits of the MAC (buildDeviceId()).
# Letters, digits, "_" and "-" stay accepted for older and custom builds.
_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_ANNOUNCEMENT_BYTES = 64 * 1024

SIGNATURE_UNSIGNED = "unsigned"
SIGNATURE_VALID = "valid"
SIGNATURE_INVALID = "invalid"
_SIGNATURE_SUFFIX = re.compile(r',"sig":"([0-9a-f]{64})"\}\s*\Z')

# Discovery cards opened from MQTT announcements.
DISCOVERY_MAX_PENDING = 3
DISCOVERY_MAX_NEW = 5
DISCOVERY_WINDOW_S = 600.0


def valid_device_id(value: Any) -> bool:
  return isinstance(value, str) and bool(_DEVICE_ID.match(value))


def topic_device_id(topic: Any) -> Optional[str]:
  """The {id} of tab5_lvgl/config/{id}/bridge, or None for another topic."""
  if not isinstance(topic, str):
    return None
  if not topic.startswith(CONFIG_TOPIC_PREFIX) or not topic.endswith(CONFIG_TOPIC_SUFFIX):
    return None
  device_id = topic[len(CONFIG_TOPIC_PREFIX):-len(CONFIG_TOPIC_SUFFIX)]
  return device_id if valid_device_id(device_id) else None


def announcement_matches_topic(topic: Any, device_id: Any) -> bool:
  """Every firmware publishes its announcement under its own device id."""
  return valid_device_id(device_id) and topic_device_id(topic) == device_id


def check_signature(announce_key: Optional[bytes], topic: str, raw_payload: Any) -> str:
  """Return SIGNATURE_UNSIGNED, SIGNATURE_VALID or SIGNATURE_INVALID."""
  if isinstance(raw_payload, (bytes, bytearray)):
    try:
      raw_payload = bytes(raw_payload).decode("utf-8")
    except UnicodeDecodeError:
      return SIGNATURE_INVALID
  if not isinstance(raw_payload, str):
    return SIGNATURE_INVALID
  match = _SIGNATURE_SUFFIX.search(raw_payload)
  if match is None:
    return SIGNATURE_UNSIGNED
  if announce_key is None:
    # Signed by a paired panel, but this Bridge entry has no code: the
    # content is treated like any unsigned announcement.
    return SIGNATURE_UNSIGNED
  unsigned = raw_payload[:match.start()] + "}"
  expected = hmac.new(
    announce_key, (topic + "\n" + unsigned).encode("utf-8"), hashlib.sha256
  ).hexdigest()
  return SIGNATURE_VALID if hmac.compare_digest(expected, match.group(1)) else SIGNATURE_INVALID


class DiscoveryLimiter:
  """Bounds the discovery cards that MQTT announcements may open.

  At most ``max_pending`` cards wait at the same time, and at most ``max_new``
  different panels get a card per ``window_s``. A panel that already got a
  card in the window may announce again (Home Assistant merges it into the
  existing card by its unique id).
  """

  def __init__(self, clock: Callable[[], float], *, max_pending: int = DISCOVERY_MAX_PENDING,
               max_new: int = DISCOVERY_MAX_NEW, window_s: float = DISCOVERY_WINDOW_S) -> None:
    self._clock = clock
    self._max_pending = max_pending
    self._max_new = max_new
    self._window_s = window_s
    self._offered: Dict[str, float] = {}

  def allow(self, device_id: str, pending: int) -> bool:
    now = self._clock()
    self._offered = {
      key: moment for key, moment in self._offered.items() if now - moment < self._window_s
    }
    if device_id in self._offered:
      return True
    if pending >= self._max_pending or len(self._offered) >= self._max_new:
      return False
    self._offered[device_id] = now
    return True
