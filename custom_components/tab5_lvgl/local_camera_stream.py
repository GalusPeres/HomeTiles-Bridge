"""Live JPEG stream from a panel's own camera to Home Assistant.

The module has no Home Assistant imports so the wire protocol, the upload
receiver and the viewer lifecycle can be tested without a Home Assistant
install. It reuses the acknowledged TCP design of ``camera_stream.py`` in the
reverse direction: the panel is the TCP client and the sender, the Bridge
listens on the shared camera port (8124-8131) and acknowledges every chunk.

Capability
    The panel announces ``"local_camera_stream": true`` next to
    ``"local_camera": true`` in its retained bridge configuration. Without it
    the camera entity keeps the still-image (snapshot) behaviour unchanged.

MQTT (base = the panel base topic), Bridge to panel, QoS 0, never retained
    ``{base}/cmnd/local_camera`` start and keepalive::

        {"v":1,"action":"stream","session":"<32 lowercase hex>",
         "host":"<Bridge IPv4>","port":<listener port>,"token":"<32 lowercase hex>",
         "width":640,"height":360,"fps":15,"quality":65,"ttl_ms":6000}

    The identical message is re-sent every 2 s while viewers exist. The panel
    stops by itself when ``ttl_ms`` passes without a keepalive. A different
    ``session`` replaces the previous stream. Stop::

        {"v":1,"action":"stream_stop","session":"<32 lowercase hex>"}

    Snapshot requests keep their shape without an ``action`` key
    (``{"v":1,"id":"<hex>","op":"snapshot","max_bytes":N}``).

TCP (panel connects to ``host:port``; all integers big-endian)
    1. Panel sends one ASCII line of at most 256 bytes within 5 s::

           b"HTCAMUP/1 <session> <token>\\n"

    2. Bridge answers the existing hello struct ``>4sHH`` (8 bytes):
       ``b"HTC1"``, status u16 (0 accepted, 1 rejected), chunk size u16
       (8192). The Bridge closes the connection after a rejection.
    3. Per frame the panel sends the existing frame header ``>4sB3xII``
       (16 bytes): ``b"HTF1"``, type u8, 3 zero bytes, sequence u32,
       payload length u32. Type 1 is a JPEG frame (length 4..131072),
       type 2 (flush) and type 3 (end, closes the upload) carry length 0.
    4. The JPEG follows in chunks of exactly ``min(8192, remaining)`` bytes.
       After each chunk the panel waits for the existing ACK struct ``>4sII``
       (12 bytes): ``b"HTA1"``, the frame's sequence u32 and the cumulative
       number of payload bytes received for that frame u32. The Bridge reads
       and acknowledges chunks strictly in order; a panel may send the next
       chunk before the previous ACK arrives. The next frame header may be
       sent only after the final ACK of the previous frame.
    5. The Bridge closes the connection on any violation: bad magic, unknown
       type, length out of range, a payload that does not start with FFD8 (checked
       on the first chunk) or end with FFD9 (checked before the final ACK, so
       an invalid frame is never fully acknowledged), 5 s without a pending
       chunk, or 10 s without a new frame header.
    6. One upload per panel: a second authenticated connection for the same
       session replaces (closes) the first. Revoking or replacing the session
       also closes it. While keepalives continue, the panel may reconnect
       after a closed or failed connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
import ipaddress
import logging
import re
import secrets
import socket
import struct
from time import monotonic
from typing import Any, Final

from .local_camera import LOCAL_CAMERA_PROTOCOL_VERSION, RateLimitedWarnings

_LOGGER = logging.getLogger(__name__)

# --- Wire constants ---------------------------------------------------------
# Magic values and struct layouts are shared with camera_stream.py (display
# downstream); only the handshake line identifies the upload direction.
UPLOAD_REQUEST_PREFIX: Final = "HTCAMUP/1 "
UPLOAD_HANDSHAKE_MAX_BYTES: Final = 256
HELLO_MAGIC: Final = b"HTC1"
FRAME_MAGIC: Final = b"HTF1"
ACK_MAGIC: Final = b"HTA1"
HELLO_STRUCT: Final = struct.Struct(">4sHH")
FRAME_STRUCT: Final = struct.Struct(">4sB3xII")
ACK_STRUCT: Final = struct.Struct(">4sII")
HELLO_ACCEPTED: Final = 0
HELLO_REJECTED: Final = 1
MESSAGE_FRAME: Final = 1
MESSAGE_FLUSH: Final = 2
MESSAGE_END: Final = 3
CHUNK_BYTES: Final = 8 * 1024
MAX_JPEG_BYTES: Final = 131072
MIN_JPEG_BYTES: Final = 4
HANDSHAKE_TIMEOUT_S: Final = 5.0
CHUNK_TIMEOUT_S: Final = 5.0
FRAME_IDLE_TIMEOUT_S: Final = 10.0

# --- MQTT request constants -------------------------------------------------
ACTION_STREAM: Final = "stream"
ACTION_STREAM_STOP: Final = "stream_stop"
STREAM_WIDTH: Final = 640
STREAM_HEIGHT: Final = 360
# Requested rate for the Auto mode. The panel caps it to its own mode table
# and lowers the JPEG quality for high rates.
STREAM_FPS: Final = 25
STREAM_QUALITY: Final = 65
STREAM_TTL_MS: Final = 6000

# --- Viewer lifecycle -------------------------------------------------------
KEEPALIVE_INTERVAL_S: Final = 2.0
STOP_GRACE_S: Final = 3.0
# A live frame older than this no longer answers a still-image request.
LIVE_FRAME_FRESH_S: Final = 1.0
# How long an MJPEG viewer waits for the next live frame before falling back.
LIVE_FRAME_WAIT_S: Final = 5.0
# Consecutive waits without a live frame after which an MJPEG viewer's
# response ends instead of repeating the last image (a frozen view).
LIVE_FRAME_MAX_MISSES: Final = 3
# Bridge-side backstop: an upload session not refreshed by a keepalive for
# this long no longer authenticates new connections.
SESSION_EXPIRY_S: Final = STREAM_TTL_MS / 1000.0 + 2 * KEEPALIVE_INTERVAL_S
WARNING_INTERVAL_S: Final = 60.0

_SESSION_RE = re.compile(r"^[0-9a-f]{16,32}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{16,64}$")
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


class UploadViolation(Exception):
    """The panel broke the upload protocol; the connection is closed."""


def new_session_id() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_hex(16)


def valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and _SESSION_RE.fullmatch(value) is not None


def valid_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def valid_ipv4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return not (address.is_unspecified or address.is_multicast)


def build_stream_request(session: str, host: str, port: int, token: str) -> dict[str, Any]:
    if not valid_session_id(session) or not valid_token(token):
        raise ValueError("invalid_stream_session")
    if not valid_ipv4(host) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid_stream_endpoint")
    return {
        "v": LOCAL_CAMERA_PROTOCOL_VERSION,
        "action": ACTION_STREAM,
        "session": session,
        "host": host,
        "port": port,
        "token": token,
        "width": STREAM_WIDTH,
        "height": STREAM_HEIGHT,
        "fps": STREAM_FPS,
        "quality": STREAM_QUALITY,
        "ttl_ms": STREAM_TTL_MS,
    }


def build_stream_stop(session: str) -> dict[str, Any]:
    if not valid_session_id(session):
        raise ValueError("invalid_stream_session")
    return {"v": LOCAL_CAMERA_PROTOCOL_VERSION, "action": ACTION_STREAM_STOP, "session": session}


def parse_upload_handshake(line: Any) -> tuple[str, str] | None:
    """Return ``(session, token)`` from ``HTCAMUP/1 <session> <token>``."""
    if isinstance(line, (bytes, bytearray)):
        if len(line) > UPLOAD_HANDSHAKE_MAX_BYTES:
            return None
        try:
            line = bytes(line).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(line, str) or len(line) > UPLOAD_HANDSHAKE_MAX_BYTES:
        return None
    line = line.rstrip("\r\n")
    if not line.startswith(UPLOAD_REQUEST_PREFIX):
        return None
    parts = line[len(UPLOAD_REQUEST_PREFIX):].split(" ")
    if len(parts) != 2 or not valid_session_id(parts[0]) or not valid_token(parts[1]):
        return None
    return parts[0], parts[1]


def is_upload_handshake(line: str) -> bool:
    return isinstance(line, str) and line.startswith(UPLOAD_REQUEST_PREFIX)


# --- Upload receiver --------------------------------------------------------

class _UploadConnection:
    """One authenticated panel connection; closing it ends its receive loop."""

    __slots__ = ("writer", "closed")

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Closing the transport feeds EOF to the reader and wakes the loop.
        with suppress(Exception):
            self.writer.close()


@dataclass(slots=True)
class _UploadSession:
    token: str
    on_frame: Callable[[bytes], None]
    expires_at: float
    connection: _UploadConnection | None = None


class LocalCameraUploadRegistry:
    """Authorised upload sessions and their single active connection."""

    def __init__(self, *, clock: Callable[[], float] = monotonic,
                 expiry_s: float = SESSION_EXPIRY_S) -> None:
        self._clock = clock
        self._expiry_s = expiry_s
        self._sessions: dict[str, _UploadSession] = {}
        self._warnings = RateLimitedWarnings(WARNING_INTERVAL_S)

    def register(self, session: str, token: str, on_frame: Callable[[bytes], None]) -> None:
        if not valid_session_id(session) or not valid_token(token):
            raise ValueError("invalid_stream_session")
        self.revoke(session)
        self._sessions[session] = _UploadSession(
            token=token, on_frame=on_frame, expires_at=self._clock() + self._expiry_s)

    def touch(self, session: str) -> None:
        if (entry := self._sessions.get(session)) is not None:
            entry.expires_at = self._clock() + self._expiry_s

    def disconnect(self, session: str) -> None:
        """Close the active connection but keep the session authorised."""
        entry = self._sessions.get(session)
        if entry is not None and entry.connection is not None:
            entry.connection.close()
            entry.connection = None

    def revoke(self, session: str) -> None:
        self.disconnect(session)
        self._sessions.pop(session, None)

    def close_all(self) -> None:
        for session in list(self._sessions):
            self.revoke(session)

    def is_registered(self, session: str) -> bool:
        return session in self._sessions

    def is_connected(self, session: str) -> bool:
        entry = self._sessions.get(session)
        return entry is not None and entry.connection is not None

    def _warn(self, reason: str, message: str, *args) -> None:
        if self._warnings.allow(reason, self._clock()):
            _LOGGER.warning(message, *args)

    def _authorise(self, session: str, token: str) -> _UploadSession | None:
        entry = self._sessions.get(session)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self.revoke(session)
            return None
        if not secrets.compare_digest(entry.token, token):
            return None
        return entry

    async def async_handle_upload(
        self,
        line: bytes | str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: Any = None,
    ) -> None:
        """Run one upload after the listener read the handshake line.

        The caller owns *writer* and closes it after this returns.
        """
        parsed = parse_upload_handshake(line)
        entry = self._authorise(*parsed) if parsed is not None else None
        if entry is None:
            self._warn("rejected", "HomeTiles local camera stream upload rejected from %s: %s",
                       peer, "invalid handshake" if parsed is None else "unknown session or token")
            with suppress(Exception):
                writer.write(HELLO_STRUCT.pack(HELLO_MAGIC, HELLO_REJECTED, CHUNK_BYTES))
                await writer.drain()
            return

        session = parsed[0]
        connection = _UploadConnection(writer)
        previous = entry.connection
        entry.connection = connection
        if previous is not None:
            _LOGGER.debug("HomeTiles local camera stream upload replaced for session %s", session)
            previous.close()

        transport_socket = writer.get_extra_info("socket")
        if transport_socket is not None:
            with suppress(OSError):
                # ACKs are 12 bytes; Nagle would delay every one of them.
                transport_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        frames = 0
        try:
            writer.write(HELLO_STRUCT.pack(HELLO_MAGIC, HELLO_ACCEPTED, CHUNK_BYTES))
            await writer.drain()
            _LOGGER.debug("HomeTiles local camera stream upload connected from %s", peer)
            while not connection.closed:
                jpeg = await self._async_receive_frame(reader, writer)
                if jpeg is None:
                    break
                if connection.closed or entry.connection is not connection:
                    break
                if self._sessions.get(session) is not entry:
                    break
                frames += 1
                entry.on_frame(jpeg)
        except UploadViolation as err:
            self._warn(str(err), "HomeTiles local camera stream upload closed for %s: %s", peer, err)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            if not connection.closed:
                _LOGGER.debug("HomeTiles local camera stream upload disconnected (%s)", peer)
        finally:
            connection.close()
            if entry.connection is connection:
                entry.connection = None
            _LOGGER.debug("HomeTiles local camera stream upload ended (%s, %d frames)", peer, frames)

    @staticmethod
    async def _async_receive_frame(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bytes | None:
        """Return one validated JPEG, or None when the panel ended the upload."""
        while True:
            header = await asyncio.wait_for(
                reader.readexactly(FRAME_STRUCT.size), FRAME_IDLE_TIMEOUT_S)
            magic, message_type, sequence, length = FRAME_STRUCT.unpack(header)
            if magic != FRAME_MAGIC:
                raise UploadViolation("invalid_frame_magic")
            if message_type in (MESSAGE_FLUSH, MESSAGE_END):
                if length:
                    raise UploadViolation("invalid_control_length")
                if message_type == MESSAGE_END:
                    return None
                continue
            if message_type != MESSAGE_FRAME:
                raise UploadViolation("invalid_frame_type")
            if length < MIN_JPEG_BYTES:
                raise UploadViolation("frame_too_small")
            if length > MAX_JPEG_BYTES:
                raise UploadViolation("frame_too_large")
            break

        payload = bytearray(length)
        received = 0
        while received < length:
            size = min(CHUNK_BYTES, length - received)
            chunk = await asyncio.wait_for(reader.readexactly(size), CHUNK_TIMEOUT_S)
            if received == 0 and chunk[:2] != _JPEG_SOI:
                raise UploadViolation("jpeg_start_missing")
            payload[received:received + size] = chunk
            received += size
            if received == length and payload[-2:] != _JPEG_EOI:
                # Checked before the final ACK so a broken frame is never
                # reported as delivered.
                raise UploadViolation("jpeg_end_missing")
            writer.write(ACK_STRUCT.pack(ACK_MAGIC, sequence, received))
            await writer.drain()
        return bytes(payload)


# --- Viewer lifecycle -------------------------------------------------------

Endpoint = tuple[str, int]


class LocalCameraLiveStream:
    """Start, keep alive and stop one panel's live stream for its viewers.

    Only the latest frame is kept. The first viewer starts the stream, the
    last viewer stops it after a short grace period so page reloads and
    dashboard switches do not restart the camera pipeline.
    """

    def __init__(
        self,
        *,
        publish: Callable[[dict[str, Any]], Awaitable[None]],
        endpoint: Callable[[], Awaitable[Endpoint | None]],
        registry: Callable[[], LocalCameraUploadRegistry | None],
        ready: Callable[[], bool],
        log_name: str = "",
        clock: Callable[[], float] = monotonic,
        keepalive_s: float = KEEPALIVE_INTERVAL_S,
        grace_s: float = STOP_GRACE_S,
        session_factory: Callable[[], str] = new_session_id,
        token_factory: Callable[[], str] = new_token,
    ) -> None:
        self._publish = publish
        self._endpoint = endpoint
        self._registry = registry
        self._ready = ready
        self._log_name = log_name
        self._clock = clock
        self._keepalive_s = keepalive_s
        self._grace_s = grace_s
        self._session_factory = session_factory
        self._token_factory = token_factory
        self._warnings = RateLimitedWarnings(WARNING_INTERVAL_S)
        self._viewers = 0
        self._task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._session: str | None = None
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._frame_event: asyncio.Event | None = None
        # Terminal: set when the owning entity is removed; never restarts.
        self._closed = False
        # Bumped when the panel ends the current session on its display:
        # viewers that started before it end, later viewers get a new session.
        self._generation = 0
        # Set when the panel ended the session on its display, with its last
        # frame. Until a viewer starts a new session, Home Assistant keeps
        # showing that frame instead of asking the panel for new stills.
        self._ended = False
        self._ended_frame: bytes | None = None

    @property
    def viewers(self) -> int:
        return self._viewers

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def session(self) -> str | None:
        return self._session

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def ended(self) -> bool:
        """True after the panel ended its session, until the next viewer."""
        return self._ended

    @property
    def ended_frame(self) -> bytes | None:
        """The last frame of the session the panel ended, if any."""
        return self._ended_frame

    def _warn(self, reason: str, message: str, *args) -> None:
        if self._warnings.allow(reason, self._clock()):
            _LOGGER.warning(message, *args)

    # Frames ------------------------------------------------------------

    def _on_frame(self, jpeg: bytes) -> None:
        self._frame = jpeg
        self._frame_at = self._clock()
        event = self._frame_event
        self._frame_event = None
        if event is not None:
            event.set()

    def latest(self, max_age_s: float = LIVE_FRAME_FRESH_S) -> bytes | None:
        if self._frame is None or self._frame_at is None:
            return None
        if self._clock() - self._frame_at >= max_age_s:
            return None
        return self._frame

    def _ended_for(self, generation: int | None) -> bool:
        return self._closed or (generation is not None and generation != self._generation)

    async def async_next_frame(self, last: bytes | None, timeout_s: float,
                               generation: int | None = None) -> bytes | None:
        """Return a fresh frame other than *last*, or None after *timeout_s*.

        Always None once the stream is closed, or once the panel ended the
        session of the viewer's *generation*, so waiting viewers end.
        """
        if self._ended_for(generation):
            return None
        frame = self.latest()
        if frame is not None and frame is not last:
            return frame
        if self._frame_event is None:
            self._frame_event = asyncio.Event()
        event = self._frame_event
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except asyncio.TimeoutError:
            return None
        return None if self._ended_for(generation) else self._frame

    # Viewers -----------------------------------------------------------

    def acquire(self) -> None:
        # Opening the camera again: new stills and a new session are wanted.
        self._ended = False
        self._ended_frame = None
        self._viewers += 1
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        if not self.running and not self._closed:
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._async_run())

    def release(self) -> None:
        if self._viewers <= 0:
            return
        self._viewers -= 1
        if self._viewers == 0 and self.running and self._grace_task is None:
            self._grace_task = asyncio.get_running_loop().create_task(self._async_grace_stop())

    def poke(self) -> None:
        """Re-evaluate readiness now (availability or broker state changed)."""
        if self._wake is not None:
            self._wake.set()

    async def _async_grace_stop(self) -> None:
        try:
            await asyncio.sleep(self._grace_s)
        except asyncio.CancelledError:
            return
        self._grace_task = None
        if self._viewers == 0:
            await self._async_stop_task()

    async def async_stop(self) -> None:
        """Stop immediately, independent of viewers; a new viewer restarts it."""
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        await self._async_stop_task()

    async def async_end_session(self, session: str) -> bool:
        """The panel ended *session* on its display: end every current viewer.

        The run stops without further keepalives. The next viewer that
        acquires the stream starts a new session, which the panel accepts.
        Returns False when *session* is not the running one.
        """
        if self._closed or not self.running or session != self._session:
            return False
        self._generation += 1
        self._ended = True
        self._ended_frame = self._frame
        self._frame = None
        self._frame_at = None
        event, self._frame_event = self._frame_event, None
        if event is not None:
            event.set()
        await self.async_stop()
        _LOGGER.info("HomeTiles local camera stream ended on the panel for %s", self._log_name)
        return True

    async def async_close(self) -> None:
        """Stop for good (entity removal or reload) and end every viewer."""
        self._closed = True
        self._frame = None
        self._frame_at = None
        self._ended_frame = None
        event, self._frame_event = self._frame_event, None
        if event is not None:
            event.set()
        await self.async_stop()

    async def _async_stop_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # Stream loop -------------------------------------------------------

    async def _async_send(self, request: dict[str, Any]) -> bool:
        try:
            await self._publish(request)
        except Exception as err:  # noqa: BLE001 - the transport error type is HA-specific
            self._warn("publish_failed", "HomeTiles local camera stream request failed for %s: %s",
                       self._log_name, err)
            return False
        return True

    async def _async_activate(self, session: str, token: str) -> dict[str, Any] | None:
        registry = self._registry()
        if registry is None:
            self._warn("no_listener", "HomeTiles local camera stream unavailable for %s: no TCP listener",
                       self._log_name)
            return None
        try:
            endpoint = await self._endpoint()
        except Exception as err:  # noqa: BLE001 - network helpers may raise HA-specific errors
            endpoint = None
            _LOGGER.debug("HomeTiles local camera stream endpoint lookup failed: %s", err)
        if endpoint is None:
            self._warn("no_endpoint", "HomeTiles local camera stream unavailable for %s: no reachable IPv4 address",
                       self._log_name)
            return None
        request = build_stream_request(session, endpoint[0], endpoint[1], token)
        registry.register(session, token, self._on_frame)
        return request

    async def _async_run(self) -> None:
        session = self._session_factory()
        token = self._token_factory()
        self._session = session
        request: dict[str, Any] | None = None
        wake = self._wake
        try:
            while True:
                registry = self._registry()
                if self._ready():
                    if request is None:
                        request = await self._async_activate(session, token)
                        if request is not None:
                            _LOGGER.debug("HomeTiles local camera stream starting for %s", self._log_name)
                    if request is not None:
                        if registry is not None:
                            registry.touch(session)
                        await self._async_send(request)
                elif request is not None:
                    # Panel offline, camera not ready or broker gone: drop the
                    # connection and the endpoint; the panel's ttl ends capture.
                    _LOGGER.debug("HomeTiles local camera stream suspended for %s", self._log_name)
                    if registry is not None:
                        registry.revoke(session)
                    request = None
                if wake is not None:
                    wake.clear()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(wake.wait(), self._keepalive_s)
                else:
                    await asyncio.sleep(self._keepalive_s)
        finally:
            registry = self._registry()
            if registry is not None:
                registry.revoke(session)
            if request is not None and self._ready():
                await self._async_send(build_stream_stop(session))
            if self._session == session:
                self._session = None
            _LOGGER.debug("HomeTiles local camera stream stopped for %s", self._log_name)
