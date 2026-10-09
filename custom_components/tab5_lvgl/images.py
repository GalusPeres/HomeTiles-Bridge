"""Pictures for linked panels (docs-dev/images.md in the firmware repository).

A panel subscribes to the picture it shows in the size it shows it,
``<ha_prefix>/<domain>/<object_id>/image/<w>x<h>``: a media player's cover,
an image entity, or a camera's still image (the screensaver picture). Two
optional segments follow the size: ``/fit`` (the whole picture, black bars)
or ``/original`` (its own size when smaller, else like fit) instead of
filling w x h; and ``/<n>s``, a camera's still every n seconds (3..60)
instead of every 10. While
at least one linked panel holds such a subscription and the Bridge serves
that entity to it, this service loads the picture, cuts it to exactly w x h
(centred, filling) and publishes it retained on that topic:

  b"HTIMG1 <key> <w>x<h>\\n" + JPEG

A cover's ``key`` (media_artwork.image_key) is also the media state's
"image_key", so a panel shows a cover only with the song it belongs to.
Pictures above the normal message size reach the panel as a stream
(link_server.py), so the budget of a topic is the smallest room its panels
announced.

A subscription to an entity the panel is not served yet is kept: a panel
subscribes a newly chosen picture at once and declares the entity a moment
later, and recheck() then grants it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, Iterable, NamedTuple, Optional, Set

from .link_protocol import MAX_PAYLOAD

_LOGGER = logging.getLogger(__name__)

IMAGE_MAGIC = b"HTIMG1"
MIN_EDGE = 16
MAX_EDGE = 1280
# Never more than this per picture, whatever a panel announced.
MAX_PICTURE_BYTES = 512 * 1024
SOURCE_CACHE_MAX = 6
# Large sources (a full-screen image) are kept only within this total.
SOURCE_CACHE_MAX_BYTES = 8 * 1024 * 1024
NOT_SERVED = "waits: the entity is not served to this panel (released or declared)"
JPEG_QUALITIES = (88, 82, 76, 70, 62, 54, 46)
# A camera's still image is loaded this often while a panel shows it, unless
# its topic asks for another interval within the bounds.
CAMERA_REFRESH_S = 10.0
CAMERA_REFRESH_MIN_S = 3
CAMERA_REFRESH_MAX_S = 60
FILL, FIT, ORIGINAL = "fill", "fit", "original"
# Graphics (a QR code, a vacuum map) have few colours and are enlarged with
# hard edges; photos are resampled smoothly.
GRAPHIC_MAX_COLORS = 256

_IMAGE_TOPIC = re.compile(
  r"(?P<prefix>.+)/(?P<domain>media_player|image|camera)/(?P<object_id>[a-z0-9_]+)/image/"
  r"(?P<w>[1-9][0-9]{1,3})x(?P<h>[1-9][0-9]{1,3})"
  r"(?:/(?P<fit>fit|original))?(?:/(?P<every>[1-9][0-9]?)s)?\Z"
)


class ImageTopic(NamedTuple):
  entity_id: str
  width: int
  height: int
  fit: str = FILL
  every: Optional[int] = None


def parse_image_topic(topic: str) -> Optional[ImageTopic]:
  """The entity, size and options of a picture topic, or None."""
  match = _IMAGE_TOPIC.match(topic)
  if match is None:
    return None
  width, height = int(match["w"]), int(match["h"])
  if not (MIN_EDGE <= width <= MAX_EDGE and MIN_EDGE <= height <= MAX_EDGE):
    return None
  every = int(match["every"]) if match["every"] else None
  if every is not None and not CAMERA_REFRESH_MIN_S <= every <= CAMERA_REFRESH_MAX_S:
    return None
  return ImageTopic(f"{match['domain']}.{match['object_id']}", width, height, match["fit"] or FILL, every)


def build_payload(key: str, width: int, height: int, jpeg: bytes) -> bytes:
  return IMAGE_MAGIC + f" {key} {width}x{height}\n".encode("ascii") + jpeg


def content_key(data: bytes) -> str:
  """The key of a picture known only by its bytes (a camera's still image)."""
  return hashlib.sha256(data).hexdigest()[:16]


def _place(picture: Any, width: int, height: int, fit: str) -> Any:
  """The picture on exactly width x height: filled and cut (fill), as large
  as fits with black bars (fit), or 1:1 in the middle when it is smaller
  (original, else like fit). Graphics are enlarged with hard edges."""
  from PIL import Image, ImageOps

  if fit == FILL:
    scale = max(width / picture.width, height / picture.height)
  else:
    scale = min(width / picture.width, height / picture.height)
    if fit == ORIGINAL:
      scale = min(scale, 1.0)
  resample = Image.LANCZOS
  if scale > 1.0 and picture.getcolors(GRAPHIC_MAX_COLORS) is not None:
    resample = Image.NEAREST
  if fit == FILL:
    return ImageOps.fit(picture, (width, height), method=resample, centering=(0.5, 0.5))
  size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
  if size != picture.size:
    picture = picture.resize(size, resample)
  canvas = Image.new("RGB", (width, height), (0, 0, 0))
  canvas.paste(picture, ((width - size[0]) // 2, (height - size[1]) // 2))
  return canvas


def render_jpeg(data: bytes, width: int, height: int, max_bytes: int, fit: str = FILL) -> Optional[bytes]:
  """The source picture placed on width x height (_place), as a baseline
  JPEG within max_bytes (blocking: run it in an executor)."""
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
      picture = _place(picture, width, height, fit)
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
    render: Callable[[bytes, int, int, int, str], Awaitable[Optional[bytes]]],
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
    # Subscriptions to entities the panel is not served (yet); recheck().
    self._refused: Dict[str, Set[Any]] = {}
    # The last logged outcome per topic, so each change logs one line.
    self._outcomes: Dict[str, str] = {}
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

  def recheck(self) -> None:
    """The entities served to the panels changed (a declaration, the
    options): a kept subscription now served gets its picture, and one no
    longer served stops."""
    granted: Set[str] = set()
    revoked = False
    for topic, sessions in list(self._refused.items()):
      parsed = parse_image_topic(topic)
      for session in list(sessions):
        if parsed and self._allowed(session, parsed[0]):
          self._remove(self._refused, topic, session)
          self._wants.setdefault(topic, set()).add(session)
          granted.add(topic)
    for topic, sessions in list(self._wants.items()):
      parsed = parse_image_topic(topic)
      for session in list(sessions):
        if not parsed or not self._allowed(session, parsed[0]):
          self._refused.setdefault(topic, set()).add(session)
          self._remove(self._wants, topic, session)
          self._note(topic, NOT_SERVED)
          revoked = True
    if granted or revoked:
      self._retrack()
    for topic in sorted(granted):
      self._schedule(topic)

  def _remove(self, table: Dict[str, Set[Any]], topic: str, session: Any) -> None:
    sessions = table.get(topic)
    if sessions is None:
      return
    sessions.discard(session)
    if sessions:
      return
    del table[topic]
    if table is self._wants:
      refresher = self._refreshers.pop(topic, None)
      if refresher is not None:
        refresher.cancel()
    if topic not in self._wants and topic not in self._refused:
      self._outcomes.pop(topic, None)

  def _note(self, topic: str, outcome: str, detail: str = "", level: int = logging.INFO) -> None:
    """One log line when a topic's outcome changes: the picture sent, or
    why none could be sent."""
    if self._outcomes.get(topic) == outcome:
      return
    self._outcomes[topic] = outcome
    _LOGGER.log(level, "HomeTiles pictures: %s %s%s", topic, outcome, f" ({detail})" if detail else "")

  def _on_subscription(self, session: Any, topic: str, subscribed: bool) -> None:
    parsed = parse_image_topic(topic)
    if parsed is None:
      return
    if not subscribed:
      self._remove(self._refused, topic, session)
      if topic in self._wants:
        self._remove(self._wants, topic, session)
        self._retrack()
      return
    if not self._allowed(session, parsed[0]):
      self._refused.setdefault(topic, set()).add(session)
      self._note(topic, NOT_SERVED)
      return
    self._remove(self._refused, topic, session)
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
    if data and len(data) <= SOURCE_CACHE_MAX_BYTES:
      self._sources[source.key] = data
      while (len(self._sources) > SOURCE_CACHE_MAX
             or sum(len(item) for item in self._sources.values()) > SOURCE_CACHE_MAX_BYTES):
        self._sources.popitem(last=False)
    return data

  async def _render_topic(self, topic: str) -> None:
    parsed = parse_image_topic(topic)
    if parsed is None:
      return
    entity_id, width, height = parsed.entity_id, parsed.width, parsed.height
    source = self._source(entity_id)
    if source is None:
      self._note(topic, "has no picture now", level=logging.DEBUG)
      return
    if source.refresh > 0 and topic not in self._refreshers:
      every = float(parsed.every) if parsed.every else source.refresh
      self._refreshers[topic] = asyncio.get_running_loop().create_task(self._refresh(topic, every))
    if source.key is not None and self._published.get(topic) == source.key:
      return
    data = await self._load(source)
    if not data:
      self._note(topic, "could not load the picture")
      return
    key = source.key or content_key(data)
    if self._published.get(topic) == key:
      return
    budget = self._budget(topic) - len(build_payload(key, width, height, b""))
    jpeg = await self._render(data, width, height, budget, parsed.fit)
    if not jpeg:
      self._note(topic, "could not render the picture", f"{len(data)} source bytes, {budget} bytes room")
      return
    if topic not in self._wants:
      return
    if source.key is not None:
      current = self._source(entity_id)
      if current is None or current.key != source.key:
        return  # A newer picture waits for its own turn.
    self._broker.publish(topic, build_payload(key, width, height, jpeg), retain=True)
    self._published[topic] = key
    self._note(topic, "sent", f"{len(jpeg)} bytes from {len(data)} source bytes")
