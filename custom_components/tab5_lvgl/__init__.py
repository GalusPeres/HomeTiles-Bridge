"""Home Assistant integration for the Tab5 LVGL dashboard."""

from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime, timedelta
from io import BytesIO
from ipaddress import ip_address
import json
import logging
import secrets
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.components import network as ha_network
from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.components.weather.const import DATA_COMPONENT as WEATHER_DATA_COMPONENT
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.recorder import get_instance
try:
  from homeassistant.components.recorder.history import get_significant_states
except ImportError:  # pragma: no cover - older HA fallback
  get_significant_states = None
try:
  from homeassistant.components.recorder.history import get_last_state_changes
except ImportError:  # pragma: no cover - older HA fallback
  get_last_state_changes = None
try:
  from homeassistant.components.recorder.history import state_changes_during_period
except ImportError:  # pragma: no cover - older HA fallback
  state_changes_during_period = None
try:
  from homeassistant.components.recorder.statistics import statistics_during_period
except ImportError:  # pragma: no cover - older HA fallback
  statistics_during_period = None
try:
  from homeassistant.components.energy.data import async_get_manager as async_get_energy_manager
except ImportError:  # pragma: no cover - energy component not available
  async_get_energy_manager = None
try:
  from homeassistant.components.weather import async_get_forecasts
except Exception:  # pragma: no cover - optional weather helper
  async_get_forecasts = None
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import discovery_flow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
try:
  from homeassistant.helpers.icon import icon_for_entity
except Exception:  # pragma: no cover - optional fallback
  icon_for_entity = None
try:
  from homeassistant.helpers.network import get_url
except Exception:  # pragma: no cover - older HA fallback
  get_url = None
from homeassistant.util import dt as dt_util, slugify

from .binary_history import (
  BINARY_HISTORY_KIND,
  BINARY_HISTORY_RECORDER_MAX_CHANGES,
  BINARY_HISTORY_RECORDER_PAGE_SIZE,
  BINARY_HISTORY_RECORDER_RECENT_ROWS,
  BinaryHistoryRequestError,
  build_binary_history_error,
  build_binary_history_response,
  parse_binary_history_request,
)
from .binary_sensor_helpers import (
  build_binary_sensor_meta_entry,
  build_binary_sensor_state_payload,
  migrate_binary_sensor_config,
  split_binary_sensor_entities,
)
from .control_helpers import (
  ACTION_DOMAINS,
  SWITCH_DOMAINS,
  build_action_service_call,
  build_switch_service_call,
  build_switch_state_payload,
  entity_domain,
  panel_entity_list,
  resolve_action_entity,
  resolve_control_entity,
)
from .state_history import (
  STATE_HISTORY_KIND,
  STATE_HISTORY_MAX_RESPONSE_BYTES,
  StateHistoryRequestError,
  build_state_history_error,
  build_state_history_response,
  fetch_bounded_state_history,
  parse_state_history_request,
  sensor_live_state_payload,
  sensor_state_kind,
)
from .editable_helpers import (EDITABLE_LISTS, EDITABLE_DOMAINS, NUMBER_DOMAINS, SELECT_DOMAINS, DATETIME_DOMAINS, editable_selection, domain_of, build_editable_payload, build_editable_service_call, add_number_history, MAX_CONTROL_BYTES)
from .const import (
  CONF_NUMBERS, CONF_SELECTS, CONF_DATETIMES,
  CONF_BASE_TOPIC,
  CONF_BINARY_SENSORS,
  CONF_CAMERAS,
  CONF_CLIMATES,
  CONF_COVERS,
  CONF_DEVICE_ID,
  CONF_DEVICE_NAME,
  CONF_ENERGY_ELECTRICITY,
  CONF_ENERGY_GAS,
  CONF_ENERGY_WATER,
  CONF_HA_PREFIX,
  CONF_LIGHTS,
  CONF_LOCAL_IO,
  CONF_MANUFACTURER,
  CONF_MEDIA_PLAYERS,
  CONF_MODEL,
  CONF_SCENE_MAP,
  CONF_SENSORS,
  CONF_SWITCHES,
  CONF_WEATHERS,
  CONFIG_TOPIC_ROOT,
  CONFIG_TOPIC_SUB,
  DEFAULT_BASE,
  DEFAULT_PREFIX,
  DOMAIN,
  ENERGY_REQUEST_SUFFIX,
  ENERGY_RESPONSE_SUFFIX,
  HISTORY_REQUEST_SUFFIX,
  HISTORY_RESPONSE_SUFFIX,
  SERVICE_PUBLISH_SNAPSHOT,
  WEATHER_REQUEST_SUFFIX,
)
from .cover_helpers import (
  build_cover_state_payload,
  cover_command_supported,
  cover_component_icon,
  normalise_cover_command,
  parse_cover_position,
)
from .climate_helpers import (
  build_climate_service_call,
  build_climate_state_payload,
)
from .camera_stream import (
  CAMERA_BRIDGE_PROTOCOL_VERSION,
  CAMERA_STREAM_FPS,
  CAMERA_STREAM_FRAMING,
  CAMERA_STREAM_HEIGHT,
  CAMERA_STREAM_TRANSPORT,
  CAMERA_STREAM_WIDTH,
  CameraStreamManager,
)
from .device_helpers import entry_device_id, entry_device_info, entry_device_name
from .device_registry_helpers import find_entry_device, is_stale_device_entry
from .capabilities import (
  CAPABILITIES,
  merged_capabilities_data,
  normalise_capabilities,
  stale_internal_sensor,
  stale_local_camera,
)
from .local_camera import is_local_camera_self_loop
from .local_io import (
  LOCAL_IO_RELAY,
  LOCAL_IO_TEMPERATURE,
  entry_local_io,
  local_io_announced_entity_id,
  local_io_domain,
  local_io_legacy_collision_index,
  local_io_migration_target_entity_id,
  local_io_unique_id,
  normalise_local_io,
)
from .sensor_selection import (
  clean_stored_sensor_selections,
  filter_runtime_sensor_entities,
  runtime_sensor_entity_id_candidates,
  should_import_feedback_selection,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "select", "switch", "sensor", "binary_sensor", "camera"]

MEDIA_COVER_MAX_BYTES = 14000
# Source covers from HA media_player_proxy can be 200-500 KB (HD album art).
# Pillow prepares one 240x240 cover for both the compact tile and the larger
# popup. Older firmware accepts the same payload fields and JPEG format and
# applies its existing smaller decode limit, so this stays backwards compatible.
MEDIA_COVER_FETCH_MAX_BYTES = 1_500_000
MEDIA_COVER_CACHE_MAX = 24
MEDIA_COVER_THUMBNAIL_SIZE = 240
MEDIA_COVER_WARNING_INTERVAL_SECONDS = 15 * 60
MEDIA_COVER_WARNING_MAX_KEYS = 64


def _is_png_payload(data: bytes) -> bool:
  return data.startswith(b"\x89PNG\r\n\x1a\n")


def _resize_media_cover(data: bytes) -> Optional[Tuple[bytes, str]]:
  """Resize media artwork to a small JPEG payload for MQTT/display use."""
  try:
    from PIL import Image, ImageFile, ImageOps
  except Exception as err:
    _LOGGER.debug("Tab5 media cover: Pillow not available (%s)", err)
    return None

  # HA's media_player_proxy occasionally truncates the JPEG by a few bytes
  # before the EOI marker. Tell Pillow to accept partial files so we still get
  # a usable thumbnail instead of dropping the cover entirely.
  ImageFile.LOAD_TRUNCATED_IMAGES = True

  try:
    with Image.open(BytesIO(data)) as image:
      image = ImageOps.exif_transpose(image)
      image.thumbnail((MEDIA_COVER_THUMBNAIL_SIZE, MEDIA_COVER_THUMBNAIL_SIZE))

      if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, (0, 0, 0))
        alpha = image.convert("RGBA").split()[-1]
        background.paste(image.convert("RGBA"), mask=alpha)
        image = background
      else:
        image = image.convert("RGB")

      # Keep the 240x240 resolution and lower only JPEG quality as needed so
      # the Base64-encoded image still fits the existing MQTT message budget.
      for quality in (84, 76, 68, 60, 52, 44, 36, 28, 22, 18):
        output = BytesIO()
        image.save(
          output,
          format="JPEG",
          quality=quality,
          optimize=False,
          progressive=False,
          subsampling=2,
        )
        resized = output.getvalue()
        if len(resized) <= MEDIA_COVER_MAX_BYTES:
          return resized, "image/jpeg"
      return resized, "image/jpeg"
  except Exception as err:
    _LOGGER.debug(
      "Tab5 media cover: resize failed (%s: %s, %s bytes input)",
      type(err).__name__,
      err,
      len(data),
    )
    return None


LIGHT_SERVICE_FIELDS = {
  "transition",
  "brightness",
  "brightness_pct",
  "rgb_color",
  "rgbw_color",
  "rgbww_color",
  "color_temp",
  "color_temp_kelvin",
  "color_name",
  "hs_color",
  "xy_color",
  "effect",
  "flash",
  "white",
  "kelvin",
}

MEDIA_COMMAND_ALIASES = {
  "on": "turn_on",
  "turn_on": "turn_on",
  "off": "turn_off",
  "turn_off": "turn_off",
  "play": "media_play",
  "media_play": "media_play",
  "pause": "media_pause",
  "media_pause": "media_pause",
  "play_pause": "media_play_pause",
  "playpause": "media_play_pause",
  "media_play_pause": "media_play_pause",
  "toggle": "media_play_pause",
  "stop": "media_stop",
  "media_stop": "media_stop",
  "next": "media_next_track",
  "next_track": "media_next_track",
  "media_next_track": "media_next_track",
  "previous": "media_previous_track",
  "previous_track": "media_previous_track",
  "prev": "media_previous_track",
  "media_previous_track": "media_previous_track",
  "volume_up": "volume_up",
  "volume_down": "volume_down",
  "volume_set": "volume_set",
  "set_volume": "volume_set",
  "mute": "volume_mute",
  "volume_mute": "volume_mute",
  "select_source": "select_source",
  "source": "select_source",
  "seek": "media_seek",
  "media_seek": "media_seek",
  "play_media": "play_media",
}

SERVICE_SCHEMA = vol.Schema({vol.Optional("entry_id"): cv.string})

FORECAST_DAILY_TYPE = "daily"
FORECAST_HOURLY_TYPE = "hourly"
FORECAST_TWICE_DAILY_TYPE = "twice_daily"
FORECAST_FEATURES = {
  FORECAST_DAILY_TYPE: WeatherEntityFeature.FORECAST_DAILY,
  FORECAST_HOURLY_TYPE: WeatherEntityFeature.FORECAST_HOURLY,
  FORECAST_TWICE_DAILY_TYPE: WeatherEntityFeature.FORECAST_TWICE_DAILY,
}
FORECAST_DAILY_LIMIT = 8
FORECAST_HOURLY_PAYLOAD_LIMIT = 168
FORECAST_CACHE_TTL = timedelta(minutes=10)

_CONFIG_META_RUNTIME_FIELDS = frozenset(
  {"available", "icon", "last_changed", "state", "value"}
)


def _config_signature(config_data: Dict[str, Any]) -> str:
  """Return a stable signature that excludes separately published runtime data."""
  stable_data: Dict[str, Any] = {}
  for section, value in config_data.items():
    if not section.endswith("_meta") or not isinstance(value, list):
      stable_data[section] = value
      continue
    stable_data[section] = [
      {
        key: item_value
        for key, item_value in item.items()
        if key not in _CONFIG_META_RUNTIME_FIELDS
      }
      if isinstance(item, dict)
      else item
      for item in value
    ]
  return json.dumps(
    stable_data,
    sort_keys=True,
    separators=(",", ":"),
    default=str,
  )


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
  """Set up the integration namespace and service."""
  domain_data = hass.data.setdefault(DOMAIN, {"entries": {}})

  if "camera_stream_manager" not in domain_data:
    camera_stream_manager = CameraStreamManager(hass)
    domain_data["camera_stream_manager"] = camera_stream_manager
    try:
      await camera_stream_manager.async_start_tcp_server()
    except OSError as err:
      # Camera video is optional. A listener failure must not prevent state,
      # cover or control traffic from continuing over MQTT.
      _LOGGER.error("HomeTiles camera LAN TCP server could not start: %s", err)

    async def _async_stop_camera_server(_event: Event) -> None:
      await camera_stream_manager.async_shutdown()

    domain_data["_camera_stream_stop_unsub"] = hass.bus.async_listen_once(
      EVENT_HOMEASSISTANT_STOP,
      _async_stop_camera_server,
    )

  if not hass.services.has_service(DOMAIN, SERVICE_PUBLISH_SNAPSHOT):
    async def handle_service(call: ServiceCall) -> None:
      bridge = _resolve_bridge(hass, call.data.get("entry_id"))
      if bridge is None:
          raise HomeAssistantError("No Tab5 LVGL entry configured")
      await bridge.async_publish_snapshot()

    hass.services.async_register(
      DOMAIN,
      SERVICE_PUBLISH_SNAPSHOT,
      handle_service,
      schema=SERVICE_SCHEMA,
    )

  async def _handle_bridge_config(msg: ReceiveMessage) -> None:
    try:
      payload = json.loads(msg.payload)
    except (ValueError, TypeError):
      _LOGGER.warning("Tab5 LVGL: Ungültige Bridge-Konfiguration erhalten: %s", msg.payload)
      return
    await _async_process_bridge_config(hass, payload)

  if "_config_unsub" not in domain_data:
    domain_data["_config_unsub"] = await mqtt.async_subscribe(
      hass,
      CONFIG_TOPIC_SUB,
      _handle_bridge_config,
    )

  return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
  """Create the bridge instance for a config entry."""
  device_reg = dr.async_get(hass)
  dev_info = entry_device_info(entry)
  kwargs = {
    "config_entry_id": entry.entry_id,
    "identifiers": dev_info["identifiers"],
    "name": dev_info["name"],
  }
  if dev_info.get("manufacturer"):
    kwargs["manufacturer"] = dev_info["manufacturer"]
  if dev_info.get("model"):
    kwargs["model"] = dev_info["model"]
  device_reg.async_get_or_create(**kwargs)
  for sensor_entry in hass.config_entries.async_entries(DOMAIN):
    _cleanup_persisted_runtime_sensor_entities(hass, sensor_entry)
  # Der Entry-Titel wird sonst nur einmal bei der Ersterstellung gesetzt und
  # danach nie wieder - anders als der Geraetename oben, der bei jedem Setup
  # frisch berechnet wird. Ohne diesen Abgleich laufen beide Namen auseinander
  # (sichtbar als zwei verschiedene Labels uebereinander in der Geraete-Liste),
  # sobald sich am Default-/Fallback-Namen mal etwas aendert.
  if entry.title != dev_info["name"]:
    hass.config_entries.async_update_entry(entry, title=dev_info["name"])
  _remove_stale_local_io_entities(hass, entry)
  _migrate_local_io_entity_ids(hass, entry)
  bridge = Tab5Bridge(hass, entry)
  await bridge.async_setup()
  hass.data[DOMAIN]["entries"][entry.entry_id] = bridge
  entry.async_on_unload(entry.add_update_listener(_async_update_listener))
  await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
  await bridge.async_publish_config_to_device()
  return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
  """Migrate legacy generic sensor selections to dedicated entity lists."""
  if entry.version > 2:
    return False
  if entry.version == 2:
    return True

  data, options, changed = migrate_binary_sensor_config(
    entry.data,
    entry.options,
    sensor_key=CONF_SENSORS,
    binary_sensor_key=CONF_BINARY_SENSORS,
  )
  update: Dict[str, Any] = {"version": 2}
  if changed:
    update["data"] = data
    update["options"] = options
  hass.config_entries.async_update_entry(entry, **update)
  _LOGGER.info(
    "HomeTiles Bridge migrated config entry %s to version 2",
    entry.entry_id,
  )
  return True


def _preserved_user_sensor_ids(hass: HomeAssistant, entry: ConfigEntry | None,
                               incoming_data: Dict[str, Any] | None = None) -> set[str]:
  """Preserve explicit selections unless their owning panel removes support."""
  if entry is None:
    return set()
  protected = set((entry.data or {}).get("user_sensor_selections", []))
  registry = er.async_get(hass)
  for owner in hass.config_entries.async_entries(DOMAIN):
    data = merged_capabilities_data(owner)
    if owner.entry_id == entry.entry_id and incoming_data:
      data.update(incoming_data)
    expected = {local_io_unique_id(entry_device_id(owner), item)
                for item in data.get(CONF_LOCAL_IO, entry_local_io(owner))}
    for entity_id in tuple(protected):
      entity = registry.async_get(entity_id)
      if (entity is None or entity.platform != DOMAIN or entity.domain != "sensor"
          or entity.config_entry_id != owner.entry_id):
        continue
      unique_id = entity.unique_id or ""
      obsolete = (unique_id not in expected if "_local_io_" in unique_id
                  else stale_internal_sensor(unique_id, data))
      if obsolete:
        protected.remove(entity_id)
  return protected


def _runtime_managed_sensor_entity_ids(
  hass: HomeAssistant,
  entry: ConfigEntry | None,
  incoming_data: Dict[str, Any] | None = None,
) -> set[str]:
  """Resolve exact sensor IDs that the integration adds at runtime."""
  registry = er.async_get(hass)
  protected = _preserved_user_sensor_ids(hass, entry, incoming_data)
  owned_entity_ids: set[str] = set()
  device_labels: List[str] = []
  announced_entity_ids: set[str] = set()
  local_io_descriptors: List[Dict[str, Any]] = []

  if entry is not None:
    merged = dict(entry.data or {})
    if entry.options:
      merged.update(entry.options)
    device_labels.extend(
      [
        entry.title,
        entry_device_name(entry),
        merged.get(CONF_DEVICE_NAME),
        merged.get(CONF_MODEL),
        merged.get(CONF_DEVICE_ID),
      ]
    )
    local_io_descriptors.extend(entry_local_io(entry))
    for registry_entry in registry.entities.values():
      if (
        registry_entry.config_entry_id == entry.entry_id
        and registry_entry.domain == "sensor"
        and registry_entry.platform == DOMAIN
        and registry_entry.entity_id
      ):
        owned_entity_ids.add(registry_entry.entity_id)

  if incoming_data is not None:
    device_labels.extend(
      [
        incoming_data.get(CONF_DEVICE_NAME),
        incoming_data.get(CONF_MODEL),
        incoming_data.get(CONF_DEVICE_ID),
      ]
    )
    if CONF_LOCAL_IO in incoming_data:
      local_io_descriptors.extend(
        normalise_local_io(incoming_data.get(CONF_LOCAL_IO))
      )

  labels = _unique_entities(
    [str(label).strip() for label in device_labels if str(label or "").strip()]
  )
  for descriptor in local_io_descriptors:
    if descriptor.get("type") != LOCAL_IO_TEMPERATURE:
      continue
    if announced_entity_id := local_io_announced_entity_id(descriptor):
      announced_entity_ids.add(announced_entity_id)
    announced_entity_ids.update(descriptor.get("legacy_entity_ids", []))
    sensor_name = str(descriptor.get("name") or "").strip()
    if sensor_name:
      object_id = slugify(sensor_name)
      if object_id:
        announced_entity_ids.add(f"sensor.{object_id}")
      for label in labels:
        object_id = slugify(f"{label} {sensor_name}")
        if object_id:
          announced_entity_ids.add(f"sensor.{object_id}")

  candidates = runtime_sensor_entity_id_candidates(
    labels,
    owned_entity_ids,
    announced_entity_ids,
    slugify,
  )

  # A historic candidate may since have been claimed by another integration.
  # Keep that entity selectable unless it still belongs to this config entry.
  result: set[str] = set()
  for entity_id in candidates:
    registry_entry = registry.async_get(entity_id)
    if entity_id in protected:
      continue
    if registry_entry is None:
      result.add(entity_id)
      continue
    if entry is not None and (
      registry_entry.config_entry_id == entry.entry_id
      and registry_entry.domain == "sensor"
      and registry_entry.platform == DOMAIN
    ):
      result.add(entity_id)
  return result


def _cleanup_persisted_runtime_sensor_entities(
  hass: HomeAssistant,
  entry: ConfigEntry,
) -> None:
  """Repair sensor selections affected by the firmware feedback loop."""
  runtime_entity_ids = set()
  for owner in hass.config_entries.async_entries(DOMAIN):
    runtime_entity_ids.update(_runtime_managed_sensor_entity_ids(hass, owner))
  protected = _preserved_user_sensor_ids(hass, entry)
  runtime_entity_ids.difference_update(protected)
  data, options, data_changed, options_changed, removed_count = (
    clean_stored_sensor_selections(
      entry.data, entry.options, CONF_SENSORS, runtime_entity_ids
    )
  )
  for storage, is_options in ((data, False), (options, True)):
    selected = storage.get("user_sensor_selections")
    if selected is not None:
      cleaned = [entity_id for entity_id in selected if entity_id in protected]
      if cleaned != selected:
        storage["user_sensor_selections"] = cleaned
        if is_options:
          options_changed = True
        else:
          data_changed = True
  if not data_changed and not options_changed:
    return

  update: Dict[str, Any] = {}
  if data_changed:
    update["data"] = data
  if options_changed:
    update["options"] = options
  hass.config_entries.async_update_entry(entry, **update)
  _LOGGER.info(
    "HomeTiles Bridge removed %s stale runtime sensor selection(s) from %s",
    removed_count,
    entry.entry_id,
  )


def _remove_stale_local_io_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
  """Remove registry entries for channels no longer announced by firmware."""
  device_id = entry_device_id(entry)
  merged = merged_capabilities_data(entry)
  local_io_announced = CONF_LOCAL_IO in merged
  legacy_temperature_id = f"{device_id}_external_temperature"
  expected = {
    local_io_unique_id(device_id, descriptor)
    for descriptor in entry_local_io(entry)
  }
  registry = er.async_get(hass)
  stale: List[str] = []
  for entity in registry.entities.values():
    if entity.config_entry_id != entry.entry_id or entity.platform != DOMAIN:
      continue
    unique_id = entity.unique_id or ""
    # Match by the integration-owned delimiter rather than only the current
    # device ID. The fallback adoption path may replace a provisional device
    # ID; entities created under that old ID must not remain as registry orphans.
    if (entity.domain == "sensor" and "_local_io_" not in unique_id
        and stale_internal_sensor(unique_id, merged)):
      stale.append(entity.entity_id)
    elif "_local_io_" in unique_id and unique_id not in expected:
      stale.append(entity.entity_id)
    elif (entity.domain in ("camera", "switch") and "_local_io_" not in unique_id
          and stale_local_camera(unique_id, merged)):
      # The panel withdrew its own camera (opt-in off, sensor missing or
      # older firmware); a registry orphan would stay unavailable forever.
      # The camera's pause switch shares the camera's unique ID suffix.
      stale.append(entity.entity_id)
    elif local_io_announced and (
      unique_id == legacy_temperature_id
      or unique_id.endswith("_external_temperature")
    ):
      stale.append(entity.entity_id)
  for entity_id in stale:
    registry.async_remove(entity_id)
    _LOGGER.info("HomeTiles local I/O entity removed: %s", entity_id)


def _migrate_local_io_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
  """Apply firmware-announced IDs to unmodified local I/O registry entries.

  An entity ID is only moved when its current value is one of the deterministic
  IDs Home Assistant generated from the device and original entity names. This
  deliberately leaves user-renamed IDs alone. New registry entities get the
  same target from the entity ID suggestion made before platform registration.
  """
  registry = er.async_get(hass)
  by_unique_id = {
    entity.unique_id: entity
    for entity in registry.entities.values()
    if entity.config_entry_id == entry.entry_id
    and entity.platform == DOMAIN
    and entity.unique_id
  }
  for descriptor in entry_local_io(entry):
    target_entity_id = local_io_announced_entity_id(descriptor)
    if target_entity_id is None:
      continue
    unique_id = local_io_unique_id(entry_device_id(entry), descriptor)
    reg_entry = by_unique_id.get(unique_id)
    if reg_entry is None or reg_entry.domain != local_io_domain(descriptor["type"]):
      continue
    if reg_entry.entity_id == target_entity_id:
      continue
    if not _is_default_local_io_entity_id(
      hass, registry, entry, reg_entry, descriptor
    ):
      _LOGGER.info(
        "HomeTiles local I/O entity %s was renamed by the user; keeping it",
        reg_entry.entity_id,
      )
      continue
    migration_target_entity_id = local_io_migration_target_entity_id(
      reg_entry.entity_id,
      target_entity_id,
      descriptor.get("legacy_entity_ids", []),
    )
    if reg_entry.entity_id == migration_target_entity_id:
      continue
    if registry.async_get(migration_target_entity_id) is not None:
      _LOGGER.warning(
        "HomeTiles local I/O entity ID %s is already in use; keeping %s",
        migration_target_entity_id,
        reg_entry.entity_id,
      )
      continue
    try:
      registry.async_update_entity(
        reg_entry.entity_id,
        new_entity_id=migration_target_entity_id,
      )
      _LOGGER.info(
        "HomeTiles local I/O entity migration: %s -> %s",
        reg_entry.entity_id,
        migration_target_entity_id,
      )
    except (ValueError, TypeError) as err:
      _LOGGER.warning(
        "HomeTiles local I/O entity migration failed for %s -> %s: %s",
        reg_entry.entity_id,
        migration_target_entity_id,
        err,
      )


def _is_default_local_io_entity_id(
  hass: HomeAssistant,
  registry: Any,
  entry: ConfigEntry,
  reg_entry: Any,
  descriptor: Dict[str, Any],
) -> bool:
  """Return whether an existing ID matches a known integration default."""
  if not _local_io_registry_metadata_matches(entry, reg_entry, descriptor):
    return False

  # Current HA can regenerate the integration-provided ID from registry
  # metadata while accounting for custom area/device/entity naming settings.
  # If an older supported HA does not expose that helper, use the legacy
  # deterministic forms below.
  regenerate = getattr(registry, "async_regenerate_entity_id", None)
  if callable(regenerate) and getattr(reg_entry, "name", None) is None:
    try:
      if regenerate(reg_entry) == reg_entry.entity_id:
        # Numeric suffixes of explicit legacy suggestions need the stable
        # multi-entry proof below. Otherwise the result changes when the first
        # colliding entry is migrated and frees the unsuffixed base ID.
        if not any(
          local_io_legacy_collision_index(
            reg_entry.entity_id, legacy_entity_id
          ) is not None
          for legacy_entity_id in descriptor.get("legacy_entity_ids", [])
        ):
          return True
    except (AttributeError, TypeError, ValueError):
      pass

  domain = local_io_domain(descriptor["type"])
  for legacy_entity_id in descriptor.get("legacy_entity_ids", []):
    if (
      reg_entry.entity_id == legacy_entity_id
      and _local_io_registry_suggestion_matches(
        reg_entry, descriptor, legacy_entity_id
      )
    ):
      return True

  if _is_legacy_local_io_collision_id(
    hass, registry, entry, reg_entry, descriptor
  ):
    return True

  entity_name = (reg_entry.original_name or descriptor["name"] or "").strip()
  if not entity_name:
    return False

  # With has_entity_name=True Home Assistant combines the device and entity
  # names. The name-only form covers registry entries created by older HA
  # versions or before the device registry entry was available.
  device_names = {
    str(entry_device_name(entry) or "").strip(),
    str(entry.title or "").strip(),
  }
  object_ids = {slugify(entity_name)}
  object_ids.update(
    slugify(f"{device_name} {entity_name}")
    for device_name in device_names
    if device_name
  )

  # Firmware versions that first announced explicit local-I/O entity IDs used
  # the hidden channel ID as the visible suffix, for example
  # switch.m5stacks_tab5_relay_1. The channel ID remains the stable MQTT and
  # Home Assistant unique-ID key, but newer firmware derives the visible suffix
  # from the user-facing name instead. Recognise only deterministic legacy
  # forms so genuinely user-renamed registry IDs remain untouched.
  channel_object_id = slugify(str(descriptor.get("id") or ""))
  if channel_object_id:
    announced_entity_id = local_io_announced_entity_id(descriptor)
    if announced_entity_id:
      announced_domain, announced_object_id = announced_entity_id.split(".", 1)
      announced_name_slug = _firmware_entity_slug(descriptor.get("name"))
      name_suffix = f"_{announced_name_slug}" if announced_name_slug else ""
      if (
        announced_domain == domain
        and name_suffix
        and announced_object_id.endswith(name_suffix)
      ):
        device_object_id = announced_object_id[:-len(name_suffix)]
        if device_object_id:
          object_ids.add(f"{device_object_id}_{channel_object_id}")

  return reg_entry.entity_id in {
    f"{domain}.{object_id}" for object_id in object_ids if object_id
  }


def _is_legacy_local_io_collision_id(
  hass: HomeAssistant,
  registry: Any,
  entry: ConfigEntry,
  reg_entry: Any,
  descriptor: Dict[str, Any],
) -> bool:
  """Return whether HA added a numeric collision suffix to a legacy ID."""
  if not _local_io_registry_metadata_matches(entry, reg_entry, descriptor):
    return False

  for legacy_entity_id in descriptor.get("legacy_entity_ids", []):
    collision_index = local_io_legacy_collision_index(
      reg_entry.entity_id, legacy_entity_id
    )
    if collision_index is None:
      continue

    # Home Assistant stores the integration-provided suggestion separately
    # from the final entity ID. A prior failed migration may already have
    # refreshed it to the new target, so both integration-owned values are
    # accepted; an unrelated user-selected base is not.
    if not _local_io_registry_suggestion_matches(
      reg_entry, descriptor, legacy_entity_id
    ):
      continue

    owner_count = _legacy_local_io_alias_owner_count(
      hass, registry, entry, descriptor, legacy_entity_id
    )
    if collision_index <= owner_count:
      return True
  return False


def _local_io_registry_suggestion_matches(
  reg_entry: Any,
  descriptor: Dict[str, Any],
  legacy_entity_id: str,
) -> bool:
  """Verify that HA stored an integration-owned object-ID suggestion."""
  target_entity_id = local_io_announced_entity_id(descriptor)
  if target_entity_id is None:
    return False
  target_object_id = target_entity_id.split(".", 1)[1]
  legacy_object_id = legacy_entity_id.split(".", 1)[1]
  return getattr(reg_entry, "suggested_object_id", None) in {
    legacy_object_id,
    target_object_id,
  }


def _legacy_local_io_alias_owner_count(
  hass: HomeAssistant,
  registry: Any,
  entry: ConfigEntry,
  descriptor: Dict[str, Any],
  legacy_entity_id: str,
) -> int:
  """Count verified HomeTiles entries which announced the same legacy ID."""
  owners = {entry.entry_id}
  for candidate_entry in hass.config_entries.async_entries(DOMAIN):
    if candidate_entry.entry_id in owners:
      continue
    for candidate_descriptor in entry_local_io(candidate_entry):
      if (
        candidate_descriptor["type"] != descriptor["type"]
        or candidate_descriptor["id"] != descriptor["id"]
      ):
        continue
      candidate_legacy_ids = candidate_descriptor.get("legacy_entity_ids", [])
      if (
        legacy_entity_id not in candidate_legacy_ids
        and local_io_announced_entity_id(candidate_descriptor) != legacy_entity_id
      ):
        continue
      candidate_unique_id = local_io_unique_id(
        entry_device_id(candidate_entry), candidate_descriptor
      )
      candidate_registry_entry = next(
        (
          item
          for item in registry.entities.values()
          if item.config_entry_id == candidate_entry.entry_id
          and item.platform == DOMAIN
          and item.unique_id == candidate_unique_id
          and item.domain == local_io_domain(candidate_descriptor["type"])
        ),
        None,
      )
      if candidate_registry_entry is None or not _local_io_registry_metadata_matches(
        candidate_entry, candidate_registry_entry, candidate_descriptor
      ):
        continue
      owners.add(candidate_entry.entry_id)
      break
  return len(owners)


def _local_io_registry_metadata_matches(
  entry: ConfigEntry,
  reg_entry: Any,
  descriptor: Dict[str, Any],
) -> bool:
  """Verify registry ownership and immutable local-I/O identity metadata."""
  expected_unique_id = local_io_unique_id(entry_device_id(entry), descriptor)
  expected_name = str(descriptor.get("name") or "").strip()
  original_name = str(getattr(reg_entry, "original_name", None) or "").strip()
  return (
    reg_entry.config_entry_id == entry.entry_id
    and reg_entry.platform == DOMAIN
    and reg_entry.unique_id == expected_unique_id
    and reg_entry.domain == local_io_domain(descriptor["type"])
    and bool(getattr(reg_entry, "device_id", None))
    and getattr(reg_entry, "has_entity_name", False) is True
    and getattr(reg_entry, "name", None) is None
    and bool(expected_name)
    and original_name == expected_name
  )


def _firmware_entity_slug(value: Any) -> str:
  """Return the ASCII slug used by firmware for announced entity IDs."""
  result = []
  separator_pending = False
  for character in str(value or ""):
    if "A" <= character <= "Z":
      character = chr(ord(character) + (ord("a") - ord("A")))
    if "a" <= character <= "z" or "0" <= character <= "9":
      if separator_pending and result:
        result.append("_")
      result.append(character)
      separator_pending = False
    elif result:
      separator_pending = True
  return "".join(result)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
  """Unload a config entry."""
  bridge: Tab5Bridge | None = hass.data[DOMAIN]["entries"].get(entry.entry_id)
  unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
  if unload_ok and bridge is not None:
    await bridge.async_unload()
    hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
  return unload_ok


async def async_remove_config_entry_device(
  hass: HomeAssistant,
  config_entry: ConfigEntry,
  device_entry: dr.DeviceEntry,
) -> bool:
  """Allow Home Assistant to remove a stale HomeTiles registry device."""
  # A manually created entry initially uses its config-entry ID, then adopts
  # the firmware-announced device ID after the first bridge-config message.
  # The old registry device is stale; the current identifier must stay owned.
  return is_stale_device_entry(
    device_entry.identifiers,
    (DOMAIN, entry_device_id(config_entry)),
  )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
  """Reload when the config entry is updated."""
  await hass.config_entries.async_reload(entry.entry_id)


def _resolve_bridge(hass: HomeAssistant, entry_id: Optional[str]) -> Optional["Tab5Bridge"]:
  entries = hass.data.get(DOMAIN, {}).get("entries", {})
  if not entries:
    return None

  if entry_id:
    return entries.get(entry_id)

  # Fallback to the first (and typically only) entry
  return next(iter(entries.values()))


class Tab5Bridge:
  """Copies Home Assistant state to the Tab5 MQTT topics."""

  def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
    self.hass = hass
    self.entry = entry

    data = dict(entry.data or {})
    if entry.options:
      # Options override stored data (HA keeps UI edits in entry.options).
      data.update(entry.options)
    self.device_id = data.get(CONF_DEVICE_ID)
    self.base_topic = _normalise_topic(data.get(CONF_BASE_TOPIC, DEFAULT_BASE), DEFAULT_BASE)
    self.ha_prefix = _normalise_topic(data.get(CONF_HA_PREFIX, DEFAULT_PREFIX), DEFAULT_PREFIX)
    raw_sensors = _unique_entities(list(data.get(CONF_SENSORS, [])))
    raw_binary_sensors = _unique_entities(
      list(data.get(CONF_BINARY_SENSORS, []))
    )
    raw_weathers = _unique_entities(list(data.get(CONF_WEATHERS, [])))
    legacy_weathers, legacy_sensors = _split_weather_entities(raw_sensors)
    legacy_binary_sensors, configured_sensors = split_binary_sensor_entities(
      legacy_sensors
    )
    configured_binary_sensors, _ = split_binary_sensor_entities(
      raw_binary_sensors
    )
    self.weathers = _unique_entities(legacy_weathers + raw_weathers)
    self._configured_sensors: List[str] = configured_sensors
    self.sensors: List[str] = []
    self.binary_sensors: List[str] = _unique_entities(
      configured_binary_sensors + legacy_binary_sensors
    )
    self.lights: List[str] = _unique_entities(list(data.get(CONF_LIGHTS, [])))
    self.switches: List[str] = _unique_entities(list(data.get(CONF_SWITCHES, [])))
    self.media_players: List[str] = _unique_entities(list(data.get(CONF_MEDIA_PLAYERS, [])))
    self.climates: List[str] = _unique_entities(list(data.get(CONF_CLIMATES, [])))
    self.numbers = editable_selection(data.get(CONF_NUMBERS, []), EDITABLE_LISTS["numbers"])
    self.selects = editable_selection(data.get(CONF_SELECTS, []), EDITABLE_LISTS["selects"])
    self.datetimes = editable_selection(data.get(CONF_DATETIMES, []), EDITABLE_LISTS["datetimes"])
    self.covers: List[str] = _unique_entities(list(data.get(CONF_COVERS, [])))
    self.cameras: List[str] = _unique_entities(list(data.get(CONF_CAMERAS, [])))
    self.tracked_entities: List[str] = []
    self._media_cover_cache: Dict[str, Dict[str, Any]] = {}
    self._media_cover_warning_last: Dict[Tuple[str, str], float] = {}
    self._media_publish_generation: Dict[str, int] = {}
    self.scene_map: Dict[str, str] = {
      (alias or "").lower(): entity
      for alias, entity in (data.get(CONF_SCENE_MAP, {}) or {}).items()
    }
    self.config_topic = f"{CONFIG_TOPIC_ROOT}/{self.device_id}/bridge/apply" if self.device_id else None
    self.icons_topic = f"{CONFIG_TOPIC_ROOT}/{self.device_id}/bridge/icons" if self.device_id else None
    self.history_request_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{HISTORY_REQUEST_SUFFIX}" if self.device_id else None
    )
    self.history_response_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{HISTORY_RESPONSE_SUFFIX}" if self.device_id else None
    )
    self.weather_request_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{WEATHER_REQUEST_SUFFIX}" if self.device_id else None
    )
    self.energy_request_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{ENERGY_REQUEST_SUFFIX}" if self.device_id else None
    )
    self.energy_response_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{ENERGY_RESPONSE_SUFFIX}" if self.device_id else None
    )
    self._unsub_state = None
    self._device_ip: str | None = None
    self._unsub_connected = None
    self._unsub_ip = None
    self._unsub_scene = None
    self._unsub_light = None
    self._unsub_switch = None
    self._unsub_value = None
    self._editable_seen = {}
    self._editable_session = self.hass.data.setdefault(DOMAIN, {}).setdefault("editable_session", secrets.token_hex(16))
    self._unsub_media = None
    self._unsub_climate = None
    self._unsub_cover = None
    self._unsub_camera = None
    self._unsub_request = None
    self._unsub_history = None
    self._unsub_weather = None
    self._unsub_energy = None
    self._runtime_setup_complete = False
    self._config_refresh_handles: List = []
    self._config_refresh_pending = 0
    self._config_publish_lock = asyncio.Lock()
    self._last_config_signature: Optional[str] = None
    self._icon_cache: Dict[str, str] = {}
    self._icon_refresh_handle = None
    self._forecast_cache: Dict[Tuple[str, str], Tuple[datetime, List[Dict[str, Any]]]] = {}
    self._weather_subscriptions = {}
    self._weather_pending = set()
    self._weather_initial_updates = {}
    self._weather_update_task = None
    self._unsub_weather_started = None
    self._unsub_weather_stop = None
    self._weather_last_payload: Dict[str, str] = {}
    self._weather_revision: Dict[str, int] = {}
    self._refresh_runtime_entity_lists()

  def _resolve_internal_sensor_entities(self) -> List[str]:
    """Find all integration-owned sensor entities for this Tab5 entry."""
    registry = er.async_get(self.hass)
    result: List[str] = []
    for entry in registry.entities.values():
      if entry.config_entry_id != self.entry.entry_id:
        continue
      if entry.domain != "sensor":
        continue
      if entry.disabled_by is not None:
        continue
      if entry.entity_id:
        result.append(entry.entity_id)
    return _unique_entities(result)

  def _resolve_local_io_entities(self, domain: str, channel_type: str) -> List[str]:
    """Resolve integration-owned local I/O entity IDs in descriptor order."""
    expected = [
      local_io_unique_id(entry_device_id(self.entry), descriptor)
      for descriptor in entry_local_io(self.entry)
      if descriptor["type"] == channel_type
    ]
    if not expected:
      return []

    registry = er.async_get(self.hass)
    by_unique_id: Dict[str, str] = {}
    for entity in registry.entities.values():
      if entity.config_entry_id != self.entry.entry_id:
        continue
      if entity.domain != domain or entity.disabled_by is not None:
        continue
      if entity.unique_id and entity.entity_id:
        by_unique_id[entity.unique_id] = entity.entity_id
    return [by_unique_id[item] for item in expected if item in by_unique_id]

  def _collect_all_entries_entities(self) -> Dict[str, Any]:
    """Merge entity lists from all config entries in this integration."""
    all_sensors: List[str] = []
    all_binary_sensors: List[str] = []
    all_lights: List[str] = []
    all_switches: List[str] = []
    all_media_players: List[str] = []
    all_climates: List[str] = []
    all_numbers: List[str] = []
    all_selects: List[str] = []
    all_datetimes: List[str] = []
    all_covers: List[str] = []
    all_cameras: List[str] = []
    all_weathers: List[str] = []
    all_scene_map: Dict[str, str] = {}
    for entry in self.hass.config_entries.async_entries(DOMAIN):
      data = dict(entry.data or {})
      if entry.options:
        data.update(entry.options)
      raw_sensors = _unique_entities(list(data.get(CONF_SENSORS, [])))
      raw_binary_sensors = _unique_entities(
        list(data.get(CONF_BINARY_SENSORS, []))
      )
      raw_weathers = _unique_entities(list(data.get(CONF_WEATHERS, [])))
      legacy_weathers, legacy_sensors = _split_weather_entities(raw_sensors)
      legacy_binary_sensors, sensors = split_binary_sensor_entities(
        legacy_sensors
      )
      binary_sensors, _ = split_binary_sensor_entities(raw_binary_sensors)
      weathers = _unique_entities(legacy_weathers + raw_weathers)
      all_sensors.extend(sensors)
      all_binary_sensors.extend(binary_sensors)
      all_binary_sensors.extend(legacy_binary_sensors)
      all_lights.extend(list(data.get(CONF_LIGHTS, [])))
      all_switches.extend(list(data.get(CONF_SWITCHES, [])))
      all_media_players.extend(list(data.get(CONF_MEDIA_PLAYERS, [])))
      all_climates.extend(list(data.get(CONF_CLIMATES, [])))
      all_numbers.extend(editable_selection(data.get(CONF_NUMBERS, []), EDITABLE_LISTS["numbers"]))
      all_selects.extend(editable_selection(data.get(CONF_SELECTS, []), EDITABLE_LISTS["selects"]))
      all_datetimes.extend(editable_selection(data.get(CONF_DATETIMES, []), EDITABLE_LISTS["datetimes"]))
      all_covers.extend(list(data.get(CONF_COVERS, [])))
      all_cameras.extend(list(data.get(CONF_CAMERAS, [])))
      all_weathers.extend(weathers)
      for alias, entity in (data.get(CONF_SCENE_MAP, {}) or {}).items():
        if alias and entity:
          all_scene_map.setdefault((alias or "").lower(), entity)
    return {
      "sensors": _unique_entities(all_sensors),
      "binary_sensors": _unique_entities(all_binary_sensors),
      "lights": _unique_entities(all_lights),
      "switches": _unique_entities(all_switches),
      "media_players": _unique_entities(all_media_players),
      "climates": _unique_entities(all_climates),
      "numbers": _unique_entities(all_numbers),
      "selects": _unique_entities(all_selects),
      "datetimes": _unique_entities(all_datetimes),
      "covers": _unique_entities(all_covers),
      "cameras": _unique_entities(all_cameras),
      "weathers": _unique_entities(all_weathers),
      "scene_map": all_scene_map,
    }

  def _refresh_runtime_entity_lists(self) -> None:
    """Keep runtime sensor/tracked lists in sync — merges from all entries."""
    previous_tracked = tuple(self.tracked_entities)
    merged = self._collect_all_entries_entities()
    internal_sensors = self._resolve_internal_sensor_entities()
    local_temperatures = self._resolve_local_io_entities(
      "sensor", LOCAL_IO_TEMPERATURE
    )
    local_relays = self._resolve_local_io_entities("switch", LOCAL_IO_RELAY)
    self._configured_sensors = merged["sensors"]
    self.sensors = _unique_entities(
      merged["sensors"] + internal_sensors + local_temperatures
    )
    self.binary_sensors = merged["binary_sensors"]
    self.lights = merged["lights"]
    self.switches = _unique_entities(merged["switches"] + local_relays)
    self.media_players = merged["media_players"]
    self.climates = merged["climates"]
    self.numbers = merged["numbers"]
    self.selects = merged["selects"]
    self.datetimes = merged["datetimes"]
    self.covers = merged["covers"]
    self.cameras = merged["cameras"]
    self.weathers = merged["weathers"]
    self.scene_map = dict(merged["scene_map"])
    self.tracked_entities = _unique_entities(
      self.sensors
      + self.binary_sensors
      + self.lights
      + self.switches
      + self.media_players
      + self.climates
      + self.numbers
      + self.selects
      + self.datetimes
      + self.covers
      + self.weathers
      + list(self.scene_map.values())
    )
    if self._runtime_setup_complete and tuple(self.tracked_entities) != previous_tracked:
      if self._unsub_state:
        self._unsub_state()
        self._unsub_state = None
      if self.tracked_entities:
        self._unsub_state = async_track_state_change_event(
          self.hass,
          self.tracked_entities,
          self._handle_state_event,
        )

    if self._runtime_setup_complete:
      self._sync_weather_subscriptions()

  async def async_setup(self) -> None:
    """Subscribe to MQTT topics and start observers."""
    self._refresh_runtime_entity_lists()
    self._unsub_connected = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/stat/connected",
      self._async_handle_connected,
    )
    self._unsub_ip = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/stat/ip",
      self._async_handle_ip,
    )
    self._unsub_scene = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/scene",
      self._async_handle_scene_command,
    )

    self._unsub_light = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/light",
      self._async_handle_light_command,
    )

    self._unsub_switch = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/switch",
      self._async_handle_switch_command,
    )

    self._unsub_value = await mqtt.async_subscribe(
      self.hass, f"{self.base_topic}/cmnd/value", self._async_handle_value_command,
    )
    self._unsub_media = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/media",
      self._async_handle_media_command,
    )

    self._unsub_climate = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/climate",
      self._async_handle_climate_command,
    )

    self._unsub_cover = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/cover",
      self._async_handle_cover_command,
    )

    self._unsub_camera = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/camera",
      self._async_handle_camera_command,
    )

    if self.tracked_entities:
      self._unsub_state = async_track_state_change_event(
        self.hass,
        self.tracked_entities,
        self._handle_state_event,
      )
    self._runtime_setup_complete = True

    self._prime_icon_cache()

    _LOGGER.info(
      "Tab5 MQTT bridge ready (device=%s, base=%s, ha_prefix=%s, sensors=%d, lights=%d, switches=%d, media=%d, climate=%d, covers=%d, cameras=%d)",
      self.device_id or "n/a",
      self.base_topic,
      self.ha_prefix,
      len(self.sensors),
      len(self.lights),
      len(self.switches),
      len(self.media_players),
      len(self.climates),
      len(self.covers),
      len(self.cameras),
    )
    if self.config_topic:
      request_topic = f"{CONFIG_TOPIC_ROOT}/{self.device_id}/bridge/request"
      self._unsub_request = await mqtt.async_subscribe(
        self.hass,
        request_topic,
        self._async_handle_request,
      )
      _LOGGER.debug("Tab5 subscribed to request topic %s", request_topic)
    if self.history_request_topic:
      self._unsub_history = await mqtt.async_subscribe(
        self.hass,
        self.history_request_topic,
        self._async_handle_history_request,
      )
      _LOGGER.debug("Tab5 subscribed to history topic %s", self.history_request_topic)
    if self.weather_request_topic:
      self._unsub_weather = await mqtt.async_subscribe(
        self.hass,
        self.weather_request_topic,
        self._async_handle_weather_request,
      )
      _LOGGER.debug("Tab5 subscribed to weather topic %s", self.weather_request_topic)
    if self.energy_request_topic:
      self._unsub_energy = await mqtt.async_subscribe(
        self.hass,
        self.energy_request_topic,
        self._async_handle_energy_request,
      )
      _LOGGER.debug("Tab5 subscribed to energy topic %s", self.energy_request_topic)
    self._schedule_config_refresh()
    self._unsub_weather_stop = self.hass.bus.async_listen_once(
      EVENT_HOMEASSISTANT_STOP, self._async_stop_weather_updates
    )
    self._unsub_weather_started = async_at_started(self.hass, self._weather_started)

  async def async_unload(self) -> None:
    """Cleanup subscriptions."""
    self._runtime_setup_complete = False
    await self._async_stop_weather_updates()
    # Release any state-publish ownership so a surviving entry can reclaim it.
    owners = self.hass.data.get(DOMAIN, {}).get("state_owners")
    if owners:
      for key in [k for k, v in owners.items() if v == self.entry.entry_id]:
        owners.pop(key, None)
    if self._unsub_state:
      self._unsub_state()
      self._unsub_state = None
    if self._unsub_connected:
      self._unsub_connected()
      self._unsub_connected = None
    if self._unsub_ip:
      self._unsub_ip()
      self._unsub_ip = None
    if self._unsub_scene:
      self._unsub_scene()
      self._unsub_scene = None
    if self._unsub_light:
      self._unsub_light()
      self._unsub_light = None
    if self._unsub_switch:
      self._unsub_switch()
      self._unsub_switch = None
    if self._unsub_value:
      self._unsub_value()
      self._unsub_value = None
    if self._unsub_media:
      self._unsub_media()
      self._unsub_media = None
    if self._unsub_climate:
      self._unsub_climate()
      self._unsub_climate = None
    if self._unsub_cover:
      self._unsub_cover()
      self._unsub_cover = None
    if self._unsub_camera:
      self._unsub_camera()
      self._unsub_camera = None
    camera_stream_manager = self.hass.data.get(DOMAIN, {}).get("camera_stream_manager")
    if camera_stream_manager and self.device_id:
      await camera_stream_manager.async_stop_device(self.device_id)
    if hasattr(self, "_unsub_request") and self._unsub_request:
      self._unsub_request()
      self._unsub_request = None
    if self._unsub_history:
      self._unsub_history()
      self._unsub_history = None
    if self._unsub_weather:
      self._unsub_weather()
      self._unsub_weather = None
    if self._unsub_energy:
      self._unsub_energy()
      self._unsub_energy = None
    if self._config_refresh_handles:
      for unsub in self._config_refresh_handles:
        unsub()
      self._config_refresh_handles = []
      self._config_refresh_pending = 0
    if self._icon_refresh_handle:
      self._icon_refresh_handle()
      self._icon_refresh_handle = None

  async def async_publish_config_to_device(self, *, force: bool = False) -> None:
    """Publish retained config when its stable metadata changed.

    Startup retries still rebuild the payload so entities registered a little
    later are picked up. Runtime values, states and icons are delivered through
    their lightweight topics and do not invalidate the config signature. An
    explicit firmware request can force a full resend when needed.
    """
    if not self.config_topic or not self.device_id:
      return
    async with self._config_publish_lock:
      self._refresh_runtime_entity_lists()
      config_data: dict[str, Any] = {
          "device_id": self.device_id,
          "base_topic": self.base_topic,
          "ha_prefix": self.ha_prefix,
          "sensors": self.sensors,
          "configured_sensors": self._configured_sensors,
          "sensor_meta": self._build_sensor_meta(),
          CONF_BINARY_SENSORS: self.binary_sensors,
          "binary_sensor_meta": self._build_binary_sensor_meta(),
          CONF_WEATHERS: self.weathers,
          "weather_meta": self._build_weather_meta(),
          "lights": self.lights,
          "light_meta": self._build_entity_meta(self.lights),
          "switches": self.switches,
          "switch_meta": self._build_entity_meta(self.switches),
          CONF_MEDIA_PLAYERS: self.media_players,
          "media_player_meta": self._build_entity_meta(self.media_players),
          CONF_CLIMATES: self.climates,
          "climate_meta": self._build_entity_meta(self.climates),
          CONF_NUMBERS: self.numbers,
          CONF_SELECTS: self.selects,
          CONF_DATETIMES: self.datetimes,
          CONF_COVERS: self.covers,
          "cover_meta": self._build_entity_meta(self.covers),
          "editable_meta": self._build_entity_meta(self.numbers + self.selects + self.datetimes),
          CONF_CAMERAS: self.cameras,
          "camera_meta": self._build_entity_meta(self.cameras),
          "scene_meta": self._build_scene_meta(),
          "scene_map": self.scene_map,
      }
      energy_cats = set()
      if self.entry.data.get(CONF_ENERGY_ELECTRICITY):
        energy_cats.update({"solar", "grid", "battery", "device"})
      if self.entry.data.get(CONF_ENERGY_GAS):
        energy_cats.add("gas")
      if self.entry.data.get(CONF_ENERGY_WATER):
        energy_cats.update({"water", "device_water"})
      if energy_cats:
        energy_entries = await self._build_energy_meta(energy_cats)
        if energy_entries:
          config_data["energy"] = energy_entries
      signature = _config_signature(config_data)
      if not force and signature == self._last_config_signature:
        _LOGGER.debug(
          "Tab5 config unchanged; skipping duplicate publish to %s",
          self.config_topic,
        )
        return
      payload = json.dumps(config_data)
      _LOGGER.debug(
        "Publishing Tab5 config to %s (%d bytes, forced=%s)",
        self.config_topic,
        len(payload),
        force,
      )
      await mqtt.async_publish(
        self.hass,
        self.config_topic,
        payload,
        qos=1,
        retain=True,
      )
      self._last_config_signature = signature
      await self._async_publish_icon_update()

  async def async_publish_snapshot(self) -> None:
    """Push all configured entities to MQTT."""
    for entity_id in self.tracked_entities:
      state = self.hass.states.get(entity_id)
      if entity_id in getattr(self, "numbers", []) + getattr(self, "selects", []) + getattr(self, "datetimes", []) and not state:
        await self._async_publish_editable_state(entity_id, None)
      if not state:
        if entity_id in self.binary_sensors:
          await self._async_publish_binary_sensor_absent_state(entity_id)
        elif entity_id in self.switches:
          await self._async_publish_switch_absent_state(entity_id)
        continue
      await self._async_publish_entity_state(entity_id, state)

  def _editable_payload(self, entity_id, state):
    payload = build_editable_payload(
      entity_id, state.state if state else None,
      state.attributes if state else {}, self._editable_session,
      self.hass.config.time_zone,
    )
    changed = getattr(state, "last_changed", None)
    if isinstance(changed, datetime):
      payload["last_changed"] = int(changed.timestamp())
    return payload

  async def _async_publish_editable_state(self, entity_id, state):
    if not self._owns_state_publish(entity_id):
      return
    payload = json.dumps(self._editable_payload(entity_id, state), ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_CONTROL_BYTES:
      return
    # An additive topic preserves plain state payloads consumed by old firmware.
    await mqtt.async_publish(self.hass, self._ha_topic_for_entity(entity_id, "control"),
                             payload, qos=0, retain=True)

  async def _async_handle_value_command(self, msg):
    if getattr(msg, "retain", False) or len(msg.payload) > 2048:
      return
    command = _try_parse_json(msg.payload)
    if not isinstance(command, dict):
      return
    entity_id = command.get("entity_id")
    command_id = command.get("id")
    if (entity_id not in self.numbers + self.selects + self.datetimes or
        not isinstance(command_id, str) or not 1 <= len(command_id) <= 48):
      return
    now = dt_util.utcnow().timestamp()
    deadline = command.get("deadline")
    status = "invalid_value"
    try:
      if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or
          not 0 < deadline - now <= 15 or command.get("session") != self._editable_session):
        raise ValueError("expired")
      self._editable_seen = {key: expiry for key, expiry in self._editable_seen.items() if expiry > now}
      if command_id in self._editable_seen or len(self._editable_seen) >= 128:
        return
      self._editable_seen[command_id] = deadline
      state = self.hass.states.get(entity_id)
      payload = self._editable_payload(entity_id, state)
      if command.get("revision") != payload["revision"]:
        raise ValueError("changed")
      domain, service, data = build_editable_service_call(
        entity_id, command.get("value"), payload, self.hass.config.time_zone)
      await self.hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
      status = "ok"
    except ValueError as error:
      status = str(error)
    except Exception:
      status = "failed"
      _LOGGER.exception("HomeTiles value command failed for %s", entity_id)
    await mqtt.async_publish(self.hass, f"{self.base_topic}/stat/value",
      json.dumps({"entity_id": entity_id, "id": command_id, "status": status}), qos=0, retain=False)
    await self._async_publish_editable_state(entity_id, self.hass.states.get(entity_id))

  async def _async_handle_editable_history(self, parsed):
    entity_id = parsed.get("entity_id")
    if entity_id not in self.numbers + self.selects + self.datetimes:
      return
    try:
      request = parse_state_history_request(parsed)
    except StateHistoryRequestError:
      return
    end = dt_util.utcnow()
    start = end - timedelta(hours=request.hours)
    current = self.hass.states.get(entity_id)
    available, complete, until = False, False, start
    states = []
    try:
      recorder = get_instance(self.hass)
      recorded = getattr(recorder, "entity_filter", None)
      available = bool(state_changes_during_period or get_last_state_changes)
      if callable(recorded):
        available = available and recorded(entity_id)
      if available:
        def fetch():
          return fetch_bounded_state_history(self.hass, entity_id, start, end,
            state_changes_during_period=state_changes_during_period,
            get_last_state_changes=get_last_state_changes,
            recent_limit=request.max_transitions + 1)
        states, complete, until = await recorder.async_add_executor_job(fetch)
    except Exception:
      available = False
      _LOGGER.debug("HomeTiles Recorder unavailable for %s", entity_id, exc_info=True)
    if domain_of(entity_id) in DATETIME_DOMAINS:
      normalized = []
      attributes = current.attributes if current else {}
      for record in states:
        raw = record.get("state") if isinstance(record, dict) else getattr(record, "state", None)
        changed = record.get("last_changed", record.get("last_updated")) if isinstance(record, dict) else getattr(record, "last_changed", None)
        value = build_editable_payload(entity_id, raw, attributes, self._editable_session, self.hass.config.time_zone)
        normalized.append({"state": value["state"], "last_changed": changed})
      states = normalized
      if current is not None:
        current = {"state": self._editable_payload(entity_id, current)["state"],
                   "last_changed": getattr(current, "last_changed", None)}
    response = build_state_history_response(entity_id, states, current, start, end,
      request.hours, request.max_transitions, history_available=available,
      history_complete=complete, history_complete_until=until)
    if domain_of(entity_id) in NUMBER_DOMAINS:
      response = add_number_history(response, states if available else [], start, end,
                                   288 if request.hours == 24 else 168,
                                   complete=complete, complete_until=until)
      response["unit"] = str((current.attributes if current else {}).get("unit_of_measurement") or "")[:32]
    response["request_id"] = str(parsed.get("request_id") or "")[:48]
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) <= STATE_HISTORY_MAX_RESPONSE_BYTES:
      await mqtt.async_publish(self.hass, self.history_response_topic, encoded, qos=0, retain=False)

  async def _async_publish_binary_sensor_absent_state(
    self, entity_id: str
  ) -> None:
    """Replace a stale retained binary state when HA has no current entity."""
    if entity_id not in self.binary_sensors or not self._owns_state_publish(entity_id):
      return
    await mqtt.async_publish(
      self.hass,
      self._ha_topic_for_entity(entity_id, "state"),
      json.dumps(
        build_binary_sensor_state_payload(None, {}, None),
        separators=(",", ":"),
      ),
      qos=0,
      retain=True,
    )

  async def _async_publish_switch_absent_state(self, entity_id: str) -> None:
    """Clear retained on/off state after removal, including during startup."""
    if entity_id not in self.switches or not self._owns_state_publish(entity_id):
      return
    await mqtt.async_publish(
      self.hass, self._ha_topic_for_entity(entity_id, "state"),
      ("unavailable" if entity_domain(entity_id) == "switch" else
       json.dumps(build_switch_state_payload(entity_id, None, {}))),
      qos=0, retain=True,
    )

  async def _async_build_state_payload(
    self,
    entity_id: str,
    state: State,
    *,
    include_media_cover: bool = True,
  ) -> str:
    if entity_id.startswith("media_player."):
      payload = _extract_media_player_payload(state, self.hass)
      if include_media_cover:
        await self._async_attach_media_cover_data(entity_id, payload)
      payload_text = json.dumps(payload, default=str)
      if include_media_cover:
        _LOGGER.debug(
          "Tab5 media state payload for %s: %s chars, cover_data=%s",
          entity_id,
          len(payload_text),
          "entity_picture_data" in payload,
        )
      return payload_text
    return self._build_state_payload(entity_id, state)

  def _owns_state_publish(self, entity_id: str) -> bool:
    """Return True if this entry is responsible for publishing entity_id.

    Multiple config entries (devices) can track the same entities while
    sharing the same ha_prefix, which means they all resolve to the SAME
    global ``<ha_prefix>/<entity>/state`` topic. Without coordination every
    state change is published once per device (e.g. 3x for three displays),
    tripling MQTT traffic and the per-update work on every display. We let
    the first entry that handles a given (ha_prefix, entity) own its
    publishes; the others skip. Ownership is released on unload so a
    surviving entry can reclaim it.
    """
    owners = self.hass.data[DOMAIN].setdefault("state_owners", {})
    key = (self.ha_prefix, entity_id)
    owner = owners.get(key)
    if owner is None:
      owners[key] = self.entry.entry_id
      return True
    return owner == self.entry.entry_id

  async def _async_publish_entity_state(self, entity_id: str, state: State) -> None:
    if not self._owns_state_publish(entity_id):
      return
    topic = self._ha_topic_for_entity(entity_id, "state")
    if entity_id in self.numbers + self.selects + self.datetimes:
      await self._async_publish_editable_state(entity_id, state)

    if entity_id.startswith("media_player."):
      generation = self._media_publish_generation.get(entity_id, 0) + 1
      self._media_publish_generation[entity_id] = generation
      # Publish the small state on an additional topic first. Previously every
      # play/pause/volume change waited for cover fetch/resize and then travelled
      # as a 12-19 KB MQTT packet. New firmware subscribes to state_fast as well
      # as state. Older firmware remains on the unchanged, full retained state
      # topic, so its cover/update behaviour stays completely compatible.
      fast_payload = await self._async_build_state_payload(
        entity_id,
        state,
        include_media_cover=False,
      )
      fast_topic = self._ha_topic_for_entity(entity_id, "state_fast")
      await mqtt.async_publish(self.hass, fast_topic, fast_payload, qos=0, retain=True)

    payload = await self._async_build_state_payload(entity_id, state)
    if entity_id.startswith("media_player."):
      # State-change tasks run concurrently. A slow cover HTTP request from an
      # older event must never arrive after a newer play/pause/title state and
      # overwrite it on the retained topic.
      if self._media_publish_generation.get(entity_id) != generation:
        _LOGGER.debug("Skipping stale media payload for %s", entity_id)
        return
    await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)
    if _is_weather_entity(entity_id):
      await self._async_publish_weather_state(entity_id, state, retain=True)

  def _log_media_cover_warning(
    self,
    entity_id: str,
    reason: str,
    message: str,
    *args: Any,
  ) -> None:
    """Rate-limit recurring media-cover problems by entity and reason."""
    now = monotonic()
    key = (entity_id, reason)
    last_logged = self._media_cover_warning_last.get(key)
    if last_logged is not None and now - last_logged < MEDIA_COVER_WARNING_INTERVAL_SECONDS:
      return

    self._media_cover_warning_last[key] = now
    if len(self._media_cover_warning_last) > MEDIA_COVER_WARNING_MAX_KEYS:
      oldest_key = min(
        self._media_cover_warning_last,
        key=self._media_cover_warning_last.get,
      )
      self._media_cover_warning_last.pop(oldest_key, None)
    _LOGGER.warning(message, *args)

  async def _async_attach_media_cover_data(self, entity_id: str, payload: Dict[str, Any]) -> None:
    url = ""
    # Prefer media_image_url first - it usually points at the upstream CDN
    # (e.g. Spotify scdn.co) which is more stable than HA's media_player_proxy.
    # The proxy occasionally serves truncated JPEGs which the display then
    # renders only partially.
    for key in ("media_image_url", "entity_picture"):
      value = payload.get(key)
      if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = value
        break
    if not url:
      _LOGGER.debug("Tab5 media cover has no artwork URL for %s", entity_id)
      return

    _LOGGER.debug("Tab5 media cover fetch started for %s", entity_id)

    cached = self._media_cover_cache.get(url)
    if cached:
      payload.update(cached)
      _LOGGER.debug(
        "Tab5 media cover cache hit for %s: %s bytes",
        entity_id,
        cached.get("entity_picture_bytes"),
      )
      return

    try:
      session = async_get_clientsession(self.hass)
      async with session.get(url, timeout=5) as response:
        if response.status != 200:
          self._log_media_cover_warning(
            entity_id,
            f"http_{response.status}",
            "Tab5 media cover fetch failed for %s: HTTP %s",
            entity_id,
            response.status,
          )
          return
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        # Drain the response in chunks. A single read(N) on aiohttp
        # occasionally returns short when the server uses chunked transfer
        # encoding (HA media_player_proxy does this), which leaves the JPEG
        # truncated. Looping until EOF or the byte cap is reached avoids that.
        chunks: List[bytes] = []
        received = 0
        async for chunk in response.content.iter_chunked(16384):
          if not chunk:
            break
          chunks.append(chunk)
          received += len(chunk)
          if received > MEDIA_COVER_FETCH_MAX_BYTES:
            break
        data = b"".join(chunks)
    except Exception as err:  # pragma: no cover - network dependent
      self._log_media_cover_warning(
        entity_id,
        "network",
        "Tab5 media cover fetch failed for %s: %s",
        entity_id,
        type(err).__name__,
      )
      return

    if len(data) == 0:
      self._log_media_cover_warning(
        entity_id,
        "empty",
        "Tab5 media cover skipped for %s: empty response",
        entity_id,
      )
      return

    # Some upstreams (radio TuneIn covers, etc.) advertise
    # application/octet-stream or no content-type at all. Don't reject those;
    # sniff the magic bytes instead and assign the correct MIME so the display
    # picks the right decoder.
    if data[:3] == b"\xff\xd8\xff":
      sniffed_mime = "image/jpeg"
    elif _is_png_payload(data):
      sniffed_mime = "image/png"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
      sniffed_mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
      sniffed_mime = "image/webp"
    else:
      sniffed_mime = ""

    if not content_type or not content_type.startswith("image/"):
      if sniffed_mime:
        _LOGGER.debug(
          "Tab5 media cover: %s reported content-type=%s, sniffed %s",
          entity_id,
          content_type or "<none>",
          sniffed_mime,
        )
        content_type = sniffed_mime
      else:
        self._log_media_cover_warning(
          entity_id,
          "invalid_type",
          "Tab5 media cover skipped for %s: content-type=%s, magic=%s",
          entity_id,
          content_type or "<none>",
          data[:4].hex(),
        )
        return
    elif sniffed_mime and sniffed_mime != content_type:
      # Server lied about the type (e.g. claimed jpeg but body is png) - trust
      # the bytes since the display routes by MIME.
      content_type = sniffed_mime

    if len(data) > MEDIA_COVER_FETCH_MAX_BYTES:
      self._log_media_cover_warning(
        entity_id,
        "fetch_too_large",
        "Tab5 media cover skipped for %s: %s bytes > fetch limit %s",
        entity_id,
        len(data),
        MEDIA_COVER_FETCH_MAX_BYTES,
      )
      return

    should_convert = True

    if should_convert or len(data) > MEDIA_COVER_MAX_BYTES:
      resized = await self.hass.async_add_executor_job(_resize_media_cover, data)
      if not resized:
        # Fallback: if Pillow is missing or the resize crashes, still ship the
        # original payload as long as it fits the MQTT cover budget. Otherwise
        # we drop the cover (display has no way to scale it down on its own).
        if len(data) <= MEDIA_COVER_MAX_BYTES and (
          content_type.startswith("image/jpeg")
          or content_type.startswith("image/jpg")
          or content_type.startswith("image/png")
          or _is_png_payload(data)
          or data[:3] == b"\xff\xd8\xff"
        ):
          self._log_media_cover_warning(
            entity_id,
            "resize_fallback",
            "Tab5 media cover: resize failed for %s, sending original (%s bytes, %s)",
            entity_id,
            len(data),
            content_type or "unknown",
          )
        else:
          self._log_media_cover_warning(
            entity_id,
            "conversion_failed",
            "Tab5 media cover skipped for %s: %s bytes, type=%s, conversion failed",
            entity_id,
            len(data),
            content_type or "unknown",
          )
          return
      else:
        resized_data, resized_mime = resized
        _LOGGER.debug(
          "Tab5 media cover converted for %s: %s %s bytes -> %s %s bytes",
          entity_id,
          content_type or "unknown",
          len(data),
          resized_mime,
          len(resized_data),
        )
        data = resized_data
        content_type = resized_mime

    if len(data) > MEDIA_COVER_MAX_BYTES:
      self._log_media_cover_warning(
        entity_id,
        "resized_too_large",
        "Tab5 media cover skipped for %s: resized %s bytes > %s",
        entity_id,
        len(data),
        MEDIA_COVER_MAX_BYTES,
      )
      return

    cover_payload = {
      "entity_picture_data": base64.b64encode(data).decode("ascii"),
      "entity_picture_mime": content_type or "image/*",
      "entity_picture_bytes": len(data),
    }
    self._media_cover_cache[url] = cover_payload
    while len(self._media_cover_cache) > MEDIA_COVER_CACHE_MAX:
      self._media_cover_cache.pop(next(iter(self._media_cover_cache)))
    payload.update(cover_payload)
    _LOGGER.debug(
      "Tab5 media cover attached for %s: %s bytes, base64=%s chars",
      entity_id,
      len(data),
      len(cover_payload["entity_picture_data"]),
    )


  async def _async_handle_connected(self, msg: ReceiveMessage) -> None:
    """Handle Tab5 connection event."""
    if msg.payload == "1":
      _LOGGER.debug("Tab5 connected -> push config + snapshot")
      await self.async_publish_config_to_device(force=True)
      await self.async_publish_snapshot()
      self._schedule_config_refresh()

  async def _async_handle_ip(self, msg: ReceiveMessage) -> None:
    """Mirror the panel's current LAN IP onto the device's configuration_url,
    so the HA device page gets a working link into the panel's own web-admin
    UI. Retained on the firmware side, so this also fires right after
    subscribe with whatever IP the panel last connected from."""
    ip = msg.payload.strip()
    if not ip or not self.device_id:
      return
    try:
      self._device_ip = str(ip_address(ip))
    except ValueError:
      _LOGGER.warning("Tab5 reported an invalid LAN IP: %s", ip)
      return
    device_reg = dr.async_get(self.hass)
    device = find_entry_device(
      device_reg, (DOMAIN, self.device_id), self.entry.entry_id
    )
    if not device:
      return
    url = f"http://{ip}/"
    if device.configuration_url != url:
      device_reg.async_update_device(device.id, configuration_url=url)

  async def _async_handle_request(self, msg: ReceiveMessage) -> None:
    """Handle explicit bridge refresh requests."""
    request = msg.payload.strip().lower()
    force = request in {"force", "1", "full"}
    _LOGGER.debug(
      "Tab5 requested bridge refresh via %s (forced=%s)",
      msg.topic,
      force,
    )
    await self.async_publish_config_to_device(force=force)
    await self.async_publish_snapshot()

  async def _async_handle_state_history_request(
    self, parsed: Dict[str, Any]
  ) -> None:
    """Handle bounded history for a configured textual sensor state."""
    entity_id = str(parsed.get("entity_id") or "").strip()

    async def _publish_error(code: str) -> None:
      _LOGGER.warning(
        "HomeTiles state history request failed for %s: %s",
        entity_id or "<missing>",
        code,
      )
      await mqtt.async_publish(
        self.hass,
        self.history_response_topic,
        json.dumps(
          build_state_history_error(entity_id, code),
          separators=(",", ":"),
        ),
        qos=0,
        retain=False,
      )

    try:
      request = parse_state_history_request(parsed)
    except StateHistoryRequestError as err:
      await _publish_error(err.code)
      return

    if not entity_id:
      await _publish_error("missing_entity_id")
      return
    if not entity_id.startswith("sensor."):
      await _publish_error("invalid_entity_id")
      return
    if entity_id not in self.sensors:
      await _publish_error("entity_not_configured")
      return

    end = dt_util.utcnow()
    start = end - timedelta(hours=request.hours)
    current_state = self.hass.states.get(entity_id)

    def _fetch_state_history_states() -> Tuple[List[Any], bool, datetime]:
      return fetch_bounded_state_history(
        self.hass,
        entity_id,
        start,
        end,
        state_changes_during_period=state_changes_during_period,
        get_last_state_changes=get_last_state_changes,
        recent_limit=request.max_transitions + 1,
      )

    history_available = (
      state_changes_during_period is not None
      or get_last_state_changes is not None
    )
    history_complete = False
    history_complete_until = start
    try:
      if history_available:
        (
          history_states,
          history_complete,
          history_complete_until,
        ) = await get_instance(self.hass).async_add_executor_job(
          _fetch_state_history_states
        )
      else:
        history_states = []
    except Exception:
      _LOGGER.exception(
        "HomeTiles state history Recorder query failed for %s", entity_id
      )
      history_states = []
      history_available = False
      history_complete = False
      history_complete_until = start

    response = build_state_history_response(
      entity_id,
      history_states,
      current_state,
      start,
      end,
      request.hours,
      request.max_transitions,
      history_available=history_available,
      history_complete=history_complete,
      history_complete_until=history_complete_until,
    )
    response_payload = json.dumps(
      response,
      ensure_ascii=False,
      separators=(",", ":"),
    )
    response_bytes = len(response_payload.encode("utf-8"))
    if response_bytes > STATE_HISTORY_MAX_RESPONSE_BYTES:
      await _publish_error("response_too_large")
      return

    _LOGGER.debug(
      "HomeTiles state history response for %s: %d segments, %d activity entries, %d palette entries, %d bytes, complete=%s",
      entity_id,
      len(response["segments"]),
      len(response["activity"]),
      len(response["palette"]),
      response_bytes,
      response["timeline_complete"],
    )
    await mqtt.async_publish(
      self.hass,
      self.history_response_topic,
      response_payload,
      qos=0,
      retain=False,
    )

  async def _async_handle_binary_history_request(
    self, parsed: Dict[str, Any]
  ) -> None:
    """Handle the bounded, versioned binary history protocol."""
    entity_id = str(parsed.get("entity_id") or "").strip()

    async def _publish_error(code: str) -> None:
      _LOGGER.warning(
        "Tab5 binary history request failed for %s: %s",
        entity_id or "<missing>",
        code,
      )
      await mqtt.async_publish(
        self.hass,
        self.history_response_topic,
        json.dumps(
          build_binary_history_error(entity_id, code),
          separators=(",", ":"),
        ),
        qos=0,
        retain=False,
      )

    try:
      request = parse_binary_history_request(parsed)
    except BinaryHistoryRequestError as err:
      await _publish_error(err.code)
      return

    if not entity_id:
      await _publish_error("missing_entity_id")
      return
    if not entity_id.startswith("binary_sensor."):
      await _publish_error("invalid_entity_id")
      return
    if entity_id not in self.binary_sensors:
      await _publish_error("entity_not_configured")
      return

    end = dt_util.utcnow()
    start = end - timedelta(hours=request.hours)
    current_state = self.hass.states.get(entity_id)

    def _state_change_time(state: Any) -> Optional[datetime]:
      value = getattr(state, "last_updated", None)
      if value is None:
        value = getattr(state, "last_changed", None)
      return value if isinstance(value, datetime) else None

    def _fetch_recent_legacy_states() -> Tuple[List[Any], bool, datetime]:
      if get_last_state_changes is None:
        return [], False, start
      recent_limit = request.max_transitions + 1
      recent_history = get_last_state_changes(
        self.hass,
        recent_limit,
        entity_id,
      )
      recent_states = (
        list(recent_history.get(entity_id, [])) if recent_history else []
      )[-recent_limit:]
      complete_until = start
      for state in recent_states:
        moment = _state_change_time(state)
        if moment is not None and moment > complete_until:
          complete_until = moment
      return recent_states, False, complete_until

    def _fetch_recent_tail_states() -> List[Any]:
      if get_last_state_changes is None:
        return []
      recent_history = get_last_state_changes(
        self.hass,
        BINARY_HISTORY_RECORDER_RECENT_ROWS,
        entity_id,
      )
      return (
        list(recent_history.get(entity_id, []))
        if recent_history
        else []
      )[-BINARY_HISTORY_RECORDER_RECENT_ROWS:]

    def _fetch_binary_history_states() -> Tuple[List[Any], bool, datetime]:
      if state_changes_during_period is None:
        return _fetch_recent_legacy_states()

      history_states: List[Any] = []
      cursor = start
      include_start_state = True
      scanned_changes = 0
      complete_until = start

      while scanned_changes < BINARY_HISTORY_RECORDER_MAX_CHANGES:
        page_limit = min(
          BINARY_HISTORY_RECORDER_PAGE_SIZE,
          BINARY_HISTORY_RECORDER_MAX_CHANGES - scanned_changes,
        )
        try:
          page = state_changes_during_period(
            self.hass,
            cursor,
            end,
            entity_id,
            include_start_time_state=include_start_state,
            no_attributes=True,
            limit=page_limit,
          )
        except TypeError:
          # Compatibility fallback stays bounded for older Home Assistant
          # recorder signatures that do not support paged queries.
          if not history_states:
            return _fetch_recent_legacy_states()
          return (
            history_states + _fetch_recent_tail_states(),
            False,
            complete_until,
          )

        candidate_limit = page_limit + (1 if include_start_state else 0)
        candidates = (
          list(page.get(entity_id, []))[:candidate_limit]
          if page
          else []
        )
        page_changes: List[Tuple[datetime, Any]] = []
        if include_start_state:
          for candidate in candidates:
            moment = _state_change_time(candidate)
            if moment is None or moment <= cursor:
              history_states.append(candidate)
              break
        for candidate in candidates:
          moment = _state_change_time(candidate)
          if moment is not None and moment > cursor:
            page_changes.append((moment, candidate))

        if not page_changes:
          return history_states, True, end
        page_changes.sort(key=lambda item: item[0])
        history_states.extend(candidate for _, candidate in page_changes)
        scanned_changes += len(page_changes)
        next_cursor = page_changes[-1][0]
        if next_cursor <= cursor:
          return (
            history_states + _fetch_recent_tail_states(),
            False,
            complete_until,
          )
        cursor = next_cursor
        complete_until = cursor
        include_start_state = False
        if len(page_changes) < page_limit:
          return history_states, True, end

      return (
        history_states + _fetch_recent_tail_states(),
        False,
        complete_until,
      )

    history_available = (
      state_changes_during_period is not None
      or get_last_state_changes is not None
    )
    history_complete = False
    history_complete_until = start
    try:
      if history_available:
        (
          history_states,
          history_complete,
          history_complete_until,
        ) = await get_instance(
          self.hass
        ).async_add_executor_job(_fetch_binary_history_states)
      else:
        history_states = []
    except Exception:
      _LOGGER.exception(
        "Tab5 binary history recorder query failed for %s", entity_id
      )
      history_states = []
      history_available = False
      history_complete = False
      history_complete_until = start

    response = build_binary_history_response(
      entity_id,
      history_states,
      current_state,
      start,
      end,
      request.hours,
      request.max_transitions,
      history_available=history_available,
      history_complete=history_complete,
      history_complete_until=history_complete_until,
    )
    _LOGGER.debug(
      "Tab5 binary history response for %s: %d segments, %d activity entries, complete=%s",
      entity_id,
      len(response["segments"]),
      len(response["activity"]),
      response["timeline_complete"],
    )
    await mqtt.async_publish(
      self.hass,
      self.history_response_topic,
      json.dumps(response, separators=(",", ":")),
      qos=0,
      retain=False,
    )

  async def _async_handle_history_request(self, msg: ReceiveMessage) -> None:
    """Handle history requests from the Tab5 popup."""
    if not self.history_response_topic:
      return

    parsed = _try_parse_json(msg.payload)
    if not isinstance(parsed, dict):
      _LOGGER.warning("Tab5 history request ignored (invalid payload): %s", msg.payload)
      return

    history_kind = str(parsed.get("kind") or "").strip().lower()
    if history_kind == "editable":
      if not getattr(msg, "retain", False):
        await self._async_handle_editable_history(parsed)
      return
    if history_kind == STATE_HISTORY_KIND:
      await self._async_handle_state_history_request(parsed)
      return
    if history_kind == BINARY_HISTORY_KIND:
      await self._async_handle_binary_history_request(parsed)
      return

    entity_id = str(parsed.get("entity_id") or "").strip()
    if not entity_id:
      _LOGGER.warning("Tab5 history request ignored (missing entity_id)")
      return
    # Only configured entities may be read. Binary sensors stay allowed for
    # legacy Sensor tiles of firmware before v0.6.9. The numeric protocol has
    # no error field, so an unconfigured entity gets no reply at all.
    if entity_id not in self.sensors and entity_id not in self.binary_sensors:
      _LOGGER.debug("Tab5 history request ignored (entity not configured): %s", entity_id)
      return

    hours = _coerce_int(parsed.get("hours"), 24, 1, 168)
    period_minutes = _coerce_int(parsed.get("period_minutes"), 5, 1, 60)
    points = _coerce_int(parsed.get("points"), int(hours * 60 / period_minutes), 1, 720)
    stat = str(parsed.get("stat") or "mean").strip().lower() or "mean"

    end = dt_util.utcnow()
    start = end - timedelta(hours=hours)

    def _coerce_float(raw: Any) -> Optional[float]:
      if raw is None:
        return None
      try:
        return float(raw)
      except (TypeError, ValueError):
        return None

    if stat not in {"mean", "min", "max", "last"}:
      stat = "mean"

    state = self.hass.states.get(entity_id)
    current_numeric = _coerce_float(state.state) if state else None

    def _empty_values_with_current() -> List[Optional[float]]:
      values: List[Optional[float]] = [None] * points
      if points > 0 and current_numeric is not None:
        values[-1] = round(current_numeric, 3)
      return values

    def _fetch_history_values() -> List[Optional[float]]:
      if points <= 0 or (state_changes_during_period is None and get_significant_states is None):
        return _empty_values_with_current()

      history = None
      if state_changes_during_period is not None:
        try:
          history = state_changes_during_period(
            self.hass,
            start,
            end,
            entity_id,
            include_start_time_state=True,
            minimal_response=True,
            no_attributes=True,
          )
        except TypeError:
          history = state_changes_during_period(self.hass, start, end, entity_id)
      elif get_significant_states is not None:
        try:
          history = get_significant_states(
            self.hass,
            start,
            end,
            [entity_id],
            include_start_time_state=True,
            minimal_response=True,
            no_attributes=True,
          )
        except TypeError:
          history = get_significant_states(self.hass, start, end, [entity_id])

      states = history.get(entity_id, []) if history else []
      if not states:
        return _empty_values_with_current()

      bucket_seconds = max(period_minutes, 1) * 60
      sums = [0.0] * points
      counts = [0] * points
      mins: List[Optional[float]] = [None] * points
      maxs: List[Optional[float]] = [None] * points
      lasts: List[Optional[float]] = [None] * points

      for state in states:
        state_time = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
        if state_time is None:
          continue
        idx = int((state_time - start).total_seconds() / bucket_seconds)
        if idx < 0:
          continue
        if idx >= points:
          idx = points - 1
        value = _coerce_float(getattr(state, "state", None))
        if value is None:
          continue
        counts[idx] += 1
        sums[idx] += value
        if mins[idx] is None or value < mins[idx]:
          mins[idx] = value
        if maxs[idx] is None or value > maxs[idx]:
          maxs[idx] = value
        lasts[idx] = value

      values: List[Optional[float]] = []
      for idx in range(points):
        value: Optional[float] = None
        if counts[idx] > 0:
          if stat == "min":
            value = mins[idx]
          elif stat == "max":
            value = maxs[idx]
          elif stat == "last":
            value = lasts[idx]
          else:
            value = sums[idx] / counts[idx]
        values.append(round(value, 3) if value is not None else None)

      if not any(v is not None for v in values):
        return _empty_values_with_current()

      return values

    values = await get_instance(self.hass).async_add_executor_job(_fetch_history_values)
    numeric_points = sum(1 for v in values if v is not None)
    _LOGGER.debug("Tab5 history response for %s: %d/%d points", entity_id, numeric_points, len(values))
    response: Dict[str, Any] = {
      "entity_id": entity_id,
      "hours": hours,
      "period_minutes": period_minutes,
      "stat": stat,
      "values": values,
    }

    if state:
      unit = state.attributes.get("unit_of_measurement")
      if isinstance(unit, str) and unit.strip():
        response["unit"] = unit.strip()
      name = state.name
      if isinstance(name, str) and name.strip():
        response["name"] = name.strip()
      response["current"] = state.state

    await mqtt.async_publish(
      self.hass,
      self.history_response_topic,
      json.dumps(response, separators=(",", ":")),
      qos=0,
      retain=False,
    )

  async def _async_handle_energy_request(self, msg: ReceiveMessage) -> None:
    """Handle energy statistics requests from the Tab5 display."""
    if not self.energy_response_topic:
      return

    parsed = _try_parse_json(msg.payload)
    if not isinstance(parsed, dict):
      parsed = {}

    period = str(parsed.get("period") or "day").strip().lower()
    if period not in {"day", "week", "month"}:
      period = "day"

    if async_get_energy_manager is None:
      _LOGGER.warning("Tab5 energy request ignored (energy component not available)")
      return
    if statistics_during_period is None:
      _LOGGER.warning("Tab5 energy request ignored (statistics_during_period not available)")
      return

    try:
      manager = await async_get_energy_manager(self.hass)
    except Exception:
      _LOGGER.exception("Tab5 failed to load energy manager")
      return
    prefs = manager.data
    if not prefs:
      _LOGGER.warning("Tab5 energy request ignored (no energy preferences configured)")
      return

    sources = prefs.get("energy_sources") or []
    devices = prefs.get("device_consumption") or []
    devices_water = prefs.get("device_consumption_water") or []
    energy_runtime = self.hass.data.get("energy") or {}
    cost_sensors = energy_runtime.get("cost_sensors") or {}

    def _cost_stat(configured_cost: Any, energy_stat: str | None) -> str | None:
      if isinstance(configured_cost, str) and configured_cost.strip():
        return configured_cost.strip()
      if isinstance(energy_stat, str) and energy_stat.strip():
        mapped = cost_sensors.get(energy_stat.strip())
        if isinstance(mapped, str) and mapped.strip():
          return mapped.strip()
      return None

    def _price_fields(config: dict[str, Any], export: bool = False) -> dict[str, Any]:
      price_entity_key = "entity_energy_price_export" if export else "entity_energy_price"
      number_price_key = "number_energy_price_export" if export else "number_energy_price"
      return {
        "price_entity": config.get(price_entity_key) or config.get("entity_energy_price"),
        "number_energy_price": (
          config.get(number_price_key)
          if config.get(number_price_key) is not None
          else config.get("number_energy_price")
        ),
      }

    # Build list of statistic IDs grouped by category with sign.
    entries: list[dict[str, Any]] = []
    for source in sources:
      src_type = source.get("type")
      if src_type == "solar":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          entries.append({"stat_id": stat_id, "category": "solar", "sign": 1})

      elif src_type == "grid":
        if "stat_energy_from" in source:
          stat_id = source.get("stat_energy_from")
          if stat_id:
            entries.append({
              "stat_id": stat_id, "category": "grid", "sign": 1,
              "stat_cost": _cost_stat(source.get("stat_cost"), stat_id),
              **_price_fields(source),
            })
          stat_id_to = source.get("stat_energy_to")
          if stat_id_to:
            entries.append({
              "stat_id": stat_id_to, "category": "grid", "sign": -1,
              "stat_cost": _cost_stat(source.get("stat_compensation"), stat_id_to),
              **_price_fields(source, export=True),
            })
        for flow in source.get("flow_from") or []:
          stat_id = flow.get("stat_energy_from")
          if stat_id:
            entries.append({
              "stat_id": stat_id, "category": "grid", "sign": 1,
              "stat_cost": _cost_stat(flow.get("stat_cost"), stat_id),
              **_price_fields(flow),
            })
        for flow in source.get("flow_to") or []:
          stat_id = flow.get("stat_energy_to")
          if stat_id:
            entries.append({
              "stat_id": stat_id, "category": "grid", "sign": -1,
              "stat_cost": _cost_stat(flow.get("stat_compensation"), stat_id),
              **_price_fields(flow),
            })

      elif src_type == "battery":
        for key, sign in [("stat_energy_from", 1), ("stat_energy_to", -1)]:
          stat_id = source.get(key)
          if stat_id:
            entries.append({"stat_id": stat_id, "category": "battery", "sign": sign})

      elif src_type == "gas":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          entries.append({
            "stat_id": stat_id, "category": "gas", "sign": 1,
            "stat_cost": _cost_stat(source.get("stat_cost"), stat_id),
            **_price_fields(source),
          })

      elif src_type == "water":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          entries.append({
            "stat_id": stat_id, "category": "water", "sign": 1,
            "stat_cost": _cost_stat(source.get("stat_cost"), stat_id),
            **_price_fields(source),
          })

    for dev in devices:
      stat_id = dev.get("stat_consumption")
      if stat_id:
        entries.append({
          "stat_id": stat_id, "category": "device", "sign": 1,
          "device_name": dev.get("name"),
        })

    for dev in devices_water:
      stat_id = dev.get("stat_consumption")
      if stat_id:
        entries.append({
          "stat_id": stat_id, "category": "device_water", "sign": 1,
          "device_name": dev.get("name"),
        })

    if not entries:
      _LOGGER.warning("Tab5 energy request: no statistic IDs found in energy config")
      return

    all_stat_ids = {e["stat_id"] for e in entries}
    # Also fetch cost statistic IDs
    cost_stat_ids: set[str] = set()
    for e in entries:
      if e.get("stat_cost"):
        cost_stat_ids.add(e["stat_cost"])
        all_stat_ids.add(e["stat_cost"])

    # Determine time range
    now = dt_util.now()
    if period == "day":
      start = now.replace(hour=0, minute=0, second=0, microsecond=0)
      stat_period = "hour"
    elif period == "week":
      start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
      stat_period = "day"
    else:  # month
      start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
      stat_period = "day"
    start_utc = dt_util.as_utc(start)
    end_utc = dt_util.as_utc(now)

    def _fetch_energy_statistics() -> dict[str, list[dict[str, Any]]]:
      return statistics_during_period(
        self.hass,
        start_utc,
        end_utc,
        all_stat_ids,
        stat_period,
        None,
        {"change", "sum"},
      )

    try:
      stats = await get_instance(self.hass).async_add_executor_job(
        _fetch_energy_statistics
      )
    except Exception:
      _LOGGER.exception("Tab5 energy: failed to fetch statistics")
      stats = {}

    currency = self.hass.config.currency or "EUR"

    def _as_float(value: Any) -> float | None:
      if value is None:
        return None
      if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if not value:
          return None
      try:
        return float(value)
      except (TypeError, ValueError):
        return None

    def _apply_stat_sign(value: float, sign: int) -> float:
      if sign < 0 and value > 0:
        return -value
      return value

    def _changes_from_statistics(
      rows: list[dict[str, Any]], precision: int
    ) -> tuple[list[float | None], float]:
      changes: list[float | None] = []
      total = 0.0
      previous_sum: float | None = None

      for row in rows:
        current_sum = _as_float(row.get("sum"))
        change = _as_float(row.get("change"))

        if change is None and current_sum is not None and previous_sum is not None:
          change = current_sum - previous_sum

        if current_sum is not None:
          previous_sum = current_sum

        if change is None:
          changes.append(None)
          continue

        if abs(change) < 1e-9:
          change = 0.0
        changes.append(round(change, precision))
        total += change

      return changes, total

    def _price_for_entry(entry: dict[str, Any]) -> float | None:
      fixed_price = _as_float(entry.get("number_energy_price"))
      if fixed_price is not None:
        return fixed_price

      price_entity = entry.get("price_entity")
      if not isinstance(price_entity, str) or not price_entity.strip():
        return None
      state = self.hass.states.get(price_entity.strip())
      if not state:
        return None
      return _as_float(state.state)

    def _cost_from_energy_changes(
      changes: list[float | None], price: float
    ) -> tuple[list[float | None], float]:
      cost_changes: list[float | None] = []
      cost_total = 0.0

      for change in changes:
        if change is None:
          cost_changes.append(None)
          continue
        cost = abs(change) * price
        if abs(cost) < 1e-9:
          cost = 0.0
        cost_changes.append(round(cost, 4))
        cost_total += cost

      return cost_changes, cost_total

    def _has_numeric_change(changes: list[float | None]) -> bool:
      return any(change is not None for change in changes)

    # Build response per entry
    result_entries: list[dict[str, Any]] = []
    for entry in entries:
      stat_id = entry["stat_id"]
      stat_data = stats.get(stat_id, [])
      changes, total = _changes_from_statistics(stat_data, 3)

      state = self.hass.states.get(stat_id)
      name = entry.get("device_name") or (
        state.attributes.get("friendly_name") if state else None
      )
      unit = state.attributes.get("unit_of_measurement") if state else None

      signed_total = round(_apply_stat_sign(total, entry["sign"]), 3)

      result_entry: dict[str, Any] = {
        "id": stat_id,
        "category": entry["category"],
        "sign": entry["sign"],
        "values": changes,
        "total": signed_total,
      }
      if name:
        result_entry["name"] = name
      if unit:
        result_entry["unit"] = unit
      result_entries.append(result_entry)

      # Create separate cost entry if cost data exists
      cost_stat = entry.get("stat_cost")
      cost_changes: list[float | None] | None = None
      cost_total = 0.0

      if cost_stat and (cost_rows := stats.get(cost_stat)):
        cost_changes, cost_total = _changes_from_statistics(cost_rows, 4)
        if not _has_numeric_change(cost_changes):
          cost_changes = None
          cost_total = 0.0

      if cost_changes is None and (price := _price_for_entry(entry)) is not None:
        cost_changes, cost_total = _cost_from_energy_changes(changes, price)

      if cost_changes is not None:
        cost_entry: dict[str, Any] = {
          "id": f"{stat_id}_cost",
          "category": entry["category"],
          "sign": entry["sign"],
          "values": cost_changes,
          "total": round(_apply_stat_sign(cost_total, entry["sign"]), 2),
          "unit": currency,
          "is_cost": True,
        }
        if name:
          cost_entry["name"] = f"{name} ({currency})"
        result_entries.append(cost_entry)

    # Build total entries per category with multiple members
    # Separate kWh entries and cost entries for independent totals
    category_names = {
      "solar": "PV gesamt",
      "grid": "Netz gesamt",
      "battery": "Batterie gesamt",
      "gas": "Gas gesamt",
      "water": "Wasser gesamt",
      "device": "Geräte gesamt",
      "device_water": "Wassergeräte gesamt",
    }
    kwh_entries = [e for e in result_entries if not e.get("is_cost")]
    cost_entries = [e for e in result_entries if e.get("is_cost")]

    for group, is_cost in [(kwh_entries, False), (cost_entries, True)]:
      cat_groups: dict[str, list[dict[str, Any]]] = {}
      for e in group:
        cat_groups.setdefault(e["category"], []).append(e)

      for cat, members in cat_groups.items():
        if len(members) < 2:
          continue
        max_len = max(len(m["values"]) for m in members)
        sum_values: list[float | None] = []
        for i in range(max_len):
          slot_sum: float | None = None
          for m in members:
            v = m["values"][i] if i < len(m["values"]) else None
            if v is not None:
              s = _apply_stat_sign(v, m["sign"])
              slot_sum = (slot_sum or 0.0) + s
          if slot_sum is not None:
            sum_values.append(round(slot_sum, 4 if is_cost else 3))
          else:
            sum_values.append(None)

        total_sum = round(sum(m["total"] for m in members), 2 if is_cost else 3)
        base_name = category_names.get(cat, f"{cat} gesamt")

        total_entry: dict[str, Any] = {
          "id": f"{cat}_total" + ("_cost" if is_cost else ""),
          "category": cat,
          "sign": 1,
          "name": f"{base_name} ({currency})" if is_cost else base_name,
          "values": sum_values,
          "total": total_sum,
          "is_total": True,
        }
        if is_cost:
          total_entry["unit"] = currency
          total_entry["is_cost"] = True
        elif members[0].get("unit"):
          total_entry["unit"] = members[0]["unit"]

        result_entries.append(total_entry)

    # Build "Gesamtverbrauch" and "Nicht erfasster Verbrauch"
    electricity_cats = {"solar", "grid", "battery"}
    elec_kwh = [e for e in result_entries
                if e["category"] in electricity_cats
                and not e.get("is_total") and not e.get("is_cost")]
    if elec_kwh:
      max_len = max(len(e["values"]) for e in elec_kwh)
      consumption_values: list[float | None] = []
      for i in range(max_len):
        slot_sum: float | None = None
        for e in elec_kwh:
          v = e["values"][i] if i < len(e["values"]) else None
          if v is not None:
            slot_sum = (slot_sum or 0.0) + _apply_stat_sign(v, e["sign"])
        if slot_sum is not None:
          consumption_values.append(round(slot_sum, 3))
        else:
          consumption_values.append(None)
      consumption_total = round(sum(e["total"] for e in elec_kwh), 3)

      result_entries.append({
        "id": "consumption_total",
        "category": "consumption",
        "sign": 1,
        "name": "Gesamtverbrauch",
        "unit": "kWh",
        "values": consumption_values,
        "total": consumption_total,
        "is_total": True,
      })

      # "Nicht erfasster Verbrauch" = Gesamtverbrauch - Geräte
      device_kwh = [e for e in result_entries
                    if e["category"] == "device"
                    and not e.get("is_total") and not e.get("is_cost")]
      if device_kwh:
        device_sum = sum(e["total"] for e in device_kwh)
        dev_max = max(len(e["values"]) for e in device_kwh)
        untracked_values: list[float | None] = []
        for i in range(max_len):
          cv = consumption_values[i] if i < len(consumption_values) else None
          dv_sum = 0.0
          has_dev = False
          for e in device_kwh:
            v = e["values"][i] if i < len(e["values"]) else None
            if v is not None:
              dv_sum += v
              has_dev = True
          if cv is not None:
            untracked_values.append(round(cv - (dv_sum if has_dev else 0.0), 3))
          else:
            untracked_values.append(None)

        result_entries.append({
          "id": "consumption_untracked",
          "category": "consumption",
          "sign": 1,
          "name": "Nicht erfasster Verbrauch",
          "unit": "kWh",
          "values": untracked_values,
          "total": round(consumption_total - device_sum, 3),
          "is_total": True,
        })

    response: dict[str, Any] = {
      "period": period,
      "stat_period": stat_period,
      "start": start.isoformat(),
      "entries": result_entries,
    }

    await mqtt.async_publish(
      self.hass,
      self.energy_response_topic,
      json.dumps(response, separators=(",", ":")),
      qos=0,
      retain=False,
    )
    cost_entries_count = sum(1 for e in result_entries if e.get("is_cost"))
    cost_entries_with_values = sum(
      1 for e in result_entries
      if e.get("is_cost") and any(v is not None for v in e.get("values", []))
    )
    _LOGGER.debug(
      "Tab5 energy response: period=%s entries=%d cost_entries=%d cost_entries_with_values=%d",
      period,
      len(result_entries),
      cost_entries_count,
      cost_entries_with_values,
    )

  async def _async_handle_weather_request(self, msg: ReceiveMessage) -> None:
    """Handle explicit weather refresh requests from the Tab5 popup."""
    parsed = _try_parse_json(msg.payload)
    entity_id = ""
    if isinstance(parsed, dict):
      entity_id = str(parsed.get("entity_id") or "").strip()

    if entity_id:
      if not _is_weather_entity(entity_id):
        _LOGGER.debug("Tab5 weather request ignored for non-weather entity %s", entity_id)
        return
      if entity_id not in self.weathers:
        _LOGGER.debug("Tab5 weather request ignored for unconfigured entity %s", entity_id)
        return
      state = self.hass.states.get(entity_id)
      if not state:
        _LOGGER.debug("Tab5 weather request ignored for missing entity %s", entity_id)
        return
      await self._async_publish_weather_state(entity_id, state, retain=True)
      return

    for weather_entity in self.weathers:
      state = self.hass.states.get(weather_entity)
      if not state:
        continue
      await self._async_publish_weather_state(weather_entity, state, retain=True)

  async def _async_handle_scene_command(self, msg: ReceiveMessage) -> None:
    """Run a configured scene/script or press a configured button."""
    if getattr(msg, "retain", False):
      return
    payload = msg.payload.strip()
    if not payload:
      return
    entity_id = resolve_action_entity(payload, self.scene_map)
    call = build_action_service_call(
      payload, self.scene_map, self.hass.states.get(entity_id) if entity_id else None,
    )
    if not call:
      _LOGGER.debug("Ignoring unsupported or unavailable HomeTiles action: %s", payload)
      return
    domain, service, service_data = call
    await self.hass.services.async_call(
      domain, service, service_data, blocking=False,
    )

  async def _async_handle_light_command(self, msg: ReceiveMessage) -> None:
    """Execute light commands originating from the Tab5."""
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id = None
    command = None
    service_data: Dict[str, Any] = {}

    parsed = _try_parse_json(payload)
    if isinstance(parsed, dict):
      entity_id = parsed.get("entity_id") or parsed.get("entity")
      if entity_id is not None:
        entity_id = str(entity_id).strip()
      command = _normalise_command(parsed.get("state") or parsed.get("command"))
      service_data = _extract_light_service_data(parsed)
    elif isinstance(parsed, str):
      payload = parsed.strip()

    if command is None:
      parsed_entity, parsed_command = _parse_simple_command(payload)
      if entity_id is None:
        entity_id = parsed_entity
      if command is None:
        command = parsed_command

    entity_id = self._resolve_target_entity(entity_id, self.lights)
    if not entity_id:
      _LOGGER.warning("Unhandled light command from Tab5 (unknown entity): %s", msg.payload)
      return

    if command is None:
      if service_data:
        command = "on"
      else:
        _LOGGER.warning("Unhandled light command from Tab5 (missing state): %s", msg.payload)
        return

    command = _normalise_command(command)
    if not command:
      _LOGGER.warning("Unhandled light command from Tab5: %s", msg.payload)
      return

    if command == "toggle":
      service = "toggle"
      service_payload = {"entity_id": entity_id}
    elif command == "off":
      service = "turn_off"
      service_payload = {"entity_id": entity_id}
      if "transition" in service_data:
        service_payload["transition"] = service_data["transition"]
    else:
      service = "turn_on"
      service_payload = {"entity_id": entity_id}
      service_payload.update(service_data)

    await self.hass.services.async_call(
      "light",
      service,
      service_payload,
      blocking=False,
    )

  async def _async_handle_switch_command(self, msg: ReceiveMessage) -> None:
    """Execute on/off commands for configured switch-compatible entities."""
    if getattr(msg, "retain", False):
      return
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id = None
    command = None

    parsed = _try_parse_json(payload)
    if isinstance(parsed, dict):
      entity_id = parsed.get("entity_id") or parsed.get("entity")
      if entity_id is not None:
        entity_id = str(entity_id).strip()
      command = _normalise_command(parsed.get("state") or parsed.get("command"))
    elif isinstance(parsed, str):
      payload = parsed.strip()

    if command is None:
      parsed_entity, parsed_command = _parse_simple_command(payload)
      if entity_id is None:
        entity_id = parsed_entity
      if command is None:
        command = parsed_command

    entity_id = resolve_control_entity(entity_id, self.switches)
    if not entity_id:
      _LOGGER.warning("Unhandled switch command from Tab5 (unknown entity): %s", msg.payload)
      return

    command = _normalise_command(command)
    if not command:
      _LOGGER.warning("Unhandled switch command from Tab5: %s", msg.payload)
      return

    call = build_switch_service_call(
      entity_id, command, self.switches, self.hass.states.get(entity_id),
    )
    if not call:
      _LOGGER.debug("Ignoring unsupported or unavailable HomeTiles switch: %s", entity_id)
      return
    domain, service, service_data = call
    await self.hass.services.async_call(
      domain, service, service_data, blocking=False,
    )

  async def _async_handle_climate_command(self, msg: ReceiveMessage) -> None:
    """Execute climate commands originating from a HomeTiles popup."""
    parsed = _try_parse_json(msg.payload.strip())
    if not isinstance(parsed, dict):
      _LOGGER.warning("Unhandled climate command from HomeTiles: %s", msg.payload)
      return

    raw_entity = parsed.get("entity_id") or parsed.get("entity")
    entity_id = str(raw_entity).strip() if raw_entity is not None else None
    entity_id = self._resolve_target_entity(entity_id, self.climates)
    if not entity_id:
      _LOGGER.warning(
        "Unhandled climate command from HomeTiles (unknown entity): %s",
        msg.payload,
      )
      return

    state = self.hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    try:
      service, command_data = build_climate_service_call(parsed, attributes)
    except ValueError as err:
      _LOGGER.warning("Climate command rejected (%s): %s", err, msg.payload)
      return
    service_data: Dict[str, Any] = {"entity_id": entity_id, **command_data}

    await self.hass.services.async_call(
      "climate",
      service,
      service_data,
      blocking=False,
    )

  async def _async_handle_cover_command(self, msg: ReceiveMessage) -> None:
    """Execute cover and cover-tilt commands originating from HomeTiles."""
    parsed = _try_parse_json(msg.payload.strip())
    if not isinstance(parsed, dict):
      _LOGGER.warning("Unhandled cover command from HomeTiles: %s", msg.payload)
      return

    raw_entity = parsed.get("entity_id") or parsed.get("entity")
    requested_entity = str(raw_entity).strip() if raw_entity is not None else ""
    if requested_entity:
      entity_id = requested_entity if requested_entity in self.covers else None
    else:
      entity_id = self.covers[0] if len(self.covers) == 1 else None
    if not entity_id or not entity_id.startswith("cover."):
      _LOGGER.warning(
        "Unhandled cover command from HomeTiles (unknown entity): %s",
        msg.payload,
      )
      return

    command = normalise_cover_command(
      parsed.get("command")
      or parsed.get("service")
      or parsed.get("action")
      or parsed.get("state")
    )
    if command is None:
      if parsed.get("tilt_position") is not None:
        command = "set_cover_tilt_position"
      elif parsed.get("position") is not None:
        command = "set_cover_position"
    if command is None:
      _LOGGER.warning("Cover command is missing a supported action: %s", msg.payload)
      return

    state = self.hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    if "supported_features" in attributes and not cover_command_supported(
      command, attributes.get("supported_features")
    ):
      _LOGGER.warning(
        "Cover command %s is not supported by %s",
        command,
        entity_id,
      )
      return

    service_data: Dict[str, Any] = {"entity_id": entity_id}
    if command == "set_cover_position":
      position = parse_cover_position(parsed.get("position"))
      if position is None:
        _LOGGER.warning("Cover position must be an integer from 0 to 100: %s", msg.payload)
        return
      service_data["position"] = position
    elif command == "set_cover_tilt_position":
      tilt_position = parse_cover_position(parsed.get("tilt_position"))
      if tilt_position is None:
        _LOGGER.warning("Cover tilt position must be an integer from 0 to 100: %s", msg.payload)
        return
      service_data["tilt_position"] = tilt_position

    await self.hass.services.async_call(
      "cover",
      command,
      service_data,
      blocking=False,
    )

  def _is_local_camera_self_loop(self, entity_id: str) -> bool:
    """Return whether the panel asked to stream its own built-in camera."""
    registry_entry = er.async_get(self.hass).async_get(entity_id)
    return is_local_camera_self_loop(registry_entry, DOMAIN, self.entry.entry_id)

  async def _async_handle_camera_command(self, msg: ReceiveMessage) -> None:
    """Create or stop the short-lived JPEG-frame stream used by the popup."""
    parsed = _try_parse_json(msg.payload.strip())
    if not isinstance(parsed, dict):
      _LOGGER.warning("Unhandled camera command from HomeTiles: %s", msg.payload)
      return

    command = str(parsed.get("command") or "open").strip().lower()
    raw_entity = parsed.get("entity_id") or parsed.get("entity")
    requested_entity = str(raw_entity).strip() if raw_entity is not None else None
    entity_id = self._resolve_target_entity(requested_entity, self.cameras)
    status_topic = f"{self.base_topic}/stat/camera"
    manager = self.hass.data.get(DOMAIN, {}).get("camera_stream_manager")

    if command in ("close", "stop"):
      if manager and self.device_id:
        await manager.async_stop_device(self.device_id)
      await mqtt.async_publish(
        self.hass,
        status_topic,
        json.dumps({
          "status": "stopped",
          "entity_id": entity_id or requested_entity or "",
          "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
        }),
        qos=0,
        retain=False,
      )
      return

    if command == "open" and entity_id and self._is_local_camera_self_loop(entity_id):
      _LOGGER.warning(
        "HomeTiles camera stream refused for %s: it is this panel's own camera",
        entity_id,
      )
      await mqtt.async_publish(
        self.hass,
        status_topic,
        json.dumps({
          "status": "error",
          "entity_id": entity_id,
          "error": "camera_self_loop",
          "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
        }),
        qos=0,
        retain=False,
      )
      return

    if command != "open" or not entity_id or manager is None or not self.device_id:
      await mqtt.async_publish(
        self.hass,
        status_topic,
        json.dumps({
          "status": "error",
          "entity_id": requested_entity or "",
          "error": "unknown_camera",
          "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
        }),
        qos=0,
        retain=False,
      )
      return

    try:
      if str(parsed.get("transport") or "").strip() != CAMERA_STREAM_TRANSPORT:
        raise ValueError("camera_invalid_stream_request")
      session = await manager.async_create_session(
        self.device_id,
        entity_id,
        parsed.get("width", CAMERA_STREAM_WIDTH),
        parsed.get("height", CAMERA_STREAM_HEIGHT),
        parsed.get("fps", CAMERA_STREAM_FPS),
      )

      async def _async_notify_panel_end(stopped_entity: str = entity_id) -> None:
        # The camera's panel ended the live view: this popup shows "stopped".
        await mqtt.async_publish(
          self.hass,
          status_topic,
          json.dumps({
            "status": "stopped",
            "entity_id": stopped_entity,
            "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
          }),
          qos=0,
          retain=False,
        )

      session.on_panel_end = _async_notify_panel_end
      _LOGGER.info(
        "HomeTiles camera session ready (%s, mode=%s, %dx%d@%d)",
        entity_id,
        "image" if session.source is None else "stream",
        session.width,
        session.height,
        session.fps,
      )
      if self._device_ip:
        source_ip = await ha_network.async_get_source_ip(
          self.hass,
          target_ip=self._device_ip,
        )
      else:
        source_ip = await ha_network.async_get_source_ip(self.hass)
      stream_url = manager.stream_url(source_ip, session.token)
      _LOGGER.info(
        "HomeTiles camera LAN endpoint ready (host=%s, port=%d, panel=%s)",
        source_ip,
        manager.tcp_port,
        self._device_ip or "unknown",
      )
      response_payload = {
        "status": "ready",
        "entity_id": entity_id,
        "url": stream_url,
        "codec": "jpeg",
        "transport": CAMERA_STREAM_TRANSPORT,
        "framing": CAMERA_STREAM_FRAMING,
        "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
        "width": session.width,
        "height": session.height,
        "fps": session.fps,
      }
    except Exception as err:
      _LOGGER.warning("HomeTiles camera stream setup failed for %s: %s", entity_id, err)
      error_code = str(err)
      if error_code not in {
        "camera_has_no_stream_source",
        "camera_image_unavailable",
        "home_assistant_url_unavailable",
        "camera_tcp_server_unavailable",
        "camera_invalid_stream_request",
      }:
        error_code = "camera_setup_failed"
      response_payload = {
        "status": "error",
        "entity_id": entity_id,
        "error": error_code,
        "protocol_version": CAMERA_BRIDGE_PROTOCOL_VERSION,
      }

    await mqtt.async_publish(
      self.hass,
      status_topic,
      json.dumps(response_payload),
      qos=0,
      retain=False,
    )

  async def _async_handle_media_command(self, msg: ReceiveMessage) -> None:
    """Execute media player commands originating from the Tab5."""
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id = None
    command = None
    parsed_payload: Dict[str, Any] = {}

    parsed = _try_parse_json(payload)
    if isinstance(parsed, dict):
      parsed_payload = parsed
      entity_id = parsed.get("entity_id") or parsed.get("entity")
      if entity_id is not None:
        entity_id = str(entity_id).strip()
      command = _normalise_media_command(
        parsed.get("command") or parsed.get("state") or parsed.get("service")
      )
    elif isinstance(parsed, str):
      payload = parsed.strip()

    if command is None:
      parsed_entity, parsed_command = _parse_simple_command(payload)
      if entity_id is None:
        entity_id = parsed_entity
      if command is None:
        command = _normalise_media_command(parsed_command)

    if command is None and parsed_payload:
      if parsed_payload.get("volume_level") is not None or parsed_payload.get("volume") is not None:
        command = "volume_set"
      elif parsed_payload.get("is_volume_muted") is not None or parsed_payload.get("muted") is not None:
        command = "volume_mute"
      elif parsed_payload.get("source"):
        command = "select_source"
      elif parsed_payload.get("seek_position") is not None or parsed_payload.get("position") is not None:
        command = "media_seek"
      elif parsed_payload.get("media_content_id") and parsed_payload.get("media_content_type"):
        command = "play_media"

    entity_id = self._resolve_target_entity(entity_id, self.media_players)
    if not entity_id:
      _LOGGER.warning("Unhandled media command from Tab5 (unknown entity): %s", msg.payload)
      return

    command = _normalise_media_command(command)
    if not command:
      _LOGGER.warning("Unhandled media command from Tab5: %s", msg.payload)
      return

    service_payload: Dict[str, Any] = {"entity_id": entity_id}

    if command == "volume_set":
      raw_volume = parsed_payload.get("volume_level", parsed_payload.get("volume"))
      volume = _coerce_float(raw_volume)
      if volume is None:
        _LOGGER.warning("Unhandled media volume command from Tab5 (missing volume): %s", msg.payload)
        return
      if volume > 1.0:
        volume = volume / 100.0
      service_payload["volume_level"] = min(1.0, max(0.0, volume))
    elif command == "volume_mute":
      muted = _coerce_bool(parsed_payload.get("is_volume_muted", parsed_payload.get("muted")))
      if muted is None:
        muted = True
      service_payload["is_volume_muted"] = muted
    elif command == "select_source":
      source = str(parsed_payload.get("source") or "").strip()
      if not source:
        _LOGGER.warning("Unhandled media source command from Tab5 (missing source): %s", msg.payload)
        return
      service_payload["source"] = source
    elif command == "media_seek":
      position = _coerce_float(parsed_payload.get("seek_position", parsed_payload.get("position")))
      if position is None:
        _LOGGER.warning("Unhandled media seek command from Tab5 (missing position): %s", msg.payload)
        return
      service_payload["seek_position"] = position
    elif command == "play_media":
      content_id = str(parsed_payload.get("media_content_id") or "").strip()
      content_type = str(parsed_payload.get("media_content_type") or "").strip()
      if not content_id or not content_type:
        _LOGGER.warning("Unhandled play_media command from Tab5 (missing content): %s", msg.payload)
        return
      service_payload["media_content_id"] = content_id
      service_payload["media_content_type"] = content_type

    await self.hass.services.async_call(
      "media_player",
      command,
      service_payload,
      blocking=False,
    )

  def _resolve_target_entity(self, entity_id: Optional[str], candidates: List[str]) -> Optional[str]:
    """Accept only a configured entity; legacy commands may omit a single target."""
    if entity_id:
      entity_id = entity_id.strip()
    return resolve_control_entity(entity_id, candidates)

  @callback
  def _handle_state_event(self, event) -> None:
    entity_id = event.data.get("entity_id")
    new_state = event.data.get("new_state")
    if not entity_id:
      return
    if entity_id.startswith("weather.") and entity_id in self.weathers:
      self._sync_weather_subscriptions()
    if new_state is None:
      if entity_id in getattr(self, "numbers", []) + getattr(self, "selects", []) + getattr(self, "datetimes", []):
        self.hass.async_create_task(self._async_publish_editable_state(entity_id, None))
      if entity_id in self.binary_sensors:
        self.hass.async_create_task(
          self._async_publish_binary_sensor_absent_state(entity_id)
        )
      elif entity_id in self.switches:
        self.hass.async_create_task(self._async_publish_switch_absent_state(entity_id))
      return

    self.hass.async_create_task(self._async_publish_entity_state(entity_id, new_state))

    # Live icon updates without full grid reload.
    if entity_id in self.tracked_entities:
      if _is_weather_entity(entity_id):
        icon = _weather_icon_from_state(new_state, self.hass) or ""
      elif entity_id.startswith("media_player."):
        icon = _extract_media_player_mdi_icon(new_state, self.hass) or ""
      else:
        icon = _extract_mdi_icon(new_state, self.hass) or ""
      if self._icon_cache.get(entity_id, "") != icon:
        self._icon_cache[entity_id] = icon
        self._schedule_icon_refresh()

  def _schedule_config_refresh(self, delays: Optional[Tuple[float, ...]] = None) -> None:
    if not self.config_topic:
      return
    if self._config_refresh_pending > 0:
      return
    if delays is None:
      delays = (6.0, 30.0, 120.0)

    self._config_refresh_pending = len(delays)
    if self._config_refresh_pending == 0:
      return

    def _refresh(_now) -> None:
      asyncio.run_coroutine_threadsafe(
        self.async_publish_config_to_device(),
        self.hass.loop,
      )
      self._config_refresh_pending -= 1
      if self._config_refresh_pending <= 0:
        self._config_refresh_handles = []
        self._config_refresh_pending = 0

    for delay in delays:
      self._config_refresh_handles.append(async_call_later(self.hass, delay, _refresh))

  def _schedule_icon_refresh(self, delay: float = 2.0) -> None:
    if not self.icons_topic:
      return
    if self._icon_refresh_handle:
      return

    def _refresh(_now) -> None:
      self._icon_refresh_handle = None
      asyncio.run_coroutine_threadsafe(
        self._async_publish_icon_update(),
        self.hass.loop,
      )

    self._icon_refresh_handle = async_call_later(self.hass, delay, _refresh)

  async def _async_publish_icon_update(self) -> None:
    """Publish only the icon map; lightweight, no full config push."""
    if not self.icons_topic:
      return
    # Entities and registry overrides may become available after startup without
    # a state event. Never follow a fresh config with an older empty icon map.
    self._prime_icon_cache()
    payload = json.dumps(self._icon_cache)
    await mqtt.async_publish(self.hass, self.icons_topic, payload, qos=0, retain=False)

  async def _get_weather_forecast(
    self,
    entity_id: str,
    forecast_type: str,
  ) -> Optional[List[Dict[str, Any]]]:
    state = self.hass.states.get(entity_id)
    features = (state.attributes or {}).get("supported_features") if state else None
    required_feature = FORECAST_FEATURES.get(forecast_type)
    if isinstance(features, int) and required_feature and not features & required_feature:
      return None

    now = dt_util.utcnow()
    cache_key = (entity_id, forecast_type)
    cached = self._forecast_cache.get(cache_key)
    if cached and (now - cached[0]) < FORECAST_CACHE_TTL:
      return cached[1]

    forecast = await self._fetch_weather_forecast(entity_id, forecast_type)
    updated = self._forecast_cache.get(cache_key)
    if updated is not cached:
      # A subscription update received during the service call is newer.
      return updated[1] if updated else None
    if forecast:
      self._forecast_cache[cache_key] = (now, forecast)
    return forecast

  async def _fetch_weather_forecast(
    self,
    entity_id: str,
    forecast_type: str,
  ) -> Optional[List[Dict[str, Any]]]:
    forecast: Optional[List[Dict[str, Any]]] = None

    if async_get_forecasts:
      try:
        result = await async_get_forecasts(self.hass, entity_id, forecast_type)
      except TypeError:
        try:
          result = await async_get_forecasts(self.hass, entity_id)
        except Exception as err:  # pragma: no cover - optional helper
          _LOGGER.debug("Tab5 LVGL: async_get_forecasts failed for %s (%s)", entity_id, err)
          result = None
      except Exception as err:  # pragma: no cover - optional helper
        _LOGGER.debug("Tab5 LVGL: async_get_forecasts failed for %s (%s)", entity_id, err)
        result = None

      if result is not None:
        forecast = _extract_forecast_from_result(result, entity_id)

    if forecast is None:
      try:
        response = await self.hass.services.async_call(
          "weather",
          "get_forecasts",
          {"entity_id": entity_id, "type": forecast_type},
          blocking=True,
          return_response=True,
        )
      except TypeError:
        try:
          await self.hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": forecast_type},
            blocking=True,
          )
        except Exception as err:
          _LOGGER.debug("Tab5 LVGL: weather.get_forecasts failed for %s (%s)", entity_id, err)
        response = None
      except Exception as err:
        _LOGGER.debug("Tab5 LVGL: weather.get_forecasts failed for %s (%s)", entity_id, err)
        response = None

      if response is not None:
        forecast = _extract_forecast_from_result(response, entity_id)

    forecast = _sanitize_forecast_list(forecast)
    if not forecast:
      return None
    _apply_forecast_icons(forecast)
    if forecast_type == FORECAST_DAILY_TYPE and len(forecast) > FORECAST_DAILY_LIMIT:
      forecast = forecast[:FORECAST_DAILY_LIMIT]
    return forecast

  def _prime_icon_cache(self) -> None:
    if not self.tracked_entities:
      return
    for entity_id in self.tracked_entities:
      state = self.hass.states.get(entity_id)
      if not state:
        self._icon_cache[entity_id] = ""
        continue
      if _is_weather_entity(entity_id):
        icon = _weather_icon_from_state(state, self.hass) or ""
      elif entity_id.startswith("media_player."):
        icon = _extract_media_player_mdi_icon(state, self.hass) or ""
      else:
        icon = _extract_mdi_icon(state, self.hass) or ""
      self._icon_cache[entity_id] = icon

  def _build_state_payload(self, entity_id: str, state: State) -> str:
    if entity_id.startswith("binary_sensor."):
      return json.dumps(
        build_binary_sensor_state_payload(
          state.state,
          state.attributes or {},
          state.last_changed,
          icon=_extract_mdi_icon(state, self.hass),
        )
      )
    if entity_id.startswith("light."):
      payload: Dict[str, Any] = {"state": state.state}
      attrs = state.attributes or {}
      supported_modes = attrs.get("supported_color_modes")
      if supported_modes:
        payload["supported_color_modes"] = list(supported_modes)
      color_mode = attrs.get("color_mode")
      if color_mode:
        payload["color_mode"] = color_mode
      brightness_pct = attrs.get("brightness_pct")
      if brightness_pct is None:
        brightness = attrs.get("brightness")
        if isinstance(brightness, (int, float)):
          brightness_pct = round(brightness / 255 * 100)
      if brightness_pct is not None:
        payload["brightness_pct"] = brightness_pct

      # Preserve Home Assistant's native CCT state and range. A CCT-only
      # light can also expose converted rgb_color/hs_color attributes; those
      # values describe its current appearance, not additional capabilities.
      color_temp_kelvin = attrs.get("color_temp_kelvin")
      if not isinstance(color_temp_kelvin, (int, float)):
        color_temp_mired = attrs.get("color_temp")
        if isinstance(color_temp_mired, (int, float)) and color_temp_mired > 0:
          color_temp_kelvin = 1_000_000 / color_temp_mired
      if isinstance(color_temp_kelvin, (int, float)) and color_temp_kelvin > 0:
        payload["color_temp_kelvin"] = round(color_temp_kelvin)

      min_color_temp_kelvin = attrs.get("min_color_temp_kelvin")
      if not isinstance(min_color_temp_kelvin, (int, float)):
        max_mireds = attrs.get("max_mireds")
        if isinstance(max_mireds, (int, float)) and max_mireds > 0:
          min_color_temp_kelvin = 1_000_000 / max_mireds
      if isinstance(min_color_temp_kelvin, (int, float)) and min_color_temp_kelvin > 0:
        payload["min_color_temp_kelvin"] = round(min_color_temp_kelvin)

      max_color_temp_kelvin = attrs.get("max_color_temp_kelvin")
      if not isinstance(max_color_temp_kelvin, (int, float)):
        min_mireds = attrs.get("min_mireds")
        if isinstance(min_mireds, (int, float)) and min_mireds > 0:
          max_color_temp_kelvin = 1_000_000 / min_mireds
      if isinstance(max_color_temp_kelvin, (int, float)) and max_color_temp_kelvin > 0:
        payload["max_color_temp_kelvin"] = round(max_color_temp_kelvin)

      rgb = attrs.get("rgb_color")
      if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        payload["rgb_color"] = [r, g, b]
        payload["color"] = f"#{r:02X}{g:02X}{b:02X}"
      hs = attrs.get("hs_color")
      if isinstance(hs, (list, tuple)) and len(hs) >= 2:
        payload["hs_color"] = [float(hs[0]), float(hs[1])]
      return json.dumps(payload)
    if entity_id.startswith("cover."):
      return json.dumps(build_cover_state_payload(state.state, state.attributes or {}))
    if entity_id.startswith("climate."):
      attrs = state.attributes or {}
      unit = getattr(self.hass.config.units, "temperature_unit", None)
      return json.dumps(
        build_climate_state_payload(state.state, attrs, unit),
        default=str,
      )
    if entity_id.startswith("media_player."):
      return json.dumps(_extract_media_player_payload(state, self.hass), default=str)
    if entity_id.startswith("sensor."):
      return sensor_live_state_payload(state)
    # Preserve the established wire format for existing switch entities.
    if entity_domain(entity_id) == "switch" and state.state in ("on", "off", "unavailable"):
      return state.state
    if entity_domain(entity_id) in SWITCH_DOMAINS:
      return json.dumps(build_switch_state_payload(entity_id, state.state, state.attributes or {}))
    return state.state.replace(",", ".")

  @callback
  def _weather_started(self, _hass) -> None:
    self._sync_weather_subscriptions()

  @callback
  def _sync_weather_subscriptions(self) -> None:
    """Use the same forecast subscriptions as Home Assistant's weather UI."""
    if not self._runtime_setup_complete or not self.hass.is_running:
      return
    component = self.hass.data.get(WEATHER_DATA_COMPONENT)
    desired = {}
    for entity_id in self.weathers:
      entity = component.get_entity(entity_id) if component else None
      if entity is None or self.hass.states.get(entity_id) is None:
        continue
      features = entity.supported_features or 0
      for forecast_type, feature in FORECAST_FEATURES.items():
        if features & feature:
          desired[(entity_id, forecast_type)] = entity

    for key, (entity, unsubscribe, _token) in tuple(self._weather_subscriptions.items()):
      if desired.get(key) is not entity:
        self._weather_subscriptions.pop(key)
        unsubscribe()
        self._forecast_cache.pop(key, None)
    for key, entity in desired.items():
      if key not in self._weather_subscriptions:
        self._subscribe_weather_forecast(key, entity)
    for entity_id in tuple(self._weather_last_payload):
      if entity_id not in self.weathers:
        self._weather_last_payload.pop(entity_id, None)
        self._weather_revision.pop(entity_id, None)

  @callback
  def _subscribe_weather_forecast(self, key, entity) -> None:
    entity_id, forecast_type = key
    token = object()

    @callback
    def _forecast_updated(forecast) -> None:
      subscription = self._weather_subscriptions.get(key)
      if not self._runtime_setup_complete or subscription is None or subscription[2] is not token:
        return
      cleaned = _sanitize_forecast_list(forecast) or []
      _apply_forecast_icons(cleaned)
      self._forecast_cache[key] = (dt_util.utcnow(), cleaned)
      self._weather_revision[entity_id] = self._weather_revision.get(entity_id, 0) + 1
      self._queue_weather_update(entity_id)

    unsubscribe = entity.async_subscribe_forecast(forecast_type, _forecast_updated)
    self._weather_subscriptions[key] = (entity, unsubscribe, token)
    self._forecast_cache[key] = (dt_util.utcnow(), [])
    # Subscribe before requesting the initial snapshot so later updates cannot be lost.
    self._weather_initial_updates.setdefault(entity_id, set()).add(forecast_type)
    self._queue_weather_update(entity_id)

  @callback
  def _queue_weather_update(self, entity_id: str) -> None:
    if not self._runtime_setup_complete or entity_id not in self.weathers:
      return
    self._weather_pending.add(entity_id)
    if self._weather_update_task is None:
      # Coalesce same-loop day/night/hourly callbacks without a timed delay.
      self._weather_update_task = self.hass.async_create_task(
        self._async_process_weather_updates(), eager_start=False
      )

  async def _async_process_weather_updates(self) -> None:
    try:
      while self._runtime_setup_complete and self._weather_pending:
        entity_id = self._weather_pending.pop()
        initial_types = self._weather_initial_updates.pop(entity_id, set())
        try:
          for forecast_type in initial_types:
            subscription = self._weather_subscriptions.get((entity_id, forecast_type))
            if subscription is not None:
              entity = subscription[0]
              try:
                await entity.async_update_listeners({forecast_type})
              except Exception:
                _LOGGER.debug("Tab5 initial forecast unavailable for %s (%s)",
                              entity_id, forecast_type, exc_info=True)
          if entity_id not in self.weathers or not self._owns_state_publish(entity_id):
            continue
          state = self.hass.states.get(entity_id)
          if state is not None and self._runtime_setup_complete:
            await self._async_publish_weather_state(entity_id, state, only_if_changed=True)
        except Exception:
          _LOGGER.debug("Tab5 weather update failed for %s", entity_id, exc_info=True)
    finally:
      self._weather_update_task = None

  async def _async_stop_weather_updates(self, _event=None) -> None:
    """Remove forecast/start/stop listeners and cancel an in-flight publication."""
    self._runtime_setup_complete = False
    for name in ("_unsub_weather_stop", "_unsub_weather_started"):
      unsubscribe = getattr(self, name)
      if unsubscribe is not None:
        unsubscribe()
        setattr(self, name, None)
    subscriptions = list(self._weather_subscriptions.values())
    self._weather_subscriptions.clear()
    for _entity, unsubscribe, _token in subscriptions:
      unsubscribe()
    task = self._weather_update_task
    if task is not None:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass
    self._weather_update_task = None
    self._weather_pending.clear()
    self._weather_initial_updates.clear()
    self._weather_last_payload.clear()
    self._weather_revision.clear()
    self._forecast_cache.clear()

  async def _async_publish_weather_state(
    self, entity_id: str, state: State, retain: bool = True, *, only_if_changed: bool = False
  ) -> None:
    revision = self._weather_revision.get(entity_id, 0)
    payload = await self._build_weather_payload(entity_id, state)
    if not self._runtime_setup_complete or revision != self._weather_revision.get(entity_id, 0):
      return
    if only_if_changed and self._weather_last_payload.get(entity_id) == payload:
      return
    topic = self._ha_topic_for_entity(entity_id, "weather")
    await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=retain)
    if entity_id in self.weathers:
      self._weather_last_payload[entity_id] = payload

  async def _build_weather_payload(self, entity_id: str, state: State) -> str:
    payload = _extract_weather_payload(state, self.hass)
    daily_forecast = payload.get("forecast")
    if not isinstance(daily_forecast, list) or not daily_forecast:
      daily_forecast = await self._get_weather_forecast(entity_id, FORECAST_DAILY_TYPE)

    twice_daily_forecast = None
    if not daily_forecast:
      twice_daily_forecast = await self._get_weather_forecast(entity_id, FORECAST_TWICE_DAILY_TYPE)
      if twice_daily_forecast:
        daily_forecast = _build_daily_forecast_from_periods(twice_daily_forecast, twice_daily=True)

    hourly_forecast = await self._get_weather_forecast(entity_id, FORECAST_HOURLY_TYPE)

    prepared_daily: List[Dict[str, Any]] = []
    if isinstance(daily_forecast, list) and daily_forecast:
      prepared_daily = [dict(entry) for entry in daily_forecast if isinstance(entry, dict)]
      _apply_forecast_icons(prepared_daily)
      for entry in prepared_daily:
        local_day = _forecast_entry_local_date(entry)
        if local_day is not None:
          entry["date_local"] = local_day.isoformat()
      if hourly_forecast and not twice_daily_forecast:
        prepared_daily = _merge_hourly_precip_into_daily(prepared_daily, hourly_forecast)

    if hourly_forecast:
      built_daily = _build_daily_forecast_from_periods(hourly_forecast)
      if prepared_daily:
        by_day: Dict[str, Dict[str, Any]] = {}
        for entry in built_daily:
          local_day = entry.get("date_local")
          if isinstance(local_day, str) and local_day:
            by_day[local_day] = entry
        for entry in prepared_daily:
          local_day = entry.get("date_local")
          if isinstance(local_day, str) and local_day:
            by_day[local_day] = entry
        prepared_daily = [by_day[key] for key in sorted(by_day.keys())]
      else:
        prepared_daily = built_daily

    if prepared_daily:
      payload["forecast"] = _compact_daily_forecast(prepared_daily)

    if hourly_forecast:
      prepared_hourly = _compact_hourly_forecast(hourly_forecast)
      if prepared_hourly:
        payload["forecast_hourly"] = prepared_hourly

    return json.dumps(payload)

  def _ha_topic_for_entity(self, entity_id: str, suffix: str) -> str:
    path = entity_id.replace(".", "/")
    return f"{self.ha_prefix}/{path}/{suffix}"

  def _build_sensor_meta(self) -> List[Dict[str, Any]]:
    meta: List[Dict[str, Any]] = []
    for entity_id in self.sensors:
      entry: Dict[str, Any] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        unit = state.attributes.get("unit_of_measurement")
        name = state.name
        value = state.state
        if entity_id.startswith("media_player."):
          icon = _extract_media_player_mdi_icon(state, self.hass)
        else:
          icon = _extract_mdi_icon(state, self.hass)
        if isinstance(unit, str) and unit.strip():
          entry["unit"] = unit.strip()
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        if isinstance(value, str) and value.strip():
          entry["value"] = value.strip()
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
        entry["state_kind"] = sensor_state_kind(state)
      meta.append(entry)
    return meta

  def _build_weather_meta(self) -> List[Dict[str, Any]]:
    meta: List[Dict[str, Any]] = []
    for entity_id in self.weathers:
      entry: Dict[str, Any] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        name = state.name
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        icon = _weather_icon_from_state(state, self.hass)
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
      meta.append(entry)
    return meta

  def _build_entity_meta(self, entities: List[str]) -> List[Dict[str, str]]:
    meta: List[Dict[str, str]] = []
    for entity_id in entities:
      entry: Dict[str, str] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        name = state.name
        value = state.state
        icon = _extract_mdi_icon(state, self.hass)
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        if isinstance(value, str) and value.strip():
          entry["state"] = value.strip()
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
      meta.append(entry)
    return meta

  def _build_binary_sensor_meta(self) -> List[Dict[str, Any]]:
    meta: List[Dict[str, Any]] = []
    for entity_id in self.binary_sensors:
      entry: Dict[str, Any] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        entry = build_binary_sensor_meta_entry(
          entity_id,
          state.state,
          state.attributes or {},
          state.last_changed,
          name=state.name,
          icon=_extract_mdi_icon(state, self.hass),
        )
      meta.append(entry)
    return meta

  def _build_scene_meta(self) -> List[Dict[str, str]]:
    entities = list({entity for entity in self.scene_map.values() if entity})
    return self._build_entity_meta(entities)

  async def _build_energy_meta(self, categories: set[str] | None = None) -> List[Dict[str, Any]]:
    """Build energy source list from HA Energy Dashboard config."""
    currency = self.hass.config.currency or "EUR"
    if async_get_energy_manager is None:
      return []
    try:
      manager = await async_get_energy_manager(self.hass)
    except Exception:
      return []
    prefs = manager.data
    if not prefs:
      return []
    sources = prefs.get("energy_sources") or []
    devices = prefs.get("device_consumption") or []
    devices_water = prefs.get("device_consumption_water") or []
    entries: List[Dict[str, Any]] = []
    energy_runtime = self.hass.data.get("energy") or {}
    cost_sensors = energy_runtime.get("cost_sensors") or {}

    def _has_cost_config(
      config: dict[str, Any], cost_key: str, energy_stat: str | None
    ) -> bool:
      return bool(
        config.get(cost_key)
        or (
          isinstance(energy_stat, str)
          and energy_stat.strip()
          and cost_sensors.get(energy_stat.strip())
        )
        or config.get("entity_energy_price")
        or config.get("number_energy_price") is not None
      )

    def _has_export_cost_config(config: dict[str, Any], energy_stat: str | None) -> bool:
      return bool(
        config.get("stat_compensation")
        or (
          isinstance(energy_stat, str)
          and energy_stat.strip()
          and cost_sensors.get(energy_stat.strip())
        )
        or config.get("entity_energy_price_export")
        or config.get("number_energy_price_export") is not None
        or config.get("entity_energy_price")
        or config.get("number_energy_price") is not None
      )

    for source in sources:
      src_type = source.get("type")
      if src_type == "solar":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          state = self.hass.states.get(stat_id)
          entry: Dict[str, Any] = {"id": stat_id, "category": "solar", "sign": 1}
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)

      elif src_type == "grid":
        if "stat_energy_from" in source:
          stat_id = source.get("stat_energy_from")
          if stat_id:
            state = self.hass.states.get(stat_id)
            entry = {"id": stat_id, "category": "grid", "sign": 1}
            entry["_has_cost"] = _has_cost_config(source, "stat_cost", stat_id)
            if state and state.attributes.get("friendly_name"):
              entry["name"] = state.attributes["friendly_name"]
            if state and state.attributes.get("unit_of_measurement"):
              entry["unit"] = state.attributes["unit_of_measurement"]
            entries.append(entry)
          stat_id_to = source.get("stat_energy_to")
          if stat_id_to:
            state = self.hass.states.get(stat_id_to)
            entry = {"id": stat_id_to, "category": "grid", "sign": -1}
            entry["_has_cost"] = _has_export_cost_config(source, stat_id_to)
            if state and state.attributes.get("friendly_name"):
              entry["name"] = state.attributes["friendly_name"]
            if state and state.attributes.get("unit_of_measurement"):
              entry["unit"] = state.attributes["unit_of_measurement"]
            entries.append(entry)
        for flow in source.get("flow_from") or []:
          stat_id = flow.get("stat_energy_from")
          if not stat_id:
            continue
          state = self.hass.states.get(stat_id)
          entry = {"id": stat_id, "category": "grid", "sign": 1}
          entry["_has_cost"] = _has_cost_config(flow, "stat_cost", stat_id)
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)
        for flow in source.get("flow_to") or []:
          stat_id = flow.get("stat_energy_to")
          if not stat_id:
            continue
          state = self.hass.states.get(stat_id)
          entry = {"id": stat_id, "category": "grid", "sign": -1}
          entry["_has_cost"] = _has_cost_config(flow, "stat_compensation", stat_id)
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)

      elif src_type == "battery":
        for key, sign in [("stat_energy_from", 1), ("stat_energy_to", -1)]:
          stat_id = source.get(key)
          if not stat_id:
            continue
          state = self.hass.states.get(stat_id)
          entry = {"id": stat_id, "category": "battery", "sign": sign}
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)

      elif src_type == "gas":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          state = self.hass.states.get(stat_id)
          entry = {"id": stat_id, "category": "gas", "sign": 1}
          entry["_has_cost"] = _has_cost_config(source, "stat_cost", stat_id)
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)

      elif src_type == "water":
        stat_id = source.get("stat_energy_from")
        if stat_id:
          state = self.hass.states.get(stat_id)
          entry = {"id": stat_id, "category": "water", "sign": 1}
          entry["_has_cost"] = _has_cost_config(source, "stat_cost", stat_id)
          if state and state.attributes.get("friendly_name"):
            entry["name"] = state.attributes["friendly_name"]
          if state and state.attributes.get("unit_of_measurement"):
            entry["unit"] = state.attributes["unit_of_measurement"]
          entries.append(entry)

    for dev in devices:
      stat_id = dev.get("stat_consumption")
      if stat_id:
        state = self.hass.states.get(stat_id)
        entry = {"id": stat_id, "category": "device", "sign": 1}
        name = dev.get("name") or (
          state.attributes.get("friendly_name") if state else None
        )
        if name:
          entry["name"] = name
        if state and state.attributes.get("unit_of_measurement"):
          entry["unit"] = state.attributes["unit_of_measurement"]
        entries.append(entry)

    for dev in devices_water:
      stat_id = dev.get("stat_consumption")
      if stat_id:
        state = self.hass.states.get(stat_id)
        entry = {"id": stat_id, "category": "device_water", "sign": 1}
        name = dev.get("name") or (
          state.attributes.get("friendly_name") if state else None
        )
        if name:
          entry["name"] = name
        if state and state.attributes.get("unit_of_measurement"):
          entry["unit"] = state.attributes["unit_of_measurement"]
        entries.append(entry)

    # Add cost meta entries for sources that have stat_cost
    cost_cats = {"grid", "gas", "water"}
    cost_meta: List[Dict[str, Any]] = []
    for e in entries:
      if e["category"] in cost_cats and e.get("_has_cost"):
        cost_meta.append({
          "id": f"{e['id']}_cost",
          "category": e["category"],
          "sign": e["sign"],
          "name": f"{e.get('name', e['id'])} ({currency})",
          "unit": currency,
          "is_cost": True,
        })
    entries.extend(cost_meta)

    # Add total entries for categories with multiple members
    category_names = {
      "solar": "PV gesamt",
      "grid": "Netz gesamt",
      "battery": "Batterie gesamt",
      "gas": "Gas gesamt",
      "water": "Wasser gesamt",
      "device": "Geräte gesamt",
      "device_water": "Wassergeräte gesamt",
    }
    kwh_entries = [e for e in entries if not e.get("is_cost")]
    cost_entries = [e for e in entries if e.get("is_cost")]
    for group, is_cost in [(kwh_entries, False), (cost_entries, True)]:
      cat_groups: Dict[str, list] = {}
      for e in group:
        cat_groups.setdefault(e["category"], []).append(e)
      for cat, members in cat_groups.items():
        if len(members) < 2:
          continue
        base_name = category_names.get(cat, f"{cat} gesamt")
        total_entry: Dict[str, Any] = {
          "id": f"{cat}_total" + ("_cost" if is_cost else ""),
          "category": cat,
          "sign": 1,
          "name": f"{base_name} ({currency})" if is_cost else base_name,
          "is_total": True,
        }
        if is_cost:
          total_entry["unit"] = currency
          total_entry["is_cost"] = True
        elif members[0].get("unit"):
          total_entry["unit"] = members[0]["unit"]
        entries.append(total_entry)

    for e in entries:
      e.pop("_has_cost", None)

    # Add Gesamtverbrauch and Nicht erfasster Verbrauch meta
    electricity_cats = {"solar", "grid", "battery"}
    has_electricity = any(e["category"] in electricity_cats for e in entries
                         if not e.get("is_total") and not e.get("is_cost"))
    if has_electricity:
      entries.append({
        "id": "consumption_total",
        "category": "consumption",
        "sign": 1,
        "name": "Gesamtverbrauch",
        "unit": "kWh",
        "is_total": True,
      })
      has_devices = any(e["category"] == "device" for e in entries
                        if not e.get("is_total") and not e.get("is_cost"))
      if has_devices:
        entries.append({
          "id": "consumption_untracked",
          "category": "consumption",
          "sign": 1,
          "name": "Nicht erfasster Verbrauch",
          "unit": "kWh",
          "is_total": True,
        })

    if categories:
      categories = categories | {"consumption"}
      entries = [e for e in entries if e["category"] in categories]

    return entries


def _unique_entities(entities: List[str]) -> List[str]:
  seen = set()
  result: List[str] = []
  for entity_id in entities:
    cleaned = (entity_id or "").strip()
    if not cleaned or cleaned in seen:
      continue
    seen.add(cleaned)
    result.append(cleaned)
  return result


def _is_weather_entity(entity_id: str) -> bool:
  return isinstance(entity_id, str) and entity_id.startswith("weather.")


def _split_weather_entities(entities: List[str]) -> Tuple[List[str], List[str]]:
  weathers: List[str] = []
  sensors: List[str] = []
  for entity_id in entities:
    if _is_weather_entity(entity_id):
      weathers.append(entity_id)
    else:
      sensors.append(entity_id)
  return _unique_entities(weathers), _unique_entities(sensors)


def _weather_number(value: Any) -> Optional[float]:
  if isinstance(value, bool):
    return None
  if isinstance(value, (int, float)):
    return float(value)
  if isinstance(value, str):
    text = value.strip().replace(",", ".")
    if not text:
      return None
    try:
      return float(text)
    except ValueError:
      return None
  return None


def _forecast_entry_local_date(entry: Dict[str, Any]) -> Optional[date]:
  raw = entry.get("datetime") or entry.get("date")
  if isinstance(raw, datetime):
    try:
      return dt_util.as_local(raw).date()
    except Exception:
      return raw.date()
  if isinstance(raw, date):
    return raw
  if not isinstance(raw, str):
    return None

  parsed = dt_util.parse_datetime(raw)
  if parsed is not None:
    try:
      return dt_util.as_local(parsed).date()
    except Exception:
      return parsed.date()

  try:
    return date.fromisoformat(raw[:10])
  except ValueError:
    return None


def _forecast_entry_local_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
  raw = entry.get("datetime")
  if isinstance(raw, datetime):
    try:
      return dt_util.as_local(raw)
    except Exception:
      return raw
  if not isinstance(raw, str):
    return None

  parsed = dt_util.parse_datetime(raw)
  if parsed is None:
    return None
  try:
    return dt_util.as_local(parsed)
  except Exception:
    return parsed


def _merge_hourly_precip_into_daily(
  daily_forecast: List[Dict[str, Any]],
  hourly_forecast: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
  hourly_by_day: Dict[date, Dict[str, Any]] = {}

  for entry in hourly_forecast:
    if not isinstance(entry, dict):
      continue
    forecast_day = _forecast_entry_local_date(entry)
    if forecast_day is None:
      continue

    bucket = hourly_by_day.setdefault(
      forecast_day,
      {
        "precipitation_total": 0.0,
        "has_precipitation": False,
        "precipitation_probability_max": 0.0,
        "has_probability": False,
      },
    )

    precipitation = _weather_number(entry.get("precipitation"))
    if precipitation is not None:
      bucket["precipitation_total"] += precipitation
      bucket["has_precipitation"] = True

    precipitation_probability = _weather_number(entry.get("precipitation_probability"))
    if precipitation_probability is not None:
      bucket["precipitation_probability_max"] = max(
        bucket["precipitation_probability_max"],
        precipitation_probability,
      )
      bucket["has_probability"] = True

  merged: List[Dict[str, Any]] = []
  for entry in daily_forecast:
    if not isinstance(entry, dict):
      continue
    out = dict(entry)
    forecast_day = _forecast_entry_local_date(out)
    bucket = hourly_by_day.get(forecast_day) if forecast_day is not None else None
    if bucket:
      if bucket["has_precipitation"]:
        out["precipitation"] = round(bucket["precipitation_total"], 1)
      if bucket["has_probability"]:
        out["precipitation_probability"] = int(round(bucket["precipitation_probability_max"]))
    merged.append(out)

  return merged


def _build_daily_forecast_from_periods(
  forecast: List[Dict[str, Any]], *, twice_daily: bool = False,
) -> List[Dict[str, Any]]:
  """Aggregate local dates, using period lows and daytime icons for twice-daily data."""
  buckets: Dict[date, Dict[str, Any]] = {}

  for entry in forecast:
    if not isinstance(entry, dict):
      continue
    forecast_day = _forecast_entry_local_date(entry)
    if forecast_day is None:
      continue

    bucket = buckets.setdefault(
      forecast_day,
      {
        "high": None,
        "low": None,
        "precipitation_total": 0.0,
        "has_precipitation": False,
        "precipitation_probability_max": 0.0,
        "has_probability": False,
        "midday_distance": 99,
        "condition": None,
        "icon": None,
        "datetime": None,
      },
    )

    temperature = _weather_number(entry.get("temperature"))
    if temperature is not None:
      bucket["high"] = temperature if bucket["high"] is None else max(bucket["high"], temperature)
    low = _weather_number(entry.get("templow")) if twice_daily else None
    if low is None:
      low = temperature
    if low is not None:
      bucket["low"] = low if bucket["low"] is None else min(bucket["low"], low)

    precipitation = _weather_number(entry.get("precipitation"))
    if precipitation is not None:
      bucket["precipitation_total"] += precipitation
      bucket["has_precipitation"] = True

    precipitation_probability = _weather_number(entry.get("precipitation_probability"))
    if precipitation_probability is not None:
      bucket["precipitation_probability_max"] = max(
        bucket["precipitation_probability_max"],
        precipitation_probability,
      )
      bucket["has_probability"] = True

    local_dt = _forecast_entry_local_datetime(entry)
    hour = local_dt.hour if local_dt is not None else None
    distance = abs(hour - 12) if hour is not None else 99
    if twice_daily:
      # Providers may use the same timestamp for both periods. Use HA's explicit flag.
      distance = 0 if entry.get("is_daytime") is True else 1
    if distance <= bucket["midday_distance"]:
      bucket["midday_distance"] = distance
      bucket["condition"] = entry.get("condition")
      bucket["icon"] = entry.get("icon")
      bucket["datetime"] = local_dt.isoformat() if local_dt is not None else entry.get("datetime")

  built: List[Dict[str, Any]] = []
  for forecast_day in sorted(buckets.keys()):
    bucket = buckets[forecast_day]
    out: Dict[str, Any] = {
      "date_local": forecast_day.isoformat(),
      "datetime": bucket["datetime"] or forecast_day.isoformat(),
    }
    if bucket["condition"] is not None:
      out["condition"] = bucket["condition"]
    if bucket["icon"] is not None:
      out["icon"] = bucket["icon"]
    if bucket["high"] is not None:
      out["temperature"] = round(bucket["high"], 1)
    if bucket["low"] is not None:
      out["templow"] = round(bucket["low"], 1)
    if bucket["has_precipitation"]:
      out["precipitation"] = round(bucket["precipitation_total"], 1)
    if bucket["has_probability"]:
      out["precipitation_probability"] = int(round(bucket["precipitation_probability_max"]))
    built.append(out)

  return built


def _compact_daily_forecast(daily_forecast: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
  compact: List[Dict[str, Any]] = []
  for entry in daily_forecast[:FORECAST_DAILY_LIMIT]:
    if not isinstance(entry, dict):
      continue

    out: Dict[str, Any] = {}
    local_day = _forecast_entry_local_date(entry)
    if local_day is not None:
      out["date_local"] = local_day.isoformat()
      out["datetime"] = entry.get("datetime") or local_day.isoformat()
    elif isinstance(entry.get("date_local"), str):
      out["date_local"] = entry["date_local"]
      if entry.get("datetime") is not None:
        out["datetime"] = entry.get("datetime")
    elif entry.get("datetime") is not None:
      out["datetime"] = entry.get("datetime")

    for key in ("condition", "icon", "temperature", "precipitation", "precipitation_probability"):
      value = entry.get(key)
      if value is not None:
        out[key] = value

    for low_key in ("templow", "temperature_low", "temp_low", "low"):
      value = entry.get(low_key)
      if value is not None:
        out["templow"] = value
        break

    if out:
      compact.append(out)

  return compact


def _compact_hourly_forecast(hourly_forecast: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
  compact: List[Dict[str, Any]] = []
  for entry in hourly_forecast[:FORECAST_HOURLY_PAYLOAD_LIMIT]:
    if not isinstance(entry, dict):
      continue
    out: Dict[str, Any] = {}
    local_dt = _forecast_entry_local_datetime(entry)
    if local_dt is not None:
      out["d"] = local_dt.date().isoformat()
      out["h"] = int(local_dt.hour)
      condition = entry.get("condition")
      if isinstance(condition, str) and condition.strip():
        out["c"] = condition.strip()
      else:
        icon = entry.get("icon")
        if isinstance(icon, str) and icon.strip():
          out["i"] = icon.strip()
    temperature = entry.get("temperature")
    if temperature is not None:
      out["t"] = temperature
    precipitation = entry.get("precipitation")
    if precipitation is not None:
      out["p"] = precipitation
    precipitation_probability = entry.get("precipitation_probability")
    if precipitation_probability is not None:
      out["pp"] = precipitation_probability
    if out:
      compact.append(out)
  return compact


def _try_parse_json(payload: str) -> Any:
  try:
    return json.loads(payload)
  except (ValueError, TypeError):
    return None


def _normalise_command(value: Any) -> Optional[str]:
  if value is None:
    return None
  text = str(value).strip().lower()
  if not text:
    return None
  if text in ("on", "off", "toggle"):
    return text
  if text in ("1", "true", "yes"):
    return "on"
  if text in ("0", "false", "no"):
    return "off"
  return None


def _normalise_media_command(value: Any) -> Optional[str]:
  if value is None:
    return None
  text = str(value).strip().lower().replace("-", "_")
  if not text:
    return None
  return MEDIA_COMMAND_ALIASES.get(text)


def _parse_simple_command(payload: str) -> Tuple[Optional[str], Optional[str]]:
  text = (payload or "").strip()
  if not text:
    return None, None
  if " " in text:
    first, rest = text.split(None, 1)
    if "." in first and rest.strip():
      return first.strip(), rest.strip()
  for separator in (":", "="):
    if separator in text:
      left, right = text.split(separator, 1)
      if "." in left.strip() and right.strip():
        return left.strip(), right.strip()
  return None, text


def _extract_light_service_data(payload: Dict[str, Any]) -> Dict[str, Any]:
  data: Dict[str, Any] = {}
  for key in LIGHT_SERVICE_FIELDS:
    if key in payload and payload[key] is not None:
      data[key] = payload[key]
  return data


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
  try:
    result = int(value)
  except (TypeError, ValueError):
    result = default
  if result < minimum:
    return minimum
  if result > maximum:
    return maximum
  return result


def _coerce_float(value: Any) -> Optional[float]:
  if isinstance(value, bool):
    return None
  try:
    return float(str(value).strip().replace(",", "."))
  except (TypeError, ValueError):
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
  if isinstance(value, bool):
    return value
  if value is None:
    return None
  text = str(value).strip().lower()
  if text in ("1", "true", "yes", "on"):
    return True
  if text in ("0", "false", "no", "off"):
    return False
  return None


def _normalise_topic(value: Optional[str], default: str) -> str:
  result = (value or "").strip() or default
  while result.endswith("/"):
    result = result[:-1]
  return result or default


def _fallback_icon_from_state(state: State) -> Optional[str]:
  if not state:
    return None
  entity_id = state.entity_id or ""
  domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
  attrs = state.attributes or {}
  device_class = str(attrs.get("device_class") or "").strip().lower()
  unit = str(attrs.get("unit_of_measurement") or "").strip()
  unit_norm = unit.lower().replace(" ", "")

  def _battery_icon_from_state() -> Optional[str]:
    raw = attrs.get("battery_level")
    if raw is None:
      raw = state.state
    if raw is None:
      return None
    if isinstance(raw, str):
      raw = raw.strip()
      if raw.endswith("%"):
        raw = raw[:-1].strip()
    try:
      level = float(raw)
    except (TypeError, ValueError):
      return None
    if level < 0:
      level = 0
    if level >= 95:
      return "mdi:battery"
    bucket = int(level // 10) * 10
    if bucket <= 0:
      bucket = 10
    if bucket >= 100:
      return "mdi:battery"
    return f"mdi:battery-{bucket}"

  if domain == "light":
    return "mdi:lightbulb"
  if domain == "switch":
    return "mdi:toggle-switch"
  if domain == "cover":
    return cover_component_icon(device_class, state.state)
  if domain == "scene":
    return "mdi:palette"
  if domain == "media_player":
    if device_class == "speaker":
      return "mdi:speaker"
    return "mdi:television"

  if domain == "sensor":
    is_battery_like = device_class == "battery"
    if not is_battery_like and unit_norm in {"%", "percent", "percentage"}:
      name = str(attrs.get("friendly_name") or state.name or "").lower()
      if "battery" in name or "batterie" in name or "soc" in entity_id:
        is_battery_like = True
    if is_battery_like:
      icon = _battery_icon_from_state()
      if icon:
        return icon
      return "mdi:battery"
    device_class_icons = {
      "temperature": "mdi:thermometer",
      "humidity": "mdi:water-percent",
      "power": "mdi:flash",
      "apparent_power": "mdi:flash",
      "voltage": "mdi:flash",
      "current": "mdi:flash",
      "energy": "mdi:lightning-bolt",
      "pressure": "mdi:gauge",
    }
    if device_class in device_class_icons:
      return device_class_icons[device_class]

    if unit_norm in {"°c", "°f", "c", "f", "degc", "degf"}:
      return "mdi:thermometer"
    if unit_norm in {"w", "kw", "mw", "va", "kva", "mva"}:
      return "mdi:flash"
    if unit_norm in {"wh", "kwh", "mwh"}:
      return "mdi:lightning-bolt"

  return None


def _normalize_mdi_icon_value(icon: Any) -> Optional[str]:
  if not isinstance(icon, str):
    return None
  icon = icon.strip()
  if not icon:
    return None
  # Accept standard MDI prefixes (mdi:home, mdi-home) or bare icon names.
  if ":" in icon and not icon.startswith("mdi:"):
    return None
  if icon.startswith("mdi-"):
    return "mdi:" + icon[4:]
  return icon


def _extract_mdi_icon(state: State, hass: Optional[HomeAssistant] = None) -> Optional[str]:
  if not state:
    return None
  icon = ""
  if hass:
    try:
      registry_entry = er.async_get(hass).async_get(state.entity_id)
      icon = getattr(registry_entry, "icon", None) if registry_entry else ""
    except Exception:
      icon = ""
  if not icon:
    raw_icon = state.attributes.get("icon")
    icon = raw_icon.strip() if isinstance(raw_icon, str) else ""
  if not icon and hass and icon_for_entity:
    try:
      # HA 2025+ typically supports state kwarg.
      icon = icon_for_entity(hass, state.entity_id, state=state)
    except TypeError:
      try:
        # Older signature: (hass, entity_id, state)
        icon = icon_for_entity(hass, state.entity_id, state)
      except TypeError:
        try:
          # Older signature: (hass, entity_id)
          icon = icon_for_entity(hass, state.entity_id)
        except TypeError:
          try:
            # Legacy fallback: (hass, state)
            icon = icon_for_entity(hass, state)
          except Exception:
            icon = None
        except Exception:
          icon = None
      except Exception:
        icon = None
    except Exception:
      icon = None
  if not icon:
    icon = _fallback_icon_from_state(state)
  return _normalize_mdi_icon_value(icon)


def _extract_media_player_mdi_icon(state: State, hass: Optional[HomeAssistant] = None) -> Optional[str]:
  """Return the entity icon for media players, not the playback-state icon."""
  if not state:
    return None

  raw_icon = state.attributes.get("icon")
  icon = _normalize_mdi_icon_value(raw_icon)
  if icon:
    return icon

  if hass and icon_for_entity:
    try:
      icon = icon_for_entity(hass, state.entity_id)
    except TypeError:
      try:
        icon = icon_for_entity(hass, state)
      except Exception:
        icon = None
    except Exception:
      icon = None
    icon = _normalize_mdi_icon_value(icon)
    if icon:
      return icon

  return _fallback_icon_from_state(state)


def _normalize_weather_value(value: Any) -> Any:
  if isinstance(value, datetime):
    try:
      return dt_util.as_utc(value).isoformat()
    except Exception:
      return value.isoformat()
  if isinstance(value, date):
    return value.isoformat()
  return value


def _sanitize_forecast_list(value: Any) -> Optional[List[Dict[str, Any]]]:
  if not isinstance(value, list):
    return None
  cleaned: List[Dict[str, Any]] = []
  for item in value:
    if not isinstance(item, dict):
      continue
    out: Dict[str, Any] = {}
    for key, val in item.items():
      if val is None:
        continue
      out[key] = _normalize_weather_value(val)
    if out:
      cleaned.append(out)
  return cleaned if cleaned else None


_WEATHER_ICON_MAP = {
  "clear-night": "mdi:weather-night",
  "cloudy": "mdi:weather-cloudy",
  "exceptional": "mdi:alert-circle-outline",
  "fog": "mdi:weather-fog",
  "hail": "mdi:weather-hail",
  "lightning": "mdi:weather-lightning",
  "lightning-rainy": "mdi:weather-lightning-rainy",
  "partlycloudy": "mdi:weather-partly-cloudy",
  "pouring": "mdi:weather-pouring",
  "rainy": "mdi:weather-rainy",
  "snowy": "mdi:weather-snowy",
  "snowy-rainy": "mdi:weather-snowy-rainy",
  "sunny": "mdi:weather-sunny",
  "windy": "mdi:weather-windy",
  "windy-variant": "mdi:weather-windy-variant",
}


def _apply_forecast_icons(forecast: List[Dict[str, Any]]) -> None:
  for entry in forecast:
    if not isinstance(entry, dict):
      continue
    icon = entry.get("icon")
    if isinstance(icon, str) and icon.strip():
      continue
    condition = entry.get("condition") or entry.get("state")
    if not isinstance(condition, str):
      continue
    key = condition.strip().lower()
    if key in _WEATHER_ICON_MAP:
      entry["icon"] = _WEATHER_ICON_MAP[key]


def _extract_forecast_from_result(result: Any, entity_id: str) -> Optional[List[Dict[str, Any]]]:
  if isinstance(result, list):
    return result
  if not isinstance(result, dict):
    return None
  if isinstance(result.get("forecast"), list):
    return result.get("forecast")
  entry = result.get(entity_id) or result.get(entity_id.lower()) or result.get(entity_id.upper())
  if isinstance(entry, list):
    return entry
  if isinstance(entry, dict):
    if isinstance(entry.get("forecast"), list):
      return entry.get("forecast")
    if isinstance(entry.get("forecasts"), list):
      return entry.get("forecasts")
  return None


def _weather_icon_from_state(state: State, hass: Optional[HomeAssistant] = None) -> Optional[str]:
  if not state:
    return None
  icon = _extract_mdi_icon(state, hass)
  if icon:
    return icon
  attrs = state.attributes or {}
  condition = attrs.get("condition") or state.state
  if isinstance(condition, str):
    key = condition.strip().lower()
    if key in _WEATHER_ICON_MAP:
      return _WEATHER_ICON_MAP[key]
  return None


def _extract_media_player_payload(state: State, hass: Optional[HomeAssistant] = None) -> Dict[str, Any]:
  attrs = state.attributes or {}
  payload: Dict[str, Any] = {"state": state.state}

  name = state.name
  if isinstance(name, str) and name.strip():
    payload["name"] = name.strip()

  icon = _extract_media_player_mdi_icon(state, hass)
  if isinstance(icon, str) and icon.strip():
    payload["icon"] = icon.strip()

  for key in (
    "app_name",
    "entity_picture",
    "is_volume_muted",
    "media_album_name",
    "media_artist",
    "media_channel",
    "media_image_url",
    "media_duration",
    "media_position",
    "media_position_updated_at",
    "media_title",
    "source",
    "volume_level",
  ):
    if key in attrs and attrs[key] is not None:
      payload[key] = _normalize_weather_value(attrs[key])

  for url_key in ("entity_picture", "media_image_url"):
    value = payload.get(url_key)
    if isinstance(value, str) and value.startswith("/") and hass is not None and get_url is not None:
      try:
        base = get_url(hass, prefer_external=False, allow_internal=True, allow_external=True)
      except Exception:  # pragma: no cover - get_url may raise NoURLAvailableError
        base = None
      if isinstance(base, str) and base:
        payload[url_key] = base.rstrip("/") + value

  return payload


def _extract_weather_payload(state: State, hass: Optional[HomeAssistant] = None) -> Dict[str, Any]:
  attrs = state.attributes or {}
  payload: Dict[str, Any] = {"state": state.state}

  name = state.name
  if isinstance(name, str) and name.strip():
    payload["name"] = name.strip()

  icon = _weather_icon_from_state(state, hass)
  if isinstance(icon, str) and icon.strip():
    payload["icon"] = icon.strip()

  for key in (
    "temperature",
    "apparent_temperature",
    "dew_point",
    "humidity",
    "pressure",
    "wind_speed",
    "wind_bearing",
    "wind_gust_speed",
    "visibility",
    "ozone",
    "uv_index",
    "cloud_coverage",
    "precipitation",
    "precipitation_probability",
  ):
    if key in attrs and attrs[key] is not None:
      payload[key] = _normalize_weather_value(attrs[key])

  units: Dict[str, Any] = {}
  for unit_key, unit_name in (
    ("temperature_unit", "temperature"),
    ("pressure_unit", "pressure"),
    ("wind_speed_unit", "wind_speed"),
    ("visibility_unit", "visibility"),
    ("precipitation_unit", "precipitation"),
  ):
    unit_val = attrs.get(unit_key)
    if unit_val:
      units[unit_name] = unit_val
  if units:
    payload["units"] = units

  attribution = attrs.get("attribution")
  if isinstance(attribution, str) and attribution.strip():
    payload["attribution"] = attribution.strip()

  forecast = _sanitize_forecast_list(attrs.get("forecast"))
  if forecast:
    _apply_forecast_icons(forecast)
    if len(forecast) > FORECAST_DAILY_LIMIT:
      forecast = forecast[:FORECAST_DAILY_LIMIT]
    payload["forecast"] = forecast

  return payload


async def _async_process_bridge_config(hass: HomeAssistant, payload: Dict[str, Any]) -> None:
  try:
    data = _payload_to_entry_data(payload)
  except ValueError as err:
    _LOGGER.warning("HomeTiles Bridge ignored configuration payload (%s)", err)
    return

  device_id = data.get(CONF_DEVICE_ID)
  entry = _find_entry_by_device_id(hass, device_id)

  if entry and entry.source == config_entries.SOURCE_IGNORE:
    # The user ignored this panel's discovery card: store nothing for it.
    return

  if entry:
    runtime_sensor_ids = _runtime_managed_sensor_entity_ids(hass, entry, data)
    data[CONF_SENSORS] = filter_runtime_sensor_entities(
      data.get(CONF_SENSORS, []), runtime_sensor_ids
    )
    existing, cleaned_options, data_cleaned, options_cleaned, removed_count = (
      clean_stored_sensor_selections(
        entry.data, entry.options, CONF_SENSORS, runtime_sensor_ids
      )
    )
    # Geraet ist bereits verbunden - trotzdem pruefen, ob die Firmware jetzt
    # Geraetename/Hersteller/Modell mitliefert, die beim urspruenglichen
    # Verbinden noch fehlten (z.B. nach einem Firmware-Update). Ein vom Nutzer
    # manuell gesetzter Wert (siehe entry_device_name()) bleibt unangetastet,
    # da hier nur echte Luecken aufgefuellt werden, nie Bestehendes ueberschrieben.
    changed = data_cleaned or options_cleaned
    for key in (CONF_MANUFACTURER, CONF_MODEL, CONF_DEVICE_NAME):
      if not existing.get(key) and data.get(key):
        existing[key] = data[key]
        changed = True
    # Gleiches Prinzip fuer Entity-/Szenen-Zuordnungen: ein per Zeroconf VOR
    # dem ersten MQTT-Connect erzeugter Eintrag hat hier noch nichts (siehe
    # async_step_zeroconf_confirm in config_flow.py), waehrend der Nutzer auf
    # dem Panel selbst (handleSaveBridge) schon vorher etwas eingerichtet
    # haben kann. Ohne dieses Nachtragen wuerde genau dieser Fall die bereits
    # gemachte Konfiguration beim ersten echten Connect stillschweigend
    # verwerfen, weil sonst nur der Erstell-Pfad sie uebernimmt.
    for key in (
      CONF_SENSORS, CONF_BINARY_SENSORS, CONF_WEATHERS, CONF_LIGHTS, CONF_SWITCHES,
      CONF_MEDIA_PLAYERS, CONF_CLIMATES, CONF_COVERS, CONF_CAMERAS,
      CONF_NUMBERS, CONF_SELECTS, CONF_DATETIMES,
      CONF_SCENE_MAP,
    ):
      if should_import_feedback_selection(
        existing, cleaned_options, key, data.get(key)
      ):
        existing[key] = data[key]
        changed = True
    # Local hardware belongs to this one panel and is device-announced rather
    # than user-selected. Unlike the shared entity lists above, an explicit
    # empty list must therefore remove channels that disappeared from firmware.
    # If the field is absent (older firmware), keep the last known list.
    if CONF_LOCAL_IO in data and existing.get(CONF_LOCAL_IO) != data[CONF_LOCAL_IO]:
      existing[CONF_LOCAL_IO] = data[CONF_LOCAL_IO]
      changed = True
    if CAPABILITIES in data and existing.get(CAPABILITIES) != data[CAPABILITIES]:
      existing[CAPABILITIES] = data[CAPABILITIES]
      changed = True
    if changed:
      if removed_count:
        _LOGGER.info(
          "HomeTiles Bridge removed %s stale runtime sensor selection(s) from %s",
          removed_count,
          entry.entry_id,
        )
      _LOGGER.info("HomeTiles Bridge updated device metadata for %s", device_id)
      # The integration's regular update listener reloads the entry, so new or
      # removed local-I/O platform entities appear without a manual restart.
      update: Dict[str, Any] = {"data": existing}
      if options_cleaned:
        update["options"] = cleaned_options
      hass.config_entries.async_update_entry(entry, **update)
    return

  fallback = _find_entry_by_base(hass, data.get(CONF_BASE_TOPIC))
  if fallback and not _may_adopt_entry(fallback, device_id):
    # The base topic belongs to another panel; do not take over its entry.
    _LOGGER.debug(
      "HomeTiles Bridge ignored device %s: base topic already used by %s",
      device_id,
      fallback.title,
    )
    return
  if fallback:
    new_data = dict(fallback.data)
    changed = False
    if device_id and new_data.get(CONF_DEVICE_ID) != device_id:
      new_data[CONF_DEVICE_ID] = device_id
      changed = True
    if CONF_LOCAL_IO in data and new_data.get(CONF_LOCAL_IO) != data[CONF_LOCAL_IO]:
      new_data[CONF_LOCAL_IO] = data[CONF_LOCAL_IO]
      changed = True
    if CAPABILITIES in data and new_data.get(CAPABILITIES) != data[CAPABILITIES]:
      new_data[CAPABILITIES] = data[CAPABILITIES]
      changed = True
    if not changed:
      return

    _LOGGER.info("HomeTiles Bridge adopted device %s into the existing entry", device_id)
    hass.config_entries.async_update_entry(
      fallback,
      data=new_data,
      title=_entry_title(new_data),
      unique_id=device_id or fallback.unique_id,
    )
    await hass.config_entries.async_reload(fallback.entry_id)
    return

  _LOGGER.info("HomeTiles Bridge discovered device %s; waiting for confirmation", device_id)
  data[CONF_SENSORS] = filter_runtime_sensor_entities(
    data.get(CONF_SENSORS, []),
    _runtime_managed_sensor_entity_ids(hass, None, data),
  )
  # Home Assistant shows a discovery card; the entry is created on confirm.
  discovery_flow.async_create_flow(
    hass,
    DOMAIN,
    context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
    data=data,
  )


def _payload_to_entry_data(payload: Dict[str, Any]) -> Dict[str, Any]:
  device_id = payload.get("device_id")
  if not device_id:
    raise ValueError("missing_device_id")

  base = _normalise_topic(payload.get("base_topic"), DEFAULT_BASE)
  prefix = _normalise_topic(payload.get("ha_prefix"), DEFAULT_PREFIX)

  sensors_raw = payload.get("configured_sensors", payload.get("sensors")) or []
  if not isinstance(sensors_raw, list):
    raise ValueError("invalid_sensors")
  sensors = [str(item).strip() for item in sensors_raw if str(item).strip()]

  binary_sensors_raw = payload.get(CONF_BINARY_SENSORS) or []
  if not isinstance(binary_sensors_raw, list):
    raise ValueError("invalid_binary_sensors")
  binary_sensors = [
    str(item).strip() for item in binary_sensors_raw if str(item).strip()
  ]
  if any(not entity_id.startswith("binary_sensor.") for entity_id in binary_sensors):
    raise ValueError("invalid_binary_sensors")

  weathers_raw = payload.get("weathers") or []
  if not isinstance(weathers_raw, list):
    raise ValueError("invalid_weathers")
  weathers = [str(item).strip() for item in weathers_raw if str(item).strip()]
  legacy_weathers, sensors = _split_weather_entities(sensors)
  legacy_binary_sensors, sensors = split_binary_sensor_entities(sensors)
  binary_sensors = _unique_entities(binary_sensors + legacy_binary_sensors)
  weathers = _unique_entities(weathers + legacy_weathers)

  # A panel must not add entities of other domains to the shared lists.
  lights = panel_entity_list(payload.get("lights"), CONF_LIGHTS)
  switches = panel_entity_list(payload.get("switches"), CONF_SWITCHES)
  media_players = panel_entity_list(payload.get("media_players"), CONF_MEDIA_PLAYERS)
  climates = panel_entity_list(payload.get("climates"), CONF_CLIMATES)

  covers_raw = payload.get("covers") or []
  if not isinstance(covers_raw, list):
    raise ValueError("invalid_covers")
  covers = [str(item).strip() for item in covers_raw if str(item).strip()]
  if any(not entity_id.startswith("cover.") for entity_id in covers):
    raise ValueError("invalid_covers")

  cameras = panel_entity_list(payload.get("cameras"), CONF_CAMERAS)

  scene_map_raw = payload.get("scene_map") or {}
  if not isinstance(scene_map_raw, dict):
    raise ValueError("invalid_scene_map")
  scene_map: Dict[str, str] = {}
  for alias, entity in scene_map_raw.items():
    # Scene tiles only run scenes, scripts and buttons; drop anything else.
    if not alias or entity_domain(entity) not in ACTION_DOMAINS:
      continue
    scene_map[str(alias).lower()] = str(entity)

  manufacturer = str(payload.get("manufacturer") or "").strip()
  model = str(payload.get("model") or "").strip()
  device_name = str(payload.get("device_name") or "").strip()

  data = {
    CONF_DEVICE_ID: device_id,
    CONF_BASE_TOPIC: base,
    CONF_HA_PREFIX: prefix,
    CONF_SENSORS: sensors,
    CONF_BINARY_SENSORS: binary_sensors,
    CONF_WEATHERS: weathers,
    CONF_LIGHTS: lights,
    CONF_SWITCHES: switches,
    CONF_MEDIA_PLAYERS: media_players,
    CONF_CLIMATES: climates,
    CONF_COVERS: covers,
    CONF_CAMERAS: cameras,
    CONF_SCENE_MAP: scene_map,
  }
  for key, domains in EDITABLE_LISTS.items():
    if key in payload:
      data[key] = editable_selection(payload[key], domains)
  if manufacturer:
    data[CONF_MANUFACTURER] = manufacturer
  if model:
    data[CONF_MODEL] = model
  if device_name:
    data[CONF_DEVICE_NAME] = device_name
  if CAPABILITIES in payload:
    data[CAPABILITIES] = normalise_capabilities(payload[CAPABILITIES])
  if CONF_LOCAL_IO in payload:
    local_io_raw = payload.get(CONF_LOCAL_IO)
    if local_io_raw is None:
      raise ValueError("invalid_local_io")
    data[CONF_LOCAL_IO] = normalise_local_io(local_io_raw)
  return data


def _find_entry_by_device_id(hass: HomeAssistant, device_id: Optional[str]) -> Optional[ConfigEntry]:
  if not device_id:
    return None
  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.data.get(CONF_DEVICE_ID) == device_id or entry.unique_id == device_id:
      return entry
  return None


def _may_adopt_entry(entry: ConfigEntry, device_id: Optional[str]) -> bool:
  """Only a manual entry without a panel, or a pre-v0.3.1 tab5_lvgl_XXXX
  entry with the same MAC suffix, may be adopted by an announcing panel."""
  bound = str(entry.data.get(CONF_DEVICE_ID) or entry.unique_id or "")
  if not bound:
    return True
  return (
    len(bound) == 14 and bound.startswith("tab5_lvgl_")
    and str(device_id or "").upper().endswith(bound[-4:].upper())
  )


def _find_entry_by_base(hass: HomeAssistant, base_topic: Optional[str]) -> Optional[ConfigEntry]:
  if not base_topic:
    return None
  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.data.get(CONF_BASE_TOPIC) == base_topic:
      return entry
  return None


def _entry_title(data: Dict[str, Any]) -> str:
  device_name = data.get(CONF_DEVICE_NAME)
  if device_name:
    return device_name
  device_id = data.get(CONF_DEVICE_ID)
  if device_id:
    suffix = device_id[-4:].upper()
    return f"Panel {suffix}"
  return "HomeTiles Panel"
