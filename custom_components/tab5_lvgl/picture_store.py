"""Pictures with a history on Home Assistant's disk: one rule for every Bridge camera.

Rain radar places and webcams (camera_sources.py) keep their pictures alike. Each
picture has a fixed time on the 10-minute grid, the time its source gives it where
it gives one, so the panel's time bar reads 10:00, 10:10, ... and one picture never
shows under two times. A slot holds one picture. A day and at most MAX_FRAMES stay,
the newest always, and a restart reads them back. The newest few stay in memory,
the rest is read from the disk when it is shown.

Nothing here imports Home Assistant; the owner hands in the blocking runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

SLOT_S = 600
KEEP_S = 24 * 3600
MAX_FRAMES = 150
RAM_FRAMES = 8
SERIES_FILE = "series.json"
FOLDER = "hometiles_cameras"
# Bridge betas up to v0.9.8b7 kept only the webcams, in this folder.
OLD_FOLDER = "hometiles_webcams"

_TAG_RE = re.compile(r"[^a-z0-9]")


def slot_of(when: float) -> int:
  """The start of the 10-minute slot a time falls into."""
  return int(when // SLOT_S) * SLOT_S


def clean_tag(tag: str) -> str:
  return _TAG_RE.sub("", str(tag).lower())[:24] or "x"


def move_old_folder(old: Path, new: Path) -> None:
  """The webcams' folder of earlier betas becomes the folder of every camera."""
  if old.is_dir() and not new.exists():
    old.rename(new)


@dataclass
class Frame:
  when: int
  tag: str


class FrameStore:
  """One camera's pictures, oldest first."""

  def __init__(
    self,
    directory: Path,
    *,
    run_blocking: Callable[..., Awaitable[Any]],
    clock: Callable[[], float] = time.time,
  ) -> None:
    self._directory = directory
    self._run_blocking = run_blocking
    self._clock = clock
    self._frames: List[Frame] = []
    self._ram: Dict[int, bytes] = {}
    self._series: Optional[str] = None
    self._load_task: Optional[asyncio.Task] = None

  # ---- Reading ----

  def times(self) -> List[int]:
    return [frame.when for frame in self._frames]

  @property
  def newest(self) -> Optional[Frame]:
    return self._frames[-1] if self._frames else None

  @property
  def series(self) -> Optional[str]:
    return self._series

  def tag_at(self, when: int) -> Optional[str]:
    for frame in self._frames:
      if frame.when == when:
        return frame.tag
    return None

  def has_tag(self, tag: str) -> bool:
    tag = clean_tag(tag)
    return any(frame.tag == tag for frame in self._frames)

  async def async_frame(self, when: int) -> Optional[bytes]:
    await self.async_load()
    if when in self._ram:
      return self._ram[when]
    tag = self.tag_at(when)
    if tag is None:
      return None
    return await self._run_blocking(self._read, when, tag)

  # ---- Changing ----

  async def async_load(self) -> None:
    """What an earlier run kept; read once, every caller waits for the same."""
    if self._load_task is None:
      self._load_task = asyncio.get_running_loop().create_task(self._async_read_disk())
    await asyncio.shield(self._load_task)

  async def _async_read_disk(self) -> None:
    try:
      frames, series = await self._run_blocking(self._read_index)
    except Exception as err:  # A broken folder starts empty instead of stopping the camera.
      _LOGGER.warning("HomeTiles pictures: %s could not be read (%s)", self._directory, err)
      return
    self._frames = frames[-MAX_FRAMES:]
    self._series = series

  async def async_series(self, key: str) -> bool:
    """The series the pictures belong to (a radar's source and place): another one
    starts empty. True when it changed."""
    await self.async_load()
    if self._series == key:
      return False
    self._series = key
    self._frames = []
    self._ram = {}
    await self._run_blocking(self._start_series, key)
    return True

  async def async_put(self, when: float, tag: str, jpeg: bytes) -> bool:
    """A picture at its slot, replacing another one there. False when that very
    picture was there already."""
    await self.async_load()
    slot = slot_of(when)
    tag = clean_tag(tag)
    before = self.tag_at(slot)
    if before == tag:
      return False
    await self._run_blocking(self._write, slot, tag, jpeg, before)
    self._frames = sorted([frame for frame in self._frames if frame.when != slot] + [Frame(slot, tag)],
                          key=lambda frame: frame.when)
    self._ram[slot] = jpeg
    await self.async_prune()
    return True

  async def async_prune(self) -> bool:
    """Older than a day and beyond MAX_FRAMES goes; the newest always stays.
    True when a picture went."""
    oldest = self._clock() - KEEP_S
    gone = [frame for frame in self._frames[:-1] if frame.when < oldest]
    extra = len(self._frames) - len(gone) - MAX_FRAMES
    if extra > 0:
      left = [frame for frame in self._frames[:-1] if frame not in gone]
      gone.extend(left[:extra])
    if gone:
      whens = {frame.when for frame in gone}
      self._frames = [frame for frame in self._frames if frame.when not in whens]
      await self._run_blocking(self._delete, [(frame.when, frame.tag) for frame in gone])
    keep = set(self.times()[-RAM_FRAMES:])
    self._ram = {when: jpeg for when, jpeg in self._ram.items() if when in keep}
    return bool(gone)

  async def async_remove_files(self) -> None:
    self._frames = []
    self._ram = {}
    await self._run_blocking(shutil.rmtree, self._directory, True)

  # ---- The disk (run_blocking) ----

  def _path(self, when: int, tag: str) -> Path:
    return self._directory / f"{when}_{tag}.jpg"

  def _read_index(self) -> Tuple[List[Frame], Optional[str]]:
    if not self._directory.is_dir():
      return [], None
    found: Dict[int, Tuple[int, Path, str]] = {}
    for path in self._directory.glob("*.jpg"):
      parts = path.stem.split("_")
      if len(parts) != 2 or not parts[0].isdigit():
        continue
      when = int(parts[0])
      slot = slot_of(when)
      kept = found.get(slot)
      # Earlier betas named a picture by the moment it was fetched: one per slot,
      # the latest of it, so a picture fetched twice shows once.
      if kept is not None and kept[0] >= when:
        path.unlink(missing_ok=True)
        continue
      if kept is not None:
        kept[1].unlink(missing_ok=True)
      found[slot] = (when, path, parts[1])
    frames = []
    for slot, (when, path, tag) in sorted(found.items()):
      if when != slot:
        target = self._path(slot, tag)
        path.rename(target)
      frames.append(Frame(slot, tag))
    series = None
    try:
      data = json.loads((self._directory / SERIES_FILE).read_text(encoding="utf-8"))
      series = data.get("series") if isinstance(data, dict) and isinstance(data.get("series"), str) else None
    except (OSError, ValueError):
      pass
    return frames, series

  def _start_series(self, key: str) -> None:
    self._directory.mkdir(parents=True, exist_ok=True)
    for path in self._directory.glob("*.jpg"):
      path.unlink(missing_ok=True)
    (self._directory / SERIES_FILE).write_text(json.dumps({"series": key}), encoding="utf-8")

  def _write(self, when: int, tag: str, jpeg: bytes, before: Optional[str]) -> None:
    self._directory.mkdir(parents=True, exist_ok=True)
    self._path(when, tag).write_bytes(jpeg)
    if before is not None and before != tag:
      self._path(when, before).unlink(missing_ok=True)

  def _read(self, when: int, tag: str) -> Optional[bytes]:
    try:
      return self._path(when, tag).read_bytes()
    except OSError:
      return None

  def _delete(self, frames: List[Tuple[int, str]]) -> None:
    for when, tag in frames:
      self._path(when, tag).unlink(missing_ok=True)
