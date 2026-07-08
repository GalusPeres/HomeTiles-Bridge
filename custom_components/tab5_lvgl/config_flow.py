# Config flow + options flow for the Tab5 LVGL integration.

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

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

  # Von async_step_zeroconf zwischengespeichert, bis async_step_zeroconf_confirm
  # abgeschlossen ist (kein persistenter State, nur fuer die Dauer des Flows).
  _discovered_host: Optional[str] = None
  _discovered_device_id: Optional[str] = None
  _discovered_name: Optional[str] = None
  _discovered_model: Optional[str] = None
  _discovered_base_topic: Optional[str] = None
  _discovered_ha_prefix: Optional[str] = None

  def _validate_topic_input(self, user_input: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Normalisiert base_topic/ha_prefix und prueft auf Kollision. Von async_step_user
    UND async_step_zeroconf_confirm genutzt, damit beide Wege dieselbe Regel haben."""
    errors: Dict[str, str] = {}
    base = _normalise_topic(user_input.get(CONF_BASE_TOPIC, DEFAULT_BASE), DEFAULT_BASE)
    prefix = _normalise_topic(user_input.get(CONF_HA_PREFIX, DEFAULT_PREFIX), DEFAULT_PREFIX)
    for entry in self._async_current_entries():
      if _entry_base_topic(entry) == base:
        errors["base_topic"] = "topic_already_configured"
        break
    return {CONF_BASE_TOPIC: base, CONF_HA_PREFIX: prefix}, errors

  async def async_step_user(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}

    if user_input is not None:
      topics, errors = self._validate_topic_input(user_input)

      if not errors:
        data: Dict[str, Any] = dict(topics)
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

  async def async_step_zeroconf(self, discovery_info: Any):
    """Panel per mDNS gefunden, BEVOR es MQTT-Zugangsdaten hat (siehe Firmware:
    startMdns() in network_manager.cpp). Zeigt eine Discovery-Karte; der eigentliche
    Zugangsdaten-Push passiert erst nach Nutzerbestaetigung in der confirm-Stufe."""
    _LOGGER.info("Tab5 LVGL: Zeroconf-Discovery ausgeloest: %r", discovery_info)
    props = getattr(discovery_info, "properties", None) or {}
    device_id = _txt(props, "device_id")
    if not device_id:
      _LOGGER.warning("Tab5 LVGL: Zeroconf-Discovery ohne device_id in den TXT-Records, ignoriert. properties=%r", props)
      return self.async_abort(reason="missing_device_id")

    # Erste Zeile, bei JEDEM Aufruf: HA ruft diese Stufe bei jedem Re-Announce /
    # jedem Neustart erneut auf, solange das Geraet sendet. Ohne konsistente
    # unique_id wuerden mehrere "Neues Geraet gefunden"-Karten fuer dasselbe
    # physische Panel entstehen.
    await self.async_set_unique_id(device_id)
    self._abort_if_unique_id_configured()

    name = _txt(props, "name") or device_id
    self._discovered_host = _discovery_host(discovery_info)
    self._discovered_device_id = device_id
    self._discovered_name = name
    self._discovered_model = _txt(props, "model")
    self._discovered_base_topic = _txt(props, "base_topic")
    self._discovered_ha_prefix = _txt(props, "ha_prefix")
    self.context["title_placeholders"] = {"name": name}

    if not self._discovered_host:
      _LOGGER.warning("Tab5 LVGL: Zeroconf-Discovery fuer %s ohne verwertbare Host-Adresse, ignoriert.", device_id)
      return self.async_abort(reason="missing_device_id")

    _LOGGER.info("Tab5 LVGL: neues Panel per Zeroconf gefunden: device_id=%s host=%s name=%s model=%s base=%s",
                 device_id, self._discovered_host, name, self._discovered_model, self._discovered_base_topic)
    return await self.async_step_zeroconf_confirm()

  async def async_step_zeroconf_confirm(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}

    if user_input is not None:
      topics, errors = self._validate_topic_input(user_input)

      if not errors:
        creds, cred_error = _get_broker_credentials(self.hass, self._discovered_host)
        if cred_error:
          _LOGGER.warning("Tab5 LVGL: Zeroconf-Pairing fuer %s abgebrochen, Grund=%s",
                           self._discovered_device_id, cred_error)
          # Nicht ueber dieses Formular loesbar (keine/inkompatible MQTT-Konfiguration
          # in HA) -- Flow beenden statt ein Formular zu zeigen, das nichts hilft.
          return self.async_abort(reason=cred_error)

        pushed = await _push_credentials_to_device(
          self.hass,
          self._discovered_host,
          creds,
          topics[CONF_BASE_TOPIC],
          topics[CONF_HA_PREFIX],
        )
        if not pushed:
          _LOGGER.warning("Tab5 LVGL: Zugangsdaten-Push an %s (%s) fehlgeschlagen",
                           self._discovered_device_id, self._discovered_host)
          errors["base"] = "cannot_connect"
        else:
          _LOGGER.info("Tab5 LVGL: Zugangsdaten erfolgreich an %s (%s) gepusht",
                       self._discovered_device_id, self._discovered_host)
          data: Dict[str, Any] = dict(topics)
          data[CONF_DEVICE_ID] = self._discovered_device_id
          if self._discovered_name:
            data[CONF_DEVICE_NAME] = self._discovered_name
          if self._discovered_model:
            data[CONF_MANUFACTURER] = "HomeTiles"
            data[CONF_MODEL] = self._discovered_model
          return self.async_create_entry(title=_entry_title(data), data=data)

    base_default = _default_base_for_discovery(
      self._discovered_base_topic,
      self._discovered_device_id,
      self._async_current_entries(),
    )
    prefix_default = _normalise_topic(self._discovered_ha_prefix, DEFAULT_PREFIX)

    return self.async_show_form(
      step_id="zeroconf_confirm",
      data_schema=vol.Schema({
        vol.Required(CONF_BASE_TOPIC, default=base_default): str,
        vol.Required(CONF_HA_PREFIX, default=prefix_default): str,
      }),
      description_placeholders={
        "name": self._discovered_name or self._discovered_device_id or "",
        "model": self._discovered_model or "HomeTiles",
        "host": self._discovered_host or "",
      },
      errors=errors,
    )

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


def _entry_base_topic(entry: config_entries.ConfigEntry) -> str:
  data = dict(entry.data or {})
  if entry.options:
    data.update(entry.options)
  return _normalise_topic(data.get(CONF_BASE_TOPIC), DEFAULT_BASE)


def _base_topic_used(entries: list[config_entries.ConfigEntry], base_topic: str) -> bool:
  return any(_entry_base_topic(entry) == base_topic for entry in entries)


def _default_base_for_discovery(
  preferred: str | None,
  device_id: str | None,
  entries: list[config_entries.ConfigEntry],
) -> str:
  base = _normalise_topic(preferred, DEFAULT_BASE)
  if not _base_topic_used(entries, base):
    return base

  suffix = ""
  if device_id:
    suffix = str(device_id).split("_")[-1].strip().lower()
  if not suffix:
    suffix = "panel"

  candidate = _normalise_topic(f"{DEFAULT_BASE}_{suffix}", DEFAULT_BASE)
  if not _base_topic_used(entries, candidate):
    return candidate

  index = 2
  while True:
    numbered = f"{candidate}_{index}"
    if not _base_topic_used(entries, numbered):
      return numbered
    index += 1


def _txt(props: Dict[str, Any], key: str) -> str:
  """mDNS-TXT-Werte sind je nach HA-Version bereits str oder noch rohe bytes."""
  val = props.get(key)
  if isinstance(val, bytes):
    try:
      return val.decode("utf-8").strip()
    except Exception:  # pragma: no cover - kaputte TXT-Daten
      return ""
  return str(val).strip() if val is not None else ""


def _discovery_host(discovery_info: Any) -> str:
  """Feldname fuer die Ziel-IP variiert je nach HA-Version (host vs. ip_address)."""
  host = getattr(discovery_info, "host", None)
  if host:
    return str(host)
  ip = getattr(discovery_info, "ip_address", None)
  if ip:
    return str(ip)
  return ""


def _host_from_url(value: str | None) -> str:
  text = str(value or "").strip()
  if not text:
    return ""
  parsed = urlparse(text if "://" in text else f"//{text}")
  return (parsed.hostname or text).strip("[]").lower()


def _ha_url_host(hass: HomeAssistant, prefer_external: bool) -> str:
  try:
    url = get_url(
      hass,
      prefer_external=prefer_external,
      allow_internal=True,
      allow_external=True,
    )
  except Exception:  # pragma: no cover - HA may not have both URLs configured
    return ""
  return _host_from_url(url)


def _source_ip_for_target(target_host: str | None) -> str:
  try:
    target_ip = ipaddress.ip_address(str(target_host or "").strip())
  except ValueError:
    return ""

  family = socket.AF_INET6 if target_ip.version == 6 else socket.AF_INET
  address = (str(target_ip), 9, 0, 0) if target_ip.version == 6 else (str(target_ip), 9)
  try:
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
      sock.connect(address)
      source_ip = sock.getsockname()[0]
  except OSError:
    return ""
  return source_ip if source_ip and not source_ip.startswith("0.") else ""


def _broker_host_for_panel(hass: HomeAssistant, broker: str, device_host: str | None = None) -> str:
  broker = str(broker or "").strip()
  broker_host = _host_from_url(broker)
  if not broker_host:
    return broker

  internal_host = _ha_url_host(hass, prefer_external=False)
  external_host = _ha_url_host(hass, prefer_external=True)
  source_ip = _source_ip_for_target(device_host)
  local_only_hosts = {"core-mosquitto", "localhost", "127.0.0.1", "::1"}

  if broker_host in local_only_hosts:
    return source_ip or internal_host or broker

  if broker_host in {host for host in (internal_host, external_host) if host}:
    _LOGGER.info(
      "Tab5 LVGL: MQTT-Broker %s entspricht HA-URL, verwende fuer Panel lokale Adresse %s",
      broker,
      source_ip or internal_host or broker,
    )
    return source_ip or internal_host or broker

  return broker


def _get_broker_credentials(hass: HomeAssistant, device_host: str | None = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
  """Liest die Zugangsdaten von HA's eigener mqtt-Integration aus.

  Rueckgabe (creds, error): error ist None bei Erfolg, sonst einer von
  "mqtt_not_configured" / "unsupported_broker" / "mqtt_read_failed" -- alle
  drei sind ueber dieses Formular nicht loesbar, der Flow bricht dann ab statt
  ein Formular zu zeigen, das dem Nutzer nicht weiterhilft.
  """
  try:
    from homeassistant.components.mqtt.const import CONF_BROKER, CONF_CLIENT_CERT, CONF_CLIENT_KEY
    from homeassistant.const import CONF_PASSWORD, CONF_PORT, CONF_USERNAME
  except ImportError:  # pragma: no cover - HA-interne Struktur hat sich geaendert
    return None, "mqtt_read_failed"

  # "mqtt" ist Literal statt mqtt.DOMAIN, um nicht von einem re-exportierten
  # Attribut abzuhaengen, das in dieser HA-Version evtl. nicht existiert.
  mqtt_entries = [
    entry for entry in hass.config_entries.async_entries("mqtt")
    if entry.state is ConfigEntryState.LOADED
  ]
  if not mqtt_entries:
    return None, "mqtt_not_configured"

  data = dict(mqtt_entries[0].data or {})
  broker = data.get(CONF_BROKER)
  if not broker:
    return None, "mqtt_not_configured"
  broker = _broker_host_for_panel(hass, str(broker), device_host)

  try:
    port = int(data.get(CONF_PORT, 1883) or 1883)
  except (TypeError, ValueError):
    port = 1883

  # Die Firmware kann kein TLS (nur ein plaines WiFiClient, siehe
  # network_manager.h) -- ein Broker, der Client-Zertifikate verlangt oder nur
  # ueber den TLS-Standardport 8883 erreichbar ist, wuerde nie funktionieren.
  if port == 8883 or data.get(CONF_CLIENT_CERT) or data.get(CONF_CLIENT_KEY):
    return None, "unsupported_broker"

  return {
    "host": broker,
    "port": port,
    "username": data.get(CONF_USERNAME) or "",
    "password": data.get(CONF_PASSWORD) or "",
  }, None


async def _push_credentials_to_device(
  hass: HomeAssistant,
  device_host: str,
  creds: Dict[str, Any],
  base_topic: str,
  ha_prefix: str,
) -> bool:
  """Schiebt die Broker-Zugangsdaten per POST /mqtt (bestehender Admin-Endpoint,
  siehe web_admin_handlers.cpp:handleSaveMQTT) auf das Panel und stoesst danach
  einen Neustart an (POST /restart) -- ohne den bleibt mqtt_enabled auf dem
  Boot-Latch stehen und die neuen Zugangsdaten wirken nie (siehe network_manager.cpp)."""
  session = async_get_clientsession(hass)
  timeout = aiohttp.ClientTimeout(total=5)
  form = {
    "mqtt_host": str(creds["host"]),
    "mqtt_port": str(creds["port"]),
    "mqtt_user": creds["username"],
    "mqtt_pass": creds["password"],
    "mqtt_base": base_topic,
    "ha_prefix": ha_prefix,
  }
  try:
    async with session.post(f"http://{device_host}/mqtt", data=form, timeout=timeout, allow_redirects=False) as resp:
      if resp.status not in (200, 303):
        return False
    async with session.post(f"http://{device_host}/restart", data={}, timeout=timeout, allow_redirects=False) as resp:
      if resp.status not in (200, 303):
        return False
  except (aiohttp.ClientError, asyncio.TimeoutError):
    return False
  return True


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
