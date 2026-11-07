"""A public webcam behind a Bridge camera (camera_sources.py).

The panel hands in one address. It is a picture, or a page naming its picture in
its "og:image" tag; no site needs code of its own. The Bridge then loads the
picture every few minutes, also while nobody looks, and keeps each new one with its
time for a day on Home Assistant's disk, so the panel's time bar fills up by itself.
The panel stores no frames.

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
import shutil
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

_LOGGER = logging.getLogger(__name__)


class SiteRefused(Exception):
  """The site answered, and does not let the Bridge read this address (fetch raises it)."""


# Bergfex's pictures say max-age=600.
REFRESH_S = 600.0
RETRY_S = 120.0
# A page is read again once a day, or when its picture stopped coming.
PAGE_READ_S = 24 * 3600.0
KEEP_S = 24 * 3600.0
MAX_FRAMES = 150
FIRST_WAIT_S = 8.0
PAGE_MAX_BYTES = 3 * 1024 * 1024
IMAGE_MAX_BYTES = 8 * 1024 * 1024
# The panels scale further down; a day of frames stays small on the disk.
STORED_MAX_SIDE = 1280
JPEG_QUALITY = 85
RAM_FRAMES = 8
URL_MAX_CHARS = 400

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


@dataclass
class Frame:
  when: int
  digest: str


class WebcamSource:
  """One address: its frames, newest last. Runs while its camera exists."""

  def __init__(
    self,
    url: str,
    *,
    fetch: Callable[[str, int], Awaitable[Optional[bytes]]],
    run_blocking: Callable[..., Awaitable[object]],
    directory: Path,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.url = url
    self._fetch = fetch
    self._run_blocking = run_blocking
    self._directory = directory
    self._clock = clock
    self._frames: List[Frame] = []
    self._ram: Dict[int, bytes] = {}
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
    return [frame.when for frame in self._frames]

  def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
    self._listeners.append(listener)

    def remove() -> None:
      if listener in self._listeners:
        self._listeners.remove(listener)
    return remove

  async def async_frame_at(self, when: int) -> Optional[bytes]:
    if when in self._ram:
      return self._ram[when]
    if not any(frame.when == when for frame in self._frames):
      return None
    return await self._run_blocking(self._read_frame, when)

  async def async_picture(self) -> Optional[bytes]:
    """The newest JPEG; only the very first one is waited for."""
    if not self._frames and not self._first.is_set():
      try:
        await asyncio.wait_for(self._first.wait(), FIRST_WAIT_S)
      except asyncio.TimeoutError:
        pass
    return await self.async_frame_at(self._frames[-1].when) if self._frames else None

  @property
  def picture_time(self) -> Optional[int]:
    return self._frames[-1].when if self._frames else None

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
    await self._run_blocking(shutil.rmtree, self._directory, True)

  async def _async_run(self) -> None:
    await self._run_blocking(self._load_index)
    if self._frames:
      self._first.set()
      self._tell_listeners()
    while True:
      try:
        wait = REFRESH_S if await self.async_refresh() else RETRY_S
      except asyncio.CancelledError:
        raise
      except Exception as err:  # One broken answer never ends the webcam.
        _LOGGER.warning("HomeTiles webcam %s: %s", self.url, err)
        wait = RETRY_S
      self._first.set()
      await asyncio.sleep(wait)

  async def async_refresh(self) -> bool:
    """Load the picture once; False when it did not come."""
    data = await self._async_load_picture()
    if data is None:
      return False
    digest = hashlib.sha1(data).hexdigest()
    self.failed = False
    if self._frames and self._frames[-1].digest == digest:
      await self._async_prune()
      return True
    jpeg = await self._run_blocking(to_jpeg, data)
    when = int(self._clock())
    if self._frames and when <= self._frames[-1].when:
      when = self._frames[-1].when + 1
    await self._run_blocking(self._write_frame, when, digest, jpeg)
    self._frames.append(Frame(when, digest))
    self._ram[when] = jpeg
    await self._async_prune()
    self._tell_listeners()
    return True

  async def _async_load_picture(self) -> Optional[bytes]:
    """The picture, or None. A site that refuses is told apart: its picture's own
    address may still work, and only the person who added it can find that."""
    try:
      data = await self._async_load_from_address()
    except SiteRefused:
      if not self.refused:
        _LOGGER.info("HomeTiles webcam: %s does not let the Bridge read it", self.url)
        self.refused = True
        self._tell_listeners()
      return None
    if data is not None and self.refused:
      self.refused = False
      self._tell_listeners()
    return data

  async def _async_load_from_address(self) -> Optional[bytes]:
    now = self._clock()
    if self._picture_url and now - self._page_read_at < PAGE_READ_S:
      data = await self._fetch(self._picture_url, IMAGE_MAX_BYTES)
      if data and is_picture(data):
        return data
    # First time, once a day, or the picture stopped coming: from the address.
    data = await self._fetch(self.url, max(PAGE_MAX_BYTES, IMAGE_MAX_BYTES))
    if not data:
      return None  # Not reached: this says nothing about the address.
    self._page_read_at = now
    if is_picture(data):
      self._picture_url = self.url
      return data
    found = page_picture(data, self.url)
    if not found or not url_allowed(found):
      self._picture_url = None
      if not self.failed:
        self.failed = True
        _LOGGER.info("HomeTiles webcam: %s names no picture", self.url)
        self._tell_listeners()
      return None
    self._picture_url = found
    picture = await self._fetch(found, IMAGE_MAX_BYTES)
    if picture and is_picture(picture):
      return picture
    if not self.failed:
      self.failed = True
      self._tell_listeners()
    return None

  async def _async_prune(self) -> None:
    oldest = self._clock() - KEEP_S
    gone = [frame.when for frame in self._frames[:-1] if frame.when < oldest]
    extra = len(self._frames) - len(gone) - MAX_FRAMES
    if extra > 0:
      left = [frame.when for frame in self._frames[:-1] if frame.when not in gone]
      gone.extend(left[:extra])
    if gone:
      self._frames = [frame for frame in self._frames if frame.when not in gone]
      await self._run_blocking(self._delete_frames, gone)
      self._tell_listeners()
    keep = set(self.times()[-RAM_FRAMES:])
    self._ram = {when: jpeg for when, jpeg in self._ram.items() if when in keep}

  def _tell_listeners(self) -> None:
    for listener in list(self._listeners):
      listener()

  # ---- The disk (run_blocking) ----

  def _path(self, when: int, digest: str) -> Path:
    return self._directory / f"{when}_{digest[:16]}.jpg"

  def _load_index(self) -> None:
    if not self._directory.is_dir():
      return
    frames: List[Frame] = []
    for path in self._directory.glob("*.jpg"):
      parts = path.stem.split("_")
      if len(parts) == 2 and parts[0].isdigit():
        frames.append(Frame(int(parts[0]), parts[1]))
    frames.sort(key=lambda frame: frame.when)
    self._frames = frames[-MAX_FRAMES:]

  def _write_frame(self, when: int, digest: str, jpeg: bytes) -> None:
    self._directory.mkdir(parents=True, exist_ok=True)
    self._path(when, digest).write_bytes(jpeg)

  def _read_frame(self, when: int) -> Optional[bytes]:
    for path in self._directory.glob(f"{when}_*.jpg"):
      return path.read_bytes()
    return None

  def _delete_frames(self, whens: List[int]) -> None:
    for when in whens:
      for path in self._directory.glob(f"{when}_*.jpg"):
        path.unlink(missing_ok=True)
