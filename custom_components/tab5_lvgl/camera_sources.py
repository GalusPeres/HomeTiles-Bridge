"""The cameras the Bridge itself provides: rain radar places and webcams.

One thing is one entity, however many tiles and panels use it. They belong to the
Bridge's own config entry and device, never to a panel, so removing a panel moves
nothing. The rain radar of Home Assistant's home always exists; every other place
and webcam exists while a panel declares it (entity_search.py) and goes with the
last one. A panel names a source by its object id, so its camera tiles simply use
camera.<id>:

  ht_radar_home                 Home Assistant's home
  ht_radar_zone_<zone id>       a Home Assistant zone
  ht_radar_<lat>_<lon>          coordinates, hundredths of a degree, m for minus
  ht_cam_<hash>                 a webcam's address

Nothing here imports Home Assistant; __init__.py hands in what it needs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .webcam_source import url_allowed

_LOGGER = logging.getLogger(__name__)

DATA_CAMERA_SOURCES = "camera_sources"
BRIDGE_DEVICE_ID = "hometiles_bridge"
HOME_ID = "ht_radar_home"
UNIQUE_ID_PREFIX = "hometiles_source_"
STORE_KEY = "tab5_lvgl.camera_sources"
MAX_PANEL_SOURCES = 32
MAX_NAME_CHARS = 64

_ID_RE = re.compile(r"^ht_(radar|cam)_[a-z0-9_]{1,48}$")
_ZONE_RE = re.compile(r"^zone\.[a-z0-9_]{1,64}$")

# Times of a history in a "ready" or "frames" message. A sealed message holds at
# most 2 KB: up to this many times as they are, more as minutes from the first.
PLAIN_FRAME_TIMES = 32


def frames_payload(times: List[int]) -> Dict[str, Any]:
  if len(times) <= PLAIN_FRAME_TIMES:
    return {"frames": times}
  base = times[0]
  return {"frames_base": base, "frames_min": [round((when - base) / 60) for when in times]}


def source_unique_id(source_id: str) -> str:
  return f"{UNIQUE_ID_PREFIX}{source_id}"


def source_entity_id(source_id: str) -> str:
  return f"camera.{source_id}"


def source_id_of(entity_id: Optional[str]) -> Optional[str]:
  """The source id behind a camera entity id, None for any other entity."""
  if not entity_id or not entity_id.startswith("camera.ht_"):
    return None
  source_id = entity_id.split(".", 1)[1]
  return source_id if _ID_RE.match(source_id) else None


def clean_source(item: Any) -> Optional[Dict[str, Any]]:
  """One declared source, or None. Nothing else of it reaches a request."""
  if not isinstance(item, Mapping):
    return None
  source_id = item.get("id")
  if not isinstance(source_id, str) or not _ID_RE.match(source_id):
    return None
  name = item.get("n")
  name = name.strip()[:MAX_NAME_CHARS] if isinstance(name, str) else ""
  if source_id == HOME_ID:
    return {"id": source_id, "kind": "radar"}
  if source_id.startswith("ht_cam_"):
    url = item.get("url")
    if not url_allowed(url):
      return None
    return {"id": source_id, "kind": "webcam", "url": url.strip(), "name": name}
  zone = item.get("zone")
  if zone is not None:
    if not isinstance(zone, str) or not _ZONE_RE.match(zone):
      return None
    return {"id": source_id, "kind": "radar", "zone": zone, "name": name}
  lat, lon = item.get("lat"), item.get("lon")
  for value, limit in ((lat, 90.0), (lon, 180.0)):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > limit:
      return None
  return {"id": source_id, "kind": "radar", "lat": float(lat), "lon": float(lon), "name": name}


def clean_sources(items: Any) -> List[Dict[str, Any]]:
  """The declared sources without duplicates, at most MAX_PANEL_SOURCES."""
  if not isinstance(items, list):
    return []
  found: Dict[str, Dict[str, Any]] = {}
  for item in items:
    source = clean_source(item)
    if source is not None:
      found.setdefault(source["id"], source)
    if len(found) >= MAX_PANEL_SOURCES:
      break
  return [found[key] for key in sorted(found)]


def clean_radar_source(url: Any) -> str:
  """The radar's emergency address from a panel's System page; "" for the default."""
  return url.strip() if url_allowed(url) else ""


class SourceRegistry:
  """Which sources exist, why, and their runtime objects."""

  def __init__(
    self,
    *,
    load: Callable[[], Awaitable[Any]],
    save: Callable[[Dict[str, Any]], Awaitable[None]],
    make_source: Callable[[Dict[str, Any]], Any],
    make_entity: Callable[[Dict[str, Any], Any], Any],
    remove_entity: Callable[[str], Awaitable[None]],
  ) -> None:
    self._load = load
    self._save = save
    self._make_source = make_source
    self._make_entity = make_entity
    self._remove_entity = remove_entity
    self._panels: Dict[str, List[Dict[str, Any]]] = {}
    self._sources: Dict[str, Any] = {}
    self._specs: Dict[str, Dict[str, Any]] = {}
    self._add_entities: Optional[Callable[[Iterable[Any]], None]] = None
    self._listeners: List[Callable[[str], None]] = []
    self.radar_source = ""

  async def async_load(self) -> None:
    data = await self._load()
    if isinstance(data, dict):
      panels = data.get("panels")
      if isinstance(panels, dict):
        self._panels = {str(key): clean_sources(value) for key, value in panels.items()}
      self.radar_source = clean_radar_source(data.get("radar_source"))

  def attach(self, add_entities: Callable[[Iterable[Any]], None]) -> None:
    """The Bridge entry's camera platform is ready: every source gets its entity."""
    self._add_entities = add_entities
    self._sync_runtime()

  def detach(self) -> None:
    self._add_entities = None

  def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
    """Told a source's id whenever its pictures or its state changed."""
    self._listeners.append(listener)

    def remove() -> None:
      if listener in self._listeners:
        self._listeners.remove(listener)
    return remove

  def source(self, source_id: Optional[str]) -> Any:
    return self._sources.get(source_id) if source_id else None

  def source_ids_of(self, device_id: str) -> List[str]:
    """What a panel may use: its own declared sources and the home radar."""
    ids = [spec["id"] for spec in self._panels.get(device_id, [])]
    return [HOME_ID] + [item for item in ids if item != HOME_ID]

  def status_of(self, device_id: str) -> Dict[str, str]:
    """"none" for an address without a picture, for the panel's Web Admin."""
    result: Dict[str, str] = {}
    for spec in self._panels.get(device_id, []):
      if spec["kind"] == "webcam":
        source = self._sources.get(spec["id"])
        result[spec["id"]] = "none" if source is not None and source.failed else "ok"
    return result

  async def async_set_panel(self, device_id: str, sources: List[Dict[str, Any]], radar_source: str = "") -> bool:
    """A panel's complete declaration; True when anything changed."""
    changed = self._panels.get(device_id, []) != sources or self.radar_source != radar_source
    if not changed:
      return False
    self._panels[device_id] = sources
    self.radar_source = radar_source
    await self._async_persist()
    await self._async_apply()
    return True

  async def async_forget_panel(self, device_id: str) -> None:
    if self._panels.pop(device_id, None) is None:
      return
    await self._async_persist()
    await self._async_apply()

  async def async_stop(self) -> None:
    for source in self._sources.values():
      stop = getattr(source, "async_stop", None)
      if stop is not None:
        await stop()
    self._sources = {}
    self._specs = {}

  async def _async_persist(self) -> None:
    await self._save({"panels": self._panels, "radar_source": self.radar_source})

  def _wanted(self) -> Dict[str, Dict[str, Any]]:
    wanted: Dict[str, Dict[str, Any]] = {HOME_ID: {"id": HOME_ID, "kind": "radar"}}
    for specs in self._panels.values():
      for spec in specs:
        wanted.setdefault(spec["id"], spec)
    return wanted

  async def _async_apply(self) -> None:
    wanted = self._wanted()
    for source_id in [key for key in self._sources if key not in wanted]:
      source = self._sources.pop(source_id)
      self._specs.pop(source_id, None)
      stop = getattr(source, "async_stop", None)
      if stop is not None:
        await stop()
      remove_files = getattr(source, "async_remove_files", None)
      if remove_files is not None:
        await remove_files()
      await self._remove_entity(source_id)
    self._sync_runtime()

  def _sync_runtime(self) -> None:
    """A source and its entity for everything wanted that does not run yet."""
    if self._add_entities is None:
      return
    entities = []
    for source_id, spec in self._wanted().items():
      if source_id in self._sources:
        continue
      source = self._make_source(spec)
      self._sources[source_id] = source
      self._specs[source_id] = spec
      source.add_listener(lambda sid=source_id: self._tell(sid))
      start = getattr(source, "start", None)
      if start is not None:
        start()
      entities.append(self._make_entity(spec, source))
    if entities:
      self._add_entities(entities)

  def _tell(self, source_id: str) -> None:
    for listener in list(self._listeners):
      listener(source_id)


def coordinates_id(lat: float, lon: float) -> str:
  """The object id of a place by coordinates; the panel builds the same."""
  def part(value: float) -> str:
    number = round(value * 100)
    return f"m{-number}" if number < 0 else str(number)
  return f"ht_radar_{part(lat)}_{part(lon)}"


def parse_place_answer(data: Optional[bytes]) -> List[Tuple[str, str, str, float, float]]:
  """(name, region, country, lat, lon) of a place search's answer."""
  import json

  try:
    results = json.loads(data).get("results") if data else None
  except (TypeError, ValueError, AttributeError):
    return []
  found = []
  for item in results if isinstance(results, list) else []:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
      continue
    lat, lon = item.get("latitude"), item.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
      continue
    found.append((item["name"], str(item.get("admin1") or ""), str(item.get("country") or ""), float(lat), float(lon)))
  return found
