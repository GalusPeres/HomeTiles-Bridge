"""Short-lived JPEG-frame video streams for the HomeTiles camera popup."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import secrets
import struct
import time
from typing import Final

from aiohttp import web

from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CAMERA_STREAM_ROUTE: Final = "/api/hometiles/camera/{token}"
CAMERA_STREAM_HTTP_PORT_FIRST: Final = 8124
CAMERA_STREAM_HTTP_PORT_LAST: Final = 8131
CAMERA_STREAM_WIDTH: Final = 640
CAMERA_STREAM_HEIGHT: Final = 480
CAMERA_STREAM_FPS: Final = 5
CAMERA_STILL_FPS: Final = 2
CAMERA_JPEG_QUALITY: Final = 5
CAMERA_HTTP_WRITE_BYTES: Final = 2 * 1024
CAMERA_HTTP_WRITE_PAUSE_SECONDS: Final = 0.002
CAMERA_SESSION_TTL_SECONDS: Final = 30.0
CAMERA_IMAGE_FAILURE_LIMIT: Final = 10
CAMERA_MAX_JPEG_BYTES: Final = 256 * 1024
CAMERA_STREAM_FRAMING: Final = "be32-jpeg"
# A zero-length protocol record flushes the HTTP response without presenting
# a fake JPEG frame to the decoder.
CAMERA_STREAM_FLUSH_RECORD: Final = b"\x00\x00\x00\x00"


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
  fps: int
  expires_at: float


class CameraStreamManager:
  """Own HomeTiles camera sessions and their FFmpeg processes."""

  def __init__(self, hass: HomeAssistant) -> None:
    self.hass = hass
    self._sessions: dict[str, CameraStreamSession] = {}
    self._device_tokens: dict[str, str] = {}
    self._processes: dict[str, asyncio.subprocess.Process] = {}
    self._lock = asyncio.Lock()
    self._http_runner: web.AppRunner | None = None
    self._http_port: int | None = None
    self._http_view = CameraStreamView(self)

  async def async_start_http_server(self) -> None:
    """Start the LAN-only plain-HTTP endpoint used by the display."""
    if self._http_runner is not None:
      return

    last_error: OSError | None = None
    for port in range(
      CAMERA_STREAM_HTTP_PORT_FIRST,
      CAMERA_STREAM_HTTP_PORT_LAST + 1,
    ):
      application = web.Application()
      application.router.add_get(
        CAMERA_STREAM_ROUTE,
        self._http_view.async_handle,
      )
      runner = web.AppRunner(application, access_log=None)
      await runner.setup()
      site = web.TCPSite(runner, "0.0.0.0", port)
      try:
        await site.start()
      except OSError as err:
        last_error = err
        await runner.cleanup()
        continue
      self._http_runner = runner
      self._http_port = port
      _LOGGER.info(
        "HomeTiles camera LAN HTTP server listening on port %d (TLS disabled)",
        port,
      )
      return

    raise OSError(
      "No free HomeTiles camera HTTP port in range "
      f"{CAMERA_STREAM_HTTP_PORT_FIRST}-{CAMERA_STREAM_HTTP_PORT_LAST}"
    ) from last_error

  @property
  def http_port(self) -> int | None:
    """Return the active LAN HTTP listener port."""
    return self._http_port

  def stream_url(self, host: str, token: str) -> str:
    """Build a plain-HTTP URL using Home Assistant's actual LAN address."""
    if self._http_port is None:
      raise ValueError("camera_http_server_unavailable")
    if not host:
      raise ValueError("home_assistant_url_unavailable")
    if ":" in host:
      host = f"[{host}]"
    path = CAMERA_STREAM_ROUTE.replace("{token}", token)
    return f"http://{host}:{self._http_port}{path}"

  async def async_shutdown(self) -> None:
    """Stop all camera work and close the auxiliary HTTP listener."""
    async with self._lock:
      device_ids = set(self._device_tokens) | set(self._processes)
    for device_id in device_ids:
      await self.async_stop_device(device_id)
    if self._http_runner is not None:
      await self._http_runner.cleanup()
      self._http_runner = None
      self._http_port = None

  async def async_create_session(
    self, device_id: str, entity_id: str
  ) -> CameraStreamSession:
    """Resolve a direct stream or a still-image camera into a video session."""
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
    fps = CAMERA_STREAM_FPS
    if not source:
      try:
        image = await async_get_image(
          self.hass,
          entity_id,
          timeout=10,
          width=CAMERA_STREAM_WIDTH,
          height=CAMERA_STREAM_HEIGHT,
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
      fps = CAMERA_STILL_FPS

    await self.async_stop_device(device_id)
    token = secrets.token_urlsafe(24)
    session = CameraStreamSession(
      token=token,
      device_id=device_id,
      entity_id=entity_id,
      source=source,
      first_image=first_image,
      fps=fps,
      expires_at=time.monotonic() + CAMERA_SESSION_TTL_SECONDS,
    )
    async with self._lock:
      self._drop_expired_sessions_locked()
      self._sessions[token] = session
      self._device_tokens[device_id] = token
    return session

  async def async_stop_device(self, device_id: str) -> None:
    """Revoke a pending session and terminate its active FFmpeg process."""
    process: asyncio.subprocess.Process | None = None
    async with self._lock:
      token = self._device_tokens.pop(device_id, None)
      if token:
        self._sessions.pop(token, None)
      process = self._processes.pop(device_id, None)
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


class CameraStreamView:
  """Serve length-framed JPEG video frames."""

  def __init__(self, manager: CameraStreamManager) -> None:
    self._manager = manager

  async def async_handle(self, request: web.Request) -> web.StreamResponse:
    """Handle a request from the dedicated plain-HTTP LAN listener."""
    return await self.get(request, request.match_info["token"])

  async def _async_feed_camera_images(
    self,
    session: CameraStreamSession,
    process: asyncio.subprocess.Process,
  ) -> None:
    """Feed HA still images to FFmpeg for cameras without stream_source()."""
    if process.stdin is None or session.first_image is None:
      return

    frame = session.first_image
    failures = 0
    interval = 1.0 / max(1, session.fps)
    image_task: asyncio.Task | None = None
    try:
      while process.returncode is None:
        process.stdin.write(frame)
        await process.stdin.drain()

        # BambuLab snapshot retrieval can take several seconds. Keep feeding
        # the latest good JPEG at the requested cadence while the next image
        # is fetched in parallel; otherwise FFmpeg receives only one frame and
        # cannot emit a JPEG video frame before the ESP's HTTP timeout.
        if image_task is None:
          image_task = self._manager.hass.async_create_task(
            async_get_image(
              self._manager.hass,
              session.entity_id,
              timeout=5,
              width=CAMERA_STREAM_WIDTH,
              height=CAMERA_STREAM_HEIGHT,
            ),
            f"HomeTiles camera refresh {session.entity_id}",
          )
        await asyncio.sleep(interval)
        if image_task.done():
          try:
            image = image_task.result()
            if image.content:
              frame = bytes(image.content)
              failures = 0
            else:
              failures += 1
          except asyncio.CancelledError:
            raise
          except Exception as err:
            failures += 1
            _LOGGER.debug(
              "HomeTiles camera image refresh failed for %s (%d/%d): %s",
              session.entity_id,
              failures,
              CAMERA_IMAGE_FAILURE_LIMIT,
              err,
            )
          finally:
            image_task = None
        if failures >= CAMERA_IMAGE_FAILURE_LIMIT:
          _LOGGER.warning(
            "HomeTiles camera image source stopped responding: %s",
            session.entity_id,
          )
          break
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

  async def get(self, request: web.Request, token: str) -> web.StreamResponse:
    """Start FFmpeg after validating the single-use session token."""
    session = await self._manager.async_take_session(token)
    if session is None:
      raise web.HTTPNotFound()

    ffmpeg_binary = get_ffmpeg_manager(self._manager.hass).binary
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning"]
    image_mode = session.source is None
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
        command.extend(["-rtsp_transport", "tcp"])
      command.extend(["-i", session.source])
    video_filters: list[str] = []
    if not image_mode:
      video_filters.append(f"fps={session.fps}")
    video_filters.extend([
      (
        f"scale={CAMERA_STREAM_WIDTH}:{CAMERA_STREAM_HEIGHT}:"
        "force_original_aspect_ratio=decrease:"
        "out_color_matrix=bt601:out_range=full"
      ),
      (
        f"pad={CAMERA_STREAM_WIDTH}:{CAMERA_STREAM_HEIGHT}:"
        "(ow-iw)/2:(oh-ih)/2:black"
      ),
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
      "-q:v", str(CAMERA_JPEG_QUALITY),
      "-f", "image2pipe",
      "pipe:1",
    ])

    response = web.StreamResponse(
      status=200,
      headers={
        "Content-Type": "application/x-hometiles-jpeg-stream",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "X-HomeTiles-Framing": CAMERA_STREAM_FRAMING,
        "X-HomeTiles-Video": (
          f"jpeg; width={CAMERA_STREAM_WIDTH}; "
          f"height={CAMERA_STREAM_HEIGHT}; fps={session.fps}"
        ),
      },
    )

    sent = 0
    feeder: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    try:
      # Send the HTTP response immediately. On slower HA hosts, waiting for
      # FFmpeg to spawn before prepare() made the ESP32-P4 time out while it
      # was still waiting for the status line and headers.
      await response.prepare(request)
      _LOGGER.info(
        "HomeTiles camera HTTP client connected (%s, mode=%s, fps=%d)",
        session.entity_id,
        "image" if image_mode else "stream",
        session.fps,
      )
      await response.write(CAMERA_STREAM_FLUSH_RECORD)
      sent += len(CAMERA_STREAM_FLUSH_RECORD)
      process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE
        if image_mode
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
      )
      await self._manager.async_register_process(session.device_id, process)
      stderr_task = self._manager.hass.async_create_task(
        self._async_log_ffmpeg_stderr(session, process, image_mode),
        f"HomeTiles camera FFmpeg log {session.entity_id}",
      )
      if image_mode:
        feeder = self._manager.hass.async_create_task(
          self._async_feed_camera_images(session, process),
          f"HomeTiles camera images {session.entity_id}",
        )
      assert process.stdout is not None
      parser = JpegFrameParser()
      encoder_started = False
      while chunk := await process.stdout.read(16 * 1024):
        for jpeg in parser.feed(chunk):
          record = struct.pack(">I", len(jpeg)) + jpeg
          # ESP-Hosted receives through the C6 into scarce internal DMA RAM.
          # Do not hand a complete JPEG picture to the socket in one burst:
          # pace small writes so the P4 can drain them into its PSRAM buffer
          # while MQTT continues using the same SDIO transport.
          for offset in range(0, len(record), CAMERA_HTTP_WRITE_BYTES):
            await response.write(
              record[offset : offset + CAMERA_HTTP_WRITE_BYTES]
            )
            await asyncio.sleep(CAMERA_HTTP_WRITE_PAUSE_SECONDS)
          if not encoder_started:
            encoder_started = True
            _LOGGER.info(
              "HomeTiles camera first JPEG frame sent (%s, %d bytes)",
              session.entity_id,
              len(jpeg),
            )
          sent += len(record)
    except (ConnectionResetError, asyncio.CancelledError):
      _LOGGER.debug(
        "HomeTiles camera client disconnected (%s, %d bytes)",
        session.entity_id,
        sent,
      )
    except Exception:
      _LOGGER.exception(
        "HomeTiles camera HTTP/FFmpeg pipeline failed (%s, %d bytes)",
        session.entity_id,
        sent,
      )
    finally:
      if feeder:
        feeder.cancel()
        with suppress(asyncio.CancelledError):
          await feeder
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
        await self._manager.async_forget_process(session.device_id, process)

    return response
