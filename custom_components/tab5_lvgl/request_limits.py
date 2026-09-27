"""Bounds for panel requests that make the Recorder read history.

A history request costs a database query in Home Assistant's executor. Any
MQTT client can publish on a panel's request topic, so each panel gets a
small number of concurrent requests and a request budget per minute. A
panel opens one history popup at a time, far below these limits.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque

HISTORY_MAX_ACTIVE = 2
HISTORY_MAX_PER_WINDOW = 30
HISTORY_WINDOW_S = 60.0


class RequestGate:
  """Concurrency and sliding-window rate limit for one panel's requests."""

  def __init__(self, clock: Callable[[], float], *, max_active: int = HISTORY_MAX_ACTIVE,
               max_per_window: int = HISTORY_MAX_PER_WINDOW, window_s: float = HISTORY_WINDOW_S) -> None:
    self._clock = clock
    self._max_active = max_active
    self._max_per_window = max_per_window
    self._window_s = window_s
    self._active = 0
    self._started: Deque[float] = deque()

  @property
  def active(self) -> int:
    return self._active

  def try_acquire(self) -> bool:
    now = self._clock()
    while self._started and now - self._started[0] >= self._window_s:
      self._started.popleft()
    if self._active >= self._max_active or len(self._started) >= self._max_per_window:
      return False
    self._active += 1
    self._started.append(now)
    return True

  def release(self) -> None:
    if self._active > 0:
      self._active -= 1
