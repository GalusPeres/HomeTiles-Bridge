"""The rain radar behind the Bridge's rain radar camera (camera.py).

Nobody configures anything: the picture is drawn for a place (Home Assistant's
home, a zone or coordinates). While a panel uses the place the Bridge collects its
frames by itself; otherwise only when somebody shows it. Sources are data and are
tried in order, so one that fails falls over to the next by itself:

- "list": a frame list like RainViewer's, worldwide and current;
- "wms": the OGC Web Map Service standard. Another service of this kind needs
  one more entry in SOURCES and no code.

A list source brings its history. The frames are kept like every Bridge camera's
pictures (picture_store.py): on the disk, at their fixed times, for a day, also
after the source no longer lists them. The panel gets their times with the
camera's "ready" and picks one by its time (camera_stream.py shows it instead of
the newest). The panel itself stores no frames.

Each place has its own RainRadar (camera_sources.py). When every source fails the
last picture stays. Only the location rounded to 0.1 degrees (about 10 km) leaves
the Bridge; the marker on the map uses the exact one. The picture itself names only
its source, which the sources ask for; the panel shows the time around it.

The map is Natural Earth (public domain, rain_radar_map.json.gz, built by
tools/build_rain_radar_map.py), so no map server is involved. Nothing here
imports Home Assistant; __init__.py hands in fetching and the blocking runner.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from .picture_store import KEEP_S, FrameStore, slot_of

_LOGGER = logging.getLogger(__name__)

# Bridge betas up to v0.9.8b4 had one radar entity per panel (migrated away).
UNIQUE_ID_SUFFIX = "_rain_radar"
MAP_FILE = Path(__file__).with_name("rain_radar_map.json.gz")
FONT_FILE = Path(__file__).with_name("rain_radar_font.ttf")

# The picture, in the shape of the panel's popup with its bottom row (752 x 538 on
# 1280 x 800). Panels show it through the camera paths, which scale it.
PICTURE_WIDTH = 1024
PICTURE_HEIGHT = 732
JPEG_QUALITY = 85
# Web Mercator zoom of RainViewer's 512 pixel images (its maximum is 7): one
# image is about 430 km wide in central Europe.
ZOOM = 6
SOURCE_SIZE = 512
SCALE = PICTURE_WIDTH / SOURCE_SIZE
WORLD_METERS = 40075016.68557849
COORDINATE_DECIMALS = 1

# The frames taken from one list. RainViewer lists 13, one every 10 minutes;
# picture_store.py keeps a day of them.
MAX_FRAMES = 16

# A list source publishes on a fixed step, so it is asked only when a new frame is
# due, then every minute until it came. Others every few minutes, and after a
# failed round every minute.
REFRESH_S = 300.0
DUE_CHECK_S = 60.0
RETRY_S = 60.0
# A first picture is worth waiting for, within the callers' 10 s timeouts.
FIRST_WAIT_S = 8.0
FILL_TELL_EVERY = 3
LIST_MAX_BYTES = 256 * 1024
IMAGE_MAX_BYTES = 2 * 1024 * 1024
CAPABILITIES_MAX_BYTES = 8 * 1024 * 1024

OVERLAY_OPACITY = 0.85
SEA = (17, 24, 28)
LAND = (42, 44, 45)
BORDER = (93, 97, 99)
TEXT = (255, 255, 255)
OUTLINE = (0, 0, 0)
LABEL_PX = 22
STAMP_PX = 24
MAX_LABELS = 16
HOME_RADIUS = 9


class Source(NamedTuple):
  id: str
  kind: str
  url: str
  credit: str
  layer: str = ""
  # (west, south, east, north) when the source does not cover the world.
  area: Optional[Tuple[float, float, float, float]] = None
  # Seconds between its frames when it publishes on a fixed step.
  step_s: float = 0.0


SOURCES: Tuple[Source, ...] = (
  Source("rainviewer", "list", "https://api.rainviewer.com/public/weather-maps.json", "RainViewer", step_s=600.0),
  Source("dwd", "wms", "https://maps.dwd.de/geoserver/dwd/Radar_rv_product_1x1km_ger/ows", "DWD",
         layer="Radar_rv_product_1x1km_ger", area=(5.5, 47.0, 15.5, 55.2)),
  # Worldwide and run by a public agency, but several hours behind: the last
  # resort, with its time in the picture.
  Source("nasa", "wms", "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi", "NASA",
         layer="IMERG_Precipitation_Rate_30min"),
)


def custom_source(url: str) -> Source:
  """The emergency address from a panel's System page: a list like RainViewer's."""
  return Source("custom", "list", url, urlsplit(url).hostname or "Radar", step_s=600.0)


def is_rain_radar_unique_id(unique_id: Optional[str]) -> bool:
  return bool(unique_id) and str(unique_id).endswith(UNIQUE_ID_SUFFIX)


class View(NamedTuple):
  """The centre sent to the sources (rounded) and the exact home marker."""

  lat: float
  lon: float
  home_lat: float
  home_lon: float


def make_view(lat: float, lon: float) -> View:
  lat = max(-85.0, min(85.0, float(lat)))
  lon = max(-180.0, min(180.0, float(lon)))
  return View(round(lat, COORDINATE_DECIMALS), round(lon, COORDINATE_DECIMALS), lat, lon)


def _world_px() -> float:
  return SOURCE_SIZE * (2 ** ZOOM) * SCALE


def _mercator(lat: float, lon: float) -> Tuple[float, float]:
  """World pixels of a coordinate."""
  world = _world_px()
  sine = math.sin(max(-85.0, min(85.0, lat)) * math.pi / 180.0)
  return (lon + 180.0) / 360.0 * world, (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * world


def project(view: View, lat: float, lon: float) -> Tuple[float, float]:
  """Picture pixels of a coordinate."""
  x, y = _mercator(lat, lon)
  cx, cy = _mercator(view.lat, view.lon)
  return x - cx + PICTURE_WIDTH / 2, y - cy + PICTURE_HEIGHT / 2


def bbox_meters(view: View) -> Tuple[float, float, float, float]:
  """The picture's area in EPSG:3857: west, south, east, north."""
  per_px = WORLD_METERS / _world_px()
  cx, cy = _mercator(view.lat, view.lon)
  world = _world_px()
  mx, my = (cx - world / 2) * per_px, (world / 2 - cy) * per_px
  half_w, half_h = PICTURE_WIDTH / 2 * per_px, PICTURE_HEIGHT / 2 * per_px
  return mx - half_w, my - half_h, mx + half_w, my + half_h


def covers(source: Source, view: View) -> bool:
  if source.area is None:
    return True
  west, south, east, north = source.area
  return west <= view.home_lon <= east and south <= view.home_lat <= north


# ---- Reading the sources ----

_RADAR_PATH = re.compile(r"/v2/radar/[0-9A-Za-z_]+\Z")


def parse_frame_list(data: bytes) -> Optional[List[Tuple[int, str]]]:
  """[(time, address prefix)] of a RainViewer list, oldest first and at most
  MAX_FRAMES; None without a usable newest frame. A bad older entry is left out."""
  try:
    parsed = json.loads(data)
    host, listed = parsed["host"], parsed["radar"]["past"]
  except (KeyError, IndexError, TypeError, ValueError):
    return None
  if not isinstance(host, str) or not host.startswith("https://") or "/" in host[len("https://"):]:
    return None
  if not isinstance(listed, list) or not listed:
    return None
  frames: List[Tuple[int, str]] = []
  for entry in listed:
    when = entry.get("time") if isinstance(entry, dict) else None
    path = entry.get("path") if isinstance(entry, dict) else None
    valid = (not isinstance(when, bool) and isinstance(when, int) and when > 0
             and isinstance(path, str) and _RADAR_PATH.match(path) is not None)
    if valid:
      frames.append((when, f"{host}{path}"))
    elif entry is listed[-1]:
      return None
  return sorted(frames)[-MAX_FRAMES:] or None


def frame_image_url(prefix: str, view: View) -> str:
  """One image centred on the rounded location: 512 pixels, the universal
  blue colour scheme (the only one left to free use), smoothed, with snow."""
  return f"{prefix}/{SOURCE_SIZE}/{ZOOM}/{view.lat:.{COORDINATE_DECIMALS}f}/{view.lon:.{COORDINATE_DECIMALS}f}/2/1_1.png"


def capabilities_url(source: Source) -> str:
  return f"{source.url}?{urlencode({'service': 'WMS', 'version': '1.3.0', 'request': 'GetCapabilities'})}"


def _dimension_default(block: str, name: str) -> Optional[str]:
  for tag in re.findall(r"<Dimension\b[^>]*>", block):
    if re.search(rf'\bname="{name}"', tag, re.IGNORECASE):
      found = re.search(r'\bdefault="([^"]+)"', tag)
      if found:
        return found.group(1)
  return None


def _epoch(text: str) -> Optional[int]:
  try:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
  except ValueError:
    return None


def parse_wms_time(data: bytes, layer: str) -> Optional[Tuple[Optional[str], int]]:
  """(TIME value, the frame's time) of a layer's newest picture. A layer whose
  default time is "current" takes no TIME and tells its age by REFERENCE_TIME."""
  text = data.decode("utf-8", "replace")
  found = re.search(rf"<Name>(?:[^<:]*:)?{re.escape(layer)}</Name>", text)
  if found is None:
    return None
  end = text.find("</Layer>", found.end())
  block = text[found.end():end if end > 0 else len(text)]
  default = _dimension_default(block, "time")
  if default is None:
    return None
  if default.lower() == "current":
    reference = _dimension_default(block, "reference_time")
    when = _epoch(reference) if reference else None
    return (None, when) if when else None
  when = _epoch(default)
  return (default, when) if when else None


def wms_image_url(source: Source, view: View, time_value: Optional[str]) -> str:
  west, south, east, north = bbox_meters(view)
  query = {
    "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": source.layer, "STYLES": "",
    "CRS": "EPSG:3857", "BBOX": f"{west:.0f},{south:.0f},{east:.0f},{north:.0f}",
    "WIDTH": PICTURE_WIDTH, "HEIGHT": PICTURE_HEIGHT, "FORMAT": "image/png", "TRANSPARENT": "true",
  }
  if time_value:
    query["TIME"] = time_value
  return f"{source.url}?{urlencode(query)}"


# ---- Drawing (blocking: run_blocking) ----


def _font(size: int) -> Any:
  from PIL import ImageFont

  # Pillow's own default font has no umlauts ("M?nchen").
  return ImageFont.truetype(str(FONT_FILE), size)


def _png(image: Any) -> bytes:
  output = BytesIO()
  image.save(output, format="PNG")
  return output.getvalue()


def _points(flat: List[int], view: View) -> List[Tuple[float, float]]:
  points = []
  x, y = 0, 0
  for index in range(0, len(flat), 2):
    x, y = (flat[0], flat[1]) if index == 0 else (x + flat[index], y + flat[index + 1])
    points.append(project(view, y / 100.0, x / 100.0))
  return points


def _on_picture(points: List[Tuple[float, float]]) -> bool:
  return (max(p[0] for p in points) >= 0 and min(p[0] for p in points) <= PICTURE_WIDTH
          and max(p[1] for p in points) >= 0 and min(p[1] for p in points) <= PICTURE_HEIGHT)


def draw_layers(view: View, language: str, map_file: Path = MAP_FILE) -> Tuple[bytes, bytes]:
  """The map under the rain and, above it, the place names and the home mark."""
  from PIL import Image, ImageDraw

  data = json.loads(gzip.decompress(map_file.read_bytes()))
  base = Image.new("RGB", (PICTURE_WIDTH, PICTURE_HEIGHT), SEA)
  draw = ImageDraw.Draw(base)
  for ring in data["land"]:
    points = _points(ring, view)
    if _on_picture(points):
      draw.polygon(points, fill=LAND)
  for line in data["borders"]:
    points = _points(line, view)
    if _on_picture(points):
      draw.line(points, fill=BORDER, width=2)

  top = Image.new("RGBA", (PICTURE_WIDTH, PICTURE_HEIGHT), (0, 0, 0, 0))
  draw = ImageDraw.Draw(top)
  font = _font(LABEL_PX)
  home_x, home_y = project(view, view.home_lat, view.home_lon)
  taken = [(home_x - 24, home_y - 24, home_x + 24, home_y + 24),
           (PICTURE_WIDTH / 2 - 180, PICTURE_HEIGHT - 44, PICTURE_WIDTH / 2 + 180, PICTURE_HEIGHT)]
  short = language.split("-")[0].lower()
  labels = 0
  for lon, lat, _rank, name, names in data["places"]:
    if labels >= MAX_LABELS:
      break
    x, y = project(view, lat / 100.0, lon / 100.0)
    text = names.get(short, name)
    left, upper, right, lower = draw.textbbox((x + 10, y), text, font=font, anchor="lm", stroke_width=2)
    if left < 4 or upper < 4 or right > PICTURE_WIDTH - 4 or lower > PICTURE_HEIGHT - 4 or x < 6:
      continue
    box = (x - 6, upper - 2, right + 2, lower + 2)
    if any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in taken):
      continue
    taken.append(box)
    draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=TEXT, outline=OUTLINE)
    draw.text((x + 10, y), text, font=font, fill=TEXT, anchor="lm", stroke_width=2, stroke_fill=OUTLINE)
    labels += 1
  draw.ellipse((home_x - HOME_RADIUS, home_y - HOME_RADIUS, home_x + HOME_RADIUS, home_y + HOME_RADIUS),
               fill=TEXT, outline=OUTLINE, width=3)
  return _png(base), _png(top)


def _overlay_image(data: bytes) -> Any:
  """The source's rain on the picture's size, whatever size it came in."""
  from PIL import Image

  with Image.open(BytesIO(data)) as source:
    rain = source.convert("RGBA")
  if rain.size != (PICTURE_WIDTH, PICTURE_HEIGHT):
    # A square image: the picture's middle band, enlarged.
    width, height = rain.size
    band = width * PICTURE_HEIGHT / PICTURE_WIDTH
    top = (height - band) / 2
    rain = rain.resize((PICTURE_WIDTH, PICTURE_HEIGHT), Image.BICUBIC, box=(0, top, width, top + band))
  alpha = rain.getchannel("A").point(lambda value: int(value * OVERLAY_OPACITY))
  rain.putalpha(alpha)
  return rain


def stamp_time(jpeg: bytes, text: str) -> bytes:
  """The frame's time in the picture, for a window without a time bar."""
  from PIL import Image, ImageDraw

  with Image.open(BytesIO(jpeg)) as source:
    picture = source.convert("RGB")
  ImageDraw.Draw(picture).text((16, PICTURE_HEIGHT - 8), text, font=_font(STAMP_PX), fill=TEXT, anchor="ls",
                               stroke_width=2, stroke_fill=OUTLINE)
  output = BytesIO()
  picture.save(output, format="JPEG", quality=JPEG_QUALITY, subsampling=2)
  return output.getvalue()


def render_picture(layers: Tuple[bytes, bytes], overlay: bytes, stamp: str) -> bytes:
  """Map, rain, names and mark, and the frame's source."""
  from PIL import Image, ImageDraw

  with Image.open(BytesIO(layers[0])) as base_file:
    picture = base_file.convert("RGBA")
  picture.alpha_composite(_overlay_image(overlay))
  with Image.open(BytesIO(layers[1])) as top_file:
    picture.alpha_composite(top_file.convert("RGBA"))
  # The source's name, bottom centre: a tile that fills its shape cuts the sides,
  # not this.
  ImageDraw.Draw(picture).text((PICTURE_WIDTH / 2, PICTURE_HEIGHT - 8), stamp, font=_font(STAMP_PX), fill=TEXT,
                               anchor="ms", stroke_width=2, stroke_fill=OUTLINE)
  output = BytesIO()
  picture.convert("RGB").save(output, format="JPEG", quality=JPEG_QUALITY, optimize=False, progressive=False,
                              subsampling=2)
  return output.getvalue()


# ---- The service ----


@dataclass
class Picture:
  """The newest frame: where it came from and when."""

  source: str
  credit: str
  when: int
  view: View


def place_key(view: View) -> str:
  return f"{view.lat:.{COORDINATE_DECIMALS}f},{view.lon:.{COORDINATE_DECIMALS}f}"


def series_key(source: Source, view: View) -> str:
  """The pictures of one source for one place; another starts a new series."""
  return f"{source.id}@{place_key(view)}"


class RainRadar:
  """The frames for one place, newest last, kept like every Bridge camera's pictures
  (picture_store.py). While a panel uses the place it collects by itself (start);
  otherwise only when somebody asks."""

  def __init__(
    self,
    *,
    fetch: Callable[[str, int], Awaitable[Optional[bytes]]],
    location: Callable[[], Optional[Tuple[float, float]]],
    language: Callable[[], str],
    format_time: Callable[[int], str],
    run_blocking: Callable[..., Awaitable[Any]],
    directory: Path,
    clock: Callable[[], float] = time.time,
    sources: Tuple[Source, ...] = SOURCES,
    map_file: Path = MAP_FILE,
    first_sources: Callable[[], Tuple[Source, ...]] = tuple,
  ) -> None:
    self._fetch = fetch
    self._location = location
    self._language = language
    self._format_time = format_time
    self._run_blocking = run_blocking
    self._first_sources = first_sources
    self._clock = clock
    self._sources = sources
    self._map_file = map_file
    self._store = FrameStore(directory, run_blocking=run_blocking, clock=clock)
    self._view: Optional[View] = None
    self._layers: Optional[Tuple[Tuple[View, str], Tuple[bytes, bytes]]] = None
    self._task: Optional[asyncio.Task] = None
    self._fill_task: Optional[asyncio.Task] = None
    self._run_task: Optional[asyncio.Task] = None
    self._checked_at: Optional[float] = None
    self._failed = False
    self._listeners: List[Callable[[], None]] = []
    self._stamped: Dict[int, bytes] = {}

  @property
  def picture(self) -> Optional[Picture]:
    newest = self._store.newest
    if newest is None or self._view is None:
      return None
    return Picture(newest.tag, self._credit_of(newest.tag), newest.when, self._view)

  def times(self) -> List[int]:
    """The times of the kept frames, oldest first."""
    return self._store.times()

  async def async_frame_at(self, when: int) -> Optional[bytes]:
    await self._async_ready()
    return await self._store.async_frame(when)

  async def async_stamped_frame(self, when: int) -> Optional[bytes]:
    """A frame with its time in the picture (Home Assistant's camera window)."""
    if when not in self._stamped:
      jpeg = await self.async_frame_at(when)
      if jpeg is None:
        return None
      self._stamped = {key: value for key, value in self._stamped.items() if key in self.times()}
      self._stamped[when] = await self._run_blocking(stamp_time, jpeg, self._format_time(when))
    return self._stamped[when]

  def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
    """Told whenever the frames changed; returns the function that ends it."""
    self._listeners.append(listener)

    def remove() -> None:
      if listener in self._listeners:
        self._listeners.remove(listener)
    return remove

  async def async_picture(self) -> Optional[bytes]:
    """The newest JPEG. A due refresh runs behind it; only the very first
    picture is waited for."""
    await self._async_ready()
    self._refresh_if_due()
    task = self._task
    if not self._store.times() and task is not None:
      try:
        await asyncio.wait_for(asyncio.shield(task), FIRST_WAIT_S)
      except asyncio.TimeoutError:
        pass
    newest = self._store.newest
    return await self._store.async_frame(newest.when) if newest else None

  def prepare(self) -> None:
    """Load ahead of being shown; nothing when the frames are fresh."""
    self._refresh_if_due()

  # ---- Collecting while a panel uses the place ----

  def start(self) -> None:
    if self._run_task is None or self._run_task.done():
      self._run_task = asyncio.get_running_loop().create_task(self._async_run())

  async def async_stop(self) -> None:
    """Back to loading only when somebody asks."""
    task, self._run_task = self._run_task, None
    if task is not None:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass

  async def async_remove_files(self) -> None:
    for task in (self._fill_task, self._task):
      if task is not None and not task.done():
        task.cancel()
    await self._store.async_remove_files()

  async def _async_run(self) -> None:
    await self._async_ready()
    if self._store.times():
      self._tell_listeners()
    while True:
      self._refresh_if_due()
      await asyncio.sleep(DUE_CHECK_S)

  # ---- Loading ----

  async def _async_ready(self) -> None:
    """The kept pictures read back; the place they show is known again."""
    await self._store.async_load()
    if self._view is None and self._store.series:
      location = self._location()
      if location is not None:
        view = make_view(location[0], location[1])
        if self._store.series.endswith("@" + place_key(view)):
          self._view = view

  def _refresh_if_due(self) -> None:
    if self._task is not None and not self._task.done():
      return
    now = self._clock()
    if self._checked_at is not None and now < self._next_check():
      return
    self._checked_at = now
    self._task = asyncio.get_running_loop().create_task(self._async_refresh())

  def _next_check(self) -> float:
    if self._failed:
      return self._checked_at + RETRY_S
    picture = self.picture
    source = next((item for item in self._all_sources() if picture and item.id == picture.source), None)
    if source is None or not source.step_s:
      return self._checked_at + REFRESH_S
    return max(picture.when + source.step_s, self._checked_at + DUE_CHECK_S)

  def _all_sources(self) -> Tuple[Source, ...]:
    return tuple(self._first_sources()) + tuple(self._sources)

  def _credit_of(self, source_id: str) -> str:
    source = next((item for item in self._all_sources() if item.id == source_id), None)
    return source.credit if source is not None else source_id

  async def _async_refresh(self) -> None:
    await self._async_ready()
    location = self._location()
    self._failed = True
    if location is None:
      return  # A zone that is gone.
    view = make_view(location[0], location[1])
    for source in self._all_sources():
      if not covers(source, view):
        continue
      try:
        if await self._async_try(source, view):
          self._failed = False
          return
      except asyncio.CancelledError:
        raise
      except Exception as err:  # A broken source must never stop the next one.
        _LOGGER.warning("HomeTiles rain radar: source %s failed (%s)", source.id, err)
    _LOGGER.warning("HomeTiles rain radar: no source answered; keeping the last picture")

  def _tell_listeners(self) -> None:
    for listener in list(self._listeners):
      listener()

  async def _async_try(self, source: Source, view: View) -> bool:
    listing = await self._async_listing(source, view)
    if not listing:
      _LOGGER.info("HomeTiles rain radar: source %s has no picture now", source.id)
      return False
    key = series_key(source, view)
    newest, newest_url = listing[-1]
    if self._store.series != key or self._store.tag_at(slot_of(newest)) is None:
      jpeg = await self._async_render(source, view, newest_url)
      if jpeg is None:
        _LOGGER.info("HomeTiles rain radar: source %s did not deliver its picture", source.id)
        return False
      # Another source or place starts its own pictures; the frames of the one
      # before stay until this picture is there.
      if await self._store.async_series(key):
        _LOGGER.info("HomeTiles rain radar: showing %s", source.id)
        self._stamped = {}
      self._view = view
      await self._store.async_put(newest, source.id, jpeg)
      self._tell_listeners()
    self._view = view
    oldest = self._clock() - KEEP_S
    have = set(self.times())
    missing = [item for item in listing[:-1] if slot_of(item[0]) not in have and item[0] >= oldest]
    if missing and (self._fill_task is None or self._fill_task.done()):
      # Behind the newest picture, so nobody waits for the whole history.
      self._fill_task = asyncio.get_running_loop().create_task(self._async_fill(source, view, missing))
    return True

  async def _async_render(self, source: Source, view: View, url: str) -> Optional[bytes]:
    overlay = await self._fetch(url, IMAGE_MAX_BYTES)
    if not overlay:
      return None
    layers = await self._async_layers(view)
    return await self._run_blocking(render_picture, layers, overlay, source.credit)

  async def _async_fill(self, source: Source, view: View, missing: List[Tuple[int, str]]) -> None:
    """The older frames, one after the other; the next refresh loads what failed."""
    key = series_key(source, view)
    changed = False
    loaded = 0
    try:
      for when, url in reversed(missing):
        if self._store.series != key:
          return  # Another source or place took over meanwhile.
        jpeg = await self._async_render(source, view, url)
        if jpeg is None or self._store.series != key:
          continue
        if not await self._store.async_put(when, source.id, jpeg):
          continue
        changed = True
        loaded += 1
        # The time bar shows soon and grows, not only when all frames are there.
        if loaded % FILL_TELL_EVERY == 0:
          changed = False
          self._tell_listeners()
    except asyncio.CancelledError:
      raise
    except Exception as err:
      _LOGGER.info("HomeTiles rain radar: older frames of %s stopped (%s)", source.id, err)
    finally:
      if changed:
        self._tell_listeners()

  async def _async_listing(self, source: Source, view: View) -> Optional[List[Tuple[int, str]]]:
    """[(time, picture address)] of the source's frames, newest last."""
    if source.kind == "list":
      data = await self._fetch(source.url, LIST_MAX_BYTES)
      parsed = parse_frame_list(data) if data else None
      return [(when, frame_image_url(prefix, view)) for when, prefix in parsed] if parsed else None
    data = await self._fetch(capabilities_url(source), CAPABILITIES_MAX_BYTES)
    newest = parse_wms_time(data, source.layer) if data else None
    return [(newest[1], wms_image_url(source, view, newest[0]))] if newest else None

  async def _async_layers(self, view: View) -> Tuple[bytes, bytes]:
    key = (view, self._language())
    if self._layers is None or self._layers[0] != key:
      self._layers = (key, await self._run_blocking(draw_layers, view, key[1], self._map_file))
    return self._layers[1]
