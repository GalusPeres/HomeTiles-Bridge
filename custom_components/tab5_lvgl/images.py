"""Pictures for linked panels (docs-dev/images.md in the firmware repository).

A panel subscribes to the picture it shows in the size it shows it,
``<ha_prefix>/media_player/<object_id>/image/<w>x<h>``. While at least one
linked panel holds such a subscription and the Bridge serves the player to
it, this service fetches the player's artwork, cuts it to exactly w x h
(centred, filling) and publishes it retained on that topic:

  b"HTIMG1 <key> <w>x<h>\\n" + JPEG

``key`` (media_artwork.image_key) is also the media state's "image_key", so a
panel shows a picture only with the song it belongs to. Pictures above the
normal message size reach the panel as a stream (link_server.py), so the
budget of a topic is the smallest room its panels announced.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

from .link_protocol import MAX_PAYLOAD
from .media_artwork import image_key

_LOGGER = logging.getLogger(__name__)

IMAGE_MAGIC = b"HTIMG1"
MIN_EDGE = 16
MAX_EDGE = 1280
# Never more than this per picture, whatever a panel announced.
MAX_PICTURE_BYTES = 512 * 1024
SOURCE_CACHE_MAX = 6
JPEG_QUALITIES = (88, 82, 76, 70, 62, 54, 46)

_IMAGE_TOPIC = re.compile(
  r"(?P<prefix>.+)/(?P<domain>media_player)/(?P<object_id>[a-z0-9_]+)/image/"
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


class ImageService:
  """Renders the pictures linked panels subscribe to."""

  def __init__(
    self,
    broker: Any,
    *,
    artwork: Callable[[str], str],
    fetch: Callable[[str], Awaitable[Optional[bytes]]],
    render: Callable[[bytes, int, int, int], Awaitable[Optional[bytes]]],
    allowed: Callable[[Any, str], bool],
  ) -> None:
    self._broker = broker
    self._artwork = artwork
    self._fetch = fetch
    self._render = render
    self._allowed = allowed
    self._wants: Dict[str, Set[Any]] = {}
    self._published: Dict[str, str] = {}
    self._tasks: Dict[str, asyncio.Task] = {}
    self._again: Set[str] = set()
    self._sources: "OrderedDict[str, bytes]" = OrderedDict()
    self._stop = broker.add_subscription_listener(self._on_subscription)

  def stop(self) -> None:
    self._stop()
    for task in self._tasks.values():
      task.cancel()
    self._tasks.clear()

  def wanted(self) -> Dict[str, Set[Any]]:
    return {topic: set(sessions) for topic, sessions in self._wants.items()}

  def artwork_changed(self, entity_id: str) -> None:
    """A player's state changed; pictures with new artwork are rendered."""
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
      return
    if not self._allowed(session, parsed[0]):
      return
    self._wants.setdefault(topic, set()).add(session)
    self._schedule(topic)

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

  async def _render_topic(self, topic: str) -> None:
    parsed = parse_image_topic(topic)
    if parsed is None:
      return
    entity_id, width, height = parsed
    url = self._artwork(entity_id)
    if not url:
      return
    key = image_key(url)
    if self._published.get(topic) == key:
      return
    data = self._sources.get(url)
    if data is None:
      data = await self._fetch(url)
      if not data:
        return
      self._sources[url] = data
      while len(self._sources) > SOURCE_CACHE_MAX:
        self._sources.popitem(last=False)
    else:
      self._sources.move_to_end(url)
    budget = self._budget(topic) - len(build_payload(key, width, height, b""))
    jpeg = await self._render(data, width, height, budget)
    if not jpeg:
      _LOGGER.debug("HomeTiles pictures: nothing to send for %s", topic)
      return
    if topic not in self._wants or image_key(self._artwork(entity_id) or url) != key:
      return  # Unsubscribed, or newer artwork waits for its own turn.
    self._broker.publish(topic, build_payload(key, width, height, jpeg), retain=True)
    self._published[topic] = key
