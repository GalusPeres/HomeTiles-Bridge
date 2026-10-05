"""Entity icons as Home Assistant's frontend resolves them.

Home Assistant keeps most entity icons in the icons.json files of the
integrations (icon translations), not in the state attributes: per
translation key of an integration's entities ("entity", for example the
GitHub stars sensor) and per device class of a domain ("entity_component",
for example a door binary sensor). The Bridge loads them once per
integration with homeassistant.helpers.icon.async_get_icons and resolves
them like the frontend: an icon of the current state, an icon of a numeric
range, else the default icon.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

try:
  from homeassistant.helpers.icon import async_get_icons
except ImportError:  # Home Assistant without icon translations
  async_get_icons = None

_LOGGER = logging.getLogger(__name__)

DATA_ICON_TRANSLATIONS = "tab5_lvgl_icon_translations"


def resolve_icon(data: Any, state_value: Any) -> Optional[str]:
  """The icon of one icons.json entry for a state value."""
  if not isinstance(data, dict):
    return None
  states = data.get("state")
  if isinstance(states, dict) and state_value is not None:
    icon = states.get(str(state_value))
    if isinstance(icon, str) and icon:
      return icon
  ranges = data.get("range")
  if isinstance(ranges, dict):
    try:
      value = float(state_value)
    except (TypeError, ValueError):
      value = None
    if value is not None:
      # The highest range that the value reaches.
      best_threshold, best_icon = None, None
      for key, icon in ranges.items():
        try:
          threshold = float(key)
        except (TypeError, ValueError):
          continue
        if threshold <= value and isinstance(icon, str) and icon and (
            best_threshold is None or threshold > best_threshold):
          best_threshold, best_icon = threshold, icon
      if best_icon:
        return best_icon
  default = data.get("default")
  return default if isinstance(default, str) and default else None


def _cache(hass: Any) -> Dict[str, Dict[str, Any]]:
  return hass.data.setdefault(DATA_ICON_TRANSLATIONS, {"entity": {}, "component": {}})


def cached_entity_icon(hass: Any, state: Any, registry_entry: Any, *, use_state: bool = True) -> Optional[str]:
  """The integration's icon for the entity's translation key (its default
  icon only without `use_state`)."""
  platform = getattr(registry_entry, "platform", None)
  key = getattr(registry_entry, "translation_key", None)
  if not hass or not platform or not key:
    return None
  domain = state.entity_id.split(".", 1)[0]
  data = _cache(hass)["entity"].get(platform, {}).get(domain, {}).get(key)
  return resolve_icon(data, state.state if use_state else None)


def cached_component_icon(hass: Any, state: Any) -> Optional[str]:
  """The domain's icon for the entity's device class (or its default)."""
  if not hass:
    return None
  domain = state.entity_id.split(".", 1)[0]
  icons = _cache(hass)["component"].get(domain, {})
  device_class = (state.attributes or {}).get("device_class")
  data = icons.get(device_class) if isinstance(device_class, str) and device_class in icons else icons.get("_")
  return resolve_icon(data, state.state)


async def _async_fetch(hass: Any, category: str, components: Iterable[str]) -> Dict[str, Any]:
  components = set(components)
  if not components:
    return {}
  try:
    return await async_get_icons(hass, category, components)
  except Exception:  # noqa: BLE001 - one broken integration must not hide the others
    found: Dict[str, Any] = {}
    for component in components:
      try:
        found.update(await async_get_icons(hass, category, {component}))
      except Exception as err:  # noqa: BLE001
        _LOGGER.debug("No %s icons for %s: %s", category, component, err)
    return found


async def async_load_entity_icons(hass: Any, entity_ids: Iterable[str]) -> None:
  """Load the icon translations of these entities' integrations and domains.

  Home Assistant caches the icon files; this keeps the result for the sync
  icon lookups and only asks for integrations not loaded yet.
  """
  if async_get_icons is None or not hass:
    return
  from homeassistant.helpers import entity_registry as er

  registry = er.async_get(hass)
  cache = _cache(hass)
  platforms, domains = set(), set()
  for entity_id in entity_ids:
    if not isinstance(entity_id, str) or "." not in entity_id:
      continue
    domain = entity_id.split(".", 1)[0]
    if domain not in cache["component"]:
      domains.add(domain)
    entry = registry.async_get(entity_id)
    platform = getattr(entry, "platform", None)
    if platform and getattr(entry, "translation_key", None) and platform not in cache["entity"]:
      platforms.add(platform)
  if platforms:
    cache["entity"].update(await _async_fetch(hass, "entity", platforms))
    for platform in platforms:
      cache["entity"].setdefault(platform, {})
  if domains:
    cache["component"].update(await _async_fetch(hass, "entity_component", domains))
    for domain in domains:
      cache["component"].setdefault(domain, {})
