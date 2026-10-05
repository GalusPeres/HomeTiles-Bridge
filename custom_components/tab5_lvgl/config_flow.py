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

from .access_helpers import parse_access_codes
from .binary_sensor_helpers import split_binary_sensor_entities
from .panel_auth import ERROR_CANNOT_CONNECT, async_push_credentials, async_push_link
from .capabilities import CAPABILITIES
from .command_channel import entry_pairing_key, entry_removing_key, key_id_for_key, with_pairing_key, without_pairing
from .pairing import ANSWER_PAIRED, ANSWER_WAITING
from .control_helpers import ACTION_DOMAINS, SWITCH_DOMAINS, build_action_map, entity_domain
from .editable_helpers import (EDITABLE_LISTS, EDITABLE_DOMAINS, NUMBER_DOMAINS, SELECT_DOMAINS, DATETIME_DOMAINS, editable_selection, domain_of, build_editable_payload, build_editable_service_call, add_number_history, MAX_CONTROL_BYTES)
from .const import (
  CONF_NUMBERS, CONF_SELECTS, CONF_DATETIMES,
  CONF_ACCESS_CODES,
  CONF_BASE_TOPIC,
  CONF_BINARY_SENSORS,
  CONF_CAMERAS,
  CONF_ALARM_PANELS,
  CONF_CLIMATES,
  CONF_COMMAND_PAIRING_REMOVING,
  CONF_COVERS,
  CONF_DEVICE_ID,
  CONF_DEVICE_NAME,
  CONF_ENERGY_ELECTRICITY,
  CONF_ENERGY_GAS,
  CONF_ENERGY_WATER,
  CONF_FANS,
  CONF_HA_PREFIX,
  CONF_LIGHTS,
  CONF_LOCAL_IO,
  CONF_LOCKS,
  CONF_MANUFACTURER,
  CONF_MEDIA_PLAYERS,
  CONF_MODEL,
  CONF_OPEN_WITHOUT_CODE,
  CONF_SCENE_ENTITIES,
  CONF_SCENE_MAP,
  CONF_SCENE_MAP_TEXT,
  CONF_SENSORS,
  CONF_SWITCHES,
  CONF_TRANSPORT,
  DATA_LINK,
  CONF_WEATHERS,
  DEFAULT_BASE,
  DEFAULT_PREFIX,
  DISCOVERY_ADOPT_ENTRY,
  DISCOVERY_PAIRING_ATTEMPT,
  DISCOVERY_PAIRING_ENTRY,
  DOMAIN,
  PAIRING_UNIQUE_ID_PREFIX,
  TRANSPORT_LINK,
)

_LOGGER = logging.getLogger(__name__)

CONF_PROVISION_MQTT_HOST = "mqtt_host"
CONF_PROVISION_MQTT_PORT = "mqtt_port"
CONF_PROVISION_MQTT_USERNAME = "mqtt_username"
CONF_PROVISION_MQTT_PASSWORD = "mqtt_password"
# Optional Web Admin password of the panel. Used only for the pairing push and
# never stored in the config entry or logged.
CONF_PROVISION_PANEL_PASSWORD = "panel_password"
# Options step "security": removes the pairing key (CONF_COMMAND_PAIRING).
CONF_REMOVE_PAIRING = "remove_pairing"
# Result of the user's answer on a pairing card (pairing.ANSWER_*).
_PAIRING_RESULTS = {"paired": "pairing_done", "waiting": "pairing_confirmed", "rejected": "pairing_rejected"}
# Direct link setup: the panel restarts before it pairs; the user then has the
# pairing's own two minutes to confirm on the display.
LINK_PROMPT_TIMEOUT_S = 90.0
LINK_FINISH_TIMEOUT_S = 120.0
# State of the pairing in the options menu ("Security: encrypted").
_SECURITY_STATES = {
  "de": {"paired": "verschl\u00fcsselt", "removing": "wird ausgeschaltet", "off": "nicht verschl\u00fcsselt"},
  "en": {"paired": "encrypted", "removing": "turning off", "off": "not encrypted"},
}


def _language(hass: Any) -> str:
  """Home Assistant's language for texts the Bridge builds itself; English otherwise."""
  language = str(getattr(hass.config, "language", None) or "en").split("-")[0].lower()
  return language if language in _SECURITY_STATES else "en"


def _security_state(hass: Any, entry: Any) -> str:
  if entry_pairing_key(entry) is not None:
    state = "paired"
  elif entry_removing_key(entry) is not None:
    state = "removing"
  else:
    state = "off"
  return _SECURITY_STATES[_language(hass)][state]


# ---------------------------------------------------------------------------
#  Config flow — adding a new panel (device info only)
# ---------------------------------------------------------------------------

class Tab5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
  VERSION = 2

  # Von async_step_zeroconf zwischengespeichert, bis async_step_zeroconf_confirm
  # abgeschlossen ist (kein persistenter State, nur fuer die Dauer des Flows).
  _discovered_host: Optional[str] = None
  _discovered_device_id: Optional[str] = None
  _discovered_name: Optional[str] = None
  _discovered_model: Optional[str] = None
  _discovered_base_topic: Optional[str] = None
  _discovered_ha_prefix: Optional[str] = None
  _discovered_mqtt_creds: Optional[Dict[str, Any]] = None
  _discovered_mqtt_error: Optional[str] = None
  # Panel data announced over MQTT, kept until the user confirms the card.
  _discovered_data: Optional[Dict[str, Any]] = None
  # Existing entry the announcing panel would be linked to (adopt_confirm).
  _adopt_entry_id: Optional[str] = None
  # Pairing card: the entry whose panel asks to pair, and the attempt.
  _pairing_entry_id: Optional[str] = None
  _pairing_attempt: Optional[str] = None
  # Direct link setup (link_runtime.PendingLink): the pairing this dialog
  # waits for, the entry data it creates and the task behind a progress step.
  _link_pending: Any = None
  _link_data: Optional[Dict[str, Any]] = None
  _link_task: Optional[asyncio.Task] = None
  # An entry set up over MQTT that this dialog switches to the link.
  _link_entry_id: Optional[str] = None

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

  async def _mqtt_defaults_for_discovery(self) -> Tuple[Dict[str, Any], Optional[str]]:
    """Ermittelt MQTT-Vorschlaege fuer den Discovery-Dialog.

    Die Werte werden nur als Formular-Default genutzt. Der Nutzer kann sie vor
    dem Push ans Panel immer ueberschreiben.
    """
    if self._discovered_mqtt_creds is not None:
      return dict(self._discovered_mqtt_creds), self._discovered_mqtt_error

    creds, cred_error = _get_broker_credentials(self.hass, self._discovered_host)
    if creds is None:
      creds = _fallback_broker_credentials(self.hass, self._discovered_host)
    else:
      creds = dict(creds)
    # HA's own MQTT integration credentials are often internal/add-on
    # credentials and may not be the login a physical panel should use, and
    # guessing a login via the Supervisor API is unreliable (picks up
    # whatever single login exists, even if it belongs to an unrelated
    # device). Keep host/port automatic, but make auth an explicit user
    # decision.
    creds["username"] = ""
    creds["password"] = ""
    self._discovered_mqtt_creds = dict(creds)
    self._discovered_mqtt_error = cred_error
    return dict(creds), cred_error

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

  async def async_step_integration_discovery(self, discovery_info: Dict[str, Any]):
    """A panel announced itself over MQTT; ask the user before adding it."""
    if discovery_info.get(DISCOVERY_PAIRING_ENTRY):
      return await self._async_start_pairing_card(discovery_info)
    device_id = discovery_info.get(CONF_DEVICE_ID)
    if not device_id:
      return self.async_abort(reason="missing_device_id")
    await self.async_set_unique_id(device_id)
    self._abort_if_unique_id_configured()
    self._discovered_data = dict(discovery_info)
    self._adopt_entry_id = self._discovered_data.pop(DISCOVERY_ADOPT_ENTRY, None)
    self.context["title_placeholders"] = {"name": _entry_title(self._discovered_data)}
    if self._adopt_entry_id:
      return await self.async_step_adopt_confirm()
    return await self.async_step_discovery_confirm()

  def _adoptable_entry(self) -> Optional[config_entries.ConfigEntry]:
    """The entry the announcing panel asks to be linked to, while still free."""
    entry = self.hass.config_entries.async_get_entry(self._adopt_entry_id or "")
    if entry is None or entry.domain != DOMAIN:
      return None
    data = self._discovered_data or {}
    if _entry_base_topic(entry) != data.get(CONF_BASE_TOPIC):
      return None
    bound = str(entry.data.get(CONF_DEVICE_ID) or entry.unique_id or "")
    if bound and not (
      len(bound) == 14 and bound.startswith("tab5_lvgl_")
      and str(data.get(CONF_DEVICE_ID) or "").upper().endswith(bound[-4:].upper())
    ):
      return None
    return entry

  async def async_step_adopt_confirm(self, user_input: Dict[str, Any] | None = None):
    """Link an announcing panel to an existing entry with its base topic.

    Any MQTT client can announce a base topic, so the user confirms before a
    manually added entry (or one from firmware before v0.3.1) is bound to
    this panel's device id.
    """
    entry = self._adoptable_entry()
    if entry is None:
      return self.async_abort(reason="adopt_target_changed")
    data = dict(self._discovered_data or {})
    if user_input is not None:
      self._abort_if_unique_id_configured()
      new_data = dict(entry.data)
      new_data[CONF_DEVICE_ID] = data[CONF_DEVICE_ID]
      for key in (CONF_LOCAL_IO, CAPABILITIES):
        if key in data:
          new_data[key] = data[key]
      _LOGGER.info("HomeTiles Bridge linked device %s to the existing entry", data[CONF_DEVICE_ID])
      self.hass.config_entries.async_update_entry(
        entry, data=new_data, title=_entry_title(new_data), unique_id=data[CONF_DEVICE_ID],
      )
      await self.hass.config_entries.async_reload(entry.entry_id)
      return self.async_abort(reason="panel_linked")
    self._set_confirm_only()
    return self.async_show_form(
      step_id="adopt_confirm",
      description_placeholders={
        "name": _entry_title(data),
        "base_topic": data.get(CONF_BASE_TOPIC) or "",
        "entry": entry.title,
      },
    )

  async def async_step_discovery_confirm(self, user_input: Dict[str, Any] | None = None):
    data = dict(self._discovered_data or {})
    if user_input is not None:
      self._abort_if_unique_id_configured()
      return self.async_create_entry(title=_entry_title(data), data=data)
    self._set_confirm_only()
    return self.async_show_form(
      step_id="discovery_confirm",
      description_placeholders={
        "name": _entry_title(data),
        "base_topic": data.get(CONF_BASE_TOPIC) or "",
      },
    )

  # ---- Pairing card: the panel asks to pair (pairing.py) ----

  def _pairing_bridge(self) -> Any:
    return self.hass.data.get(DOMAIN, {}).get("entries", {}).get(self._pairing_entry_id or "")

  def _pairing_number(self) -> Optional[str]:
    bridge = self._pairing_bridge()
    return bridge.pairing_number(self._pairing_attempt) if bridge is not None else None

  async def _async_start_pairing_card(self, discovery_info: Dict[str, Any]):
    self._pairing_entry_id = str(discovery_info.get(DISCOVERY_PAIRING_ENTRY))
    self._pairing_attempt = str(discovery_info.get(DISCOVERY_PAIRING_ATTEMPT) or "")
    # One card per panel; "Ignore" on it rejects the attempt (async_step_ignore).
    await self.async_set_unique_id(f"{PAIRING_UNIQUE_ID_PREFIX}{self._pairing_entry_id}")
    entry = self.hass.config_entries.async_get_entry(self._pairing_entry_id)
    number = self._pairing_number()
    if entry is None or number is None:
      return self.async_abort(reason="pairing_expired")
    # Short, so the card never cuts the number off; the dialog explains the rest.
    self.context["title_placeholders"] = {"name": f"{entry.title} ({number})"}
    return await self.async_step_pairing_confirm()

  async def async_step_pairing_confirm(self, user_input: Dict[str, Any] | None = None):
    """Show the number; the user compares it with the one on the display."""
    entry = self.hass.config_entries.async_get_entry(self._pairing_entry_id or "")
    number = self._pairing_number()
    if entry is None or number is None:
      return self.async_abort(reason="pairing_expired")
    return self.async_show_menu(
      step_id="pairing_confirm",
      menu_options=["pairing_accept", "pairing_reject"],
      description_placeholders={"name": entry.title, "number": number},
    )

  async def async_step_pairing_accept(self, user_input: Dict[str, Any] | None = None):
    return await self._async_answer_pairing(True)

  async def async_step_pairing_reject(self, user_input: Dict[str, Any] | None = None):
    return await self._async_answer_pairing(False)

  async def _async_answer_pairing(self, accept: bool):
    bridge = self._pairing_bridge()
    outcome = None
    if bridge is not None:
      outcome = await bridge.async_answer_pairing(self._pairing_attempt, accept, self.flow_id)
    return self.async_abort(reason=_PAIRING_RESULTS.get(outcome, "pairing_expired"))

  async def async_step_ignore(self, user_input: Dict[str, Any]):
    """Ignoring a pairing card rejects the pairing; other cards are ignored as usual."""
    unique_id = str(user_input.get("unique_id") or "")
    if not unique_id.startswith(PAIRING_UNIQUE_ID_PREFIX):
      return await super().async_step_ignore(user_input)
    self._pairing_entry_id = unique_id[len(PAIRING_UNIQUE_ID_PREFIX):]
    bridge = self._pairing_bridge()
    if bridge is not None:
      await bridge.async_answer_pairing(None, False)
    return self.async_abort(reason="pairing_rejected")

  async def async_step_zeroconf(self, discovery_info: Any):
    """Panel per mDNS gefunden, BEVOR es MQTT-Zugangsdaten hat (siehe Firmware:
    startMdns() in network_manager.cpp). Zeigt eine Discovery-Karte; der eigentliche
    Zugangsdaten-Push passiert erst nach Nutzerbestaetigung in der confirm-Stufe."""
    # WARNING statt INFO: bei diesem Nutzer kommen INFO-Meldungen im
    # exportierten HA-Log nicht an (Logger-Level-Problem, unabhaengig von
    # unserem Code) -- WARNING kommt nachweislich durch (siehe die
    # "Tab5 LVGL DEBUG: Publishing config..."-Zeilen in __init__.py).
    _LOGGER.warning("Tab5 LVGL DEBUG: Zeroconf-Discovery ausgeloest: %r", discovery_info)
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
    # Firmware with the direct link announces itself only while Pair was
    # pressed on the panel (TXT pair=1). Then an existing entry is switched to
    # the link instead of being skipped.
    link = _txt(props, "link") == "1"
    pairing = link and _txt(props, "pair") == "1"
    if not pairing:
      self._abort_if_unique_id_configured()
    if link and not pairing:
      return self.async_abort(reason="link_press_pair")

    name = _txt(props, "name") or device_id
    self._discovered_host = _discovery_host(discovery_info)
    self._discovered_device_id = device_id
    self._discovered_name = name
    self._discovered_model = _txt(props, "model")
    self._discovered_base_topic = _txt(props, "base_topic")
    self._discovered_ha_prefix = _txt(props, "ha_prefix")
    self._discovered_mqtt_creds = None
    self._discovered_mqtt_error = None
    self.context["title_placeholders"] = {"name": name}

    if not self._discovered_host:
      _LOGGER.warning("Tab5 LVGL: Zeroconf-Discovery fuer %s ohne verwertbare Host-Adresse, ignoriert.", device_id)
      return self.async_abort(reason="missing_device_id")

    _LOGGER.warning("Tab5 LVGL DEBUG: neues Panel per Zeroconf gefunden: device_id=%s host=%s name=%s model=%s base=%s",
                    device_id, self._discovered_host, name, self._discovered_model, self._discovered_base_topic)
    if pairing:
      # No MQTT, pairing in this dialog. The first panel arrives before Home
      # Assistant set the integration up, so the link starts here if needed.
      from . import async_ensure_link_runtime
      await async_ensure_link_runtime(self.hass)
      existing = self._link_existing_entry(device_id)
      self._link_entry_id = existing.entry_id if existing is not None else None
      if existing is not None:
        self.context["title_placeholders"] = {"name": existing.title}
        return await self.async_step_link_switch()
      return await self.async_step_link_confirm()
    return await self.async_step_zeroconf_confirm()

  async def async_step_zeroconf_confirm(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}
    mqtt_defaults, _ = await self._mqtt_defaults_for_discovery()

    if user_input is not None:
      topics, errors = self._validate_topic_input(user_input)
      creds, mqtt_errors = _mqtt_credentials_from_user_input(user_input, mqtt_defaults)
      errors.update(mqtt_errors)

      if not errors:
        push_error = await _push_credentials_to_device(
          self.hass,
          self._discovered_host,
          creds,
          topics[CONF_BASE_TOPIC],
          topics[CONF_HA_PREFIX],
          str(user_input.get(CONF_PROVISION_PANEL_PASSWORD) or ""),
        )
        if push_error:
          _LOGGER.warning("HomeTiles Bridge could not send MQTT credentials to %s (%s): %s",
                          self._discovered_device_id, self._discovered_host, push_error)
          errors["base"] = push_error
        else:
          _LOGGER.info("HomeTiles Bridge sent MQTT credentials to %s (%s)",
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
        vol.Required(CONF_PROVISION_MQTT_HOST, default=mqtt_defaults.get("host", "")): str,
        vol.Required(CONF_PROVISION_MQTT_PORT, default=mqtt_defaults.get("port", 1883)): int,
        vol.Optional(CONF_PROVISION_MQTT_USERNAME, default=mqtt_defaults.get("username", "")): str,
        vol.Optional(CONF_PROVISION_MQTT_PASSWORD, default=mqtt_defaults.get("password", "")): _password_schema(),
        vol.Required(CONF_BASE_TOPIC, default=base_default): str,
        vol.Required(CONF_HA_PREFIX, default=prefix_default): str,
        # Never prefilled: the panel password is typed for each pairing.
        vol.Optional(CONF_PROVISION_PANEL_PASSWORD, default=""): _password_schema(),
      }),
      description_placeholders={
        "name": self._discovered_name or self._discovered_device_id or "",
        "model": self._discovered_model or "HomeTiles",
        "host": self._discovered_host or "",
      },
      errors=errors,
    )

  # ---- Direct link: add a panel without MQTT and pair it in this dialog ----

  def _link_existing_entry(self, device_id: str) -> Optional[config_entries.ConfigEntry]:
    """The entry of this panel, unless the user ignored its discovery."""
    for entry in self._async_current_entries(include_ignore=False):
      values = dict(entry.data or {})
      values.update(entry.options or {})
      if entry.unique_id == device_id or str(values.get(CONF_DEVICE_ID) or "") == device_id:
        return entry
    return None

  async def async_step_link_confirm(self, user_input: Dict[str, Any] | None = None):
    """Add a panel whose Pair button was pressed (docs-dev/bridge-link.md, Setup)."""
    return await self._async_link_start("link_confirm", user_input)

  async def async_step_link_switch(self, user_input: Dict[str, Any] | None = None):
    """Move a panel set up over MQTT to the link; its entry stays."""
    return await self._async_link_start("link_switch", user_input)

  async def _async_link_start(self, step_id: str, user_input: Dict[str, Any] | None):
    errors: Dict[str, str] = {}
    runtime = _link_runtime(self.hass)
    if runtime is None or runtime.port is None:
      return self.async_abort(reason="link_unavailable")
    existing = (self.hass.config_entries.async_get_entry(self._link_entry_id)
                if self._link_entry_id else None)
    if self._link_entry_id and existing is None:
      return self.async_abort(reason="pairing_expired")
    if user_input is not None:
      if existing is None:
        self._abort_if_unique_id_configured()
        base = _default_base_for_discovery(
          self._discovered_base_topic, self._discovered_device_id, self._async_current_entries(),
        )
        prefix = _normalise_topic(self._discovered_ha_prefix, DEFAULT_PREFIX)
      else:
        # The panel keeps its topics, so every tile keeps working.
        values = dict(existing.data or {})
        values.update(existing.options or {})
        base = _entry_base_topic(existing)
        prefix = _normalise_topic(values.get(CONF_HA_PREFIX), DEFAULT_PREFIX)
      self._close_link()
      pending = runtime.begin_setup(str(self._discovered_device_id), base, prefix)
      error = await _push_link_to_device(self.hass, self._discovered_host, runtime.port, base, prefix)
      if error:
        pending.close()
        _LOGGER.warning("HomeTiles Bridge could not set up the link of %s (%s): %s",
                        self._discovered_device_id, self._discovered_host, error)
        errors["base"] = error
      else:
        _LOGGER.info("HomeTiles Bridge sent its link address to %s (%s); waiting for pairing",
                     self._discovered_device_id, self._discovered_host)
        self._link_pending = pending
        data: Dict[str, Any] = {
          CONF_BASE_TOPIC: base,
          CONF_HA_PREFIX: prefix,
          CONF_DEVICE_ID: self._discovered_device_id,
          CONF_TRANSPORT: TRANSPORT_LINK,
        }
        if self._discovered_name:
          data[CONF_DEVICE_NAME] = self._discovered_name
        if self._discovered_model:
          data[CONF_MANUFACTURER] = "HomeTiles"
          data[CONF_MODEL] = self._discovered_model
        self._link_data = data
        return await self.async_step_link_wait()
    self._set_confirm_only()
    return self.async_show_form(
      step_id=step_id,
      description_placeholders={
        "name": (existing.title if existing is not None else None)
                or self._discovered_name or self._discovered_device_id or "",
        "model": self._discovered_model or "HomeTiles",
        "host": self._discovered_host or "",
      },
      errors=errors,
    )

  async def async_step_link_wait(self, user_input: Dict[str, Any] | None = None):
    """The panel restarts and starts pairing; then the number is shown."""
    pending = self._link_pending
    if pending is None:
      return self.async_abort(reason="pairing_expired")
    if self._link_task is None:
      self._link_task = self.hass.async_create_task(_wait_event(pending.prompted, LINK_PROMPT_TIMEOUT_S))
    if not self._link_task.done():
      return self.async_show_progress(
        step_id="link_wait", progress_action="link_wait", progress_task=self._link_task,
        description_placeholders={"name": self._discovered_name or self._discovered_device_id or ""},
      )
    self._link_task = None
    if pending.number is None or pending.failure:
      return self.async_show_progress_done(next_step_id="link_failed")
    return self.async_show_progress_done(next_step_id="link_number")

  async def async_step_link_number(self, user_input: Dict[str, Any] | None = None):
    """The user compares the number with the one on the display."""
    pending = self._link_pending
    if pending is None or pending.number is None or pending.failure:
      return await self.async_step_link_failed()
    return self.async_show_menu(
      step_id="link_number",
      menu_options=["link_accept", "link_reject"],
      description_placeholders={
        "name": self._discovered_name or self._discovered_device_id or "",
        "number": pending.number,
      },
    )

  async def async_step_link_accept(self, user_input: Dict[str, Any] | None = None):
    pending = self._link_pending
    if pending is None:
      return self.async_abort(reason="pairing_expired")
    outcome = pending.answer(True)
    if outcome == ANSWER_PAIRED and pending.key is not None:
      return self._async_create_link_entry()
    if outcome == ANSWER_WAITING:
      return await self.async_step_link_finish()
    return await self.async_step_link_failed()

  async def async_step_link_finish(self, user_input: Dict[str, Any] | None = None):
    """Confirmed here; waiting for the confirmation on the display."""
    pending = self._link_pending
    if pending is None:
      return self.async_abort(reason="pairing_expired")
    if self._link_task is None:
      self._link_task = self.hass.async_create_task(_wait_event(pending.finished, LINK_FINISH_TIMEOUT_S))
    if not self._link_task.done():
      return self.async_show_progress(
        step_id="link_finish", progress_action="link_finish", progress_task=self._link_task,
        description_placeholders={"name": self._discovered_name or self._discovered_device_id or ""},
      )
    self._link_task = None
    if pending.key is None:
      return self.async_show_progress_done(next_step_id="link_failed")
    return self.async_show_progress_done(next_step_id="link_create")

  async def async_step_link_create(self, user_input: Dict[str, Any] | None = None):
    pending = self._link_pending
    if pending is None or pending.key is None:
      return await self.async_step_link_failed()
    return self._async_create_link_entry()

  async def async_step_link_reject(self, user_input: Dict[str, Any] | None = None):
    pending = self._link_pending
    if pending is not None:
      pending.answer(False)
    self._close_link()
    return self.async_abort(reason="link_rejected")

  async def async_step_link_failed(self, user_input: Dict[str, Any] | None = None):
    self._close_link()
    return self.async_abort(reason="link_failed")

  def _async_create_link_entry(self):
    pending = self._link_pending
    key = pending.key
    link_data = dict(self._link_data or {})
    self._close_link()
    existing = (self.hass.config_entries.async_get_entry(self._link_entry_id)
                if self._link_entry_id else None)
    if existing is not None:
      # Keep everything the user set up; only the transport and the key
      # change. The update listener reloads the entry on the link.
      data = with_pairing_key(dict(existing.data or {}), key)
      data[CONF_TRANSPORT] = TRANSPORT_LINK
      data[CONF_BASE_TOPIC] = link_data.get(CONF_BASE_TOPIC, data.get(CONF_BASE_TOPIC))
      data[CONF_HA_PREFIX] = link_data.get(CONF_HA_PREFIX, data.get(CONF_HA_PREFIX))
      self.hass.config_entries.async_update_entry(
        existing, data=data, options=without_pairing(existing.options),
      )
      _LOGGER.info("HomeTiles Bridge switched %s to the direct link", data.get(CONF_DEVICE_ID))
      return self.async_abort(reason="link_switched")
    data = with_pairing_key(link_data, key)
    self._abort_if_unique_id_configured()
    _LOGGER.info("HomeTiles Bridge paired %s over the direct link", data.get(CONF_DEVICE_ID))
    return self.async_create_entry(title=_entry_title(data), data=data)

  def _close_link(self) -> None:
    if self._link_task is not None and not self._link_task.done():
      self._link_task.cancel()
    self._link_task = None
    if self._link_pending is not None:
      self._link_pending.close()
      self._link_pending = None

  @callback
  def async_remove(self) -> None:
    """The dialog was closed: stop waiting for the panel."""
    self._close_link()

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
      menu_options=["panel", "entities", "access_codes", "energy", "security"],
      description_placeholders={"security": _security_state(self.hass, self.config_entry)},
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

  # ---- Encrypted panel commands (command_channel.py) ----

  async def async_step_security(self, user_input: Dict[str, Any] | None = None):
    """Pairing happens on the display (a card shows the number); here it is removed."""
    stored_key = entry_pairing_key(self.config_entry)
    if stored_key is None:
      if entry_removing_key(self.config_entry) is not None:
        return self.async_abort(reason="pairing_removing")
      return self.async_abort(reason="pairing_not_paired")
    errors: Dict[str, str] = {}

    if user_input is not None:
      if user_input.get(CONF_REMOVE_PAIRING):
        # The Bridge keeps the key until the panel turned pairing off too:
        # it sends the panel an unpair as soon as they have a session.
        updated = without_pairing(self.config_entry.data)
        updated[CONF_COMMAND_PAIRING_REMOVING] = stored_key.hex()
        # The update listener reloads the entry with a removing channel.
        self.hass.config_entries.async_update_entry(
          self.config_entry, data=updated, options=without_pairing(self.config_entry.options),
        )
        return self.async_abort(reason="pairing_removed")
      # An empty form changes nothing; say so instead of a plain success.
      errors["base"] = "pairing_nothing_selected"

    return self.async_show_form(
      step_id="security",
      data_schema=vol.Schema({
        vol.Optional(CONF_REMOVE_PAIRING, default=False): bool,
      }),
      description_placeholders={"key_id": key_id_for_key(stored_key) or "-"},
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
        # Diese Auswahl gilt laut Formularbeschreibung "fuer alle Panels", lag
        # bisher aber nur auf dem einen Eintrag, ueber dessen Menue man sie
        # bearbeitet hat -- wurde der geloescht (z.B. zum Neu-Pairen nach
        # einem device_id-Wechsel), war die gesamte Auswahl ersatzlos weg.
        # Auf allen Eintraegen spiegeln, wie es async_step_energy schon tut.
        shared_keys = (
          "user_sensor_selections",
          CONF_SENSORS, CONF_BINARY_SENSORS, CONF_WEATHERS, CONF_LIGHTS, CONF_SWITCHES,
          CONF_NUMBERS, CONF_SELECTS, CONF_DATETIMES,
          CONF_CLIMATES, CONF_COVERS,
          CONF_MEDIA_PLAYERS, CONF_CAMERAS, CONF_SCENE_MAP, CONF_SCENE_MAP_TEXT,
          CONF_FANS, CONF_LOCKS, CONF_ALARM_PANELS, CONF_OPEN_WITHOUT_CODE,
        )
        for entry in self.hass.config_entries.async_entries(DOMAIN):
          if entry.entry_id == self.config_entry.entry_id:
            continue
          other = dict(entry.data or {})
          for key in shared_keys:
            other[key] = updated[key]
          self.hass.config_entries.async_update_entry(entry, data=other)
        return self.async_create_entry(title="", data={})

    merged = _merge_all_entities(self.hass, current)
    return self.async_show_form(
      step_id="entities",
      data_schema=vol.Schema({
        vol.Optional(CONF_SENSORS, default=merged.get(CONF_SENSORS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["sensor"], multiple=True)
        ),
        vol.Optional(CONF_BINARY_SENSORS, default=merged.get(CONF_BINARY_SENSORS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["binary_sensor"], multiple=True)
        ),
        vol.Optional(CONF_NUMBERS, default=merged.get(CONF_NUMBERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=list(NUMBER_DOMAINS), multiple=True)
        ),
        vol.Optional(CONF_SELECTS, default=merged.get(CONF_SELECTS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=list(SELECT_DOMAINS), multiple=True)
        ),
        vol.Optional(CONF_DATETIMES, default=merged.get(CONF_DATETIMES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=list(DATETIME_DOMAINS), multiple=True)
        ),
        vol.Optional(CONF_WEATHERS, default=merged.get(CONF_WEATHERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["weather"], multiple=True)
        ),
        vol.Optional(CONF_LIGHTS, default=merged.get(CONF_LIGHTS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["light"], multiple=True)
        ),
        vol.Optional(CONF_SWITCHES, default=merged.get(CONF_SWITCHES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=list(SWITCH_DOMAINS), multiple=True)
        ),
        vol.Optional(CONF_MEDIA_PLAYERS, default=merged.get(CONF_MEDIA_PLAYERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["media_player"], multiple=True)
        ),
        vol.Optional(CONF_CLIMATES, default=merged.get(CONF_CLIMATES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["climate"], multiple=True)
        ),
        vol.Optional(CONF_COVERS, default=merged.get(CONF_COVERS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["cover"], multiple=True)
        ),
        vol.Optional(CONF_FANS, default=merged.get(CONF_FANS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["fan"], multiple=True)
        ),
        vol.Optional(CONF_LOCKS, default=merged.get(CONF_LOCKS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["lock"], multiple=True)
        ),
        vol.Optional(CONF_ALARM_PANELS, default=merged.get(CONF_ALARM_PANELS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["alarm_control_panel"], multiple=True)
        ),
        vol.Optional(CONF_OPEN_WITHOUT_CODE, default=merged.get(CONF_OPEN_WITHOUT_CODE, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["lock", "alarm_control_panel"], multiple=True)
        ),
        vol.Optional(CONF_CAMERAS, default=merged.get(CONF_CAMERAS, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=["camera"], multiple=True)
        ),
        vol.Optional(CONF_SCENE_ENTITIES, default=merged.get(CONF_SCENE_ENTITIES, [])): selector.EntitySelector(
          selector.EntitySelectorConfig(domain=list(ACTION_DOMAINS), multiple=True)
        ),
        vol.Optional(CONF_SCENE_MAP_TEXT, default=merged.get(CONF_SCENE_MAP_TEXT, "")): selector.TextSelector(
          selector.TextSelectorConfig(multiline=True)
        ),
      }),
      errors=errors,
    )

  # ---- Codes the Bridge checks for locks and alarm panels ----

  async def async_step_access_codes(self, user_input: Dict[str, Any] | None = None):
    """One masked field per offered lock and alarm panel (all displays).

    A filled field makes the Bridge check every code from a panel itself
    before Home Assistant gets the command: for devices that ignore a wrong
    code silently, so "wrong code" and the lockout work there too. An empty
    field leaves the check to Home Assistant and the device.
    """
    current = dict(self.config_entry.data)
    merged = _merge_all_entities(self.hass, current)
    entities = _unique(list(merged.get(CONF_LOCKS, [])) + list(merged.get(CONF_ALARM_PANELS, [])))
    if not entities:
      return self.async_abort(reason="no_access_entities")
    stored = dict(current.get(CONF_ACCESS_CODES) or {})
    errors: Dict[str, str] = {}
    if user_input is not None:
      codes: Dict[str, str] = {}
      for entity_id in entities:
        try:
          parsed = parse_access_codes(user_input.get(entity_id))
        except ValueError:
          errors[entity_id] = "invalid_access_codes"
          continue
        if parsed:
          codes[entity_id] = ",".join(parsed)
      if not errors:
        # Shared by all displays like the entity selection.
        for entry in self.hass.config_entries.async_entries(DOMAIN):
          other = dict(entry.data or {})
          other[CONF_ACCESS_CODES] = codes
          self.hass.config_entries.async_update_entry(entry, data=other)
        return self.async_create_entry(title="", data={})
      stored = {key: value for key, value in (user_input or {}).items() if isinstance(value, str)}
    schema: Dict[Any, Any] = {}
    for entity_id in entities:
      # suggested_value, not default: a cleared field must stay empty.
      schema[vol.Optional(entity_id, description={"suggested_value": stored.get(entity_id, "")})] = (
        selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
      )
    return self.async_show_form(step_id="access_codes", data_schema=vol.Schema(schema), errors=errors)

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
  current_weather_from_sensors, current_legacy_sensors = _split_weather_entities(
    list(current.get(CONF_SENSORS, []))
  )
  current_binary_from_sensors, current_sensors = split_binary_sensor_entities(
    current_legacy_sensors
  )
  current_binary_sensors, _ = split_binary_sensor_entities(
    list(current.get(CONF_BINARY_SENSORS, []))
  )
  all_sensors = list(current_sensors)
  all_binary_sensors = _unique(
    current_binary_sensors + current_binary_from_sensors
  )
  all_weathers = _unique(list(current.get(CONF_WEATHERS, [])) + current_weather_from_sensors)
  all_lights = list(current.get(CONF_LIGHTS, []))
  all_switches = list(current.get(CONF_SWITCHES, []))
  all_media_players = list(current.get(CONF_MEDIA_PLAYERS, []))
  all_climates = list(current.get(CONF_CLIMATES, []))
  all_numbers = list(current.get(CONF_NUMBERS, []))
  all_selects = list(current.get(CONF_SELECTS, []))
  all_datetimes = list(current.get(CONF_DATETIMES, []))
  all_covers = list(current.get(CONF_COVERS, []))
  all_cameras = list(current.get(CONF_CAMERAS, []))
  all_fans = list(current.get(CONF_FANS, []))
  all_locks = list(current.get(CONF_LOCKS, []))
  all_alarm_panels = list(current.get(CONF_ALARM_PANELS, []))
  all_open_without_code = list(current.get(CONF_OPEN_WITHOUT_CODE, []))
  all_scene_ids = list((current.get(CONF_SCENE_MAP) or {}).values())
  scene_map_text = current.get(CONF_SCENE_MAP_TEXT, "")

  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.entry_id == current.get("_entry_id"):
      continue
    data = dict(entry.data or {})
    if entry.options:
      data.update(entry.options)
    entry_weather_from_sensors, entry_legacy_sensors = _split_weather_entities(list(data.get(CONF_SENSORS, [])))
    entry_binary_from_sensors, entry_sensors = split_binary_sensor_entities(
      entry_legacy_sensors
    )
    entry_binary_sensors, _ = split_binary_sensor_entities(
      list(data.get(CONF_BINARY_SENSORS, []))
    )
    all_sensors.extend(entry_sensors)
    all_binary_sensors.extend(entry_binary_sensors)
    all_binary_sensors.extend(entry_binary_from_sensors)
    all_weathers.extend(list(data.get(CONF_WEATHERS, [])))
    all_weathers.extend(entry_weather_from_sensors)
    all_lights.extend(list(data.get(CONF_LIGHTS, [])))
    all_switches.extend(list(data.get(CONF_SWITCHES, [])))
    all_media_players.extend(list(data.get(CONF_MEDIA_PLAYERS, [])))
    all_climates.extend(list(data.get(CONF_CLIMATES, [])))
    all_numbers.extend(list(data.get(CONF_NUMBERS, [])))
    all_selects.extend(list(data.get(CONF_SELECTS, [])))
    all_datetimes.extend(list(data.get(CONF_DATETIMES, [])))
    all_covers.extend(list(data.get(CONF_COVERS, [])))
    all_cameras.extend(list(data.get(CONF_CAMERAS, [])))
    all_fans.extend(list(data.get(CONF_FANS, [])))
    all_locks.extend(list(data.get(CONF_LOCKS, [])))
    all_alarm_panels.extend(list(data.get(CONF_ALARM_PANELS, [])))
    all_open_without_code.extend(list(data.get(CONF_OPEN_WITHOUT_CODE, [])))
    all_scene_ids.extend(list((data.get(CONF_SCENE_MAP) or {}).values()))

  return {
    CONF_SENSORS: _unique(all_sensors),
    CONF_BINARY_SENSORS: _unique(all_binary_sensors),
    CONF_WEATHERS: _unique(all_weathers),
    CONF_LIGHTS: _unique(all_lights),
    CONF_SWITCHES: _unique(all_switches),
    CONF_MEDIA_PLAYERS: _unique(all_media_players),
    CONF_CLIMATES: _unique(all_climates),
    CONF_NUMBERS: _unique(all_numbers),
    CONF_SELECTS: _unique(all_selects),
    CONF_DATETIMES: _unique(all_datetimes),
    CONF_COVERS: _unique(all_covers),
    CONF_CAMERAS: _unique(all_cameras),
    CONF_FANS: _unique(all_fans),
    CONF_LOCKS: _unique(all_locks),
    CONF_ALARM_PANELS: _unique(all_alarm_panels),
    CONF_OPEN_WITHOUT_CODE: _unique(all_open_without_code),
    CONF_SCENE_ENTITIES: _unique(all_scene_ids),
    CONF_SCENE_MAP_TEXT: scene_map_text,
  }


def _convert_entity_data(user_input: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
  weather_from_sensors, legacy_sensors = _split_weather_entities(
    _normalise_entity_list(user_input.get(CONF_SENSORS, []))
  )
  binary_from_sensors, sensors = split_binary_sensor_entities(legacy_sensors)
  selected_binary, _ = split_binary_sensor_entities(
    _normalise_entity_list(user_input.get(CONF_BINARY_SENSORS, []))
  )
  binary_sensors = _unique(selected_binary + binary_from_sensors)
  weathers = _unique(
    _normalise_entity_list(user_input.get(CONF_WEATHERS, [])) + weather_from_sensors
  )
  lights = _normalise_entity_list(user_input.get(CONF_LIGHTS, []))
  switches = _normalise_entity_list(user_input.get(CONF_SWITCHES, []))
  media_players = _normalise_entity_list(user_input.get(CONF_MEDIA_PLAYERS, []))
  climates = _normalise_entity_list(user_input.get(CONF_CLIMATES, []))
  covers = _normalise_entity_list(user_input.get(CONF_COVERS, []))
  cameras = _normalise_entity_list(user_input.get(CONF_CAMERAS, []))
  # Lists added after the form's first version keep their stored value when a
  # submission does not carry them.
  def selected(key: str, domains: tuple[str, ...]) -> list[str]:
    values = _normalise_entity_list(user_input.get(key, current.get(key, [])))
    return [item for item in values if entity_domain(item) in domains]

  fans = selected(CONF_FANS, ("fan",))
  locks = selected(CONF_LOCKS, ("lock",))
  alarm_panels = selected(CONF_ALARM_PANELS, ("alarm_control_panel",))
  open_without_code = selected(CONF_OPEN_WITHOUT_CODE, ("lock", "alarm_control_panel"))

  selected_scenes = _normalise_entity_list(user_input.get(CONF_SCENE_ENTITIES, []))
  scene_map_text = user_input.get(CONF_SCENE_MAP_TEXT, "").strip("\n")
  manual_map = _parse_scene_map(scene_map_text)
  scene_map = build_action_map(selected_scenes, manual_map, current.get(CONF_SCENE_MAP) or {})

  updated = dict(current)
  updated.pop("energy_enabled", None)  # remove old single checkbox
  updated.pop("energy_enabled", None)  # remove old single checkbox
  updated["user_sensor_selections"] = list(sensors)
  updated[CONF_SENSORS] = sensors
  updated[CONF_BINARY_SENSORS] = binary_sensors
  updated[CONF_WEATHERS] = weathers
  updated[CONF_LIGHTS] = lights
  updated[CONF_SWITCHES] = switches
  updated[CONF_MEDIA_PLAYERS] = media_players
  updated[CONF_CLIMATES] = climates
  for key, domains in EDITABLE_LISTS.items():
    updated[key] = editable_selection(user_input.get(key, current.get(key, [])), domains)
  updated[CONF_COVERS] = covers
  updated[CONF_CAMERAS] = cameras
  updated[CONF_FANS] = fans
  updated[CONF_LOCKS] = locks
  updated[CONF_ALARM_PANELS] = alarm_panels
  updated[CONF_OPEN_WITHOUT_CODE] = open_without_code
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
    if not alias or entity_domain(entity) not in ACTION_DOMAINS:
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
    # Letzte 4 Zeichen der device_id als kurzer Kollisions-Suffix -- unabhaengig
    # davon, ob die ID (alt) Unterstriche enthaelt oder (neu, volle MAC als
    # Hex) keine. split("_")[-1] gab bei der neuen Hex-only-Form die komplette
    # device_id zurueck, statt nur ein kurzes Suffix.
    suffix = str(device_id).strip().lower()[-4:]
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


def _password_schema() -> Any:
  try:
    return selector.TextSelector(
      selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )
  except (AttributeError, TypeError):
    return str


def _fallback_broker_credentials(hass: HomeAssistant, device_host: str | None = None) -> Dict[str, Any]:
  return {
    "host": _source_ip_for_target(device_host) or _ha_url_host(hass, prefer_external=False),
    "port": 1883,
    "username": "",
    "password": "",
  }


def _mqtt_credentials_from_user_input(
  user_input: Dict[str, Any],
  defaults: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, str]]:
  errors: Dict[str, str] = {}

  host = _normalise_broker_host_input(
    user_input.get(CONF_PROVISION_MQTT_HOST, defaults.get("host", ""))
  )
  if not host:
    errors[CONF_PROVISION_MQTT_HOST] = "mqtt_host_required"

  try:
    port = int(user_input.get(CONF_PROVISION_MQTT_PORT, defaults.get("port", 1883)) or 1883)
  except (TypeError, ValueError):
    port = 1883
    errors[CONF_PROVISION_MQTT_PORT] = "invalid_port"

  if port < 1 or port > 65535:
    errors[CONF_PROVISION_MQTT_PORT] = "invalid_port"

  return {
    "host": host,
    "port": port,
    "username": str(user_input.get(CONF_PROVISION_MQTT_USERNAME, defaults.get("username", "")) or "").strip(),
    "password": str(user_input.get(CONF_PROVISION_MQTT_PASSWORD, defaults.get("password", "")) or ""),
  }, errors


def _normalise_broker_host_input(value: Any) -> str:
  text = str(value or "").strip()
  if not text:
    return ""
  host = _host_from_url(text)
  return host or text


def _get_broker_credentials(hass: HomeAssistant, device_host: str | None = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
  """Liest die Zugangsdaten von HA's eigener mqtt-Integration aus.

  Rueckgabe (creds, error): error ist None bei Erfolg, sonst einer von
  "mqtt_not_configured" / "unsupported_broker" / "mqtt_read_failed". Der
  Discovery-Dialog zeigt in diesen Faellen trotzdem manuell aenderbare Felder.
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
  panel_password: str = "",
) -> Optional[str]:
  """Send the broker credentials with POST /mqtt (Web Admin endpoint, see
  web_admin_handlers.cpp:handleSaveMQTT) and restart the panel with POST
  /restart; without the restart mqtt_enabled keeps its boot value and the new
  credentials never take effect (see network_manager.cpp).

  A panel with the optional Web Admin password is logged in first (see
  panel_auth.py). Returns None on success or a config flow error code.
  """
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
    return await async_push_credentials(session, device_host, form, panel_password, timeout)
  except (aiohttp.ClientError, asyncio.TimeoutError):
    return ERROR_CANNOT_CONNECT


def _link_runtime(hass: HomeAssistant) -> Any:
  """The running direct link (link_runtime.LinkRuntime), or None."""
  return hass.data.get(DOMAIN, {}).get(DATA_LINK)


async def _wait_event(event: asyncio.Event, timeout: float) -> None:
  """Progress task of a link step; ends on the event or after the timeout."""
  try:
    await asyncio.wait_for(event.wait(), timeout)
  except asyncio.TimeoutError:
    pass


async def _push_link_to_device(
  hass: HomeAssistant,
  device_host: str,
  port: int,
  base_topic: str,
  ha_prefix: str,
) -> Optional[str]:
  """Send the Bridge's link address with POST /api/link (panel_auth.py).

  The panel accepts it only within two minutes after Pair was pressed on it,
  which replaces the Web Admin password. The host is the address of Home
  Assistant on the panel's network, like the camera stream URL. Returns None
  on success or a config flow error code.
  """
  host = _source_ip_for_target(device_host) or _ha_url_host(hass, prefer_external=False)
  if not host:
    return ERROR_CANNOT_CONNECT
  session = async_get_clientsession(hass)
  timeout = aiohttp.ClientTimeout(total=5)
  form = {
    "host": host,
    "port": str(port),
    "base": base_topic,
    "ha_prefix": ha_prefix,
  }
  try:
    return await async_push_link(session, device_host, form, timeout)
  except (aiohttp.ClientError, asyncio.TimeoutError):
    return ERROR_CANNOT_CONNECT


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
