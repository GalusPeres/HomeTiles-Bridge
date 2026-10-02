"""Decide when a Media tile may drop its cover (dependency-free)."""

from __future__ import annotations

from typing import Any, Dict, Optional

# Seconds a player must report no artwork before the panel drops its cover.
ARTWORK_CLEAR_DELAY = 3.0
# The next song is still loading, or the player says nothing about its
# artwork. Keep the cover the panel already shows.
ARTWORK_KEEP_STATES = frozenset({"buffering", "unavailable", "unknown"})
ARTWORK_URL_KEYS = ("entity_picture", "media_image_url")


def has_artwork(payload: Dict[str, Any]) -> bool:
  return any(
    isinstance(payload.get(key), str) and payload[key].strip()
    for key in ARTWORK_URL_KEYS
  )


class ArtworkClearGate:
  """Report missing artwork only after it stayed missing.

  Players often drop their picture for a moment between two songs. Clearing
  the cover right away would make the tile jump twice, so a payload without
  artwork leaves the picture fields out (the panel keeps its cover) until the
  player has had no artwork for ARTWORK_CLEAR_DELAY seconds. Then the payload
  carries an explicit empty entity_picture, which clears the cover.
  """

  def __init__(self, delay: float = ARTWORK_CLEAR_DELAY) -> None:
    self._delay = delay
    self._missing_since: Dict[str, float] = {}

  def apply(self, entity_id: str, payload: Dict[str, Any], now: float) -> Optional[float]:
    """Mark payload as artwork-free when due; return seconds until then."""
    if has_artwork(payload) or payload.get("state") in ARTWORK_KEEP_STATES:
      self._missing_since.pop(entity_id, None)
      return None
    remaining = self._delay - (now - self._missing_since.setdefault(entity_id, now))
    if remaining > 0:
      return remaining
    payload["entity_picture"] = ""
    return None
