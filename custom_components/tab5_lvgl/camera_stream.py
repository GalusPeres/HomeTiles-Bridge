"""Short-lived H.264 camera streams for the HomeTiles camera popup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Final

from aiohttp import web

from homeassistant.components.camera import async_get_stream_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CAMERA_STREAM_ROUTE: Final = "/api/hometiles/camera/{token}"
CAMERA_STREAM_NAME: Final = "api:hometiles:camera"
CAMERA_STREAM_WIDTH: Final = 640
CAMERA_STREAM_HEIGHT: Final = 480
CAMERA_STREAM_FPS: Final = 12
CAMERA_STREAM_BITRATE_KBIT: Final = 850
CAMERA_SESSION_TTL_SECONDS: Final = 30.0


@dataclass(slots=True)
class CameraStreamSession:
  """A single-use stream session created through MQTT."""

  token: str
  device_id: str
  entity_id: str
  source: str
  expires_at: float


class CameraStreamManager:
  """Own HomeTiles camera sessions and their FFmpeg processes."""

  def __init__(self, hass: HomeAssistant) -> None:
    self.hass = hass
    self._sessions: dict[str, CameraStreamSession] = {}
    self._device_tokens: dict[str, str] = {}
    self._processes: dict[str, asyncio.subprocess.Process] = {}
    self._lock = asyncio.Lock()

  async def async_create_session(
    self, device_id: str, entity_id: str
  ) -> CameraStreamSession:
    """Resolve a camera source and create a one-time HTTP session."""
    async with asyncio.timeout(10):
      source = await async_get_stream_source(self.hass, entity_id)
    if not source:
      raise ValueError("camera_has_no_stream_source")

    await self.async_stop_device(device_id)
    token = secrets.token_urlsafe(24)
    session = CameraStreamSession(
      token=token,
      device_id=device_id,
      entity_id=entity_id,
      source=source,
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


class CameraStreamView(HomeAssistantView):
  """Serve a transcoded constrained-baseline Annex-B H.264 stream."""

  url = CAMERA_STREAM_ROUTE
  name = CAMERA_STREAM_NAME
  requires_auth = False

  def __init__(self, manager: CameraStreamManager) -> None:
    self._manager = manager

  async def get(self, request: web.Request, token: str) -> web.StreamResponse:
    """Start FFmpeg after validating the single-use session token."""
    session = await self._manager.async_take_session(token)
    if session is None:
      raise web.HTTPNotFound()

    ffmpeg_binary = get_ffmpeg_manager(self._manager.hass).binary
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning"]
    if session.source.lower().startswith("rtsp"):
      command.extend(["-rtsp_transport", "tcp"])
    command.extend([
      "-i", session.source,
      "-map", "0:v:0",
      "-an",
      "-vf",
      (
        f"fps={CAMERA_STREAM_FPS},"
        f"scale={CAMERA_STREAM_WIDTH}:{CAMERA_STREAM_HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={CAMERA_STREAM_WIDTH}:{CAMERA_STREAM_HEIGHT}:"
        "(ow-iw)/2:(oh-ih)/2:black"
      ),
      "-pix_fmt", "yuv420p",
      "-c:v", "libx264",
      "-preset", "ultrafast",
      "-tune", "zerolatency",
      "-profile:v", "baseline",
      "-level", "3.0",
      "-bf", "0",
      "-g", str(CAMERA_STREAM_FPS),
      "-keyint_min", str(CAMERA_STREAM_FPS),
      "-sc_threshold", "0",
      "-b:v", f"{CAMERA_STREAM_BITRATE_KBIT}k",
      "-maxrate", f"{CAMERA_STREAM_BITRATE_KBIT}k",
      "-bufsize", f"{CAMERA_STREAM_BITRATE_KBIT * 2}k",
      "-x264-params", "repeat-headers=1:aud=1",
      "-f", "h264",
      "pipe:1",
    ])

    process = await asyncio.create_subprocess_exec(
      *command,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.DEVNULL,
    )
    await self._manager.async_register_process(session.device_id, process)

    response = web.StreamResponse(
      status=200,
      headers={
        "Content-Type": "video/h264",
        "Cache-Control": "no-store",
        "X-HomeTiles-Video": (
          f"h264-baseline; width={CAMERA_STREAM_WIDTH}; "
          f"height={CAMERA_STREAM_HEIGHT}; fps={CAMERA_STREAM_FPS}"
        ),
      },
    )
    await response.prepare(request)

    sent = 0
    try:
      assert process.stdout is not None
      while chunk := await process.stdout.read(16 * 1024):
        await response.write(chunk)
        sent += len(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
      _LOGGER.debug(
        "HomeTiles camera client disconnected (%s, %d bytes)",
        session.entity_id,
        sent,
      )
    finally:
      if process.returncode is None:
        process.terminate()
        try:
          await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
          process.kill()
          await process.wait()
      await self._manager.async_forget_process(session.device_id, process)

    return response
