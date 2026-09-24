"""Short-lived JPEG-frame video streams for the HomeTiles camera popup."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import secrets
import socket
import struct
import time
from typing import Any, Awaitable, Callable, Final

from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

from .local_camera_stream import (
  LIVE_FRAME_WAIT_S,
  STREAM_FPS as LIVE_STREAM_FPS,
  LocalCameraUploadRegistry,
  is_upload_handshake,
)

_LOGGER = logging.getLogger(__name__)

CAMERA_STREAM_TCP_PORT_FIRST: Final = 8124
CAMERA_STREAM_TCP_PORT_LAST: Final = 8131
CAMERA_STREAM_WIDTH: Final = 752
CAMERA_STREAM_HEIGHT: Final = 424
CAMERA_STREAM_FPS: Final = 24
CAMERA_STREAM_MIN_WIDTH: Final = 320
CAMERA_STREAM_MIN_HEIGHT: Final = 180
CAMERA_STREAM_MAX_PIXELS: Final = CAMERA_STREAM_WIDTH * CAMERA_STREAM_HEIGHT
# Still-image cameras are fetched back to back with one request in flight,
# never faster than this rate and never faster than the camera answers.
CAMERA_STILL_MAX_FPS: Final = 10
# While the next still image (or live frame) is pending, the previous frame is
# written again at this interval so FFmpeg keeps emitting output.
CAMERA_STILL_REPEAT_SECONDS: Final = 0.5
# After a failed still image the next request waits at least this long, so the
# failure limit still spans several seconds for a camera that fails fast.
CAMERA_STILL_RETRY_SECONDS: Final = 0.5
# FFmpeg's MJPEG scale is inverse: a larger value produces smaller frames.
# Direct streams must stay below roughly five acknowledged 8 KiB blocks at
# 24 FPS; still-image cameras retain the previous higher JPEG quality.
CAMERA_STREAM_JPEG_QUALITY: Final = 11
CAMERA_STILL_JPEG_QUALITY: Final = 7
CAMERA_SESSION_TTL_SECONDS: Final = 30.0
CAMERA_IMAGE_FAILURE_LIMIT: Final = 10
# After a popup stream the camera's panel ended: the "stopped" notice follows
# the TCP close, so the popup does not end on a connection error.
CAMERA_PANEL_END_STATUS_DELAY_S: Final = 0.5
CAMERA_MAX_JPEG_BYTES: Final = 256 * 1024
CAMERA_STREAM_TRANSPORT: Final = "tcp-ack-v1"
CAMERA_STREAM_FRAMING: Final = "ack-jpeg-v1"
CAMERA_BRIDGE_PROTOCOL_VERSION: Final = 1
CAMERA_STREAM_CHUNK_BYTES: Final = 8 * 1024
CAMERA_STREAM_HANDSHAKE_TIMEOUT_SECONDS: Final = 5.0
CAMERA_STREAM_ACK_TIMEOUT_SECONDS: Final = 5.0
CAMERA_STREAM_DIAGNOSTIC_INTERVAL_SECONDS: Final = 5.0
CAMERA_STREAM_REQUEST_PREFIX: Final = "HTCAM/1 "
CAMERA_STREAM_HELLO_MAGIC: Final = b"HTC1"
CAMERA_STREAM_FRAME_MAGIC: Final = b"HTF1"
CAMERA_STREAM_ACK_MAGIC: Final = b"HTA1"
CAMERA_STREAM_MESSAGE_FRAME: Final = 1
CAMERA_STREAM_MESSAGE_FLUSH: Final = 2
CAMERA_STREAM_MESSAGE_END: Final = 3
CAMERA_STREAM_HELLO_STRUCT: Final = struct.Struct(">4sHH")
CAMERA_STREAM_FRAME_STRUCT: Final = struct.Struct(">4sB3xII")
CAMERA_STREAM_ACK_STRUCT: Final = struct.Struct(">4sII")


class JpegFrameParser:
  """Reassemble FFmpeg's arbitrary stdout chunks into complete JPEG frames."""

  def __init__(self) -> None:
    self._buffer = bytearray()

  def feed(self, data: bytes) -> list[bytes]:
    """Return every complete SOI/EOI-delimited JPEG frame in *data*."""
    if data:
      self._buffer.extend(data)

    frames: list[bytes] = []
    while self._buffer:
      start = self._buffer.find(b"\xff\xd8")
      if start < 0:
        if len(self._buffer) > CAMERA_MAX_JPEG_BYTES:
          raise ValueError("jpeg_start_missing")
        if len(self._buffer) > 1:
          del self._buffer[:-1]
        break
      if start:
        del self._buffer[:start]
      finish = self._buffer.find(b"\xff\xd9", 2)
      if finish < 0:
        if len(self._buffer) > CAMERA_MAX_JPEG_BYTES:
          raise ValueError("jpeg_frame_too_large")
        break
      finish += 2
      if finish > CAMERA_MAX_JPEG_BYTES:
        raise ValueError("jpeg_frame_too_large")
      frames.append(bytes(self._buffer[:finish]))
      del self._buffer[:finish]
    return frames


@dataclass(slots=True)
class CameraStreamSession:
  """A single-use stream session created through MQTT."""

  token: str
  device_id: str
  entity_id: str
  source: str | None
  first_image: bytes | None
  width: int
  height: int
  fps: int
  expires_at: float
  stop_event: asyncio.Event
  # Live upload of another HomeTiles panel's camera (LocalCameraLiveStream).
  # The popup counts as one of its viewers while the TCP stream runs.
  live: Any = None
  # Newest still image fed to FFmpeg; an FFmpeg restart resumes from it
  # instead of the snapshot taken when the popup opened.
  latest_image: bytes | None = None
  still_rate_logged: bool = False
  # Set when the camera's panel ended the live session on its display; the
  # popup then gets a neutral "stopped" (on_panel_end) after its stream closed.
  ended_by_panel: bool = False
  on_panel_end: Callable[[], Awaitable[None]] | None = None


@dataclass(slots=True)
class CameraFrameSendMetrics:
  """Timing collected while one JPEG is sent and acknowledged."""

  chunks: int
  drain_seconds: float
  ack_wait_seconds: float
  max_ack_wait_seconds: float
  transport_seconds: float


@dataclass(slots=True)
class CameraStreamDiagnostics:
  """Low-overhead counters that locate the active camera bottleneck."""

  entity_id: str
  started_at: float = field(default_factory=time.monotonic)
  last_report_at: float = field(init=False)
  parsed_frames: int = 0
  parsed_bytes: int = 0
  dropped_frames: int = 0
  sent_frames: int = 0
  sent_bytes: int = 0
  ack_chunks: int = 0
  drain_seconds: float = 0.0
  ack_wait_seconds: float = 0.0
  transport_seconds: float = 0.0
  interval_max_ack_wait_seconds: float = 0.0
  interval_max_transport_seconds: float = 0.0
  first_parsed_ms: float | None = None
  first_sent_ms: float | None = None
  _reported_parsed_frames: int = 0
  _reported_parsed_bytes: int = 0
  _reported_dropped_frames: int = 0
  _reported_sent_frames: int = 0
  _reported_sent_bytes: int = 0
  _reported_ack_chunks: int = 0
  _reported_drain_seconds: float = 0.0
  _reported_ack_wait_seconds: float = 0.0
  _reported_transport_seconds: float = 0.0

  def __post_init__(self) -> None:
    self.last_report_at = self.started_at

  def note_parsed(self, jpeg: bytes) -> None:
    """Record one complete JPEG emitted by FFmpeg."""
    if self.first_parsed_ms is None:
      self.first_parsed_ms = (
        time.monotonic() - self.started_at
      ) * 1000.0
    self.parsed_frames += 1
    self.parsed_bytes += len(jpeg)

  def note_dropped(self) -> None:
    """Record one FFmpeg frame superseded in the latest-frame queue."""
    self.dropped_frames += 1

  def note_sent(
    self,
    jpeg: bytes,
    metrics: CameraFrameSendMetrics,
  ) -> None:
    """Record one complete, receiver-acknowledged JPEG."""
    if self.first_sent_ms is None:
      self.first_sent_ms = (
        time.monotonic() - self.started_at
      ) * 1000.0
    self.sent_frames += 1
    self.sent_bytes += len(jpeg)
    self.ack_chunks += metrics.chunks
    self.drain_seconds += metrics.drain_seconds
    self.ack_wait_seconds += metrics.ack_wait_seconds
    self.transport_seconds += metrics.transport_seconds
    self.interval_max_ack_wait_seconds = max(
      self.interval_max_ack_wait_seconds,
      metrics.max_ack_wait_seconds,
    )
    self.interval_max_transport_seconds = max(
      self.interval_max_transport_seconds,
      metrics.transport_seconds,
    )

  def maybe_log(self, *, force: bool = False) -> None:
    """Log one interval snapshot without changing stream behaviour."""
    now = time.monotonic()
    elapsed = now - self.last_report_at
    if (
      not force
      and elapsed < CAMERA_STREAM_DIAGNOSTIC_INTERVAL_SECONDS
    ):
      return
    if elapsed <= 0:
      return

    parsed = self.parsed_frames - self._reported_parsed_frames
    parsed_bytes = self.parsed_bytes - self._reported_parsed_bytes
    dropped = self.dropped_frames - self._reported_dropped_frames
    sent = self.sent_frames - self._reported_sent_frames
    sent_bytes = self.sent_bytes - self._reported_sent_bytes
    ack_chunks = self.ack_chunks - self._reported_ack_chunks
    drain_seconds = self.drain_seconds - self._reported_drain_seconds
    ack_wait_seconds = (
      self.ack_wait_seconds - self._reported_ack_wait_seconds
    )
    transport_seconds = (
      self.transport_seconds - self._reported_transport_seconds
    )

    _LOGGER.info(
      "[CameraDiag] %s interval=%.2fs ffmpeg=%.1f fps "
      "sent=%.1f fps drop=%d jpeg=%.1f KiB wire=%.2f Mbit/s "
      "tx=%.1f ms/frame "
      "drain=%.1f ms/frame ack=%.2f ms/chunk chunks=%.1f/frame "
      "max_ack=%.1f ms max_tx=%.1f ms",
      self.entity_id,
      elapsed,
      parsed / elapsed,
      sent / elapsed,
      dropped,
      parsed_bytes / parsed / 1024.0 if parsed else 0.0,
      sent_bytes * 8.0 / elapsed / 1_000_000.0,
      transport_seconds * 1000.0 / sent if sent else 0.0,
      drain_seconds * 1000.0 / sent if sent else 0.0,
      ack_wait_seconds * 1000.0 / ack_chunks if ack_chunks else 0.0,
      ack_chunks / sent if sent else 0.0,
      self.interval_max_ack_wait_seconds * 1000.0,
      self.interval_max_transport_seconds * 1000.0,
    )

    self.last_report_at = now
    self._reported_parsed_frames = self.parsed_frames
    self._reported_parsed_bytes = self.parsed_bytes
    self._reported_dropped_frames = self.dropped_frames
    self._reported_sent_frames = self.sent_frames
    self._reported_sent_bytes = self.sent_bytes
    self._reported_ack_chunks = self.ack_chunks
    self._reported_drain_seconds = self.drain_seconds
    self._reported_ack_wait_seconds = self.ack_wait_seconds
    self._reported_transport_seconds = self.transport_seconds
    self.interval_max_ack_wait_seconds = 0.0
    self.interval_max_transport_seconds = 0.0


class CameraStreamManager:
  """Own HomeTiles camera sessions and their FFmpeg processes."""

  def __init__(self, hass: HomeAssistant) -> None:
    self.hass = hass
    self._sessions: dict[str, CameraStreamSession] = {}
    self._device_tokens: dict[str, str] = {}
    self._processes: dict[str, asyncio.subprocess.Process] = {}
    self._stop_events: dict[str, asyncio.Event] = {}
    self._lock = asyncio.Lock()
    self._tcp_server: asyncio.AbstractServer | None = None
    self._tcp_port: int | None = None
    self._tcp_connection = CameraStreamConnection(self)
    # Uploads from a panel's own camera share this listener (reverse
    # direction, see local_camera_stream.py).
    self.local_camera_uploads = LocalCameraUploadRegistry()
    # HomeTiles panel cameras with a live upload, one entry per entity.
    self._live_cameras: list[Any] = []

  def register_live_camera(self, camera: Any) -> Callable[[], None]:
    """Let popup sessions find a panel camera's live stream by entity id."""
    if camera not in self._live_cameras:
      self._live_cameras.append(camera)

    def unregister() -> None:
      with suppress(ValueError):
        self._live_cameras.remove(camera)

    return unregister

  def live_camera_stream(self, entity_id: str) -> Any:
    """Return the open live stream of a registered panel camera, if any."""
    for camera in self._live_cameras:
      if getattr(camera, "entity_id", None) != entity_id:
        continue
      live = getattr(camera, "live_stream", None)
      if live is not None and not live.closed:
        return live
    return None

  async def async_start_tcp_server(self) -> None:
    """Start the LAN-only acknowledged TCP endpoint used by the display."""
    if self._tcp_server is not None:
      return

    last_error: OSError | None = None
    for port in range(
      CAMERA_STREAM_TCP_PORT_FIRST,
      CAMERA_STREAM_TCP_PORT_LAST + 1,
    ):
      try:
        server = await asyncio.start_server(
          self._tcp_connection.async_handle,
          "0.0.0.0",
          port,
          start_serving=True,
        )
      except OSError as err:
        last_error = err
        continue
      self._tcp_server = server
      self._tcp_port = port
      _LOGGER.info(
        "HomeTiles camera acknowledged TCP server listening on port %d",
        port,
      )
      return

    raise OSError(
      "No free HomeTiles camera TCP port in range "
      f"{CAMERA_STREAM_TCP_PORT_FIRST}-{CAMERA_STREAM_TCP_PORT_LAST}"
    ) from last_error

  @property
  def tcp_port(self) -> int | None:
    """Return the active LAN camera listener port."""
    return self._tcp_port

  def stream_url(self, host: str, token: str) -> str:
    """Build a local acknowledged-TCP URL using HA's actual LAN address."""
    if self._tcp_port is None:
      raise ValueError("camera_tcp_server_unavailable")
    if not host:
      raise ValueError("home_assistant_url_unavailable")
    if ":" in host:
      host = f"[{host}]"
    return f"tcp://{host}:{self._tcp_port}/{token}"

  async def async_shutdown(self) -> None:
    """Stop all camera work and close the auxiliary TCP listener."""
    async with self._lock:
      device_ids = (
        set(self._device_tokens)
        | set(self._processes)
        | set(self._stop_events)
      )
    for device_id in device_ids:
      await self.async_stop_device(device_id)
    self.local_camera_uploads.close_all()
    if self._tcp_server is not None:
      self._tcp_server.close()
      await self._tcp_server.wait_closed()
      self._tcp_server = None
      self._tcp_port = None

  async def async_create_session(
    self,
    device_id: str,
    entity_id: str,
    width: int = CAMERA_STREAM_WIDTH,
    height: int = CAMERA_STREAM_HEIGHT,
    fps: int = CAMERA_STREAM_FPS,
  ) -> CameraStreamSession:
    """Resolve a direct stream or a still-image camera into a video session."""
    width, height, fps = self._validate_stream_request(width, height, fps)
    source: str | None = None
    try:
      async with asyncio.timeout(10):
        source = await async_get_stream_source(self.hass, entity_id)
    except Exception as err:  # A still-image camera can legitimately reject this.
      _LOGGER.debug(
        "HomeTiles camera %s has no direct stream source: %s",
        entity_id,
        err,
      )

    first_image: bytes | None = None
    live: Any = None
    if not source:
      try:
        # No width/height: Home Assistant would rescale the JPEG on its event
        # loop for every fetch; FFmpeg scales and crops in its own process.
        image = await async_get_image(
          self.hass,
          entity_id,
          timeout=10,
        )
        first_image = bytes(image.content) if image.content else None
      except Exception as err:
        _LOGGER.debug(
          "HomeTiles camera %s did not provide a still image: %s",
          entity_id,
          err,
        )
      if not first_image:
        raise ValueError("camera_image_unavailable")
      live = self.live_camera_stream(entity_id)
      # A panel camera's live upload runs at its own stream rate; every
      # other still-image camera is bounded by the adaptive still rate.
      fps = min(
        fps, LIVE_STREAM_FPS if live is not None else CAMERA_STILL_MAX_FPS
      )

    await self.async_stop_device(device_id)
    token = secrets.token_urlsafe(24)
    stop_event = asyncio.Event()
    session = CameraStreamSession(
      token=token,
      device_id=device_id,
      entity_id=entity_id,
      source=source,
      first_image=first_image,
      width=width,
      height=height,
      fps=fps,
      expires_at=time.monotonic() + CAMERA_SESSION_TTL_SECONDS,
      stop_event=stop_event,
      live=live,
    )
    async with self._lock:
      self._drop_expired_sessions_locked()
      self._sessions[token] = session
      self._device_tokens[device_id] = token
      self._stop_events[device_id] = stop_event
    return session

  @staticmethod
  def _validate_stream_request(
    width: int, height: int, fps: int
  ) -> tuple[int, int, int]:
    """Validate a P4 popup format without relying on a device model name."""
    try:
      width = int(width)
      height = int(height)
      fps = int(fps)
    except (TypeError, ValueError) as err:
      raise ValueError("camera_invalid_stream_request") from err

    if (
      width < CAMERA_STREAM_MIN_WIDTH
      or width > CAMERA_STREAM_WIDTH
      or height < CAMERA_STREAM_MIN_HEIGHT
      or height > CAMERA_STREAM_HEIGHT
      or width % 2
      or height % 2
      or width * height > CAMERA_STREAM_MAX_PIXELS
      or abs(width * 9 - height * 16) > 16
      or fps < 1
      or fps > CAMERA_STREAM_FPS
    ):
      raise ValueError("camera_invalid_stream_request")
    return width, height, fps

  async def async_stop_device(self, device_id: str) -> None:
    """Revoke a pending session and terminate its active FFmpeg process."""
    process: asyncio.subprocess.Process | None = None
    stop_event: asyncio.Event | None = None
    async with self._lock:
      token = self._device_tokens.pop(device_id, None)
      if token:
        self._sessions.pop(token, None)
      process = self._processes.pop(device_id, None)
      stop_event = self._stop_events.pop(device_id, None)
    if stop_event:
      stop_event.set()
    if process and process.returncode is None:
      process.terminate()
      try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
      except TimeoutError:
        process.kill()
        await process.wait()

  async def async_take_session(self, token: str) -> CameraStreamSession | None:
    """Consume a session token so it cannot be reused."""
    async with self._lock:
      self._drop_expired_sessions_locked()
      session = self._sessions.pop(token, None)
      if session and self._device_tokens.get(session.device_id) == token:
        self._device_tokens.pop(session.device_id, None)
      return session

  async def async_register_process(
    self, device_id: str, process: asyncio.subprocess.Process
  ) -> None:
    """Register the active process after terminating a stale one."""
    stale: asyncio.subprocess.Process | None = None
    async with self._lock:
      stale = self._processes.get(device_id)
      self._processes[device_id] = process
    if stale and stale is not process and stale.returncode is None:
      stale.terminate()

  async def async_forget_process(
    self, device_id: str, process: asyncio.subprocess.Process
  ) -> None:
    """Forget a process only if it is still the current one."""
    async with self._lock:
      if self._processes.get(device_id) is process:
        self._processes.pop(device_id, None)

  async def async_forget_stop_event(
    self, device_id: str, stop_event: asyncio.Event
  ) -> None:
    """Forget a completed TCP client without touching a newer session."""
    async with self._lock:
      if self._stop_events.get(device_id) is stop_event:
        self._stop_events.pop(device_id, None)

  def _drop_expired_sessions_locked(self) -> None:
    now = time.monotonic()
    expired = [
      token for token, session in self._sessions.items()
      if session.expires_at <= now
    ]
    for token in expired:
      session = self._sessions.pop(token)
      if self._device_tokens.get(session.device_id) == token:
        self._device_tokens.pop(session.device_id, None)
      if self._stop_events.get(session.device_id) is session.stop_event:
        self._stop_events.pop(session.device_id, None)
      session.stop_event.set()


class CameraStreamConnection:
  """Serve JPEG frames with receiver-controlled 8 KiB flow control."""

  def __init__(self, manager: CameraStreamManager) -> None:
    self._manager = manager

  async def async_handle(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
  ) -> None:
    """Authenticate one single-use camera connection and run its stream."""
    peer = writer.get_extra_info("peername")
    session: CameraStreamSession | None = None
    try:
      raw_request = await asyncio.wait_for(
        reader.readline(),
        timeout=CAMERA_STREAM_HANDSHAKE_TIMEOUT_SECONDS,
      )
      if len(raw_request) > 256 or not raw_request.endswith(b"\n"):
        raise ValueError("camera_invalid_handshake")
      request = raw_request.decode("ascii", errors="strict").strip()
      if is_upload_handshake(request):
        # A panel uploading its own camera; the registry validates the
        # session/token and runs the acknowledged receive loop.
        await self._manager.local_camera_uploads.async_handle_upload(
          request,
          reader,
          writer,
          peer,
        )
        return
      if not request.startswith(CAMERA_STREAM_REQUEST_PREFIX):
        raise ValueError("camera_invalid_handshake")
      token = request[len(CAMERA_STREAM_REQUEST_PREFIX):].strip()
      if not token or any(char.isspace() for char in token):
        raise ValueError("camera_invalid_handshake")

      session = await self._manager.async_take_session(token)
      if session is None:
        writer.write(CAMERA_STREAM_HELLO_STRUCT.pack(
          CAMERA_STREAM_HELLO_MAGIC,
          1,
          CAMERA_STREAM_CHUNK_BYTES,
        ))
        await writer.drain()
        return

      transport_socket = writer.get_extra_info("socket")
      if transport_socket is not None:
        with suppress(OSError):
          transport_socket.setsockopt(
            socket.IPPROTO_TCP,
            socket.TCP_NODELAY,
            1,
          )
        with suppress(OSError):
          transport_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_SNDBUF,
            CAMERA_STREAM_CHUNK_BYTES,
          )

      writer.write(CAMERA_STREAM_HELLO_STRUCT.pack(
        CAMERA_STREAM_HELLO_MAGIC,
        0,
        CAMERA_STREAM_CHUNK_BYTES,
      ))
      await writer.drain()
      await self._async_stream(session, reader, writer)
    except (
      asyncio.IncompleteReadError,
      ConnectionResetError,
      BrokenPipeError,
      asyncio.TimeoutError,
    ):
      _LOGGER.debug(
        "HomeTiles camera TCP client disconnected during handshake/stream (%s)",
        peer,
      )
    except UnicodeError:
      _LOGGER.warning("HomeTiles camera invalid TCP handshake from %s", peer)
    except ValueError as err:
      _LOGGER.warning(
        "HomeTiles camera rejected TCP handshake from %s: %s",
        peer,
        err,
      )
    except asyncio.CancelledError:
      raise
    except Exception:
      _LOGGER.exception("HomeTiles camera TCP connection failed (%s)", peer)
    finally:
      if session is not None and not session.stop_event.is_set():
        session.stop_event.set()
        await self._manager.async_forget_stop_event(
          session.device_id,
          session.stop_event,
        )
      writer.close()
      with suppress(BrokenPipeError, ConnectionResetError):
        await writer.wait_closed()

  @staticmethod
  async def _async_send_control(
    writer: asyncio.StreamWriter,
    message_type: int,
    sequence: int,
  ) -> None:
    writer.write(CAMERA_STREAM_FRAME_STRUCT.pack(
      CAMERA_STREAM_FRAME_MAGIC,
      message_type,
      sequence,
      0,
    ))
    await writer.drain()

  @staticmethod
  async def _async_send_frame(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sequence: int,
    jpeg: bytes,
  ) -> CameraFrameSendMetrics:
    """Send one JPEG while allowing at most one unacknowledged chunk."""
    transport_started = time.monotonic()
    drain_seconds = 0.0
    ack_wait_seconds = 0.0
    max_ack_wait_seconds = 0.0
    chunks = 0
    writer.write(CAMERA_STREAM_FRAME_STRUCT.pack(
      CAMERA_STREAM_FRAME_MAGIC,
      CAMERA_STREAM_MESSAGE_FRAME,
      sequence,
      len(jpeg),
    ))
    drain_started = time.monotonic()
    await writer.drain()
    drain_seconds += time.monotonic() - drain_started

    acknowledged = 0
    while acknowledged < len(jpeg):
      chunk_end = min(
        len(jpeg),
        acknowledged + CAMERA_STREAM_CHUNK_BYTES,
      )
      writer.write(jpeg[acknowledged:chunk_end])
      drain_started = time.monotonic()
      await writer.drain()
      drain_seconds += time.monotonic() - drain_started
      ack_started = time.monotonic()
      raw_ack = await asyncio.wait_for(
        reader.readexactly(CAMERA_STREAM_ACK_STRUCT.size),
        timeout=CAMERA_STREAM_ACK_TIMEOUT_SECONDS,
      )
      ack_elapsed = time.monotonic() - ack_started
      ack_wait_seconds += ack_elapsed
      max_ack_wait_seconds = max(
        max_ack_wait_seconds,
        ack_elapsed,
      )
      magic, ack_sequence, ack_bytes = CAMERA_STREAM_ACK_STRUCT.unpack(
        raw_ack
      )
      if (
        magic != CAMERA_STREAM_ACK_MAGIC
        or ack_sequence != sequence
        or ack_bytes != chunk_end
      ):
        raise ValueError("camera_invalid_ack")
      acknowledged = chunk_end
      chunks += 1
    return CameraFrameSendMetrics(
      chunks=chunks,
      drain_seconds=drain_seconds,
      ack_wait_seconds=ack_wait_seconds,
      max_ack_wait_seconds=max_ack_wait_seconds,
      transport_seconds=time.monotonic() - transport_started,
    )

  async def _async_feed_camera_images(
    self,
    session: CameraStreamSession,
    process: asyncio.subprocess.Process,
  ) -> None:
    """Feed HA still images to FFmpeg for cameras without stream_source().

    Exactly one image request is in flight at a time. The next one starts as
    soon as the previous one answers, at most at the session rate (bounded by
    CAMERA_STILL_MAX_FPS), so a fast camera reaches that rate while a slow
    one (BambuLab snapshots take seconds) keeps its own pace. Each new image
    is written once; while a request is pending, the previous frame is
    repeated every CAMERA_STILL_REPEAT_SECONDS so FFmpeg keeps emitting.
    """
    if process.stdin is None or session.first_image is None:
      return

    # After an FFmpeg restart, resume from the newest still image instead of
    # the snapshot taken when the popup opened.
    frame = session.latest_image or session.first_image
    request_interval = 1.0 / max(1, min(session.fps, CAMERA_STILL_MAX_FPS))
    failures = 0
    image_task: asyncio.Task | None = None
    request_started = 0.0
    next_request_at = time.monotonic()
    measure_started = next_request_at
    completed = 0
    new_frames = 0
    failed = 0
    fetch_seconds = 0.0
    try:
      while process.returncode is None:
        process.stdin.write(frame)
        await process.stdin.drain()
        repeat_at = time.monotonic() + CAMERA_STILL_REPEAT_SECONDS

        new_frame: bytes | None = None
        while new_frame is None and process.returncode is None:
          now = time.monotonic()
          if image_task is None and now >= next_request_at:
            request_started = now
            next_request_at = now + request_interval
            # Full-size image on purpose, see async_create_session: up to
            # 10 fetches per second must not rescale on the event loop.
            image_task = self._manager.hass.async_create_task(
              async_get_image(
                self._manager.hass,
                session.entity_id,
                timeout=5,
              ),
              f"HomeTiles camera refresh {session.entity_id}",
            )
          wake_at = (
            repeat_at
            if image_task is not None
            else min(repeat_at, next_request_at)
          )
          timeout = wake_at - time.monotonic()
          if image_task is not None:
            if timeout > 0:
              await asyncio.wait((image_task,), timeout=timeout)
          elif timeout > 0:
            await asyncio.sleep(timeout)

          if image_task is not None and image_task.done():
            task, image_task = image_task, None
            completed += 1
            fetch_seconds += time.monotonic() - request_started
            content = b""
            error: Exception | None = None
            try:
              image = task.result()
              content = bytes(image.content) if image.content else b""
            except asyncio.CancelledError:
              raise
            except Exception as err:
              error = err
            if content:
              failures = 0
              # An unchanged image is not a new frame; the repeat covers it.
              if content != frame:
                new_frame = content
                new_frames += 1
            else:
              failures += 1
              failed += 1
              # A camera that fails fast must not exhaust the failure limit
              # at the full still rate.
              next_request_at = max(
                next_request_at,
                request_started + CAMERA_STILL_RETRY_SECONDS,
              )
              _LOGGER.debug(
                "HomeTiles camera image refresh failed for %s (%d/%d): %s",
                session.entity_id,
                failures,
                CAMERA_IMAGE_FAILURE_LIMIT,
                error if error is not None else "empty image",
              )
            if failures >= CAMERA_IMAGE_FAILURE_LIMIT:
              _LOGGER.warning(
                "HomeTiles camera image source stopped responding: %s",
                session.entity_id,
              )
              return

            elapsed = time.monotonic() - measure_started
            if (
              not session.still_rate_logged
              and elapsed >= CAMERA_STREAM_DIAGNOSTIC_INTERVAL_SECONDS
            ):
              session.still_rate_logged = True
              _LOGGER.info(
                "[CameraDiag] %s still=%.1f fps new=%.1f fps "
                "fetch=%.1f ms/image failed=%d cap=%d fps",
                session.entity_id,
                completed / elapsed,
                new_frames / elapsed,
                fetch_seconds * 1000.0 / completed,
                failed,
                round(1.0 / request_interval),
              )

          if time.monotonic() >= repeat_at:
            break

        if new_frame is not None:
          frame = new_frame
          session.latest_image = frame
    except (BrokenPipeError, ConnectionResetError):
      pass
    finally:
      if image_task:
        image_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
          await image_task
      if process.stdin and not process.stdin.is_closing():
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
          await process.stdin.wait_closed()

  def _renew_live_viewer(self, session: CameraStreamSession) -> Any:
    """Move the popup's viewer lease to a reloaded panel camera's stream.

    session.live always names the stream whose viewer lease this popup
    holds; the swap is synchronous, so the lease is never lost or doubled.
    """
    live = session.live
    if live is None or not live.closed:
      return live
    replacement = self._manager.live_camera_stream(session.entity_id)
    if replacement is None or replacement is live:
      return live
    replacement.acquire()
    live.release()
    session.live = replacement
    _LOGGER.info(
      "HomeTiles camera popup moved to the reloaded live stream: %s",
      session.entity_id,
    )
    return replacement

  async def _async_feed_live_frames(
    self,
    session: CameraStreamSession,
    process: asyncio.subprocess.Process,
  ) -> None:
    """Feed another HomeTiles panel's live camera upload to FFmpeg.

    Each new live frame is written once, at most at the session cadence.
    Until the first live frame arrives, or while the upload pauses, the
    previous frame is repeated every CAMERA_STILL_REPEAT_SECONDS so FFmpeg
    keeps emitting. After LIVE_FRAME_WAIT_S without a live frame, snapshots are
    fetched in the background like for any still-image camera.
    """
    live = session.live
    if process.stdin is None or session.first_image is None or live is None:
      return

    # After an FFmpeg restart, resume from the newest fresh live frame, or
    # the newest frame already fed, instead of the snapshot taken when the
    # popup opened.
    frame = live.latest() or session.latest_image or session.first_image
    generation = live.generation
    frame_interval = 1.0 / max(1, session.fps)
    idle_interval = CAMERA_STILL_REPEAT_SECONDS
    last_live_at = time.monotonic()
    failures = 0
    image_task: asyncio.Task | None = None
    try:
      while process.returncode is None:
        process.stdin.write(frame)
        await process.stdin.drain()
        written_at = time.monotonic()

        live_frame: bytes | None = None
        if live.closed:
          # A reloaded panel camera entity provides a new live stream.
          live = self._renew_live_viewer(session)
          generation = live.generation
        if live.closed:
          await asyncio.sleep(idle_interval)
        else:
          # Wakes on the next upload and is bounded, so it never blocks.
          live_frame = await live.async_next_frame(frame, idle_interval, generation)
        if live.generation != generation:
          # The panel ended its live view on its display: end this popup's
          # stream instead of freezing or falling back to snapshots.
          session.ended_by_panel = True
          _LOGGER.info("HomeTiles camera popup ended by the panel: %s", session.entity_id)
          break
        if live_frame is not None:
          frame = live_frame
          session.latest_image = frame
          last_live_at = time.monotonic()
          failures = 0
        elif (
          image_task is None
          and time.monotonic() - last_live_at >= LIVE_FRAME_WAIT_S
        ):
          image_task = self._manager.hass.async_create_task(
            async_get_image(
              self._manager.hass,
              session.entity_id,
              timeout=5,
              width=session.width,
              height=session.height,
            ),
            f"HomeTiles camera refresh {session.entity_id}",
          )

        if image_task is not None and image_task.done():
          task, image_task = image_task, None
          # Live frames resumed meanwhile win over a late snapshot, and a
          # late snapshot failure does not count against them.
          live_paused = time.monotonic() - last_live_at >= LIVE_FRAME_WAIT_S
          try:
            image = task.result()
            if not image.content:
              if live_paused:
                failures += 1
            elif live_paused:
              frame = bytes(image.content)
              session.latest_image = frame
              failures = 0
          except asyncio.CancelledError:
            raise
          except Exception as err:
            if live_paused:
              failures += 1
            _LOGGER.debug(
              "HomeTiles camera image refresh failed for %s (%d/%d): %s",
              session.entity_id,
              failures,
              CAMERA_IMAGE_FAILURE_LIMIT,
              err,
            )
        if failures >= CAMERA_IMAGE_FAILURE_LIMIT:
          _LOGGER.warning(
            "HomeTiles camera image source stopped responding: %s",
            session.entity_id,
          )
          break

        remaining = frame_interval - (time.monotonic() - written_at)
        if remaining > 0:
          await asyncio.sleep(remaining)
    except (BrokenPipeError, ConnectionResetError):
      pass
    finally:
      if image_task:
        image_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
          await image_task
      if process.stdin and not process.stdin.is_closing():
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
          await process.stdin.wait_closed()

  async def _async_log_ffmpeg_stderr(
    self,
    session: CameraStreamSession,
    process: asyncio.subprocess.Process,
    image_mode: bool,
  ) -> None:
    """Drain FFmpeg stderr and expose useful diagnostics for still cameras."""
    if process.stderr is None:
      return
    while line := await process.stderr.readline():
      message = line.decode(errors="replace").strip()
      if not message:
        continue
      if image_mode:
        _LOGGER.warning(
          "HomeTiles camera FFmpeg (%s): %s",
          session.entity_id,
          message,
        )
      else:
        _LOGGER.debug(
          "HomeTiles camera FFmpeg warning (%s)",
          session.entity_id,
        )

  async def _async_read_latest_jpeg_frames(
    self,
    session: CameraStreamSession,
    process: asyncio.subprocess.Process,
    frames: asyncio.Queue[bytes | None],
    diagnostics: CameraStreamDiagnostics,
  ) -> int:
    """Drain FFmpeg continuously and retain at most the newest JPEG frame."""
    assert process.stdout is not None
    parser = JpegFrameParser()
    dropped = 0
    try:
      while (
        not session.stop_event.is_set()
        and (chunk := await process.stdout.read(16 * 1024))
      ):
        for jpeg in parser.feed(chunk):
          diagnostics.note_parsed(jpeg)
          if frames.full():
            with suppress(asyncio.QueueEmpty):
              frames.get_nowait()
              dropped += 1
              diagnostics.note_dropped()
          frames.put_nowait(jpeg)
    finally:
      # Wake the frame writer even when FFmpeg exits without another frame.
      if frames.full():
        with suppress(asyncio.QueueEmpty):
          frames.get_nowait()
      frames.put_nowait(None)
    return dropped

  async def _async_stream(
    self,
    session: CameraStreamSession,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
  ) -> None:
    """Transcode and send the newest frame using acknowledged chunks."""
    ffmpeg_binary = get_ffmpeg_manager(self._manager.hass).binary
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning"]
    image_mode = session.source is None
    live = session.live if image_mode else None
    # A live panel upload is video, so it uses the direct-stream JPEG budget.
    jpeg_quality = (
      CAMERA_STILL_JPEG_QUALITY
      if image_mode and live is None
      else CAMERA_STREAM_JPEG_QUALITY
    )
    if image_mode:
      command.extend([
        # image2pipe otherwise probes roughly twelve JPEGs before producing
        # output. At low fps that otherwise leaves the P4 waiting for seconds.
        "-probesize", "32",
        "-analyzeduration", "0",
        "-f", "image2pipe",
        "-framerate", str(session.fps),
        "-vcodec", "mjpeg",
        "-i", "pipe:0",
      ])
    else:
      assert session.source is not None
      if session.source.lower().startswith("rtsp"):
        command.extend([
          "-rtsp_transport", "tcp",
          "-fflags", "nobuffer",
          "-flags", "low_delay",
          "-probesize", "32",
          "-analyzeduration", "0",
          "-max_delay", "0",
          "-timeout", "5000000",
        ])
      command.extend(["-i", session.source])
    video_filters: list[str] = []
    video_filters.extend([
      (
        f"scale={session.width}:{session.height}:"
        "force_original_aspect_ratio=increase:"
        "out_color_matrix=bt601:out_range=full"
      ),
      f"crop={session.width}:{session.height}",
      "setsar=1",
    ])
    command.extend([
      "-map", "0:v:0",
      "-an",
      "-vf", ",".join(video_filters),
      "-pix_fmt", "yuvj420p",
      "-color_range", "pc",
      "-colorspace", "smpte170m",
      "-color_primaries", "smpte170m",
      "-color_trc", "smpte170m",
      "-c:v", "mjpeg",
      "-threads:v", "1",
      "-q:v", str(jpeg_quality),
    ])
    if not image_mode:
      # A fixed fps filter duplicates frames when the source is slower (for
      # example 10 FPS from OBS), wasting ESP decode/display time and making
      # the on-device counter look faster than the real video. fpsmax only
      # drops excess source frames and never fabricates missing ones.
      command.extend(["-fpsmax", str(session.fps)])
    command.extend([
      "-flush_packets", "1",
      "-f", "image2pipe",
      "pipe:1",
    ])

    sent = 0
    sent_frame_once = False
    sequence = 0
    diagnostics = CameraStreamDiagnostics(session.entity_id)
    if live is not None:
      # The popup is a live viewer for exactly this stream's lifetime; the
      # outer finally releases it on every exit path.
      live.acquire()
    try:
      _LOGGER.info(
        "HomeTiles camera acknowledged TCP client connected "
        "(%s, mode=%s, fps=%d, chunk=%d, jpeg_q=%d)",
        session.entity_id,
        "live" if live is not None else "image" if image_mode else "stream",
        session.fps,
        CAMERA_STREAM_CHUNK_BYTES,
        jpeg_quality,
      )
      await self._async_send_control(
        writer,
        CAMERA_STREAM_MESSAGE_FLUSH,
        sequence,
      )
      reconnect_attempt = 0
      while not session.stop_event.is_set():
        feeder: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        frame_reader: asyncio.Task[int] | None = None
        process: asyncio.subprocess.Process | None = None
        frames_this_process = 0
        dropped_frames = 0
        try:
          process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE
            if image_mode
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
          )
          if session.stop_event.is_set():
            process.terminate()
            await process.wait()
            break
          await self._manager.async_register_process(
            session.device_id, process
          )
          stderr_task = self._manager.hass.async_create_task(
            self._async_log_ffmpeg_stderr(session, process, image_mode),
            f"HomeTiles camera FFmpeg log {session.entity_id}",
          )
          if live is not None:
            feeder = self._manager.hass.async_create_task(
              self._async_feed_live_frames(session, process),
              f"HomeTiles camera live frames {session.entity_id}",
            )
          elif image_mode:
            feeder = self._manager.hass.async_create_task(
              self._async_feed_camera_images(session, process),
              f"HomeTiles camera images {session.entity_id}",
            )
          latest_frames: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=1
          )
          frame_reader = self._manager.hass.async_create_task(
            self._async_read_latest_jpeg_frames(
              session,
              process,
              latest_frames,
              diagnostics,
            ),
            f"HomeTiles camera latest-frame reader {session.entity_id}",
          )
          while not session.stop_event.is_set():
            jpeg = await latest_frames.get()
            if jpeg is None:
              break
            sequence = (sequence + 1) & 0xFFFFFFFF
            send_metrics = await self._async_send_frame(
              reader,
              writer,
              sequence,
              jpeg,
            )
            diagnostics.note_sent(jpeg, send_metrics)
            diagnostics.maybe_log()
            if not sent_frame_once:
              sent_frame_once = True
              _LOGGER.info(
                "HomeTiles camera first JPEG frame sent "
                "(%s, %d bytes, first_parsed=%.1f ms, first_sent=%.1f ms)",
                session.entity_id,
                len(jpeg),
                diagnostics.first_parsed_ms or 0.0,
                diagnostics.first_sent_ms or 0.0,
              )
            frames_this_process += 1
            sent += len(jpeg)
          if frame_reader:
            dropped_frames = await frame_reader
        except (ConnectionResetError, BrokenPipeError):
          raise
        except asyncio.CancelledError:
          raise
        except Exception:
          _LOGGER.exception(
            "HomeTiles camera source process failed (%s)",
            session.entity_id,
          )
        finally:
          if feeder:
            feeder.cancel()
            with suppress(asyncio.CancelledError):
              await feeder
          if frame_reader and not frame_reader.done():
            frame_reader.cancel()
            with suppress(asyncio.CancelledError):
              await frame_reader
          if process and process.returncode is None:
            process.terminate()
            try:
              await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
              process.kill()
              await process.wait()
          if stderr_task:
            with suppress(asyncio.CancelledError):
              await stderr_task
          if process:
            await self._manager.async_forget_process(
              session.device_id, process
            )
          if dropped_frames:
            _LOGGER.debug(
              "HomeTiles camera dropped %d superseded JPEG frames (%s)",
              dropped_frames,
              session.entity_id,
            )

        if session.stop_event.is_set():
          break
        reconnect_attempt = (
          0 if frames_this_process else min(reconnect_attempt + 1, 4)
        )
        retry_delay = min(2.0, 0.25 * (2 ** reconnect_attempt))
        _LOGGER.warning(
          "HomeTiles camera source ended; reconnecting %s in %.2fs",
          session.entity_id,
          retry_delay,
        )
        await self._async_send_control(
          writer,
          CAMERA_STREAM_MESSAGE_FLUSH,
          sequence,
        )
        try:
          await asyncio.wait_for(
            session.stop_event.wait(), timeout=retry_delay
          )
        except TimeoutError:
          pass
    except (
      asyncio.IncompleteReadError,
      ConnectionResetError,
      BrokenPipeError,
      asyncio.TimeoutError,
      asyncio.CancelledError,
    ):
      _LOGGER.debug(
        "HomeTiles camera acknowledged TCP client disconnected "
        "(%s, %d JPEG bytes)",
        session.entity_id,
        sent,
      )
    except Exception:
      _LOGGER.exception(
        "HomeTiles camera TCP/FFmpeg pipeline failed (%s, %d bytes)",
        session.entity_id,
        sent,
      )
    finally:
      if live is not None:
        # session.live is the stream currently leased; the feeder may have
        # moved the lease to a reloaded panel camera's stream.
        session.live.release()
      diagnostics.maybe_log(force=True)
      session.stop_event.set()
      await self._manager.async_forget_stop_event(
        session.device_id, session.stop_event
      )
      on_panel_end = getattr(session, "on_panel_end", None)
      if getattr(session, "ended_by_panel", False) and on_panel_end is not None:
        # After the TCP stream closed, so the popup ends on the neutral
        # "stream stopped" instead of a connection error.
        await asyncio.sleep(CAMERA_PANEL_END_STATUS_DELAY_S)
        try:
          await on_panel_end()
        except Exception as err:  # noqa: BLE001 - MQTT errors are HA-specific
          _LOGGER.debug("HomeTiles camera stop notice failed: %s", err)
