"""TCP server of the direct panel link.

Protocol reference: docs-dev/bridge-link.md in the HomeTiles firmware
repository. Each panel connection runs the handshake of link_protocol.py and
then becomes a session of link_broker.py. The module has no Home Assistant
imports: the integration passes in how a hello is resolved, so the tests run
the exact server with real sockets.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import logging
import os
import time
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple, Union

from .link_broker import LinkBroker
from .link_protocol import (
  HANDSHAKE_TIMEOUT_S,
  IDLE_TIMEOUT_S,
  MAX_PAYLOAD,
  MAX_STREAM_CHUNK,
  MODE_PAIR,
  MODE_SESSION,
  NONCE_LENGTH,
  REFUSE_INVALID,
  REFUSE_PAIR,
  REFUSE_UNKNOWN,
  REFUSE_VERSION,
  TYPE_HELLO,
  TYPE_PING,
  TYPE_PONG,
  TYPE_PUBLISH,
  TYPE_READY,
  TYPE_REFUSE,
  TYPE_STREAM_BEGIN,
  TYPE_STREAM_DATA,
  TYPE_STREAM_END,
  TYPE_SUBSCRIBE,
  TYPE_UNSUBSCRIBE,
  TYPE_WELCOME,
  Opener,
  ProtocolError,
  Sealer,
  build_refuse,
  build_welcome,
  parse_frame_length,
  parse_hello,
  parse_publish,
  parse_stream_begin,
  parse_topic,
  plain_frame,
  publish_payload,
  session_keys,
  split_body,
  stream_begin_payload,
)

_LOGGER = logging.getLogger(__name__)

LINK_TCP_PORT_FIRST = 8140
LINK_TCP_PORT_LAST = 8147
# A panel that does not read its frames is dropped before it holds much memory.
MAX_QUEUED_BYTES = 4 * 1024 * 1024
LOG_INTERVAL_S = 60.0
# Queued in place of a frame: the writer sends the waiting streams.
_STREAMS = object()


class SessionTarget:
  """What the integration knows about a paired panel that connects."""

  __slots__ = ("pairing_key", "ha_prefix")

  def __init__(self, pairing_key: bytes, ha_prefix: str) -> None:
    self.pairing_key = pairing_key
    self.ha_prefix = ha_prefix

  def __repr__(self) -> str:  # Never expose the key.
    return f"SessionTarget(ha_prefix={self.ha_prefix!r})"


# resolve_session(device id, key id, base topic) -> SessionTarget or a refuse reason
ResolveSession = Callable[[str, str, str], Union[SessionTarget, str]]
# accept_pairing(device id, base topic) -> ha_prefix, or None to refuse
AcceptPairing = Callable[[str, str], Optional[str]]


class _Connection:
  """One panel connection; a session of the broker once the handshake is done."""

  def __init__(self, server: "LinkServer", writer: asyncio.StreamWriter, peer: str) -> None:
    self._server = server
    self._writer = writer
    self.peer = peer
    self.device_id = ""
    self.base = ""
    self.ha_prefix = ""
    self.mode = ""
    self.subscriptions: Set[str] = set()
    # The largest stream the panel announced (0: it takes no streams).
    self.rx_max = 0
    self._sealer: Optional[Sealer] = None
    # Frames wait unsealed: the writer seals them in the order they leave, so
    # stream frames and the frames queued between them keep the counter order.
    self._queue: "asyncio.Queue[Any]" = asyncio.Queue()
    self._queued_bytes = 0
    # Streams to the panel: [topic, payload, retain, offset], offset -1 before
    # the begin frame. Queued frames pass between their data frames.
    self._streams: Deque[List[Any]] = deque()
    self._writer_task: Optional[asyncio.Task] = None
    self.closed = False
    self._stream: Optional[Dict[str, Any]] = None

  # ---- Sending ------------------------------------------------------------

  def start_writer(self) -> None:
    self._writer_task = asyncio.get_running_loop().create_task(self._write_loop())

  def _frame(self, frame_type: int, payload: bytes = b"") -> bytes:
    if self._sealer is not None:
      return self._sealer.frame(frame_type, payload)
    return plain_frame(frame_type, payload)

  def _account(self, size: int) -> bool:
    self._queued_bytes += size
    if self._queued_bytes > MAX_QUEUED_BYTES:
      _LOGGER.warning("HomeTiles link: panel %s does not read its messages; closing", self.device_id)
      self.close()
      return False
    return True

  def send(self, frame_type: int, payload: bytes = b"") -> None:
    if self.closed or not self._account(len(payload)):
      return
    self._queue.put_nowait((frame_type, payload))

  def send_publish(self, topic: str, payload: bytes, retain: bool) -> None:
    if len(payload) > MAX_PAYLOAD:
      if len(payload) <= self.rx_max:
        self.send_stream(topic, payload, retain)
        return
      if self._server.log_due(f"large:{topic}"):
        _LOGGER.warning("HomeTiles link: %s has %d bytes, more than panel %s accepts; dropped",
                        topic, len(payload), self.device_id)
      return
    self.send(TYPE_PUBLISH, publish_payload(topic, payload, retain))

  def send_stream(self, topic: str, payload: bytes, retain: bool) -> None:
    """A message above MAX_PAYLOAD, as begin, data and end frames."""
    if self.closed:
      return
    # A newer picture of a topic replaces one that has not started yet.
    for index, stream in enumerate(self._streams):
      if stream[0] == topic and stream[3] < 0:
        self._queued_bytes -= len(stream[1])
        if self._account(len(payload)):
          self._streams[index] = [topic, payload, retain, -1]
        return
    if not self._account(len(payload)):
      return
    self._streams.append([topic, payload, retain, -1])
    self._queue.put_nowait(_STREAMS)

  def _next_stream_frame(self) -> Tuple[int, bytes]:
    stream = self._streams[0]
    topic, payload, retain, offset = stream
    if offset < 0:
      stream[3] = 0
      return TYPE_STREAM_BEGIN, stream_begin_payload(topic, len(payload), retain)
    if offset < len(payload):
      chunk = payload[offset:offset + MAX_STREAM_CHUNK]
      stream[3] = offset + len(chunk)
      self._queued_bytes -= len(chunk)
      return TYPE_STREAM_DATA, chunk
    self._streams.popleft()
    return TYPE_STREAM_END, b""

  async def _write(self, frame_type: int, payload: bytes) -> None:
    self._writer.write(self._frame(frame_type, payload))
    await self._writer.drain()

  async def _write_loop(self) -> None:
    try:
      while True:
        item = await self._queue.get()
        if item is None:
          break
        if item is not _STREAMS:
          self._queued_bytes -= len(item[1])
          await self._write(*item)
          continue
        while self._streams and not self.closed:
          # Queued frames first, then the next frame of the stream.
          while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is None:
              return
            if item is not _STREAMS:
              self._queued_bytes -= len(item[1])
              await self._write(*item)
          await self._write(*self._next_stream_frame())
          # drain() returns at once while the socket buffer has room; give
          # the producers a turn so their messages pass between the pieces.
          await asyncio.sleep(0)
    except (ConnectionError, OSError):
      pass
    finally:
      self.close()

  def write_now(self, frame: bytes) -> None:
    """Handshake frames go out before the writer task starts."""
    self._writer.write(frame)

  def close(self) -> None:
    if self.closed:
      return
    self.closed = True
    self._queue.put_nowait(None)
    with suppress(Exception):
      self._writer.close()

  # ---- Receiving ----------------------------------------------------------

  def handle(self, broker: LinkBroker, frame_type: int, payload: bytes) -> None:
    if frame_type == TYPE_PUBLISH:
      topic, data, retain = parse_publish(payload)
      broker.panel_publish(self, topic, data, retain)
    elif frame_type == TYPE_SUBSCRIBE:
      broker.panel_subscribe(self, parse_topic(payload))
    elif frame_type == TYPE_UNSUBSCRIBE:
      broker.panel_unsubscribe(self, parse_topic(payload))
    elif frame_type == TYPE_PING:
      self.send(TYPE_PONG)
    elif frame_type == TYPE_PONG:
      pass
    elif frame_type == TYPE_STREAM_BEGIN:
      if self._stream is not None:
        raise ProtocolError("stream_nested")
      topic, total, retain = parse_stream_begin(payload)
      self._stream = {"topic": topic, "total": total, "retain": retain, "data": bytearray()}
    elif frame_type == TYPE_STREAM_DATA:
      stream = self._stream
      if stream is None or len(payload) > MAX_STREAM_CHUNK or len(stream["data"]) + len(payload) > stream["total"]:
        raise ProtocolError("stream_data")
      stream["data"].extend(payload)
    elif frame_type == TYPE_STREAM_END:
      stream = self._stream
      self._stream = None
      if stream is None or len(stream["data"]) != stream["total"] or payload:
        raise ProtocolError("stream_end")
      broker.panel_publish(self, stream["topic"], bytes(stream["data"]), stream["retain"])
    else:
      raise ProtocolError("frame_type")


class LinkServer:
  """Accepts panels, runs the handshake and attaches them to the broker."""

  def __init__(
    self,
    broker: LinkBroker,
    *,
    resolve_session: ResolveSession,
    accept_pairing: AcceptPairing,
    random: Callable[[int], bytes] = os.urandom,
    clock: Callable[[], float] = time.monotonic,
  ) -> None:
    self.broker = broker
    self._resolve_session = resolve_session
    self._accept_pairing = accept_pairing
    self._random = random
    self._clock = clock
    self._server: Optional[asyncio.base_events.Server] = None
    self._connections: Set[_Connection] = set()
    self._tasks: Set[asyncio.Task] = set()
    self._log_at: Dict[str, float] = {}
    self.port: Optional[int] = None

  def log_due(self, key: str, interval: float = LOG_INTERVAL_S) -> bool:
    now = self._clock()
    last = self._log_at.get(key)
    if last is not None and now - last < interval:
      return False
    if len(self._log_at) > 256:
      self._log_at.clear()
    self._log_at[key] = now
    return True

  async def async_start(self, host: Optional[str] = None,
                        ports: Iterable[int] = range(LINK_TCP_PORT_FIRST, LINK_TCP_PORT_LAST + 1)) -> int:
    """Listen on the first free port; OSError when none is free."""
    last_error: Optional[OSError] = None
    for port in ports:
      try:
        self._server = await asyncio.start_server(self._accept, host=host, port=port)
      except OSError as err:
        last_error = err
        continue
      sockets = self._server.sockets or []
      self.port = sockets[0].getsockname()[1] if sockets else port
      _LOGGER.info("HomeTiles link server listening on TCP port %s", self.port)
      return self.port
    raise last_error or OSError("no free port for the HomeTiles link server")

  async def async_stop(self) -> None:
    if self._server is not None:
      self._server.close()
      with suppress(Exception):
        await self._server.wait_closed()
      self._server = None
    for connection in list(self._connections):
      connection.close()
    for task in list(self._tasks):
      task.cancel()
    for task in list(self._tasks):
      with suppress(asyncio.CancelledError, Exception):
        await task

  def connected(self, device_id: str) -> bool:
    session = self.broker.session(device_id)
    return session is not None and session.mode == MODE_SESSION

  def disconnect(self, device_id: str) -> None:
    """Close the connection of a panel, for example after its entry was removed."""
    session = self.broker.session(device_id)
    if isinstance(session, _Connection):
      session.close()

  async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    task = asyncio.current_task()
    if task is not None:
      self._tasks.add(task)
    peer = "?"
    with suppress(Exception):
      peer = str(writer.get_extra_info("peername")[0])
    connection = _Connection(self, writer, peer)
    self._connections.add(connection)
    try:
      await self._run(connection, reader)
    except asyncio.CancelledError:
      raise
    except ProtocolError as err:
      if self.log_due(f"protocol:{peer}"):
        _LOGGER.warning("HomeTiles link: connection from %s closed (%s)", peer, err)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
      pass
    finally:
      self.broker.detach(connection)
      connection.close()
      self._connections.discard(connection)
      if task is not None:
        self._tasks.discard(task)
      if connection.mode == MODE_SESSION:
        _LOGGER.info("HomeTiles link: panel %s disconnected", connection.device_id)

  async def _read_frame(self, reader: asyncio.StreamReader, timeout: float) -> tuple:
    header = await asyncio.wait_for(reader.readexactly(4), timeout)
    length = parse_frame_length(header)
    body = await asyncio.wait_for(reader.readexactly(length), timeout)
    return header, body

  def _refuse(self, connection: _Connection, reason: str) -> None:
    connection.write_now(plain_frame(TYPE_REFUSE, build_refuse(reason)))

  async def _run(self, connection: _Connection, reader: asyncio.StreamReader) -> None:
    _header, body = await self._read_frame(reader, HANDSHAKE_TIMEOUT_S)
    frame_type, payload = split_body(body)
    if frame_type != TYPE_HELLO:
      self._refuse(connection, REFUSE_INVALID)
      raise ProtocolError("hello_expected")
    try:
      hello = parse_hello(payload)
    except ProtocolError as err:
      self._refuse(connection, REFUSE_VERSION if str(err) == REFUSE_VERSION else REFUSE_INVALID)
      raise
    connection.device_id = hello["id"]
    connection.base = hello["base"]
    opener: Optional[Opener] = None

    if hello["mode"] == MODE_SESSION:
      target = self._resolve_session(hello["id"], hello["kid"], hello["base"])
      if not isinstance(target, SessionTarget):
        reason = target if isinstance(target, str) else REFUSE_UNKNOWN
        self._refuse(connection, reason)
        if self.log_due(f"refuse:{hello['id']}:{reason}"):
          _LOGGER.warning("HomeTiles link: panel %s (%s) refused (%s)", hello["id"], connection.peer, reason)
        return
      bridge_nonce = self._random(NONCE_LENGTH)
      connection.write_now(plain_frame(TYPE_WELCOME, build_welcome(bridge_nonce)))
      panel_key, bridge_key = session_keys(target.pairing_key, hello["id"], hello["base"], hello["n"], bridge_nonce)
      connection._sealer = Sealer(bridge_key)
      opener = Opener(panel_key)
      connection.write_now(connection._sealer.frame(TYPE_READY))
      header, body = await self._read_frame(reader, HANDSHAKE_TIMEOUT_S)
      frame_type, _payload = opener.open(header, body)
      if frame_type != TYPE_READY:
        raise ProtocolError("ready_expected")
      connection.ha_prefix = target.ha_prefix
      connection.mode = MODE_SESSION
      connection.rx_max = hello.get("rx", 0)
    else:
      ha_prefix = self._accept_pairing(hello["id"], hello["base"])
      if ha_prefix is None:
        self._refuse(connection, REFUSE_PAIR)
        if self.log_due(f"refuse:{hello['id']}:pair"):
          _LOGGER.info("HomeTiles link: panel %s asks to pair, but nothing waits for it", hello["id"])
        return
      connection.write_now(plain_frame(TYPE_WELCOME, build_welcome(None)))
      connection.ha_prefix = ha_prefix
      connection.mode = MODE_PAIR

    previous = self.broker.attach(connection)
    if isinstance(previous, _Connection):
      previous.close()
    connection.start_writer()
    _LOGGER.info("HomeTiles link: panel %s connected from %s (%s)",
                 connection.device_id, connection.peer, connection.mode)

    while not connection.closed:
      header, body = await self._read_frame(reader, IDLE_TIMEOUT_S)
      if opener is not None:
        frame_type, payload = opener.open(header, body)
      else:
        frame_type, payload = split_body(body)
      connection.handle(self.broker, frame_type, payload)
