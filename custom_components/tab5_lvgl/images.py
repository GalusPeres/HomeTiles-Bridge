"""Pictures for linked panels (docs-dev/images.md in the firmware repository).

A panel subscribes to the picture it shows in the size it shows it,
``<ha_prefix>/<domain>/<object_id>/image/<w>x<h>``: a media player's cover,
an image entity, or a camera's still image (the screensaver picture). While
at least one linked panel holds such a subscription and the Bridge serves
that entity to it, this service loads the picture, cuts it to exactly w x h
(centred, filling) and publishes it retained on that topic:

  b"HTIMG1 <key> <w>x<h>\\n" + JPEG

A cover's ``key`` (media_artwork.image_key) is also the media state's
"image_key", so a panel shows a cover only with the song it belongs to.
Pictures above the normal message size reach the panel as a stream
(link_server.py), so the budget of a topic is the smallest room its panels
announced.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Set, Tuple

from .link_protocol import MAX_PAYLOAD

_LOGGER = logging.getLogger(__name__)

IMAGE_MAGIC = b"HTIMG1"
MIN_EDGE = 16
MAX_EDGE = 1280
# Never more than this per picture, whatever a panel announced.
MAX_PICTURE_BYTES = 512 * 1024
SOURCE_CACHE_MAX = 6
JPEG_QUALITIES = (88, 82, 76, 70, 62, 54, 46)
# A camera's still image is loaded at most this often while a panel shows it.
CAMERA_REFRESH_S = 10.0

_IMAGE_TOPIC = re.compile(
  r"(?P<prefix>.+)/(?P<domain>media_player|image|camera)/(?P<object_id>[a-z0-9_]+)/image/"
  r"(?P<w>[1-9][0-9]{1,3})x(?P<h>[1-9][0-9]{1,3})\Z"
)


def parse_image_topic(topic: str) -> Optional[Tuple[str, int, int]]:
  """(entity id, width, height) of a picture topic, or None."""
  match = _IMAGE_TOPIC.match(topic)
  if match is None:
    return None
  width, height = int(match["w"]), int(match["h"])
  if not (MIN_EDGE <= width <= MAX_EDGE and MIN_EDGE <= height <= MAX_EDGE):
    return None
  return f"{match['domain']}.{match['object_id']}", width, height


def build_payload(key: str, width: int, height: int, jpeg: bytes) -> bytes:
  return IMAGE_MAGIC + f" {key} {width}x{height}\n".encode("ascii") + jpeg


def content_key(data: bytes) -> str:
  """The key of a picture known only by its bytes (a camera's still image)."""
  return hashlib.sha256(data).hexdigest()[:16]


def render_jpeg(data: bytes, width: int, height: int, max_bytes: int) -> Optional[bytes]:
  """The source picture cut to fill width x height, as a baseline JPEG
  within max_bytes (blocking: run it in an executor)."""
  try:
    from PIL import Image, ImageFile, ImageOps
  except Exception as err:  # pragma: no cover - Pillow ships with Home Assistant
    _LOGGER.debug("HomeTiles pictures: Pillow not available (%s)", err)
    return None
  # Home Assistant's media proxy at times cuts a JPEG short before its end.
  ImageFile.LOAD_TRUNCATED_IMAGES = True
  try:
    with Image.open(BytesIO(data)) as source:
      picture = ImageOps.exif_transpose(source)
      if picture.mode in ("RGBA", "LA") or (picture.mode == "P" and "transparency" in picture.info):
        background = Image.new("RGB", picture.size, (0, 0, 0))
        rgba = picture.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        picture = background
      else:
        picture = picture.convert("RGB")
      picture = ImageOps.fit(picture, (width, height), method=Image.LANCZOS, centering=(0.5, 0.5))
      for quality in JPEG_QUALITIES:
        output = BytesIO()
        picture.save(output, format="JPEG", quality=quality, optimize=False, progressive=False,
                     subsampling=2)
        jpeg = output.getvalue()
        if len(jpeg) <= max_bytes:
          return jpeg
  except Exception as err:
    _LOGGER.debug("HomeTiles pictures: could not render %sx%s (%s)", width, height, err)
  return None


@dataclass
class Source:
  """What a picture topic shows now.

  key: names the picture before it is loaded (a cover's artwork, an image
  entity's state); None when only the bytes tell (a camera), which are then
  loaded every `refresh` seconds while a panel subscribes.
  """

  key: Optional[str]
  load: Callable[[], Awaitable[Optional[bytes]]]
  refresh: float = 0.0


class ImageService:
  """Renders the pictures linked panels subscribe to."""

  def __init__(
    self,
    broker: Any,
    *,
    source: Callable[[str], Optional[Source]],
    render: Callable[[bytes, int, int, int], Awaitable[Optional[bytes]]],
    allowed: Callable[[Any, str], bool],
    track: Callable[[Iterable[str], Callable[[str], None]], Callable[[], None]],
  ) -> None:
    self._broker = broker
    self._source = source
    self._render = render
    self._allowed = allowed
    self._track = track
    self._untrack: Optional[Callable[[], None]] = None
    self._tracked: frozenset = frozenset()
    self._wants: Dict[str, Set[Any]] = {}
    self._published: Dict[str, str] = {}
    self._tasks: Dict[str, asyncio.Task] = {}
    self._refreshers: Dict[str, asyncio.Task] = {}
    self._again: Set[str] = set()
    self._sources: "OrderedDict[str, bytes]" = OrderedDict()
    self._stop = broker.add_subscription_listener(self._on_subscription)

  def stop(self) -> None:
    self._stop()
    if self._untrack is not None:
      self._untrack()
      self._untrack = None
    for task in list(self._tasks.values()) + list(self._refreshers.values()):
      task.cancel()
    self._tasks.clear()
    self._refreshers.clear()

  def wanted(self) -> Dict[str, Set[Any]]:
    return {topic: set(sessions) for topic, sessions in self._wants.items()}

  def entity_changed(self, entity_id: str) -> None:
    """An entity's state changed: its pictures are rendered when new."""
    for topic in list(self._wants):
      parsed = parse_image_topic(topic)
      if parsed and parsed[0] == entity_id:
        self._schedule(topic)

  def _on_subscription(self, session: Any, topic: str, subscribed: bool) -> None:
    parsed = parse_image_topic(topic)
    if parsed is None:
      return
    if not subscribed:
      sessions = self._wants.get(topic)
      if sessions is not None:
        sessions.discard(session)
        if not sessions:
          del self._wants[topic]
          refresher = self._refreshers.pop(topic, None)
          if refresher is not None:
            refresher.cancel()
          self._retrack()
      return
    if not self._allowed(session, parsed[0]):
      return
    self._wants.setdefault(topic, set()).add(session)
    self._retrack()
    self._schedule(topic)

  def _retrack(self) -> None:
    """Follow the state changes of the entities whose pictures are wanted."""
    entities = frozenset(parsed[0] for parsed in map(parse_image_topic, self._wants) if parsed)
    if entities == self._tracked:
      return
    if self._untrack is not None:
      self._untrack()
      self._untrack = None
    self._tracked = entities
    if entities:
      self._untrack = self._track(sorted(entities), self.entity_changed)

  def _budget(self, topic: str) -> int:
    rooms = [getattr(session, "rx_max", 0) or MAX_PAYLOAD for session in self._wants.get(topic, ())]
    return min([MAX_PICTURE_BYTES] + rooms)

  def _schedule(self, topic: str) -> None:
    task = self._tasks.get(topic)
    if task is not None and not task.done():
      self._again.add(topic)
      return
    self._tasks[topic] = asyncio.get_running_loop().create_task(self._run(topic))

  async def _run(self, topic: str) -> None:
    try:
      while topic in self._wants:
        await self._render_topic(topic)
        if topic not in self._again:
          break
        self._again.discard(topic)
    finally:
      if self._tasks.get(topic) is asyncio.current_task():
        del self._tasks[topic]

  async def _refresh(self, topic: str, every: float) -> None:
    try:
      while topic in self._wants:
        await asyncio.sleep(every)
        if topic in self._wants:
          self._schedule(topic)
    finally:
      if self._refreshers.get(topic) is asyncio.current_task():
        del self._refreshers[topic]

  async def _load(self, source: Source) -> Optional[bytes]:
    if source.key is None:
      return await source.load()
    data = self._sources.get(source.key)
    if data is not None:
      self._sources.move_to_end(source.key)
      return data
    data = await source.load()
    if data:
      self._sources[source.key] = data
      while len(self._sources) > SOURCE_CACHE_MAX:
        self._sources.popitem(last=False)
    return data

  async def _render_topic(self, topic: str) -> None:
    parsed = parse_image_topic(topic)
    if parsed is None:
      return
    entity_id, width, height = parsed
    source = self._source(entity_id)
    if source is None:
      return
    if source.refresh > 0 and topic not in self._refreshers:
      self._refreshers[topic] = asyncio.get_running_loop().create_task(self._refresh(topic, source.refresh))
    if source.key is not None and self._published.get(topic) == source.key:
      return
    data = await self._load(source)
    if not data:
      return
    key = source.key or content_key(data)
    if self._published.get(topic) == key:
      return
    budget = self._budget(topic) - len(build_payload(key, width, height, b""))
    jpeg = await self._render(data, width, height, budget)
    if not jpeg:
      _LOGGER.debug("HomeTiles pictures: nothing to send for %s", topic)
      return
    if topic not in self._wants:
      return
    if source.key is not None:
      current = self._source(entity_id)
      if current is None or current.key != source.key:
        return  # A newer picture waits for its own turn.
    self._broker.publish(topic, build_payload(key, width, height, jpeg), retain=True)
    self._published[topic] = key
