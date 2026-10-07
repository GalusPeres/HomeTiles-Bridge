"""Bounds for panel requests that make Home Assistant do work.

A history or energy request costs a database query in Home Assistant's
executor, a refresh republishes every entity. Any MQTT client can publish on
a panel's request topics, so each panel gets a small number of concurrent
requests and a request budget per minute.

A panel asks for the history of every graph tile when it shows a view, so a
burst of many requests is normal. Requests beyond the limits wait in line
instead of being dropped; only a flood that fills the line is dropped.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable, Deque, Optional

HISTORY_MAX_ACTIVE = 2
HISTORY_MAX_PER_WINDOW = 30
HISTORY_WINDOW_S = 60.0
HISTORY_MAX_WAITING = 32

# A refresh republishes the configuration and every entity state. A panel
# asks once a minute at most, plus after a reconnect or a lost message; a
# request that arrives while one runs waits, more are dropped.
REFRESH_MAX_ACTIVE = 1
REFRESH_MAX_PER_WINDOW = 6
REFRESH_MAX_WAITING = 1

# Energy reads Recorder statistics; the popup asks for day, week and month.
ENERGY_MAX_ACTIVE = 1
ENERGY_MAX_PER_WINDOW = 12
ENERGY_MAX_WAITING = 2
# Without an Energy dashboard every request is answered empty; its warning is
# logged at most this often.
ENERGY_UNAVAILABLE_LOG_S = 3600.0

# Weather requests republish the retained forecast state.
WEATHER_MAX_ACTIVE = 1
WEATHER_MAX_PER_WINDOW = 12
WEATHER_MAX_WAITING = 2


class RequestGate:
  """Concurrency and sliding-window rate limit for one panel's requests."""

  def __init__(self, clock: Callable[[], float], *, max_active: int = HISTORY_MAX_ACTIVE,
               max_per_window: int = HISTORY_MAX_PER_WINDOW, window_s: float = HISTORY_WINDOW_S,
               max_waiting: int = HISTORY_MAX_WAITING) -> None:
    self._clock = clock
    self._max_active = max_active
    self._max_per_window = max_per_window
    self._window_s = window_s
    self._max_waiting = max_waiting
    self._active = 0
    self._waiting = 0
    self._started: Deque[float] = deque()
    self._freed = asyncio.Event()

  @property
  def active(self) -> int:
    return self._active

  @property
  def waiting(self) -> int:
    return self._waiting

  def _prune(self, now: float) -> None:
    while self._started and now - self._started[0] >= self._window_s:
      self._started.popleft()

  def try_acquire(self) -> bool:
    now = self._clock()
    self._prune(now)
    if self._active >= self._max_active or len(self._started) >= self._max_per_window:
      return False
    self._active += 1
    self._started.append(now)
    return True

  def _window_delay(self) -> Optional[float]:
    """Seconds until the window has room again, or None when it has room."""
    now = self._clock()
    self._prune(now)
    if len(self._started) < self._max_per_window:
      return None
    return max(self._window_s - (now - self._started[0]), 0.0)

  async def acquire(self) -> bool:
    """Wait in line for a slot; False when the line is full."""
    if not self._waiting and self.try_acquire():
      return True
    if self._waiting >= self._max_waiting:
      return False
    self._waiting += 1
    try:
      while True:
        self._freed.clear()
        if self.try_acquire():
          return True
        # A release wakes the line; a full window also ends by itself.
        try:
          await asyncio.wait_for(self._freed.wait(), self._window_delay())
        except asyncio.TimeoutError:
          pass
    finally:
      self._waiting -= 1

  def release(self) -> None:
    if self._active > 0:
      self._active -= 1
    self._freed.set()
