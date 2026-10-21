"""Clips for a panel's speaker, decoded by the Bridge.

The panel no longer downloads and decodes a clip itself: the Bridge's FFmpeg
reads it (any format and source Home Assistant can serve) and writes mono
16-bit PCM, which the panel receives over the acknowledged TCP transport of
the camera stream (the same listener and port, request prefix ``HTAUD/1``).
The panel acknowledges a block once it lies in its PSRAM buffer, so at most
``AUDIO_STREAM_WINDOW`` blocks of ``AUDIO_STREAM_CHUNK_BYTES`` wait in its
internal network memory whatever the clip. Firmware side:
``src/audio/audio_stream_contract.h``; design: ``docs-dev/audio-stream.md``.

Messages after the hello (frame header ``>4sB3xII`` as in camera_stream.py):

- type 4 format, 16 bytes ``>IHHII``: sample rate, channels (1), bits (16),
  stream position in ms of the first sample that follows, length in ms
  (0 = not known, a live stream).
- type 1 audio: PCM, little-endian, at most one block, an even length.
- type 3 end, no payload: the clip is over; the panel plays its buffer out
  and closes the connection.

The panel acknowledges format and audio messages with (sequence, length).
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import re
import secrets
import socket
import struct
import time
from typing import Any, Callable, Final

from .speaker import AUDIO_RATE_MUSIC, AUDIO_RATE_SPEECH

_LOGGER = logging.getLogger(__name__)

AUDIO_STREAM_REQUEST_PREFIX: Final = "HTAUD/1 "
AUDIO_STREAM_HELLO_MAGIC: Final = b"HTC1"
AUDIO_STREAM_FRAME_MAGIC: Final = b"HTF1"
AUDIO_STREAM_ACK_MAGIC: Final = b"HTA1"
AUDIO_STREAM_HELLO_STRUCT: Final = struct.Struct(">4sHH")
AUDIO_STREAM_FRAME_STRUCT: Final = struct.Struct(">4sB3xII")
AUDIO_STREAM_ACK_STRUCT: Final = struct.Struct(">4sII")
AUDIO_STREAM_FORMAT_STRUCT: Final = struct.Struct(">IHHII")
AUDIO_MESSAGE_AUDIO: Final = 1
AUDIO_MESSAGE_END: Final = 3
AUDIO_MESSAGE_FORMAT: Final = 4
AUDIO_HELLO_ACCEPTED: Final = 0
AUDIO_HELLO_UNKNOWN_TOKEN: Final = 1
# Two 8 KB blocks in flight: 16 KB of the panel's internal network memory,
# 0.19 s of music, far more than the Wi-Fi round trip needs.
AUDIO_STREAM_CHUNK_BYTES: Final = 8 * 1024
AUDIO_STREAM_WINDOW: Final = 2
AUDIO_RATES: Final = frozenset({AUDIO_RATE_MUSIC, AUDIO_RATE_SPEECH})
# A panel connects right after the play command; an unused token expires.
AUDIO_SESSION_TTL_SECONDS: Final = 30.0
# A playing panel acknowledges a block every ~0.1 s. One that acknowledges
# nothing for the direct link's idle limit is gone (camera_stream.py, #116).
AUDIO_ACK_TIMEOUT_SECONDS: Final = 45.0
# A paused panel acknowledges nothing until it resumes; after this long the
# Bridge gives up the clip and frees its FFmpeg.
AUDIO_PAUSE_LIMIT_SECONDS: Final = 600.0
AUDIO_ACK_POLL_SECONDS: Final = 5.0
# FFmpeg names the input's length before it writes the first sample; a slow
# server gets this long.
AUDIO_PROBE_TIMEOUT_SECONDS: Final = 15.0
# After the end message the panel still plays its buffer (about 3 s).
AUDIO_END_WAIT_SECONDS: Final = 30.0
# FFmpeg reads only network inputs: a playlist cannot point it at local files.
AUDIO_FFMPEG_PROTOCOLS: Final = "http,https,tcp,tls,crypto"

_DURATION_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_QUERY_RE = re.compile(r"\?[^\s'\"]*")


def ffmpeg_audio_command(binary: str, url: str, rate: int, start_ms: int = 0) -> list[str]:
    """FFmpeg reading url from start_ms and writing mono 16-bit PCM at rate."""
    command = [
        binary, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-protocol_whitelist", AUDIO_FFMPEG_PROTOCOLS,
    ]
    if start_ms > 0:
        command += ["-ss", f"{start_ms / 1000:.3f}"]
    command += [
        "-i", url, "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ]
    return command


def parse_duration_ms(line: str) -> int | None:
    """The input length from FFmpeg's "Duration:" line; 0 for N/A, else None."""
    if not line.lstrip().startswith("Duration:"):
        return None
    match = _DURATION_RE.match(line)
    if match is None:
        return 0
    hours, minutes, seconds = match.groups()
    return int(round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000))


def without_query(text: str) -> str:
    """A log line without URL queries (Home Assistant's signed tokens)."""
    return _QUERY_RE.sub("?...", text)


def encode_format(rate: int, position_ms: int, duration_ms: int) -> bytes:
    return AUDIO_STREAM_FORMAT_STRUCT.pack(rate, 1, 16, position_ms, duration_ms)


def encode_frame(kind: int, sequence: int, payload: bytes = b"") -> bytes:
    return AUDIO_STREAM_FRAME_STRUCT.pack(
        AUDIO_STREAM_FRAME_MAGIC, kind, sequence, len(payload)) + payload


@dataclass(slots=True)
class AudioStreamSession:
    """One clip for one panel, created with the play command."""

    token: str
    device_id: str
    url: str
    rate: int
    start_ms: int
    expires_at: float
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False
    # Set while the connection runs, so a stop ends it at once.
    process: Any = None
    writer: Any = None


class AudioStreamManager:
    """Sessions and running streams of every panel's speaker."""

    def __init__(self, hass: Any, ffmpeg_binary: Callable[[], str] | None = None) -> None:
        self.hass = hass
        self._ffmpeg_binary = ffmpeg_binary
        self._sessions: dict[str, AudioStreamSession] = {}
        # The newest session of each panel: pending or streaming.
        self._current: dict[str, AudioStreamSession] = {}

    def binary(self) -> str:
        if self._ffmpeg_binary is not None:
            return self._ffmpeg_binary()
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(self.hass).binary

    def create_session(self, device_id: str, url: str, rate: int,
                       start_ms: int = 0) -> AudioStreamSession:
        """A new clip for the panel; its previous clip ends."""
        if rate not in AUDIO_RATES:
            raise ValueError("audio_invalid_rate")
        self.stop_device(device_id)
        self._drop_expired()
        session = AudioStreamSession(
            token=secrets.token_urlsafe(24),
            device_id=device_id,
            url=url,
            rate=rate,
            start_ms=max(0, int(start_ms)),
            expires_at=time.monotonic() + AUDIO_SESSION_TTL_SECONDS,
        )
        self._sessions[session.token] = session
        self._current[device_id] = session
        return session

    def set_paused(self, device_id: str, paused: bool) -> None:
        session = self._current.get(device_id)
        if session is not None:
            session.paused = paused

    def stop_device(self, device_id: str) -> None:
        """End the panel's clip: revoke a pending token, stop a running FFmpeg."""
        session = self._current.pop(device_id, None)
        if session is None:
            return
        self._sessions.pop(session.token, None)
        self._end(session)

    @staticmethod
    def _end(session: AudioStreamSession) -> None:
        session.stop_event.set()
        process = session.process
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        if session.writer is not None:
            session.writer.close()

    def take_session(self, token: str) -> AudioStreamSession | None:
        """Consume a token so it cannot be reused."""
        self._drop_expired()
        return self._sessions.pop(token, None)

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for token, session in list(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[token]
                if self._current.get(session.device_id) is session:
                    del self._current[session.device_id]

    async def async_shutdown(self) -> None:
        for device_id in list(self._current):
            self.stop_device(device_id)
        self._sessions.clear()

    async def async_handle(self, request: str, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, peer: Any) -> None:
        """One panel connection from the camera listener (camera_stream.py)."""
        token = request[len(AUDIO_STREAM_REQUEST_PREFIX):].strip()
        session = self.take_session(token) if token else None
        if session is None:
            writer.write(AUDIO_STREAM_HELLO_STRUCT.pack(
                AUDIO_STREAM_HELLO_MAGIC, AUDIO_HELLO_UNKNOWN_TOKEN, AUDIO_STREAM_CHUNK_BYTES))
            with suppress(ConnectionError):
                await writer.drain()
            _LOGGER.warning("HomeTiles speaker stream refused for %s: unknown or expired token",
                            peer)
            return
        transport_socket = writer.get_extra_info("socket")
        if transport_socket is not None:
            with suppress(OSError):
                transport_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with suppress(OSError):
                transport_socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF,
                    AUDIO_STREAM_CHUNK_BYTES * AUDIO_STREAM_WINDOW)
        session.writer = writer
        try:
            writer.write(AUDIO_STREAM_HELLO_STRUCT.pack(
                AUDIO_STREAM_HELLO_MAGIC, AUDIO_HELLO_ACCEPTED, AUDIO_STREAM_CHUNK_BYTES))
            await writer.drain()
            await AudioStreamConnection(self, session, reader, writer).async_run()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.IncompleteReadError):
            _LOGGER.debug("HomeTiles speaker stream for %s closed by the panel", session.device_id)
        except TimeoutError:
            _LOGGER.warning(
                "HomeTiles speaker on %s acknowledged nothing for %.0f s; clip ended",
                session.device_id,
                AUDIO_PAUSE_LIMIT_SECONDS if session.paused else AUDIO_ACK_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - one failed clip must not stick
            _LOGGER.exception("HomeTiles speaker stream for %s failed", session.device_id)
        finally:
            session.writer = None
            if self._current.get(session.device_id) is session:
                del self._current[session.device_id]
            self._end(session)


class AudioStreamConnection:
    """FFmpeg's PCM to one panel with at most AUDIO_STREAM_WINDOW blocks in flight."""

    def __init__(self, manager: AudioStreamManager, session: AudioStreamSession,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._manager = manager
        self._session = session
        self._reader = reader
        self._writer = writer
        self._sequence = 0
        self._pending: deque[tuple[int, int]] = deque()
        self._acks: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()
        self.sent_bytes = 0

    async def async_run(self) -> None:
        session = self._session
        ack_task = asyncio.ensure_future(self._async_read_acks())
        stderr_task: asyncio.Future | None = None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_audio_command(self._manager.binary(), session.url, session.rate,
                                      session.start_ms),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            session.process = process
            if session.stop_event.is_set():
                return
            try:
                duration_ms, last_line = await asyncio.wait_for(
                    self._async_probe(process.stderr), AUDIO_PROBE_TIMEOUT_SECONDS)
            except TimeoutError:
                _LOGGER.warning("HomeTiles speaker on %s: FFmpeg opened no clip within %.0f s",
                                session.device_id, AUDIO_PROBE_TIMEOUT_SECONDS)
                return
            if duration_ms is None:
                _LOGGER.warning("HomeTiles speaker on %s: FFmpeg could not read the clip (%s)",
                                session.device_id, without_query(last_line)[:200])
                return
            stderr_task = asyncio.ensure_future(self._async_drain(process.stderr))
            _LOGGER.info(
                "HomeTiles speaker stream for %s started (%d Hz, from %.1f s, length %s)",
                session.device_id, session.rate, session.start_ms / 1000,
                f"{duration_ms / 1000:.1f} s" if duration_ms else "open")
            await self._async_send(AUDIO_MESSAGE_FORMAT,
                                   encode_format(session.rate, session.start_ms, duration_ms))
            await self._async_send_pcm(process.stdout)
            if session.stop_event.is_set():
                return
            self._writer.write(encode_frame(AUDIO_MESSAGE_END, self._next_sequence()))
            await self._writer.drain()
            # The panel plays its buffer out, acknowledging what is in flight,
            # and closes the connection.
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(ack_task), AUDIO_END_WAIT_SECONDS)
            _LOGGER.debug("HomeTiles speaker stream for %s ended (%d bytes)",
                          session.device_id, self.sent_bytes)
        finally:
            ack_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await ack_task
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            if stderr_task is not None:
                stderr_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await stderr_task
            session.process = None

    @staticmethod
    async def _async_probe(stderr: asyncio.StreamReader) -> tuple[int | None, str]:
        """FFmpeg's input length once it starts writing; None when it failed."""
        duration_ms = 0
        last_line = ""
        while True:
            raw = await stderr.readline()
            if not raw:
                return None, last_line
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                last_line = line
            parsed = parse_duration_ms(line)
            if parsed is not None:
                duration_ms = parsed
            if line.startswith("Output #0"):
                return duration_ms, last_line

    @staticmethod
    async def _async_drain(stderr: asyncio.StreamReader) -> None:
        """Keep FFmpeg's log pipe empty; a full pipe would stop it."""
        while await stderr.readline():
            pass

    async def _async_send_pcm(self, stdout: asyncio.StreamReader) -> None:
        while not self._session.stop_event.is_set():
            try:
                block = await stdout.readexactly(AUDIO_STREAM_CHUNK_BYTES)
            except asyncio.IncompleteReadError as err:
                # The last samples; a stray odd byte is not a sample.
                block = err.partial[:len(err.partial) & ~1]
                if block and not self._session.stop_event.is_set():
                    await self._async_send(AUDIO_MESSAGE_AUDIO, block)
                return
            await self._async_send(AUDIO_MESSAGE_AUDIO, block)

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return self._sequence

    async def _async_send(self, kind: int, payload: bytes) -> None:
        while len(self._pending) >= AUDIO_STREAM_WINDOW:
            await self._async_take_ack()
        sequence = self._next_sequence()
        self._writer.write(encode_frame(kind, sequence, payload))
        await self._writer.drain()
        self._pending.append((sequence, len(payload)))
        self.sent_bytes += len(payload)

    async def _async_take_ack(self) -> None:
        """Wait for the oldest block's acknowledgement.

        A playing panel that falls silent for AUDIO_ACK_TIMEOUT_SECONDS is
        gone; a paused one may stay silent for AUDIO_PAUSE_LIMIT_SECONDS.
        """
        silent_playing = 0.0
        silent_paused = 0.0
        while True:
            try:
                ack = await asyncio.wait_for(self._acks.get(), AUDIO_ACK_POLL_SECONDS)
                break
            except TimeoutError:
                if self._session.paused:
                    silent_paused += AUDIO_ACK_POLL_SECONDS
                    if silent_paused >= AUDIO_PAUSE_LIMIT_SECONDS:
                        raise
                else:
                    silent_playing += AUDIO_ACK_POLL_SECONDS
                    if silent_playing >= AUDIO_ACK_TIMEOUT_SECONDS:
                        raise
        if ack is None:
            raise ConnectionResetError("audio_panel_closed")
        if ack != self._pending[0]:
            raise ValueError("audio_invalid_ack")
        self._pending.popleft()

    async def _async_read_acks(self) -> None:
        """Acknowledgements as they arrive; None once the panel closed."""
        try:
            while True:
                raw = await self._reader.readexactly(AUDIO_STREAM_ACK_STRUCT.size)
                magic, sequence, length = AUDIO_STREAM_ACK_STRUCT.unpack(raw)
                if magic != AUDIO_STREAM_ACK_MAGIC:
                    return
                self._acks.put_nowait((sequence, length))
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            self._acks.put_nowait(None)
