"""The running direct link: broker, server and setup dialogs waiting to pair.

Protocol reference: docs-dev/bridge-link.md in the HomeTiles firmware
repository. The integration creates one LinkRuntime in async_setup and keeps
it in hass.data[DOMAIN]["link"] (link_mqtt.DATA_LINK).

A setup dialog (config_flow.py) registers a PendingLink for the panel it adds.
The panel then connects in pair mode, and the PendingLink runs the Bridge end
of the pairing (pairing.py) over the broker, so the dialog itself shows the
number and creates the entry with the pairing key.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .command_channel import entry_pairing_key, entry_removing_key, key_id_for_key
from .link_broker import PAIR_BRIDGE_LEAF, PAIR_PANEL_LEAF, LinkBroker
from .link_protocol import REFUSE_BASE, REFUSE_UNKNOWN
from .link_server import LinkServer, SessionTarget
from .pairing import ANSWER_EXPIRED, PanelPairing

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tab5_lvgl"
CONF_DEVICE_ID = "device_id"
CONF_BASE_TOPIC = "base_topic"
CONF_HA_PREFIX = "ha_prefix"
DEFAULT_BASE = "hometiles"
DEFAULT_PREFIX = "ha/statestream"
PAIRING_TICK_S = 1.0


def _normalise(value: Any, default: str) -> str:
  text = str(value or "").strip().rstrip("/")
  return text or default


def _entry_values(entry: Any) -> Dict[str, Any]:
  values = dict(getattr(entry, "data", None) or {})
  values.update(getattr(entry, "options", None) or {})
  return values


class PendingLink:
  """A setup dialog waiting for its panel to pair over the link."""

  def __init__(self, runtime: "LinkRuntime", device_id: str, base: str, ha_prefix: str,
               *, clock: Callable[[], float] = monotonic) -> None:
    self._runtime = runtime
    self.device_id = device_id
    self.base = base
    self.ha_prefix = ha_prefix
    self.pairing = PanelPairing(base, clock=clock)
    self.attempt: Optional[str] = None
    self.number: Optional[str] = None
    self.key: Optional[bytes] = None
    self.failure: Optional[str] = None
    self.prompted = asyncio.Event()
    self.finished = asyncio.Event()
    self._tick_handle: Optional[asyncio.TimerHandle] = None
    self._unsubscribe = runtime.broker.subscribe(f"{base}/{PAIR_PANEL_LEAF}", self._deliver)

  def __repr__(self) -> str:  # Never expose the key.
    return f"PendingLink(device_id={self.device_id!r}, base={self.base!r})"

  def _deliver(self, _topic: str, payload: bytes, retained: bool) -> None:
    if retained:
      return
    self._apply(self.pairing.handle(payload.decode("utf-8", errors="replace"), paired=False))

  def _apply(self, events: List[Tuple[str, Any]]) -> None:
    for event, value in events:
      if event == "send":
        self._runtime.broker.publish(f"{self.base}/{PAIR_BRIDGE_LEAF}", str(value).encode("utf-8"), False)
      elif event == "prompt":
        self.attempt, self.number = value
        self.prompted.set()
      elif event == "closed":
        self.failure = str(value)
        self.prompted.set()
        self.finished.set()
      elif event == "refused":
        _LOGGER.debug("HomeTiles link setup of %s: pairing start refused (%s)", self.device_id, value)
      elif event == "paired":
        self.key = value
        self.finished.set()
    self._schedule_tick()

  def _schedule_tick(self) -> None:
    if self.pairing.active and self._tick_handle is None:
      loop = asyncio.get_running_loop()
      self._tick_handle = loop.call_later(PAIRING_TICK_S, self._tick)

  def _tick(self) -> None:
    self._tick_handle = None
    self._apply(self.pairing.tick())

  def answer(self, accept: bool) -> str:
    """The user's answer in the setup dialog (pairing.ANSWER_*)."""
    if self.attempt is None:
      return ANSWER_EXPIRED
    outcome, events = self.pairing.answer(self.attempt, accept)
    self._apply(events)
    return outcome

  def close(self) -> None:
    if self._tick_handle is not None:
      self._tick_handle.cancel()
      self._tick_handle = None
    self._unsubscribe()
    self._unsubscribe = lambda: None
    self._runtime.release(self)


class LinkRuntime:
  """Broker, server and pending setups of the integration."""

  def __init__(self, entries: Callable[[], List[Any]]) -> None:
    self._entries = entries
    self.broker = LinkBroker(log_due=self._log_due)
    self.server = LinkServer(self.broker, resolve_session=self.resolve_session,
                             accept_pairing=self.accept_pairing)
    self.pending: Dict[str, PendingLink] = {}
    self._log_at: Dict[str, float] = {}

  @property
  def port(self) -> Optional[int]:
    return self.server.port

  def _log_due(self, key: str, interval: float = 60.0) -> bool:
    now = monotonic()
    last = self._log_at.get(key)
    if last is not None and now - last < interval:
      return False
    if len(self._log_at) > 256:
      self._log_at.clear()
    self._log_at[key] = now
    return True

  async def async_start(self) -> None:
    try:
      await self.server.async_start()
    except OSError as err:
      _LOGGER.error("HomeTiles link server could not start: %s", err)

  async def async_stop(self) -> None:
    for pending in list(self.pending.values()):
      pending.close()
    with suppress(Exception):
      await self.server.async_stop()

  # ---- Setup dialogs ------------------------------------------------------

  def begin_setup(self, device_id: str, base: str, ha_prefix: str) -> PendingLink:
    """Register a setup dialog; an older one for the same panel is replaced."""
    previous = self.pending.get(device_id)
    if previous is not None:
      previous.close()
    pending = PendingLink(self, device_id, base, ha_prefix)
    self.pending[device_id] = pending
    return pending

  def release(self, pending: PendingLink) -> None:
    if self.pending.get(pending.device_id) is pending:
      del self.pending[pending.device_id]

  # ---- Hello resolution (link_server.py) ----------------------------------

  def _entries_for(self, device_id: str) -> List[Any]:
    return [entry for entry in self._entries()
            if str(_entry_values(entry).get(CONF_DEVICE_ID) or "") == device_id]

  def resolve_session(self, device_id: str, key_id: str, base: str) -> Union[SessionTarget, str]:
    reason = REFUSE_UNKNOWN
    for entry in self._entries_for(device_id):
      key = entry_pairing_key(entry) or entry_removing_key(entry)
      if key is None or key_id_for_key(key) != key_id:
        continue
      values = _entry_values(entry)
      if _normalise(values.get(CONF_BASE_TOPIC), DEFAULT_BASE) != base:
        reason = REFUSE_BASE
        continue
      return SessionTarget(key, _normalise(values.get(CONF_HA_PREFIX), DEFAULT_PREFIX))
    return reason

  def accept_pairing(self, device_id: str, base: str) -> Optional[str]:
    pending = self.pending.get(device_id)
    if pending is not None:
      return pending.ha_prefix if pending.base == base else None
    for entry in self._entries_for(device_id):
      values = _entry_values(entry)
      if _normalise(values.get(CONF_BASE_TOPIC), DEFAULT_BASE) == base:
        return _normalise(values.get(CONF_HA_PREFIX), DEFAULT_PREFIX)
    return None
