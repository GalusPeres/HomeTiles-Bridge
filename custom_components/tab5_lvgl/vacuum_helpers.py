"""Protocol helpers for the HomeTiles Vacuum tile (no Home Assistant imports)."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

VACUUM_DOMAIN = "vacuum"

# Home Assistant VacuumEntityFeature.
VACUUM_PAUSE = 4
VACUUM_STOP = 8
VACUUM_RETURN_HOME = 16
VACUUM_FAN_SPEED = 32
VACUUM_LOCATE = 512
VACUUM_CLEAN_SPOT = 1024
VACUUM_START = 8192
VACUUM_CLEAN_AREA = 16384

MAX_VACUUM_COMMAND_BYTES = 1024
MAX_FAN_SPEEDS = 16
MAX_FAN_SPEED_BYTES = 32
MAX_AREAS = 16
MAX_AREA_ID_BYTES = 48
MAX_AREA_NAME_BYTES = 48

# Pictures the panel can show as the vacuum's map (images.py serves both).
MAP_DOMAINS = ("image", "camera")
AREA_UNITS = ("m²", "m2", "ft²", "ft2")
TOTAL_STATE_CLASSES = ("total", "total_increasing")
# Duration units Home Assistant sensors report, in minutes.
DURATION_MINUTES = {"s": 1 / 60, "min": 1.0, "h": 60.0, "d": 1440.0}


def _features(attributes: Mapping[str, Any]) -> int:
  value = attributes.get("supported_features")
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    return 0
  return value


def _number(value: Any) -> Optional[float]:
  if isinstance(value, bool):
    return None
  if isinstance(value, str):
    try:
      value = float(value)
    except ValueError:
      return None
  if not isinstance(value, (int, float)) or not math.isfinite(value):
    return None
  return float(value)


def _fits(text: Any, limit: int) -> bool:
  return isinstance(text, str) and bool(text) and len(text.encode("utf-8")) <= limit


def fan_speeds(value: Any) -> List[str]:
  """The entity's fan speeds in its own order, bounded like the panel's list."""
  if not isinstance(value, (list, tuple)):
    return []
  speeds: List[str] = []
  for item in value:
    if _fits(item, MAX_FAN_SPEED_BYTES) and item not in speeds:
      speeds.append(item)
    if len(speeds) >= MAX_FAN_SPEEDS:
      break
  return speeds


def battery_level(value: Any) -> Optional[int]:
  number = _number(value)
  if number is None:
    return None
  return max(0, min(100, int(round(number))))


def area_choices(area_mapping: Any, area_names: Mapping[str, str]) -> List[Dict[str, str]]:
  """The Home Assistant areas mapped to vacuum segments, in the mapping's order.

  `area_mapping` is the vacuum entity's registry option
  options["vacuum"]["area_mapping"] ({area_id: [segment ids]}); an area
  without a segment cannot be cleaned and is left out.
  """
  if not isinstance(area_mapping, Mapping):
    return []
  areas: List[Dict[str, str]] = []
  for area_id, segments in area_mapping.items():
    if not _fits(area_id, MAX_AREA_ID_BYTES) or not isinstance(segments, (list, tuple)) or not segments:
      continue
    name = area_names.get(area_id)
    if not _fits(name, MAX_AREA_NAME_BYTES):
      name = area_id
    areas.append({"id": area_id, "name": name})
    if len(areas) >= MAX_AREAS:
      break
  return areas


def pick_companions(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Optional[str]]:
  """Entities of the vacuum's own device the tile shows beside it.

  `rows`: entity_id, device_class, unit and state_class of each enabled
  entity on the device. Home Assistant reports a vacuum's battery as a sensor
  with device class battery (since 2025.8); the cleaned area and cleaning
  time are the integration's current-run sensors (no total, no time left);
  the map is the device's image entity, else its camera, a "map" in the id
  first.
  """
  picked: Dict[str, Optional[str]] = {"battery": None, "cleaned_area": None, "cleaning_time": None, "map": None}
  maps: List[str] = []
  for row in sorted(rows, key=lambda item: str(item.get("entity_id") or "")):
    entity_id = str(row.get("entity_id") or "")
    domain, _, object_id = entity_id.partition(".")
    if not object_id:
      continue
    if domain in MAP_DOMAINS:
      maps.append(entity_id)
      continue
    if domain != "sensor":
      continue
    device_class = row.get("device_class")
    unit = row.get("unit")
    totals = row.get("state_class") in TOTAL_STATE_CLASSES or "total" in object_id
    if device_class == "battery" and unit == "%" and picked["battery"] is None:
      picked["battery"] = entity_id
    elif unit in AREA_UNITS and not totals and picked["cleaned_area"] is None:
      picked["cleaned_area"] = entity_id
    elif (device_class == "duration" and unit in DURATION_MINUTES and not totals and "clean" in object_id
          and "left" not in object_id and "remaining" not in object_id and picked["cleaning_time"] is None):
      picked["cleaning_time"] = entity_id
  maps.sort(key=lambda entity_id: (not entity_id.startswith("image."), "map" not in entity_id, entity_id))
  picked["map"] = maps[0] if maps else None
  return picked


def area_measure(state: Any, unit: Any) -> Optional[Dict[str, Any]]:
  number = _number(state)
  if number is None or number < 0 or unit not in AREA_UNITS:
    return None
  return {"value": round(number, 1), "unit": "m²" if unit in ("m²", "m2") else "ft²"}


def time_measure(state: Any, unit: Any) -> Optional[Dict[str, Any]]:
  number = _number(state)
  if number is None or number < 0 or unit not in DURATION_MINUTES:
    return None
  return {"value": int(round(number * DURATION_MINUTES[unit])), "unit": "min"}


def build_vacuum_detail(state: Optional[str], attributes: Mapping[str, Any],
                        extras: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
  """State for the Vacuum tile on {ha_prefix}/vacuum/<object>/detail.

  `extras` holds what the Bridge found beside the entity: battery (the
  device's battery sensor value), areas (area_choices), map (an entity id),
  cleaned_area and cleaning_time (area_measure, time_measure). A missing
  value stays null; the deprecated battery_level attribute is the fallback.
  """
  extras = extras or {}
  features = _features(attributes)
  speed = attributes.get("fan_speed")
  battery = extras.get("battery")
  if battery is None:
    battery = battery_level(attributes.get("battery_level"))
  areas = extras.get("areas") if features & VACUUM_CLEAN_AREA else None
  map_entity = extras.get("map")
  return {
    "state": state,
    "supported_features": features,
    "fan_speed": speed if _fits(speed, MAX_FAN_SPEED_BYTES) else None,
    "fan_speed_list": fan_speeds(attributes.get("fan_speed_list")) if features & VACUUM_FAN_SPEED else [],
    "battery": battery,
    "areas": list(areas or [])[:MAX_AREAS],
    "map": map_entity if isinstance(map_entity, str) and map_entity.partition(".")[0] in MAP_DOMAINS else None,
    "cleaned_area": extras.get("cleaned_area"),
    "cleaning_time": extras.get("cleaning_time"),
  }


def parse_vacuum_command(payload: Any) -> Optional[Dict[str, Any]]:
  """Decode a Vacuum command body; None for anything malformed."""
  if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_VACUUM_COMMAND_BYTES:
    return None
  try:
    command = json.loads(payload)
  except ValueError:
    return None
  if not isinstance(command, dict) or not isinstance(command.get("action"), str):
    return None
  return command


# Actions without arguments -> (service, feature bit).
_PLAIN_ACTIONS: Dict[str, Tuple[str, int]] = {
  "start": ("start", VACUUM_START),
  "pause": ("pause", VACUUM_PAUSE),
  "stop": ("stop", VACUUM_STOP),
  "return_to_base": ("return_to_base", VACUUM_RETURN_HOME),
  "locate": ("locate", VACUUM_LOCATE),
  "clean_spot": ("clean_spot", VACUUM_CLEAN_SPOT),
}


def build_vacuum_service_call(command: Mapping[str, Any], state: Optional[str],
                              attributes: Mapping[str, Any],
                              area_ids: Iterable[str] = ()) -> Optional[Tuple[str, Dict[str, Any]]]:
  """Service and data for a Vacuum command, or None when it is not allowed.

  Only actions whose Home Assistant feature bit is set are forwarded; a fan
  speed must come from the entity's own list and areas from its mapping
  (`area_ids`).
  """
  if state in (None, "unavailable"):
    return None
  features = _features(attributes)
  action = command.get("action")
  plain = _PLAIN_ACTIONS.get(action) if isinstance(action, str) else None
  if plain is not None:
    service, feature = plain
    return (service, {}) if features & feature else None
  if action == "set_fan_speed" and features & VACUUM_FAN_SPEED:
    speed = command.get("fan_speed")
    if not isinstance(speed, str) or speed not in fan_speeds(attributes.get("fan_speed_list")):
      return None
    return "set_fan_speed", {"fan_speed": speed}
  if action == "clean_area" and features & VACUUM_CLEAN_AREA:
    requested = command.get("areas")
    allowed = set(area_ids)
    if not isinstance(requested, list) or not 1 <= len(requested) <= MAX_AREAS:
      return None
    chosen: List[str] = []
    for area_id in requested:
      if not isinstance(area_id, str) or area_id not in allowed:
        return None
      if area_id not in chosen:
        chosen.append(area_id)
    return "clean_area", {"cleaning_area_id": chosen}
  return None
