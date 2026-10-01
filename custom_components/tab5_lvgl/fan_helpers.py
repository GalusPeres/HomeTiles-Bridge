"""Protocol helpers for the HomeTiles Fan tile (no Home Assistant imports)."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Mapping, Optional, Tuple

FAN_DOMAIN = "fan"

# Home Assistant FanEntityFeature.
FAN_SET_SPEED = 1
FAN_OSCILLATE = 2
FAN_DIRECTION = 4
FAN_PRESET_MODE = 8
FAN_TURN_OFF = 16
FAN_TURN_ON = 32

FAN_DIRECTIONS = ("forward", "reverse")
MAX_FAN_COMMAND_BYTES = 512
MAX_PRESET_MODES = 16
MAX_PRESET_BYTES = 32


def _features(attributes: Mapping[str, Any]) -> int:
  value = attributes.get("supported_features")
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    return 0
  return value


def _percentage(value: Any) -> Optional[int]:
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
    return None
  return max(0, min(100, int(round(value))))


def _preset_modes(value: Any) -> list[str]:
  if not isinstance(value, (list, tuple)):
    return []
  modes: list[str] = []
  for item in value:
    if isinstance(item, str) and item and len(item.encode("utf-8")) <= MAX_PRESET_BYTES:
      if item not in modes:
        modes.append(item)
    if len(modes) >= MAX_PRESET_MODES:
      break
  return modes


def build_fan_detail(state: Optional[str], attributes: Mapping[str, Any]) -> Dict[str, Any]:
  """State for the Fan tile on {ha_prefix}/fan/<object>/detail."""
  step = attributes.get("percentage_step")
  if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step) or not 0 < step <= 100:
    step = None
  else:
    step = round(float(step), 2)
  preset = attributes.get("preset_mode")
  oscillating = attributes.get("oscillating")
  direction = attributes.get("direction")
  return {
    "state": state,
    "supported_features": _features(attributes),
    "percentage": _percentage(attributes.get("percentage")),
    "percentage_step": step,
    "preset_mode": preset if isinstance(preset, str) and preset else None,
    "preset_modes": _preset_modes(attributes.get("preset_modes")),
    "oscillating": oscillating if isinstance(oscillating, bool) else None,
    "direction": direction if direction in FAN_DIRECTIONS else None,
  }


def parse_fan_command(payload: Any) -> Optional[Dict[str, Any]]:
  """Decode a Fan command body; None for anything malformed."""
  if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_FAN_COMMAND_BYTES:
    return None
  try:
    command = json.loads(payload)
  except ValueError:
    return None
  if not isinstance(command, dict) or not isinstance(command.get("action"), str):
    return None
  return command


def build_fan_service_call(command: Mapping[str, Any], state: Optional[str],
                           attributes: Mapping[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
  """Service and data for a Fan command, or None when it is not allowed.

  Only actions whose Home Assistant feature bit is set are forwarded; values
  are clamped or must come from the entity's own lists.
  """
  if state in (None, "unavailable"):
    return None
  features = _features(attributes)
  action = command.get("action")
  if action == "turn_on" and features & FAN_TURN_ON:
    return "turn_on", {}
  if action == "turn_off" and features & FAN_TURN_OFF:
    return "turn_off", {}
  if action == "toggle" and features & (FAN_TURN_ON | FAN_TURN_OFF) == FAN_TURN_ON | FAN_TURN_OFF:
    return "toggle", {}
  if action == "set_percentage" and features & FAN_SET_SPEED:
    percentage = _percentage(command.get("percentage"))
    if percentage is None:
      return None
    return "set_percentage", {"percentage": percentage}
  if action == "set_preset_mode" and features & FAN_PRESET_MODE:
    preset = command.get("preset_mode")
    if not isinstance(preset, str) or preset not in _preset_modes(attributes.get("preset_modes")):
      return None
    return "set_preset_mode", {"preset_mode": preset}
  if action == "oscillate" and features & FAN_OSCILLATE:
    oscillating = command.get("oscillating")
    if not isinstance(oscillating, bool):
      return None
    return "oscillate", {"oscillating": oscillating}
  if action == "set_direction" and features & FAN_DIRECTION:
    direction = command.get("direction")
    if direction not in FAN_DIRECTIONS:
      return None
    return "set_direction", {"direction": direction}
  return None
