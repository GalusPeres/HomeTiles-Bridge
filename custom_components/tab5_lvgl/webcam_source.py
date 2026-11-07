"""A public webcam behind a Bridge camera (camera_sources.py).

The panel hands in one address. It is a picture, or a page naming its picture in
its "og:image" tag; no site needs code of its own. The Bridge then loads the
picture every few minutes, also while nobody looks, and keeps it like every Bridge
camera keeps its pictures (picture_store.py): at the time the site gives it (its
Last-Modified), else when it was fetched, on the 10-minute grid, for a day. A portal
with an open archive (webcam_archives.py) fills the last day at once and the gaps
after a restart. The panel stores no frames.

Nothing here imports Home Assistant; __init__.py hands in fetching and the
blocking runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlsplit

from .picture_store import KEEP_S, FrameStore
from .webcam_archives import archive_for

_LOGGER = logging.getLogger(__name__)


class SiteRefused(Exception):
  """The site answered, and does not let the Bridge read this address (fetch raises it)."""


@dataclass
class Answer:
  """A fetched address with the time its server gives it (Last-Modified)."""

  data: bytes
  modified: Optional[int] = None


Fetched = Union[bytes, Answer, None]

# Bergfex's pictures say max-age=600.
REFRESH_S = 600.0
RETRY_S = 120.0
# A page is read again once a day, or when its picture stopped coming.
PAGE_READ_S = 24 * 3600.0
FIRST_WAIT_S = 8.0
PAGE_MAX_BYTES = 3 * 1024 * 1024
IMAGE_MAX_BYTES = 8 * 1024 * 1024
# The panels scale further down; a day of frames stays small on the disk.
STORED_MAX_SIDE = 1280
JPEG_QUALITY = 85
URL_MAX_CHARS = 400
# An archive is read one picture after the other, gently, and the time bar is told
# every few pictures so it grows while the day comes in.
ARCHIVE_PAUSE_S = 0.25
ARCHIVE_TELL_EVERY = 6
# A server's time this far ahead of the Bridge's clock is not believed.
FUTURE_SLACK_S = 120

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*("[^"]*"|'[^']*')""")
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")


def url_allowed(url: object) -> bool:
  """Only https to a public name: the address comes from a panel."""
  if not isinstance(url, str) or not url or len(url) > URL_MAX_CHARS:
    return False
  try:
    parts = urlsplit(url.strip())
    host = parts.hostname
  except ValueError:
    return False
  if parts.scheme != "https" or not host or parts.username or parts.password:
    return False
  host = host.lower().rstrip(".")
  if host == "localhost" or "." not in host and ":" not in host or host.endswith(_LOCAL_SUFFIXES):
    return False
  try:
    address = ipaddress.ip_address(host)
  except ValueError:
    return True
  return address.is_global


def http_time(value: Optional[str]) -> Optional[int]:
  """A Last-Modified header as seconds, None when there is none or it is broken."""
  if not value:
    return None
  try:
    return int(parsedate_to_datetime(value).timestamp())
  except (TypeError, ValueError, IndexError, OverflowError):
    return None


def page_picture(page: bytes, base_url: str) -> Optional[str]:
  """The address in the page's "og:image" (or "twitter:image") tag."""
  text = page[:PAGE_MAX_BYTES].decode("utf-8", "ignore")
  found: Dict[str, str] = {}
  for tag in _META_RE.findall(text):
    attrs = {key.lower(): value[1:-1] for key, value in _ATTR_RE.findall(tag)}
    name = (attrs.get("property") or attrs.get("name") or "").lower()
    if name in ("og:image", "og:image:secure_url", "twitter:image") and attrs.get("content"):
      found.setdefault(name, html.unescape(attrs["content"]).strip())
  for name in ("og:image:secure_url", "og:image", "twitter:image"):
    if name in found:
      return urljoin(base_url, found[name])
  return None


def is_picture(data: bytes) -> bool:
  return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
          or data[:4] == b"GIF8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def to_jpeg(data: bytes) -> bytes:
  """A JPEG the camera paths can use, in a size worth keeping for a day."""
  from PIL import Image

  with Image.open(BytesIO(data)) as source:
    picture = source.convert("RGB")
  picture.thumbnail((STORED_MAX_SIDE, STORED_MAX_SIDE), Image.BILINEAR)
  output = BytesIO()
  picture.save(output, format="JPEG", quality=JPEG_QUALITY)
  return output.getvalue()


def _split(answer: Fetched) -> Tuple[Optional[bytes], Optional[int]]:
  if isinstance(answer, Answer):
    return answer.data, answer.modified
  return answer, None


def _digest(data: bytes) -> str:
  return hashlib.sha1(data).hexdigest()[:16]


class WebcamSource:
  """One address: its frames, newest last. Runs while its camera exists."""

  def __init__(
    self,
    url: str,
    *,
    fetch: Callable[[str, int], Awaitable[Fetched]],
    run_blocking: Callable[..., Awaitable[object]],
    directory: Path,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.url = url
    self._fetch = fetch
    self._run_blocking = run_blocking
    self._clock = clock
    self._store = FrameStore(directory, run_blocking=run_blocking, clock=clock)
    self._picture_url: Optional[str] = None
    self._page_read_at = 0.0
    self._listeners: List[Callable[[], None]] = []
    self._task: Optional[asyncio.Task] = None
    self._first = asyncio.Event()
    # True once this address showed it has no picture to offer.
    self.failed = False
    self.refused = False

  # ---- What a camera needs (the rain radar offers the same) ----

  def times(self) -> List[int]:
    return self._store.times()

  def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
    self._listeners.append(listener)

    def remove() -> None:
      if listener in self._listeners:
        self._listeners.remove(listener)
    return remove

  async def async_frame_at(self, when: int) -> Optional[bytes]:
    return await self._store.async_frame(when)

  async def async_picture(self) -> Optional[bytes]:
    """The newest JPEG; only the very first one is waited for."""
    await self._store.async_load()
    if not self._store.times() and not self._first.is_set():
      try:
        await asyncio.wait_for(self._first.wait(), FIRST_WAIT_S)
      except asyncio.TimeoutError:
        pass
    newest = self._store.newest
    return await self._store.async_frame(newest.when) if newest else None

  @property
  def picture_time(self) -> Optional[int]:
    newest = self._store.newest
    return newest.when if newest else None

  # ---- Running ----

  def start(self) -> None:
    if self._task is None or self._task.done():
      self._task = asyncio.get_running_loop().create_task(self._async_run())

  async def async_stop(self) -> None:
    task, self._task = self._task, None
    if task is not None:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass

  async def async_remove_files(self) -> None:
    await self._store.async_remove_files()

  async def _async_run(self) -> None:
    await self._store.async_load()
    if self._store.times():
      self._first.set()
      self._tell_listeners()
    archive_read = False
    while True:
      try:
        came = await self.async_refresh()
        if came and not archive_read:
          # Behind the newest picture, so nobody waits for the whole day.
          archive_read = True
          await self.async_fill_from_archive()
        wait = REFRESH_S if came else RETRY_S
      except asyncio.CancelledError:
        raise
      except Exception as err:  # One broken answer never ends the webcam.
        _LOGGER.warning("HomeTiles webcam %s: %s", self.url, err)
        wait = RETRY_S
      self._first.set()
      await asyncio.sleep(wait)

  async def async_refresh(self) -> bool:
    """Load the picture once; False when it did not come."""
    # The kept pictures first: a picture fetched again after a restart is no new one.
    await self._store.async_load()
    loaded = await self._async_load_picture()
    if loaded is None:
      return False
    data, modified = loaded
    self.failed = False
    tag = _digest(data)
    if self._store.has_tag(tag):
      # The same picture as before, also when its time moved on.
      if await self._store.async_prune():
        self._tell_listeners()
      return True
    now = self._clock()
    when = modified if modified is not None and modified <= now + FUTURE_SLACK_S else now
    jpeg = await self._run_blocking(to_jpeg, data)
    if await self._store.async_put(when, tag, jpeg):
      self._tell_listeners()
    return True

  async def async_fill_from_archive(self) -> int:
    """The last day from the portal's archive, where it has one: only the slots that
    have no picture yet, newest first. Returns how many came."""
    archive = archive_for(self.url, self._picture_url)
    if archive is None:
      return 0
    await self._store.async_load()
    now = self._clock()
    have = set(self._store.times())
    wanted = [(when, url) for when, url in archive.addresses(now - KEEP_S, now) if when not in have]
    loaded = 0
    for when, url in wanted:
      try:
        data, _modified = _split(await self._fetch(url, IMAGE_MAX_BYTES))
      except SiteRefused:
        _LOGGER.info("HomeTiles webcam: the archive of %s does not let the Bridge read it", archive.portal)
        break
      if data and is_picture(data):
        jpeg = await self._run_blocking(to_jpeg, data)
        if await self._store.async_put(when, _digest(data), jpeg):
          loaded += 1
          if loaded % ARCHIVE_TELL_EVERY == 0:
            self._tell_listeners()
      await asyncio.sleep(ARCHIVE_PAUSE_S)
    if loaded % ARCHIVE_TELL_EVERY:
      self._tell_listeners()
    if loaded:
      _LOGGER.info("HomeTiles webcam: %d pictures of the last day from %s", loaded, archive.portal)
    return loaded

  async def _async_load_picture(self) -> Optional[Tuple[bytes, Optional[int]]]:
    """The picture and its time, or None. A site that refuses is told apart: its
    picture's own address may still work, and only the person who added it can
    find that."""
    try:
      loaded = await self._async_load_from_address()
    except SiteRefused:
      if not self.refused:
        _LOGGER.info("HomeTiles webcam: %s does not let the Bridge read it", self.url)
        self.refused = True
        self._tell_listeners()
      return None
    if loaded is not None and self.refused:
      self.refused = False
      self._tell_listeners()
    return loaded

  async def _async_load_from_address(self) -> Optional[Tuple[bytes, Optional[int]]]:
    now = self._clock()
    if self._picture_url and now - self._page_read_at < PAGE_READ_S:
      data, modified = _split(await self._fetch(self._picture_url, IMAGE_MAX_BYTES))
      if data and is_picture(data):
        return data, modified
    # First time, once a day, or the picture stopped coming: from the address.
    data, modified = _split(await self._fetch(self.url, max(PAGE_MAX_BYTES, IMAGE_MAX_BYTES)))
    if not data:
      return None  # Not reached: this says nothing about the address.
    self._page_read_at = now
    if is_picture(data):
      self._picture_url = self.url
      return data, modified
    found = page_picture(data, self.url)
    if not found or not url_allowed(found):
      self._picture_url = None
      if not self.failed:
        self.failed = True
        _LOGGER.info("HomeTiles webcam: %s names no picture", self.url)
        self._tell_listeners()
      return None
    self._picture_url = found
    picture, modified = _split(await self._fetch(found, IMAGE_MAX_BYTES))
    if picture and is_picture(picture):
      return picture, modified
    if not self.failed:
      self.failed = True
      self._tell_listeners()
    return None

  def _tell_listeners(self) -> None:
    for listener in list(self._listeners):
      listener()
