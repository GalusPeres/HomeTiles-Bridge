"""The Bridge as the message broker of its directly linked panels.

Protocol reference: docs-dev/bridge-link.md in the HomeTiles firmware
repository. The link keeps the MQTT model: topics, publish, subscribe and
retained messages. Bridge code publishes and subscribes here through
link_mqtt.py; link_server.py attaches one session per connected panel.

Rules:
- A subscribe delivers the stored retained messages it matches (flagged as
  retained), then every later publish (not flagged).
- A retained publish replaces the stored message of its topic; an empty
  retained payload deletes it.
- A session-mode panel publishes under {base}/, tab5_lvgl/config/{id}/ and
  {ha_prefix}/, and subscribes to concrete topics under the same roots. A
  pair-mode panel only publishes {base}/pair/panel and subscribes
  {base}/pair/bridge.
- A panel that leaves publishes "0" retained on {base}/stat/connected, like
  the MQTT last will.

Nothing here performs I/O; deliveries are plain calls the caller schedules.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set

from .link_protocol import MODE_PAIR, MODE_SESSION, topic_matches, valid_filter, valid_topic

_LOGGER = logging.getLogger(__name__)

CONFIG_ROOT = "tab5_lvgl/config"
DISCOVERY_ROOT = "homeassistant/"
PAIR_PANEL_LEAF = "pair/panel"
PAIR_BRIDGE_LEAF = "pair/bridge"
CONNECTED_LEAF = "stat/connected"

# deliver(topic, payload bytes, retained flag)
Deliver = Callable[[str, bytes, bool], None]


class PanelSession(Protocol):
  """What the broker needs from one connected panel (link_server.py)."""

  device_id: str
  base: str
  ha_prefix: str
  mode: str
  subscriptions: Set[str]

  def send_publish(self, topic: str, payload: bytes, retain: bool) -> None:
    ...


class _Subscription:
  __slots__ = ("topic_filter", "deliver", "active")

  def __init__(self, topic_filter: str, deliver: Deliver) -> None:
    self.topic_filter = topic_filter
    self.deliver = deliver
    self.active = True


def _publish_roots(session: PanelSession) -> List[str]:
  return [f"{session.base}/", f"{CONFIG_ROOT}/{session.device_id}/", f"{session.ha_prefix}/"]


class LinkBroker:
  """Topics, retained messages and subscriptions of all linked panels."""

  def __init__(self, *, log_due: Optional[Callable[[str], bool]] = None) -> None:
    self._retained: Dict[str, bytes] = {}
    self._subscriptions: List[_Subscription] = []
    self._sessions: Dict[str, PanelSession] = {}
    self._log_due = log_due or (lambda _reason: True)

  # ---- Bridge side --------------------------------------------------------

  def subscribe(self, topic_filter: str, deliver: Deliver) -> Callable[[], None]:
    """Subscribe Bridge code; returns the unsubscribe callable."""
    if not valid_filter(topic_filter):
      raise ValueError(f"invalid topic filter: {topic_filter!r}")
    subscription = _Subscription(topic_filter, deliver)
    self._subscriptions.append(subscription)
    for topic, payload in list(self._retained.items()):
      if topic_matches(topic_filter, topic):
        self._call(subscription, topic, payload, True)

    def unsubscribe() -> None:
      if subscription.active:
        subscription.active = False
        self._subscriptions.remove(subscription)

    return unsubscribe

  def publish(self, topic: str, payload: bytes, retain: bool = False) -> int:
    """Publish from Bridge code to the linked panels; returns the deliveries."""
    if not valid_topic(topic):
      raise ValueError(f"invalid topic: {topic!r}")
    if retain:
      self._store(topic, payload)
    delivered = 0
    for session in list(self._sessions.values()):
      if topic in session.subscriptions and self._may_receive(session, topic):
        session.send_publish(topic, payload, False)
        delivered += 1
    return delivered

  def retained(self, topic: str) -> Optional[bytes]:
    return self._retained.get(topic)

  def session(self, device_id: str) -> Optional[PanelSession]:
    return self._sessions.get(device_id)

  @property
  def sessions(self) -> Iterable[PanelSession]:
    return tuple(self._sessions.values())

  # ---- Panel side ---------------------------------------------------------

  def attach(self, session: PanelSession) -> Optional[PanelSession]:
    """Add a panel; returns the earlier session of the same panel, if any.

    The newer connection wins: an old one is usually a half-open socket of a
    panel that lost Wi-Fi. The caller closes the returned session.
    """
    previous = self._sessions.get(session.device_id)
    self._sessions[session.device_id] = session
    return previous if previous is not session else None

  def detach(self, session: PanelSession) -> None:
    """Remove a panel that left; a session panel publishes its last will."""
    if self._sessions.get(session.device_id) is not session:
      return  # Already replaced by a newer connection.
    del self._sessions[session.device_id]
    if session.mode == MODE_SESSION:
      self._from_panel(f"{session.base}/{CONNECTED_LEAF}", b"0", True)

  def panel_publish(self, session: PanelSession, topic: str, payload: bytes, retain: bool) -> bool:
    """A publish of a panel; False when the panel may not use this topic."""
    if self._sessions.get(session.device_id) is not session:
      return False
    if session.mode == MODE_PAIR:
      allowed = topic == f"{session.base}/{PAIR_PANEL_LEAF}"
      retain = False
    else:
      if topic.startswith(DISCOVERY_ROOT):
        return False  # Clean-up of the old MQTT discovery; nothing to do here.
      allowed = any(topic.startswith(root) for root in _publish_roots(session))
    if not allowed:
      if self._log_due(f"publish:{session.device_id}"):
        _LOGGER.warning("HomeTiles link: panel %s may not publish %s; dropped", session.device_id, topic)
      return False
    self._from_panel(topic, payload, retain)
    return True

  def panel_subscribe(self, session: PanelSession, topic: str) -> bool:
    """A subscribe of a panel; delivers the stored retained message."""
    if self._sessions.get(session.device_id) is not session or not valid_topic(topic):
      return False
    if not self._may_receive(session, topic):
      if self._log_due(f"subscribe:{session.device_id}"):
        _LOGGER.warning("HomeTiles link: panel %s may not subscribe %s; ignored", session.device_id, topic)
      return False
    session.subscriptions.add(topic)
    payload = self._retained.get(topic)
    if payload is not None:
      session.send_publish(topic, payload, True)
    return True

  def panel_unsubscribe(self, session: PanelSession, topic: str) -> None:
    session.subscriptions.discard(topic)

  # ---- Internals ----------------------------------------------------------

  @staticmethod
  def _may_receive(session: PanelSession, topic: str) -> bool:
    if session.mode == MODE_PAIR:
      return topic == f"{session.base}/{PAIR_BRIDGE_LEAF}"
    return any(topic.startswith(root) for root in _publish_roots(session))

  def _store(self, topic: str, payload: bytes) -> None:
    if payload:
      self._retained[topic] = bytes(payload)
    else:
      self._retained.pop(topic, None)

  def _from_panel(self, topic: str, payload: bytes, retain: bool) -> None:
    if retain:
      self._store(topic, payload)
    for subscription in list(self._subscriptions):
      if subscription.active and topic_matches(subscription.topic_filter, topic):
        self._call(subscription, topic, payload, False)

  @staticmethod
  def _call(subscription: _Subscription, topic: str, payload: bytes, retained: bool) -> None:
    try:
      subscription.deliver(topic, payload, retained)
    except Exception:  # pragma: no cover - a faulty subscriber must not stop delivery
      _LOGGER.exception("HomeTiles link: subscriber of %s failed", subscription.topic_filter)
