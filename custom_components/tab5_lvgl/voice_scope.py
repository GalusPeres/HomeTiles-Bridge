"""What a panel's voice assistant may read and control.

Pure helpers without Home Assistant imports, so the rules are unit tested.
voice_api.py wires them into the "HomeTiles room control" LLM API.

Home Assistant decides which entities Assist reaches with one exposure list
for every voice device. This API gives each panel its own lists instead,
chosen in the panel's Bridge options:

- "Voice can control": areas, labels and single entities the panel's voice
  may switch, dim, move or set. Controlled entities can be read as well.
- "Voice can read": areas, labels and single entities whose state the voice
  may tell, without any control.

The rules:
- An empty configuration allows nothing. A request without a panel behind it
  (typed chat, another device) gets nothing either.
- Every tool call is checked again against the panel's current lists before
  it runs; the prompt is only a hint for the model.
- Each tool maps to a fixed set of Home Assistant services; an action the
  entity's supported_features do not offer is refused, numbers are clamped.
- Locks and alarm panels are never controlled by voice: on the panel they
  need a paired display with a Web Admin password and a code.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

CONF_VOICE_CONTROL_AREAS = "voice_control_areas"
CONF_VOICE_CONTROL_LABELS = "voice_control_labels"
CONF_VOICE_CONTROL_ENTITIES = "voice_control_entities"
CONF_VOICE_READ_AREAS = "voice_read_areas"
CONF_VOICE_READ_LABELS = "voice_read_labels"
CONF_VOICE_READ_ENTITIES = "voice_read_entities"
VOICE_KEYS = (
  CONF_VOICE_CONTROL_AREAS, CONF_VOICE_CONTROL_LABELS, CONF_VOICE_CONTROL_ENTITIES,
  CONF_VOICE_READ_AREAS, CONF_VOICE_READ_LABELS, CONF_VOICE_READ_ENTITIES,
)

# Never controlled by voice (see the module docstring); reading is fine.
NEVER_CONTROL = frozenset({"lock", "alarm_control_panel"})
# Domains the control tools know. Others in a control list are only read.
ON_OFF_DOMAINS = frozenset({
  "light", "switch", "input_boolean", "fan", "humidifier", "siren", "remote",
  "media_player", "climate", "automation",
})
ACTIVATE_SERVICES = {"scene": "turn_on", "script": "turn_on", "button": "press", "input_button": "press"}
CONTROL_DOMAINS = ON_OFF_DOMAINS | frozenset({"cover", "valve"}) | frozenset(ACTIVATE_SERVICES)

# More entities than this in one panel's lists are cut from the prompt (the
# tools still check the full lists); a small model needs a short prompt.
MAX_PROMPT_ENTITIES = 120

# Home Assistant feature bits.
COVER_OPEN, COVER_CLOSE, COVER_SET_POSITION, COVER_STOP = 1, 2, 4, 8
VALVE_OPEN, VALVE_CLOSE, VALVE_SET_POSITION, VALVE_STOP = 1, 2, 4, 8
CLIMATE_TARGET_TEMPERATURE, CLIMATE_TURN_OFF, CLIMATE_TURN_ON = 1, 128, 256
FAN_SET_SPEED, FAN_TURN_OFF, FAN_TURN_ON = 1, 16, 32
SIREN_TURN_ON, SIREN_TURN_OFF = 1, 2
MEDIA_PAUSE, MEDIA_VOLUME_SET, MEDIA_PREVIOUS, MEDIA_NEXT = 1, 4, 16, 32
MEDIA_TURN_ON, MEDIA_TURN_OFF, MEDIA_STOP, MEDIA_PLAY = 128, 256, 4096, 16384
# Light color modes that take a color or a color temperature.
COLOR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
DIMMABLE_MODES = COLOR_MODES | frozenset({"brightness", "color_temp", "white"})

MEDIA_COMMANDS = {
  "play": ("media_play", MEDIA_PLAY),
  "pause": ("media_pause", MEDIA_PAUSE),
  "stop": ("media_stop", MEDIA_STOP),
  "next": ("media_next_track", MEDIA_NEXT),
  "previous": ("media_previous_track", MEDIA_PREVIOUS),
}
COVER_COMMANDS = {"open": ("open", COVER_OPEN), "close": ("close", COVER_CLOSE), "stop": ("stop", COVER_STOP)}

_ENTITY_ID = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
_UNAVAILABLE = ("unavailable",)


class VoiceRefusal(Exception):
  """A tool call the panel's lists or the entity do not allow; the message
  goes back to the model, which says it to the user."""


def entity_domain(entity_id: Any) -> str:
  if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
    return ""
  return entity_id.split(".", 1)[0]


def _clean_ids(raw: Any) -> List[str]:
  if isinstance(raw, str):
    raw = [raw]
  if not isinstance(raw, (list, tuple)):
    return []
  result: List[str] = []
  for item in raw:
    if isinstance(item, str) and item.strip() and item.strip() not in result:
      result.append(item.strip())
  return result


@dataclass(frozen=True)
class ScopeRule:
  """One list: areas, labels and single entities."""

  areas: frozenset = frozenset()
  labels: frozenset = frozenset()
  entities: frozenset = frozenset()

  @property
  def empty(self) -> bool:
    return not (self.areas or self.labels or self.entities)

  def matches(self, entity_id: str, area_id: Optional[str], label_ids: Iterable[str]) -> bool:
    if entity_id in self.entities:
      return True
    if area_id and area_id in self.areas:
      return True
    return bool(self.labels.intersection(label_ids or ()))


@dataclass(frozen=True)
class VoiceScope:
  control: ScopeRule = ScopeRule()
  read: ScopeRule = ScopeRule()

  @property
  def empty(self) -> bool:
    return self.control.empty and self.read.empty


def scope_from_config(data: Mapping[str, Any] | None) -> VoiceScope:
  """The panel's lists from its config entry data."""
  data = data or {}
  def rule(areas: str, labels: str, entities: str) -> ScopeRule:
    return ScopeRule(
      areas=frozenset(_clean_ids(data.get(areas))),
      labels=frozenset(_clean_ids(data.get(labels))),
      entities=frozenset(item for item in _clean_ids(data.get(entities)) if entity_domain(item)),
    )
  return VoiceScope(
    control=rule(CONF_VOICE_CONTROL_AREAS, CONF_VOICE_CONTROL_LABELS, CONF_VOICE_CONTROL_ENTITIES),
    read=rule(CONF_VOICE_READ_AREAS, CONF_VOICE_READ_LABELS, CONF_VOICE_READ_ENTITIES),
  )


@dataclass
class ScopedEntity:
  """An entity the panel's voice may reach, with what it needs for the prompt
  and for name matching."""

  entity_id: str
  name: str
  area: str = ""
  aliases: Tuple[str, ...] = ()
  can_control: bool = False

  @property
  def domain(self) -> str:
    return entity_domain(self.entity_id)


@dataclass
class EntityFacts:
  """What the registries say about one entity (filled by voice_api.py)."""

  entity_id: str
  name: str
  area_id: Optional[str] = None
  area_name: str = ""
  label_ids: Tuple[str, ...] = ()
  aliases: Tuple[str, ...] = ()
  hidden: bool = False
  # Config or diagnostic entity (factory reset buttons, status LEDs).
  auxiliary: bool = False


def scoped_entities(scope: VoiceScope, facts: Iterable[EntityFacts]) -> Dict[str, ScopedEntity]:
  """Every entity one of the panel's lists holds, by entity id. An entity in
  a control list whose domain has no control tool (a sensor) is only read;
  hidden, config and diagnostic entities count only when named one by one."""
  result: Dict[str, ScopedEntity] = {}
  for item in facts:
    domain = entity_domain(item.entity_id)
    if not domain:
      continue
    by_name = item.entity_id in scope.control.entities or item.entity_id in scope.read.entities
    if (item.hidden or item.auxiliary) and not by_name:
      continue
    control = scope.control.matches(item.entity_id, item.area_id, item.label_ids)
    read = control or scope.read.matches(item.entity_id, item.area_id, item.label_ids)
    if not read:
      continue
    result[item.entity_id] = ScopedEntity(
      entity_id=item.entity_id,
      name=item.name or item.entity_id,
      area=item.area_name,
      aliases=tuple(alias for alias in item.aliases if alias),
      can_control=control and domain in CONTROL_DOMAINS and domain not in NEVER_CONTROL,
    )
  return result


def _fold(text: Any) -> str:
  """Lower-case, accents and punctuation removed, single spaces."""
  if not isinstance(text, str):
    return ""
  text = unicodedata.normalize("NFKD", text)
  text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
  text = re.sub(r"[^\w]+", " ", text)
  return " ".join(text.split())


def resolve_entity(query: Any, entities: Mapping[str, ScopedEntity], *, area: Any = None) -> ScopedEntity:
  """The scoped entity a tool argument names: an entity id, or a name or
  alias (with the area when the name alone is ambiguous). Raises
  VoiceRefusal when nothing or more than one entity of the lists fits."""
  if isinstance(query, str) and query.strip() in entities:
    return entities[query.strip()]
  wanted = _fold(query)
  if not wanted:
    raise VoiceRefusal("Name the device to use.")
  wanted_area = _fold(area)

  def names(item: ScopedEntity) -> List[str]:
    folded = [_fold(item.name), _fold(item.entity_id.split(".", 1)[1].replace("_", " "))]
    folded += [_fold(alias) for alias in item.aliases]
    if item.area:
      # "kitchen light" for a light named "Light" in the Kitchen.
      folded += [f"{_fold(item.area)} {name}" for name in list(folded) if name]
    return [name for name in folded if name]

  matches = [item for item in entities.values() if wanted in names(item)]
  if wanted_area and len(matches) > 1:
    matches = [item for item in matches if _fold(item.area) == wanted_area] or matches
  if len(matches) == 1:
    return matches[0]
  if not matches:
    raise VoiceRefusal(f"'{query}' is not something this panel's voice assistant may use.")
  choices = ", ".join(f"{item.name} ({item.area or 'no area'})" for item in matches[:5])
  raise VoiceRefusal(f"'{query}' matches more than one device: {choices}. Ask which one.")


# Words that name no particular device; "the desk light" and "desk" count
# the same.
_FILLER = frozenset({"the", "a", "an", "my", "in", "on", "of", "please", "light", "lights", "lamp",
                     "lamps", "switch", "device", "one"})


def _words(*texts: Any) -> frozenset:
  words = set()
  for text in texts:
    for word in _fold(text).split():
      if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
      words.add(word)
  return frozenset(words)


def _name_score(spoken: frozenset, name: str, area: str, aliases: Sequence[str]) -> int:
  """How many of the spoken words the device's name, area or aliases hold;
  only words that tell devices apart count."""
  return len((spoken - _FILLER) & _words(name, area, *aliases))


def check_spoken_name(spoken: Any, chosen: ScopedEntity, facts: Iterable["EntityFacts"],
                      entities: Mapping[str, ScopedEntity]) -> None:
  """A model may map a device the user named to a similar one it is
  allowed to use ("worktable light" to the Desk Lights). When the user's
  own words fit a device outside the panel's lists better than the chosen
  one, the call is refused instead."""
  words = _words(spoken)
  if not (words - _FILLER):
    return
  mine = _name_score(words, chosen.name, chosen.area, chosen.aliases)
  for item in facts:
    if item.entity_id in entities or entity_domain(item.entity_id) != chosen.domain:
      continue
    if _name_score(words, item.name, item.area_name, item.aliases) > mine:
      raise VoiceRefusal(f"'{spoken}' is not something this panel's voice assistant may use; "
                         f"do not use another device instead.")


def _features(attributes: Mapping[str, Any]) -> int:
  value = attributes.get("supported_features", 0)
  return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number(value: Any, name: str) -> Optional[float]:
  if value is None:
    return None
  if isinstance(value, bool):
    raise VoiceRefusal(f"{name} must be a number.")
  try:
    result = float(value)
  except (TypeError, ValueError) as err:
    raise VoiceRefusal(f"{name} must be a number.") from err
  if result != result or result in (float("inf"), float("-inf")):
    raise VoiceRefusal(f"{name} must be a number.")
  return result


def _clamp(value: float, low: float, high: float) -> float:
  return max(low, min(high, value))


def _require_control(item: ScopedEntity) -> None:
  if not item.can_control:
    raise VoiceRefusal(f"This panel's voice assistant may only read {item.name}, not control it.")


def _require_available(item: ScopedEntity, state: Optional[str]) -> None:
  if state is None or state in _UNAVAILABLE:
    raise VoiceRefusal(f"{item.name} is unavailable.")


ServiceCall = Tuple[str, str, Dict[str, Any]]


def plan_turn(item: ScopedEntity, on: bool, state: Optional[str], attributes: Mapping[str, Any]) -> ServiceCall:
  """Turn an entity on or off; covers and valves open and close, scenes,
  scripts and buttons run on "on"."""
  _require_control(item)
  _require_available(item, state)
  domain = item.domain
  features = _features(attributes)
  data: Dict[str, Any] = {"entity_id": item.entity_id}
  if domain in ACTIVATE_SERVICES:
    if not on:
      raise VoiceRefusal(f"{item.name} can only be activated.")
    return domain, ACTIVATE_SERVICES[domain], data
  if domain in ("cover", "valve"):
    bit = COVER_OPEN if on else COVER_CLOSE
    if not features & bit:
      raise VoiceRefusal(f"{item.name} cannot be {'opened' if on else 'closed'}.")
    return domain, f"{'open' if on else 'close'}_{domain}", data
  if domain not in ON_OFF_DOMAINS:
    raise VoiceRefusal(f"{item.name} cannot be turned on or off.")
  needed = {
    "fan": FAN_TURN_ON if on else FAN_TURN_OFF,
    "siren": SIREN_TURN_ON if on else SIREN_TURN_OFF,
    "media_player": MEDIA_TURN_ON if on else MEDIA_TURN_OFF,
    "climate": CLIMATE_TURN_ON if on else CLIMATE_TURN_OFF,
  }.get(domain)
  if needed is not None and not features & needed:
    raise VoiceRefusal(f"{item.name} cannot be turned {'on' if on else 'off'}.")
  return domain, "turn_on" if on else "turn_off", data


# Color names a voice request may use, as Home Assistant's light.turn_on
# color_name accepts them; a fixed list keeps the model to real colors.
COLOR_NAMES = (
  "white", "warm white", "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
  "magenta", "violet", "turquoise", "lime", "gold", "teal", "lavender", "coral", "indigo",
)


def plan_light(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any], *,
               brightness: Any = None, color: Any = None, kelvin: Any = None) -> ServiceCall:
  """light.turn_on with a brightness (percent), a color name or a color
  temperature (Kelvin), as far as the light supports them."""
  _require_control(item)
  _require_available(item, state)
  if item.domain != "light":
    raise VoiceRefusal(f"{item.name} is not a light.")
  modes = set(attributes.get("supported_color_modes") or ())
  data: Dict[str, Any] = {"entity_id": item.entity_id}
  value = _number(brightness, "Brightness")
  if value is not None:
    if not modes & DIMMABLE_MODES:
      raise VoiceRefusal(f"{item.name} cannot be dimmed.")
    data["brightness_pct"] = int(round(_clamp(value, 1, 100)))
  if color is not None:
    name = _fold(color)
    if name not in COLOR_NAMES:
      raise VoiceRefusal(f"'{color}' is not a color I can set.")
    if name == "warm white":
      kelvin = kelvin if kelvin is not None else 2700
    else:
      if not modes & COLOR_MODES:
        raise VoiceRefusal(f"{item.name} cannot change color.")
      data["color_name"] = "white" if name == "white" else name
  temperature = _number(kelvin, "Color temperature")
  if temperature is not None:
    if "color_temp" not in modes:
      raise VoiceRefusal(f"{item.name} has no color temperature.")
    low = _number(attributes.get("min_color_temp_kelvin"), "min") or 2000
    high = _number(attributes.get("max_color_temp_kelvin"), "max") or 6500
    data["color_temp_kelvin"] = int(round(_clamp(temperature, low, high)))
    data.pop("color_name", None)
  return "light", "turn_on", data


def plan_position(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any], *,
                  command: Any = None, position: Any = None) -> ServiceCall:
  """A cover or valve: open, close, stop, or move to a position in percent."""
  _require_control(item)
  _require_available(item, state)
  domain = item.domain
  if domain not in ("cover", "valve"):
    raise VoiceRefusal(f"{item.name} is not a cover or valve.")
  features = _features(attributes)
  value = _number(position, "Position")
  data: Dict[str, Any] = {"entity_id": item.entity_id}
  if value is not None:
    if not features & COVER_SET_POSITION:
      raise VoiceRefusal(f"{item.name} cannot move to a position.")
    data["position"] = int(round(_clamp(value, 0, 100)))
    return domain, f"set_{domain}_position", data
  name = _fold(command)
  if name not in COVER_COMMANDS:
    raise VoiceRefusal("Say open, close, stop or a position.")
  verb, bit = COVER_COMMANDS[name]
  if not features & bit:
    raise VoiceRefusal(f"{item.name} cannot {verb}.")
  return domain, f"{verb}_{domain}", data


def plan_climate(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any], *,
                 temperature: Any = None, hvac_mode: Any = None) -> ServiceCall:
  """climate.set_temperature within the entity's limits, or
  climate.set_hvac_mode with one of its own modes."""
  _require_control(item)
  _require_available(item, state)
  if item.domain != "climate":
    raise VoiceRefusal(f"{item.name} is not a thermostat.")
  data: Dict[str, Any] = {"entity_id": item.entity_id}
  value = _number(temperature, "Temperature")
  if value is not None:
    if not _features(attributes) & CLIMATE_TARGET_TEMPERATURE:
      raise VoiceRefusal(f"{item.name} has no single target temperature.")
    low = _number(attributes.get("min_temp"), "min")
    high = _number(attributes.get("max_temp"), "max")
    if low is not None and high is not None and low <= high:
      value = _clamp(value, low, high)
    step = _number(attributes.get("target_temp_step"), "step") or 0.5
    data["temperature"] = round(round(value / step) * step, 2)
  if hvac_mode is not None:
    modes = [mode for mode in attributes.get("hvac_modes") or () if isinstance(mode, str)]
    mode = _fold(hvac_mode).replace(" ", "_")
    if mode not in modes:
      raise VoiceRefusal(f"{item.name} supports these modes: {', '.join(modes) or 'none'}.")
    if value is None:
      data["hvac_mode"] = mode
      return "climate", "set_hvac_mode", data
    data["hvac_mode"] = mode
  if value is None:
    raise VoiceRefusal("Say a temperature or a mode.")
  return "climate", "set_temperature", data


def plan_media(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any], *,
               command: Any = None, volume: Any = None) -> ServiceCall:
  """Play, pause, stop, next, previous, or a volume in percent."""
  _require_control(item)
  _require_available(item, state)
  if item.domain != "media_player":
    raise VoiceRefusal(f"{item.name} is not a media player.")
  features = _features(attributes)
  data: Dict[str, Any] = {"entity_id": item.entity_id}
  value = _number(volume, "Volume")
  if value is not None:
    if not features & MEDIA_VOLUME_SET:
      raise VoiceRefusal(f"{item.name} has no volume control.")
    data["volume_level"] = round(_clamp(value, 0, 100) / 100, 2)
    return "media_player", "volume_set", data
  name = _fold(command)
  if name not in MEDIA_COMMANDS:
    raise VoiceRefusal("Say play, pause, stop, next, previous or a volume.")
  service, bit = MEDIA_COMMANDS[name]
  if not features & bit:
    raise VoiceRefusal(f"{item.name} cannot {name}.")
  return "media_player", service, data


def plan_fan(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any], *, percentage: Any) -> ServiceCall:
  _require_control(item)
  _require_available(item, state)
  if item.domain != "fan":
    raise VoiceRefusal(f"{item.name} is not a fan.")
  if not _features(attributes) & FAN_SET_SPEED:
    raise VoiceRefusal(f"{item.name} has no speed setting.")
  value = _number(percentage, "Speed")
  if value is None:
    raise VoiceRefusal("Say a speed in percent.")
  return "fan", "set_percentage", {"entity_id": item.entity_id, "percentage": int(round(_clamp(value, 0, 100)))}


# Attributes worth telling; the rest (pictures, internal ids) stays out.
STATE_ATTRIBUTES = (
  "unit_of_measurement", "device_class", "brightness", "color_mode", "color_temp_kelvin",
  "current_position", "current_temperature", "temperature", "target_temp_high", "target_temp_low",
  "hvac_action", "hvac_modes", "humidity", "current_humidity", "percentage", "preset_mode",
  "media_title", "media_artist", "volume_level", "is_volume_muted", "source", "battery_level",
)


def state_summary(item: ScopedEntity, state: Optional[str], attributes: Mapping[str, Any]) -> Dict[str, Any]:
  """The state of one scoped entity for the get-state tool."""
  result: Dict[str, Any] = {
    "name": item.name, "entity_id": item.entity_id, "area": item.area or None,
    "state": state if state is not None else "unavailable",
    "can_control": item.can_control,
  }
  extra = {key: attributes[key] for key in STATE_ATTRIBUTES
           if key in attributes and isinstance(attributes[key], (str, int, float, bool, list))}
  if "brightness" in extra and isinstance(extra["brightness"], (int, float)) and not isinstance(extra["brightness"], bool):
    extra["brightness_pct"] = int(round(extra.pop("brightness") * 100 / 255))
  if extra:
    result["attributes"] = extra
  return result


def build_prompt(panel_name: str, entities: Mapping[str, ScopedEntity],
                 states: Mapping[str, Optional[str]], area: str = "") -> str:
  """The API's prompt: what this panel's voice may use, with live states."""
  lines = [
    f"You are the voice assistant of the HomeTiles panel '{panel_name}'"
    + (f" in the area '{area}'" if area else "") + ".",
    "You may only use the Home Assistant devices listed below; everything else in the home is off limits,"
    " so say you cannot do that instead of guessing.",
    "Pass the entity_id from the list to the tools, and the device name in the user's own words as"
    " spoken_name. Never use a different device in place of one the user named.",
    "'control' devices can be changed, 'read' devices can only be reported.",
  ]
  if area:
    lines.append(f"When the user does not name an area, prefer devices in '{area}'.")
  if not entities:
    lines.append("No devices are released for this panel.")
    return "\n".join(lines)
  lines.append("Devices (name; entity_id; area; access; state):")
  ordered = sorted(entities.values(), key=lambda item: (not item.can_control, item.area, item.name.casefold()))
  for item in ordered[:MAX_PROMPT_ENTITIES]:
    state = states.get(item.entity_id)
    lines.append(f"- {item.name}; {item.entity_id}; {item.area or '-'}; "
                 f"{'control' if item.can_control else 'read'}; {state if state is not None else 'unavailable'}")
  if len(ordered) > MAX_PROMPT_ENTITIES:
    lines.append(f"({len(ordered) - MAX_PROMPT_ENTITIES} more devices; ask the get_state tool by name.)")
  return "\n".join(lines)
