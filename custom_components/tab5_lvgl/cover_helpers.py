"""Protocol helpers for forwarding Home Assistant cover entities."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional


COVER_COMMAND_ALIASES = {
  "open": "open_cover",
  "open_cover": "open_cover",
  "close": "close_cover",
  "close_cover": "close_cover",
  "stop": "stop_cover",
  "stop_cover": "stop_cover",
  "position": "set_cover_position",
  "set_position": "set_cover_position",
  "set_cover_position": "set_cover_position",
  "open_tilt": "open_cover_tilt",
  "open_cover_tilt": "open_cover_tilt",
  "close_tilt": "close_cover_tilt",
  "close_cover_tilt": "close_cover_tilt",
  "stop_tilt": "stop_cover_tilt",
  "stop_cover_tilt": "stop_cover_tilt",
  "tilt_position": "set_cover_tilt_position",
  "set_tilt_position": "set_cover_tilt_position",
  "set_cover_tilt_position": "set_cover_tilt_position",
  "toggle": "toggle",
  "toggle_cover": "toggle",
  "toggle_tilt": "toggle_cover_tilt",
  "toggle_cover_tilt": "toggle_cover_tilt",
}


def normalise_cover_command(value: Any) -> Optional[str]:
  """Return the canonical Home Assistant cover service for a command."""
  if value is None:
    return None
  command = str(value).strip().lower().replace("-", "_").replace(" ", "_")
  if command.startswith("cover."):
    command = command[6:]
  return COVER_COMMAND_ALIASES.get(command)


def parse_cover_position(value: Any) -> Optional[int]:
  """Parse an integer percentage without silently clipping invalid commands."""
  if value is None or isinstance(value, bool):
    return None
  try:
    number = float(str(value).strip())
  except (TypeError, ValueError):
    return None
  if not math.isfinite(number) or not number.is_integer():
    return None
  position = int(number)
  if position < 0 or position > 100:
    return None
  return position


def build_cover_state_payload(state_value: Any, attributes: Mapping[str, Any]) -> dict[str, Any]:
  """Build the stable cover state contract consumed by HomeTiles firmware.

  Home Assistant defines 0 as fully closed and 100 as fully open for both
  cover and tilt positions. Unknown or unavailable entities remain explicit,
  and nullable positions prevent consumers from mistaking missing feedback for
  a real zero-percent reading.
  """
  state = str(state_value).strip().lower() if state_value is not None else ""
  if not state:
    state = "unknown"

  device_class = attributes.get("device_class")
  if isinstance(device_class, str):
    device_class = device_class.strip().lower() or None
  else:
    device_class = None

  try:
    supported_features = int(attributes.get("supported_features", 0) or 0)
  except (TypeError, ValueError):
    supported_features = 0
  supported_features = max(0, supported_features)

  assumed_state = _coerce_bool(attributes.get("assumed_state"), False)

  return {
    "state": state,
    "available": state != "unavailable",
    "current_position": _parse_state_position(attributes.get("current_position")),
    "current_tilt_position": _parse_state_position(
      attributes.get("current_tilt_position")
    ),
    "supported_features": supported_features,
    "device_class": device_class,
    "assumed_state": assumed_state,
  }


def _parse_state_position(value: Any) -> Optional[int]:
  """Normalise state feedback to an integer percentage or ``None``."""
  if value is None or isinstance(value, bool):
    return None
  try:
    number = float(str(value).strip())
  except (TypeError, ValueError):
    return None
  if not math.isfinite(number) or number < 0 or number > 100:
    return None
  return int(round(number))


def _coerce_bool(value: Any, default: bool) -> bool:
  if isinstance(value, bool):
    return value
  if value is None:
    return default
  text = str(value).strip().lower()
  if text in {"1", "true", "yes", "on"}:
    return True
  if text in {"0", "false", "no", "off"}:
    return False
  return default
