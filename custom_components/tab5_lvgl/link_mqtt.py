"""The MQTT calls of the integration, for linked panels and the MQTT broker.

Every module imports this one as ``mqtt``, so the existing calls
(``mqtt.async_publish``, ``mqtt.async_subscribe``, ``mqtt.is_connected``)
reach panels on the direct link (link_broker.py) as well. While at least one
panel still uses MQTT, publishes also go to Home Assistant's MQTT integration;
subscriptions go there whenever it is set up, so panels with older firmware
are still discovered. Without the MQTT integration only the link is used.

Protocol reference: docs-dev/bridge-link.md in the HomeTiles firmware
repository.
"""

from __future__ import annotations

import asyncio
import logging
import time
import types
from typing import Any, Callable

from homeassistant.components import mqtt as _mqtt

_LOGGER = logging.getLogger(__name__)

# Same value as const.DOMAIN; this module stays free of other imports so the
# platform tests can load it against their MQTT stubs.
DOMAIN = "tab5_lvgl"
# hass.data[DOMAIN][DATA_LINK]: the running link (broker and server).
DATA_LINK = "link"
# hass.data[DOMAIN][DATA_MQTT_ENTRIES]: ids of the loaded entries that still
# use MQTT. Absent means "unknown" and keeps MQTT on.
DATA_MQTT_ENTRIES = "mqtt_entries"
# Entry keys (same values as in const.py).
CONF_TRANSPORT = "transport"
TRANSPORT_LINK = "link"
CONF_BASE_TOPIC = "base_topic"
CONF_DEVICE_ID = "device_id"
CONFIG_ROOT = "tab5_lvgl/config"

ReceiveMessage = getattr(_mqtt, "ReceiveMessage", types.SimpleNamespace)


def __getattr__(name: str) -> Any:
  # Anything else the integration might use comes from the MQTT integration.
  return getattr(_mqtt, name)


def _domain_data(hass: Any) -> dict:
  data = getattr(hass, "data", None)
  if not isinstance(data, dict):
    return {}
  domain_data = data.get(DOMAIN)
  return domain_data if isinstance(domain_data, dict) else {}


def link_broker(hass: Any) -> Any:
  """The broker of the direct link, or None before it runs."""
  runtime = _domain_data(hass).get(DATA_LINK)
  return getattr(runtime, "broker", None)


def mqtt_ready(hass: Any) -> bool:
  """True unless Home Assistant runs without its MQTT integration."""
  try:
    components = hass.config.components
    return "mqtt" in components
  except Exception:  # A test double without a config: assume MQTT.
    return True


def mqtt_wanted(hass: Any) -> bool:
  """Publishes go to MQTT while an entry still uses it (or it is unknown)."""
  entries = _domain_data(hass).get(DATA_MQTT_ENTRIES)
  if entries is not None and not entries:
    return False
  return mqtt_ready(hass)


def _link_prefixes(hass: Any) -> tuple:
  """Topic roots of the panels on the direct link ({base}/, tab5_lvgl/config/{id}/)."""
  try:
    entries = hass.config_entries.async_entries(DOMAIN)
  except Exception:  # A test double without config entries.
    return ()
  prefixes = []
  for entry in entries:
    values = dict(getattr(entry, "data", None) or {})
    values.update(getattr(entry, "options", None) or {})
    if values.get(CONF_TRANSPORT) != TRANSPORT_LINK:
      continue
    base = str(values.get(CONF_BASE_TOPIC) or "").strip().rstrip("/")
    device_id = str(values.get(CONF_DEVICE_ID) or "").strip()
    if base:
      prefixes.append(f"{base}/")
    if device_id:
      prefixes.append(f"{CONFIG_ROOT}/{device_id}/")
  return tuple(prefixes)


def link_owned(hass: Any, topic: str) -> bool:
  """True for a topic of a panel on the direct link.

  Such a panel no longer uses MQTT, but the broker may still hold its old
  retained messages (for example "0" on {base}/stat/connected from the moment
  it left MQTT). They must neither reach the Bridge nor be renewed there.
  """
  prefixes = _link_prefixes(hass)
  return bool(prefixes) and topic.startswith(prefixes)


def _payload_bytes(payload: Any, encoding: Any) -> bytes:
  if payload is None:
    return b""
  if isinstance(payload, (bytes, bytearray)):
    return bytes(payload)
  return str(payload).encode(encoding or "utf-8")


def _message(topic: str, payload: Any, retain: bool, subscribed: str) -> Any:
  try:
    return ReceiveMessage(topic=topic, payload=payload, qos=0, retain=retain,
                          subscribed_topic=subscribed, timestamp=time.monotonic())
  except TypeError:
    return types.SimpleNamespace(topic=topic, payload=payload, qos=0, retain=retain,
                                 subscribed_topic=subscribed, timestamp=time.monotonic())


def _run_callback(hass: Any, msg_callback: Callable[[Any], Any], message: Any) -> None:
  try:
    result = msg_callback(message)
  except Exception:  # pragma: no cover - same isolation as the MQTT integration
    _LOGGER.exception("HomeTiles link: handler of %s failed", message.topic)
    return
  if asyncio.iscoroutine(result):
    hass.async_create_task(result)


async def async_publish(hass: Any, topic: str, payload: Any, *args: Any, **kwargs: Any) -> None:
  """mqtt.async_publish(hass, topic, payload, qos=0, retain=False, encoding="utf-8")."""
  broker = link_broker(hass)
  if broker is not None:
    retain = kwargs.get("retain", args[1] if len(args) > 1 else False)
    encoding = kwargs.get("encoding", args[2] if len(args) > 2 else "utf-8")
    broker.publish(topic, _payload_bytes(payload, encoding), bool(retain))
  if broker is None or (mqtt_wanted(hass) and not link_owned(hass, topic)):
    await _mqtt.async_publish(hass, topic, payload, *args, **kwargs)


async def async_subscribe(hass: Any, topic: str, msg_callback: Callable[[Any], Any],
                          *args: Any, **kwargs: Any) -> Callable[[], None]:
  """mqtt.async_subscribe(hass, topic, msg_callback, qos=0, encoding="utf-8")."""
  unsubscribes = []
  broker = link_broker(hass)
  if broker is not None:
    encoding = kwargs.get("encoding", args[1] if len(args) > 1 else "utf-8")
    loop = asyncio.get_running_loop()

    def deliver(message_topic: str, data: bytes, retained: bool) -> None:
      payload: Any = data if encoding is None else data.decode(encoding, errors="replace")
      loop.call_soon(_run_callback, hass, msg_callback, _message(message_topic, payload, retained, topic))

    unsubscribes.append(broker.subscribe(topic, deliver))
  if broker is None:
    unsubscribes.append(await _mqtt.async_subscribe(hass, topic, msg_callback, *args, **kwargs))
  elif mqtt_ready(hass):
    if "+" in topic or "#" in topic:
      # A filter can match linked and MQTT panels alike, now or after a
      # panel moved to the link: drop what MQTT still holds for linked ones.
      async def mqtt_callback(message: Any) -> None:
        if link_owned(hass, message.topic):
          return
        result = msg_callback(message)
        if asyncio.iscoroutine(result):
          await result

      unsubscribes.append(await _mqtt.async_subscribe(hass, topic, mqtt_callback, *args, **kwargs))
    elif not link_owned(hass, topic):
      unsubscribes.append(await _mqtt.async_subscribe(hass, topic, msg_callback, *args, **kwargs))

  def unsubscribe() -> None:
    while unsubscribes:
      unsubscribes.pop()()

  return unsubscribe


def is_connected(hass: Any) -> bool:
  """The MQTT connection while it is used; the in-process broker otherwise."""
  if link_broker(hass) is not None and not mqtt_wanted(hass):
    return True
  try:
    return bool(_mqtt.is_connected(hass))
  except Exception:
    return link_broker(hass) is not None


def async_subscribe_connection_status(hass: Any, connection_status_callback: Callable[[bool], None]) -> Callable[[], None]:
  """MQTT connection changes; linked panels never see a broker outage."""
  subscribe = getattr(_mqtt, "async_subscribe_connection_status", None)
  if subscribe is None or (link_broker(hass) is not None and not mqtt_wanted(hass)):
    return lambda: None
  return subscribe(hass, connection_status_callback)
