"""Assist (voice) sessions with a panel's own microphone over MQTT.

The module has no Home Assistant imports so the payloads, the binary frame
format and the session bookkeeping can be tested without a Home Assistant
install. The panel side follows the HomeTiles Assist contract v1.

MQTT contract (base = the panel base topic):
- ``{base}/cmnd/assist``: Bridge to panel, QoS 0, never retained, JSON
  ``{"v": 1, "op": "start", "id": "<hex>", "max_ms": 15000}`` or
  ``{"v": 1, "op": "stop", "id": "<hex>"}``. Sealed on the encrypted command
  channel (data name ``assist``) once the panel is paired.
- ``{base}/stat/assist_audio``: panel to Bridge, QoS 0, not retained, binary
  frames: ``"HTA1"``, id length N (16..32), N id bytes (lowercase hex),
  flags (bit 0 = last frame), uint32 little-endian sequence number, then PCM
  (signed 16-bit little-endian, mono, 16 kHz).
- ``{base}/stat/assist``: panel to Bridge, retained JSON status
  ``{"v": 1, "state": "idle"|"listening"|"wake", "id": "<hex>"}`` plus
  ``"error"`` when the last session failed, and the on-panel wake words:
  ``"wake_words"`` (detectable) and ``"active_wake_words"`` (listening for).

On-panel wake word (v1 addendum, microWakeWord models detected on the panel):
- ``{base}/cmnd/assist`` ``{"v": 1, "op": "wake_words", "active": ["okay_nabu"]}``
  sets the active words (empty list = off); sealed like start/stop.
- ``{base}/stat/assist_wake``: panel to Bridge, QoS 0, not retained,
  ``{"v": 1, "word": "okay_nabu", "seq": <uint32 per boot>}`` on detection.
  The Bridge then starts a session exactly like the Assist button.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, AsyncIterator, NamedTuple

ASSIST_LEAF = "assist"
ASSIST_AUDIO_LEAF = "assist_audio"
ASSIST_WAKE_LEAF = "assist_wake"
ASSIST_PROTOCOL_VERSION = 1
ASSIST_SATELLITE_UNIQUE_ID_SUFFIX = "_assist_satellite"
ASSIST_BUTTON_UNIQUE_ID_SUFFIX = "_assist"
# hass.data[DOMAIN] key of the running satellites, by config entry ID.
ASSIST_SATELLITES_KEY = "assist_satellites"

FRAME_MAGIC = b"HTA1"
FRAME_FLAG_LAST = 0x01
SESSION_ID_MIN_LENGTH = 16
SESSION_ID_MAX_LENGTH = 32
# 100 ms of 16 kHz mono 16-bit PCM is 3200 bytes; leave room for larger frames
# but refuse anything that cannot be a single audio frame.
FRAME_MAX_PCM_BYTES = 64 * 1024
FRAME_MAX_BYTES = 4 + 1 + SESSION_ID_MAX_LENGTH + 1 + 4 + FRAME_MAX_PCM_BYTES

DEFAULT_MAX_MS = 15000
MIN_MAX_MS = 1000
MAX_MAX_MS = 30000
# The pipeline's audio stream ends when no frame arrived for this long.
FRAME_TIMEOUT_S = 2.0
# Bounded: 30 s of 100 ms frames plus slack. Frames beyond it are dropped.
AUDIO_QUEUE_MAX_FRAMES = 320

ASSIST_STATES = frozenset({"idle", "listening", "wake"})
ASSIST_ERRORS = frozenset({"mic_unavailable", "busy", "out_of_memory", "timeout", "invalid"})
ASSIST_UNKNOWN_ERROR = "unknown"
ASSIST_STATUS_MAX_PAYLOAD = 1024

# On-panel wake words. Ids are microWakeWord model names ("okay_nabu").
WAKE_WORD_ID_MAX_LENGTH = 32
# Bound on the words a status may announce; extra entries are ignored.
WAKE_WORDS_MAX = 16
# The panel listens for one word at a time.
MAX_ACTIVE_WAKE_WORDS = 1
WAKE_EVENT_MAX_PAYLOAD = 256
_UINT32_MAX = 0xFFFFFFFF
_WAKE_WORD_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
# Display phrase and trained languages of known microWakeWord models. The
# phrase is what Home Assistant shows and what the pipeline gets as
# wake_word_phrase; unknown ids fall back to a title-cased id.
KNOWN_WAKE_WORDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "okay_nabu": ("Okay Nabu", ("en",)),
    "hey_jarvis": ("Hey Jarvis", ("en",)),
    "hey_mycroft": ("Hey Mycroft", ("en",)),
    "alexa": ("Alexa", ("en",)),
    "hey_home_assistant": ("Hey Home Assistant", ("en",)),
}

_ID_CHARS = frozenset("0123456789abcdef")


def assist_command_topic(base_topic: str) -> str:
    return f"{base_topic}/cmnd/{ASSIST_LEAF}"


def assist_status_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{ASSIST_LEAF}"


def assist_audio_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{ASSIST_AUDIO_LEAF}"


def assist_wake_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{ASSIST_WAKE_LEAF}"


def assist_satellite_unique_id(device_id: str) -> str:
    return f"{device_id}{ASSIST_SATELLITE_UNIQUE_ID_SUFFIX}"


def assist_button_unique_id(device_id: str) -> str:
    return f"{device_id}{ASSIST_BUTTON_UNIQUE_ID_SUFFIX}"


def new_session_id() -> str:
    return secrets.token_hex(16)


def valid_session_id(value: Any) -> bool:
    return (isinstance(value, str)
            and SESSION_ID_MIN_LENGTH <= len(value) <= SESSION_ID_MAX_LENGTH
            and all(c in _ID_CHARS for c in value))


def valid_wake_word_id(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= WAKE_WORD_ID_MAX_LENGTH
            and all(c in _WAKE_WORD_ID_CHARS for c in value))


def wake_word_info(word_id: str) -> tuple[str, list[str]]:
    """Display phrase and trained languages of a wake word id."""
    known = KNOWN_WAKE_WORDS.get(word_id)
    if known is not None:
        return known[0], list(known[1])
    return word_id.replace("_", " ").strip().title() or word_id, []


def wake_word_phrase(word_id: str) -> str:
    return wake_word_info(word_id)[0]


def _wake_word_list(value: Any) -> list[str] | None:
    """Valid, unique ids of a status list; None when the field is not a list."""
    if not isinstance(value, list):
        return None
    words: list[str] = []
    for item in value:
        if valid_wake_word_id(item) and item not in words:
            words.append(item)
            if len(words) >= WAKE_WORDS_MAX:
                break
    return words


def clamp_max_ms(value: Any) -> int:
    if type(value) is not int:
        return DEFAULT_MAX_MS
    return min(MAX_MAX_MS, max(MIN_MAX_MS, value))


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def build_start_command(session_id: str, max_ms: int = DEFAULT_MAX_MS) -> str:
    if not valid_session_id(session_id):
        raise ValueError("invalid_session_id")
    return _encode({"v": ASSIST_PROTOCOL_VERSION, "op": "start", "id": session_id,
                    "max_ms": clamp_max_ms(max_ms)})


def build_stop_command(session_id: str) -> str:
    if not valid_session_id(session_id):
        raise ValueError("invalid_session_id")
    return _encode({"v": ASSIST_PROTOCOL_VERSION, "op": "stop", "id": session_id})


def build_wake_words_command(active: Any) -> str:
    """Set the panel's active wake words; an empty list turns detection off."""
    if not isinstance(active, (list, tuple)):
        raise ValueError("invalid_wake_words")
    words: list[str] = []
    for word in active:
        if not valid_wake_word_id(word):
            raise ValueError("invalid_wake_word")
        if word not in words:
            words.append(word)
    if len(words) > MAX_ACTIVE_WAKE_WORDS:
        raise ValueError("too_many_wake_words")
    return _encode({"v": ASSIST_PROTOCOL_VERSION, "op": "wake_words", "active": words})


class AudioFrame(NamedTuple):
    session_id: str
    last: bool
    sequence: int
    pcm: bytes


def parse_audio_frame(payload: Any) -> AudioFrame | None:
    """Validate one binary microphone frame; None for anything malformed."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return None
    data = bytes(payload)
    if len(data) > FRAME_MAX_BYTES or len(data) < 5 or data[:4] != FRAME_MAGIC:
        return None
    id_length = data[4]
    if not SESSION_ID_MIN_LENGTH <= id_length <= SESSION_ID_MAX_LENGTH:
        return None
    header_length = 5 + id_length + 1 + 4
    if len(data) < header_length:
        return None
    try:
        session_id = data[5:5 + id_length].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not valid_session_id(session_id):
        return None
    flags = data[5 + id_length]
    sequence = int.from_bytes(data[6 + id_length:header_length], "little")
    pcm = data[header_length:]
    # Signed 16-bit samples: an odd byte count is a truncated frame.
    if len(pcm) % 2:
        return None
    return AudioFrame(session_id, bool(flags & FRAME_FLAG_LAST), sequence, pcm)


def build_audio_frame(session_id: str, sequence: int, pcm: bytes = b"",
                      last: bool = False) -> bytes:
    """The panel's frame layout; used by tests and diagnostics."""
    raw_id = session_id.encode("ascii")
    return (FRAME_MAGIC + bytes([len(raw_id)]) + raw_id
            + bytes([FRAME_FLAG_LAST if last else 0])
            + int(sequence).to_bytes(4, "little") + bytes(pcm))


def parse_status(payload: Any) -> dict[str, Any] | None:
    """Validate the retained status; None for anything malformed."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > ASSIST_STATUS_MAX_PAYLOAD:
            return None
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or len(payload) > ASSIST_STATUS_MAX_PAYLOAD:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != ASSIST_PROTOCOL_VERSION:
        return None
    state = data.get("state")
    if state not in ASSIST_STATES:
        return None
    session_id = data.get("id")
    if not valid_session_id(session_id):
        session_id = None
    error = data.get("error")
    if error is not None:
        error = error if error in ASSIST_ERRORS else ASSIST_UNKNOWN_ERROR
    # None = the panel did not report the field (firmware without wake words);
    # a list (possibly empty) = what it reported.
    available = _wake_word_list(data.get("wake_words"))
    active = _wake_word_list(data.get("active_wake_words"))
    if active is not None and available is not None:
        active = [word for word in active if word in available]
    return {"state": state, "id": session_id, "error": error,
            "wake_words": available, "active_wake_words": active}


class WakeEvent(NamedTuple):
    word: str
    sequence: int


def parse_wake_event(payload: Any, available: Any) -> WakeEvent | None:
    """Validate a wake word detection; None unless the word is available."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > WAKE_EVENT_MAX_PAYLOAD:
            return None
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or len(payload) > WAKE_EVENT_MAX_PAYLOAD:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != ASSIST_PROTOCOL_VERSION:
        return None
    word = data.get("word")
    if not valid_wake_word_id(word) or not available or word not in available:
        return None
    sequence = data.get("seq")
    if type(sequence) is not int or not 0 <= sequence <= _UINT32_MAX:
        return None
    return WakeEvent(word, sequence)


class AudioSession:
    """Frames of one capture session, handed to the pipeline as a stream.

    Frames of other sessions are dropped. Duplicates and frames older than the
    newest one already queued are dropped too; there is no reordering buffer.
    The stream ends on the frame with the last flag, on ``finish()`` (stop,
    panel idle or offline) or after ``frame_timeout_s`` without frames.
    """

    def __init__(self, session_id: str, frame_timeout_s: float = FRAME_TIMEOUT_S,
                 max_frames: int = AUDIO_QUEUE_MAX_FRAMES) -> None:
        self.session_id = session_id
        self.frame_timeout_s = frame_timeout_s
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_frames + 1)
        self._max_frames = max_frames
        self._next_sequence = 0
        self.finished = False
        self.dropped = 0
        self.stop_sent = False

    def feed(self, frame: AudioFrame) -> bool:
        """Queue a frame of this session; False when it was dropped."""
        if self.finished or frame.session_id != self.session_id:
            return False
        if frame.sequence < self._next_sequence:
            self.dropped += 1
            return False
        self._next_sequence = frame.sequence + 1
        if frame.pcm:
            if self._queue.qsize() >= self._max_frames:
                self.dropped += 1
            else:
                self._queue.put_nowait(frame.pcm)
        if frame.last:
            self.finish()
        return True

    def finish(self) -> None:
        """End the stream after the frames already queued."""
        if self.finished:
            return
        self.finished = True
        # One slot is reserved for the end marker, so this never overflows.
        self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[bytes]:
        while True:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), self.frame_timeout_s)
            except asyncio.TimeoutError:
                self.finish()
                return
            if chunk is None:
                return
            yield chunk


async def async_publish_assist_command(hass: Any, bridge: Any, paired: bool, base_topic: str,
                                       payload: str, publish) -> bool:
    """Send a microphone command; sealed through the running Bridge when paired.

    Mirrors speaker.async_publish_audio_command: a paired panel never gets
    an unencrypted command, so without a running Bridge it is dropped.
    """
    sealed_publish = getattr(bridge, "async_publish_assist_command", None)
    if sealed_publish is not None:
        await sealed_publish(payload)
        return True
    if paired:
        return False
    await publish(hass, assist_command_topic(base_topic), payload, qos=0, retain=False)
    return True
