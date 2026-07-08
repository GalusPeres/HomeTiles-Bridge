# Config flow + options flow for the Tab5 LVGL integration.

from __future__ import annotations

import logging
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
  CONF_BASE_TOPIC,
  CONF_DEVICE_ID,
  CONF_DEVICE_NAME,
  CONF_ENERGY_ELECTRICITY,
  CONF_ENERGY_GAS,
  CONF_ENERGY_WATER,
  CONF_HA_PREFIX,
  CONF_LIGHTS,
  CONF_MANUFACTURER,
  CONF_MEDIA_PLAYERS,
  CONF_MODEL,
  CONF_SCENE_ENTITIES,
  CONF_SCENE_MAP,
  CONF_SCENE_MAP_TEXT,
  CONF_SENSORS,
  CONF_SWITCHES,
  CONF_WEATHERS,
  DEFAULT_BASE,
  DEFAULT_PREFIX,
  DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Config flow — adding a new panel (device info only)
# ---------------------------------------------------------------------------

class Tab5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
  VERSION = 1

  async def async_step_user(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}

    if user_input is not None:
      base = _normalise_topic(user_input.get(CONF_BASE_TOPIC, DEFAULT_BASE), DEFAULT_BASE)
      prefix = _normalise_topic(user_input.get(CONF_HA_PREFIX, DEFAULT_PREFIX), DEFAULT_PREFIX)

      for entry in self._async_current_entries():
        if entry.data.get(CONF_BASE_TOPIC) == base:
          errors["base_topic"] = "topic_already_configured"
          break

      if not errors:
        data: Dict[str, Any] = {CONF_BASE_TOPIC: base, CONF_HA_PREFIX: prefix}
        for key in (CONF_DEVICE_NAME, CONF_MANUFACTURER, CONF_MODEL):
          val = (user_input.get(key) or "").strip()
          if val:
            data[key] = val
        return self.async_create_entry(title=_entry_title(data), data=data)

    defaults = user_input or {}
    return self.async_show_form(
      step_id="user",
      data_schema=vol.Schema({
        vol.Required(CONF_BASE_TOPIC, default=defaults.get(CONF_BASE_TOPIC, DEFAULT_BASE)): str,
        vol.Required(CONF_HA_PREFIX, default=defaults.get(CONF_HA_PREFIX, DEFAULT_PREFIX)): str,
        vol.Optional(CONF_DEVICE_NAME, default=defaults.get(CONF_DEVICE_NAME, "")): str,
        vol.Optional(CONF_MANUFACTURER, default=defaults.get(CONF_MANUFACTURER, "")): str,
        vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, "")): str,
      }),
      errors=errors,
    )

  async def async_step_import(self, import_data: Dict[str, Any]):
    device_id = import_data.get(CONF_DEVICE_ID)
    if device_id:
      await self.async_set_unique_id(device_id)
      self._abort_if_unique_id_configured()
    return self.async_create_entry(title=_entry_title(import_data), data=import_data)

  @staticmethod
  @callback
  def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
  ) -> config_entries.OptionsFlow:
    return Tab5OptionsFlowHandler()


# ---------------------------------------------------------------------------
#  Options flow — menu with two sections
# ---------------------------------------------------------------------------

class Tab5OptionsFlowHandler(config_entries.OptionsFlow):

  async def async_step_init(self, user_input: Dict[str, Any] | None = None):
    return self.async_show_menu(
      step_id="init",
      menu_options=["panel", "entities", "energy"],
    )

  # ---- Section 1: Panel settings ----

  async def async_step_panel(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}
    current = dict(self.config_entry.data)

    if user_input is not None:
      base = _normalise_topic(user_input.get(CONF_BASE_TOPIC, current.get(CONF_BASE_TOPIC)), DEFAULT_BASE)
      prefix = _normalise_topic(user_input.get(CONF_HA_PREFIX, current.get(CONF_HA_PREFIX)), DEFAULT_PREFIX)
      updated = dict(current)
      updated[CONF_BASE_TOPIC] = base
      updated[CONF_HA_PREFIX] = prefix
      for key in (CONF_DEVICE_NAME, CONF_MANUFACTURER, CONF_MODEL):
        val = (user_input.get(key) or "").strip()
        if val:
          updated[key] = val
        else:
          updated.pop(key, None)
      self.hass.config_entries.async_update_entry(self.config_entry, data=updated)
      return self.async_create_entry(title="", data={})

    return self.async_show_form(
      step_id="panel",
      data_schema=vol.Schema({
        vol.Required(CONF_BASE_TOPIC, default=current.get(CONF_BASE_TOPIC, DEFAULT_BASE)): str,
        vol.Required(CONF_HA_PREFIX, default=current.get(CONF_HA_PREFIX, DEFAULT_PREFIX)): str,
        vol.Optional(CONF_DEVICE_NAME, default=current.get(CONF_DEVICE_NAME, "")): str,
        vol.Optional(CONF_MANUFACTURER, default=current.get(CONF_MANUFACTURER, "")): str,
        vol.Optional(CONF_MODEL, default=current.get(CONF_MODEL, "")): str,
      }),
      errors=errors,
    )

  # ---- Section 2: Shared entity configuration ----

  async def async_step_entities(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}
    current = dict(self.config_entry.data)

    if user_input is not None:
      try:
        updated = _convert_entity_data(user_input, current)
      except ValueError as err:
        errors["base"] = err.args[0]
      else:
        self.hass.config_entries.async_update_entry(self.config_entry, data=updated)
        return self.async_create_entry(title="", data={})

    merged = _merge_all_entities(self.hass, current)
    return self.async_show_form(
      step_id="entities",
      data_schema=vol.Schema({
        vol.Optional(CONF_SENSORS, default=merged.get(CONF_SENSORS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(multiple=True)
        ),
        vol.Optional(CONF_WEATHERS, default=merged.get(CONF_WEATHERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["weather"], multiple=True)
        ),
        vol.Optional(CONF_LIGHTS, default=merged.get(CONF_LIGHTS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["light"], multiple=True)
        ),
        vol.Optional(CONF_SWITCHES, default=merged.get(CONF_SWITCHES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["switch"], multiple=True)
        ),
        vol.Optional(CONF_MEDIA_PLAYERS, default=merged.get(CONF_MEDIA_PLAYERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["media_player"], multiple=True)
        ),
        vol.Optional(CONF_SCENE_ENTITIES, default=merged.get(CONF_SCENE_ENTITIES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["scene", "script"], multiple=True)
        ),
        vol.Optional(CONF_SCENE_MAP_TEXT, default=merged.get(CONF_SCENE_MAP_TEXT, "")): selector.TextSelector(
          selector.TextSelectorConfig(multiline=True)
        ),
      }),
      errors=errors,
    )

  # ---- Section 3: Energy Dashboard ----

  async def async_step_energy(self, user_input: Dict[str, Any] | None = None):
    current = dict(self.config_entry.data)

    if user_input is not None:
      updated = dict(current)
      updated[CONF_ENERGY_ELECTRICITY] = bool(user_input.get(CONF_ENERGY_ELECTRICITY, False))
      updated[CONF_ENERGY_GAS] = bool(user_input.get(CONF_ENERGY_GAS, False))
      updated[CONF_ENERGY_WATER] = bool(user_input.get(CONF_ENERGY_WATER, False))
      self.hass.config_entries.async_update_entry(self.config_entry, data=updated)
      # Sync to all other entries
      for entry in self.hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == self.config_entry.entry_id:
          continue
        other = dict(entry.data or {})
        other[CONF_ENERGY_ELECTRICITY] = updated[CONF_ENERGY_ELECTRICITY]
        other[CONF_ENERGY_GAS] = updated[CONF_ENERGY_GAS]
        other[CONF_ENERGY_WATER] = updated[CONF_ENERGY_WATER]
        self.hass.config_entries.async_update_entry(entry, data=other)
      return self.async_create_entry(title="", data={})

    defaults = _merge_energy_checkboxes(self.hass, current)
    return self.async_show_form(
      step_id="energy",
      data_schema=vol.Schema({
        vol.Optional(CONF_ENERGY_ELECTRICITY, default=defaults.get(CONF_ENERGY_ELECTRICITY, False)): bool,
        vol.Optional(CONF_ENERGY_GAS, default=defaults.get(CONF_ENERGY_GAS, False)): bool,
        vol.Optional(CONF_ENERGY_WATER, default=defaults.get(CONF_ENERGY_WATER, False)): bool,
      }),
    )


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _merge_energy_checkboxes(hass, current: Dict[str, Any]) -> Dict[str, Any]:
  """Merge energy checkboxes from all entries using OR logic."""
  result = {
    CONF_ENERGY_ELECTRICITY: bool(current.get(CONF_ENERGY_ELECTRICITY, False)),
    CONF_ENERGY_GAS: bool(current.get(CONF_ENERGY_GAS, False)),
    CONF_ENERGY_WATER: bool(current.get(CONF_ENERGY_WATER, False)),
  }
  for entry in hass.config_entries.async_entries(DOMAIN):
    data = dict(entry.data or {})
    for key in (CONF_ENERGY_ELECTRICITY, CONF_ENERGY_GAS, CONF_ENERGY_WATER):
      if data.get(key):
        result[key] = True
  return result


def _merge_all_entities(hass, current: Dict[str, Any]) -> Dict[str, Any]:
  """Collect entities from all config entries to show the merged state."""
  current_weather_from_sensors, current_sensors = _split_weather_entities(
    list(current.get(CONF_SENSORS, []))
  )
  all_sensors = list(current_sensors)
  all_weathers = _unique(list(current.get(CONF_WEATHERS, [])) + current_weather_from_sensors)
  all_lights = list(current.get(CONF_LIGHTS, []))
  all_switches = list(current.get(CONF_SWITCHES, []))
  all_media_players = list(current.get(CONF_MEDIA_PLAYERS, []))
  all_scene_ids = list((current.get(CONF_SCENE_MAP) or {}).values())
  scene_map_text = current.get(CONF_SCENE_MAP_TEXT, "")

  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.entry_id == current.get("_entry_id"):
      continue
    data = dict(entry.data or {})
    if entry.options:
      data.update(entry.options)
    entry_weather_from_sensors, entry_sensors = _split_weather_entities(list(data.get(CONF_SENSORS, [])))
    all_sensors.extend(entry_sensors)
    all_weathers.extend(list(data.get(CONF_WEATHERS, [])))
    all_weathers.extend(entry_weather_from_sensors)
    all_lights.extend(list(data.get(CONF_LIGHTS, [])))
    all_switches.extend(list(data.get(CONF_SWITCHES, [])))
    all_media_players.extend(list(data.get(CONF_MEDIA_PLAYERS, [])))
    all_scene_ids.extend(list((data.get(CONF_SCENE_MAP) or {}).values()))

  return {
    CONF_SENSORS: _unique(all_sensors),
    CONF_WEATHERS: _unique(all_weathers),
    CONF_LIGHTS: _unique(all_lights),
    CONF_SWITCHES: _unique(all_switches),
    CONF_MEDIA_PLAYERS: _unique(all_media_players),
    CONF_SCENE_ENTITIES: _unique(all_scene_ids),
    CONF_SCENE_MAP_TEXT: scene_map_text,
  }


def _convert_entity_data(user_input: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
  weather_from_sensors, sensors = _split_weather_entities(_normalise_entity_list(user_input.get(CONF_SENSORS, [])))
  weathers = _unique(
    _normalise_entity_list(user_input.get(CONF_WEATHERS, [])) + weather_from_sensors
  )
  lights = _normalise_entity_list(user_input.get(CONF_LIGHTS, []))
  switches = _normalise_entity_list(user_input.get(CONF_SWITCHES, []))
  media_players = _normalise_entity_list(user_input.get(CONF_MEDIA_PLAYERS, []))

  scene_map = {}
  selected_scenes = _normalise_entity_list(user_input.get(CONF_SCENE_ENTITIES, []))
  for entity_id in selected_scenes:
    entity_id = (entity_id or "").strip()
    if not entity_id:
      continue
    alias = entity_id.split(".", 1)[-1].replace("scene.", "").lower()
    base_alias = alias
    idx = 2
    while alias in scene_map:
      alias = f"{base_alias}{idx}"
      idx += 1
    scene_map[alias] = entity_id

  scene_map_text = user_input.get(CONF_SCENE_MAP_TEXT, "").strip("\n")
  manual_map = _parse_scene_map(scene_map_text)
  scene_map.update(manual_map)

  updated = dict(current)
  updated.pop("energy_enabled", None)  # remove old single checkbox
  updated.pop("energy_enabled", None)  # remove old single checkbox
  updated[CONF_SENSORS] = sensors
  updated[CONF_WEATHERS] = weathers
  updated[CONF_LIGHTS] = lights
  updated[CONF_SWITCHES] = switches
  updated[CONF_MEDIA_PLAYERS] = media_players
  updated[CONF_SCENE_MAP] = scene_map
  updated[CONF_SCENE_MAP_TEXT] = scene_map_text
  return updated


def _split_weather_entities(entities: list[str]) -> tuple[list[str], list[str]]:
  weathers: list[str] = []
  sensors: list[str] = []
  for entity_id in entities:
    cleaned = (entity_id or "").strip()
    if not cleaned:
      continue
    if cleaned.startswith("weather."):
      weathers.append(cleaned)
    else:
      sensors.append(cleaned)
  return _unique(weathers), _unique(sensors)


def _parse_scene_map(text: str) -> Dict[str, str]:
  mapping: Dict[str, str] = {}
  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
      continue
    if "=" not in line:
      raise ValueError("invalid_scene_map")
    alias, entity = line.split("=", 1)
    alias = alias.strip().lower()
    entity = entity.strip()
    if not alias or not entity:
      raise ValueError("invalid_scene_map")
    mapping[alias] = entity
  return mapping


def _normalise_topic(value: str | None, default: str) -> str:
  result = (value or "").strip() or default
  while result.endswith("/"):
    result = result[:-1]
  return result or default


def _entry_title(data: Dict[str, Any]) -> str:
  device_name = data.get(CONF_DEVICE_NAME)
  if device_name:
    return device_name
  device_id = data.get(CONF_DEVICE_ID)
  if device_id:
    return f"Panel {device_id[-4:].upper()}"
  return "HomeTiles Panel"


def _normalise_entity_list(values: Any) -> list[str]:
  entities = []
  if not isinstance(values, list):
    values = [values] if values is not None else []
  for item in values:
    if isinstance(item, str):
      entity_id = item.strip()
    elif isinstance(item, dict):
      entity_id = str(item.get("entity_id", "")).strip()
    else:
      entity_id = str(item or "").strip()
    if entity_id:
      entities.append(entity_id)
  return entities


def _unique(items: list) -> list:
  seen = set()
  result = []
  for item in items:
    if item not in seen:
      seen.add(item)
      result.append(item)
  return result
