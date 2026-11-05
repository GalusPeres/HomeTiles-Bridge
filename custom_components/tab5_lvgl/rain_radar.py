"""The rain radar behind the Bridge's rain radar camera (camera.py).

Nobody configures anything: the picture is drawn for Home Assistant's home
location and loaded only while somebody shows it. Sources are data and are
tried in order, so one that fails falls over to the next by itself:

- "list": a frame list like RainViewer's, worldwide and current;
- "wms": the OGC Web Map Service standard. Another service of this kind needs
  one more entry in SOURCES and no code.

When every source fails the last picture stays, with its time in the picture.
Only the home location rounded to 0.1 degrees (about 10 km) leaves the Bridge;
the marker on the map uses the exact one.

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
from typing import Any, Awaitable, Callable, List, NamedTuple, Optional, Tuple
from urllib.parse import urlencode

_LOGGER = logging.getLogger(__name__)

DATA_RAIN_RADAR = "rain_radar"
UNIQUE_ID_SUFFIX = "_rain_radar"
MAP_FILE = Path(__file__).with_name("rain_radar_map.json.gz")
FONT_FILE = Path(__file__).with_name("rain_radar_font.ttf")

# The picture. Panels show it through the camera paths, which scale it.
PICTURE_WIDTH = 1024
PICTURE_HEIGHT = 576
JPEG_QUALITY = 85
# Web Mercator zoom of RainViewer's 512 pixel images (its maximum is 7): one
# image is about 430 km wide in central Europe.
ZOOM = 6
SOURCE_SIZE = 512
SCALE = PICTURE_WIDTH / SOURCE_SIZE
WORLD_METERS = 40075016.68557849
COORDINATE_DECIMALS = 1

# How often a shown radar asks for a newer frame, and after a failed round.
REFRESH_S = 300.0
RETRY_S = 60.0
# A first picture is worth waiting for, within the callers' 10 s timeouts.
FIRST_WAIT_S = 8.0
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


SOURCES: Tuple[Source, ...] = (
  Source("rainviewer", "list", "https://api.rainviewer.com/public/weather-maps.json", "RainViewer"),
  Source("dwd", "wms", "https://maps.dwd.de/geoserver/dwd/Radar_rv_product_1x1km_ger/ows", "DWD",
         layer="Radar_rv_product_1x1km_ger", area=(5.5, 47.0, 15.5, 55.2)),
  # Worldwide and run by a public agency, but several hours behind: the last
  # resort, with its time in the picture.
  Source("nasa", "wms", "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi", "NASA",
         layer="IMERG_Precipitation_Rate_30min"),
)


def rain_radar_unique_id(device_id: str) -> str:
  return f"{device_id}{UNIQUE_ID_SUFFIX}"


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


def parse_frame_list(data: bytes) -> Optional[Tuple[int, str]]:
  """(time, address prefix) of the newest frame of a RainViewer list."""
  try:
    parsed = json.loads(data)
    host, frames = parsed["host"], parsed["radar"]["past"]
    newest = frames[-1]
    when, path = newest["time"], newest["path"]
  except (KeyError, IndexError, TypeError, ValueError):
    return None
  if isinstance(when, bool) or not isinstance(when, int) or when <= 0:
    return None
  if not isinstance(host, str) or not host.startswith("https://") or "/" in host[len("https://"):]:
    return None
  if not isinstance(path, str) or not _RADAR_PATH.match(path):
    return None
  return when, f"{host}{path}"


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


def render_picture(layers: Tuple[bytes, bytes], overlay: bytes, stamp: str) -> bytes:
  """Map, rain, names and mark, and the frame's time with its source."""
  from PIL import Image, ImageDraw

  with Image.open(BytesIO(layers[0])) as base_file:
    picture = base_file.convert("RGBA")
  picture.alpha_composite(_overlay_image(overlay))
  with Image.open(BytesIO(layers[1])) as top_file:
    picture.alpha_composite(top_file.convert("RGBA"))
  # Bottom centre: a tile that fills its shape cuts the sides, not this.
  ImageDraw.Draw(picture).text((PICTURE_WIDTH / 2, PICTURE_HEIGHT - 8), stamp, font=_font(STAMP_PX), fill=TEXT,
                               anchor="ms", stroke_width=2, stroke_fill=OUTLINE)
  output = BytesIO()
  picture.convert("RGB").save(output, format="JPEG", quality=JPEG_QUALITY, optimize=False, progressive=False,
                              subsampling=2)
  return output.getvalue()


# ---- The service ----


@dataclass
class Picture:
  jpeg: bytes
  source: str
  credit: str
  when: int
  view: View


class RainRadar:
  """The newest picture for Home Assistant's location, loaded on demand."""

  def __init__(
    self,
    *,
    fetch: Callable[[str, int], Awaitable[Optional[bytes]]],
    location: Callable[[], Tuple[float, float]],
    language: Callable[[], str],
    format_time: Callable[[int], str],
    run_blocking: Callable[..., Awaitable[Any]],
    clock: Callable[[], float] = time.time,
    sources: Tuple[Source, ...] = SOURCES,
    map_file: Path = MAP_FILE,
  ) -> None:
    self._fetch = fetch
    self._location = location
    self._language = language
    self._format_time = format_time
    self._run_blocking = run_blocking
    self._clock = clock
    self._sources = sources
    self._map_file = map_file
    self._picture: Optional[Picture] = None
    self._layers: Optional[Tuple[Tuple[View, str], Tuple[bytes, bytes]]] = None
    self._task: Optional[asyncio.Task] = None
    self._checked_at: Optional[float] = None
    self._failed = False
    self._listeners: List[Callable[[], None]] = []

  @property
  def picture(self) -> Optional[Picture]:
    return self._picture

  def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
    """Told about every new picture; returns the function that ends it."""
    self._listeners.append(listener)

    def remove() -> None:
      if listener in self._listeners:
        self._listeners.remove(listener)
    return remove

  async def async_picture(self) -> Optional[bytes]:
    """The newest JPEG. A due refresh runs behind it; only the very first
    picture is waited for."""
    self._refresh_if_due()
    task = self._task
    if self._picture is None and task is not None:
      try:
        await asyncio.wait_for(asyncio.shield(task), FIRST_WAIT_S)
      except asyncio.TimeoutError:
        pass
    return self._picture.jpeg if self._picture is not None else None

  def _refresh_if_due(self) -> None:
    if self._task is not None and not self._task.done():
      return
    now = self._clock()
    if self._checked_at is not None and now - self._checked_at < (RETRY_S if self._failed else REFRESH_S):
      return
    self._checked_at = now
    self._task = asyncio.get_running_loop().create_task(self._async_refresh())

  async def _async_refresh(self) -> None:
    lat, lon = self._location()
    view = make_view(lat, lon)
    self._failed = True
    for source in self._sources:
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

  async def _async_try(self, source: Source, view: View) -> bool:
    frame = await self._async_newest(source, view)
    if frame is None:
      _LOGGER.info("HomeTiles rain radar: source %s has no picture now", source.id)
      return False
    when, image_url = frame
    current = self._picture
    if current is not None and current.source == source.id and current.when == when and current.view == view:
      return True  # Nothing new since the last look.
    overlay = await self._fetch(image_url, IMAGE_MAX_BYTES)
    if not overlay:
      _LOGGER.info("HomeTiles rain radar: source %s did not deliver its picture", source.id)
      return False
    layers = await self._async_layers(view)
    stamp = f"{self._format_time(when)}  {source.credit}"
    jpeg = await self._run_blocking(render_picture, layers, overlay, stamp)
    if current is None or current.source != source.id:
      _LOGGER.info("HomeTiles rain radar: showing %s", source.id)
    self._picture = Picture(jpeg, source.id, source.credit, when, view)
    for listener in list(self._listeners):
      listener()
    return True

  async def _async_newest(self, source: Source, view: View) -> Optional[Tuple[int, str]]:
    """(time, picture address) of the source's newest frame."""
    if source.kind == "list":
      data = await self._fetch(source.url, LIST_MAX_BYTES)
      parsed = parse_frame_list(data) if data else None
      return (parsed[0], frame_image_url(parsed[1], view)) if parsed else None
    data = await self._fetch(capabilities_url(source), CAPABILITIES_MAX_BYTES)
    parsed = parse_wms_time(data, source.layer) if data else None
    return (parsed[1], wms_image_url(source, view, parsed[0])) if parsed else None

  async def _async_layers(self, view: View) -> Tuple[bytes, bytes]:
    key = (view, self._language())
    if self._layers is None or self._layers[0] != key:
      self._layers = (key, await self._run_blocking(draw_layers, view, key[1], self._map_file))
    return self._layers[1]
