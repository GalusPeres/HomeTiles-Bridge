"""Clip playback on a panel's own speaker over MQTT.

The module has no Home Assistant imports so the payloads, validation and
topic contract can be tested without a Home Assistant install. The panel side
is ``src/audio/speaker_contract.h`` in the firmware.

MQTT contract (base = the panel base topic):
- ``{base}/cmnd/audio``: Bridge to panel, not retained, JSON
  ``{"v": 1, "op": "play", "id": "<hex>", "url": "http(s)://..."}``,
  ``{"v": 1, "op": "stop"}``, ``{"v": 1, "op": "pause"}``,
  ``{"v": 1, "op": "resume"}``, ``{"v": 1, "op": "volume", "volume": 0.5}`` or
  ``{"v": 1, "op": "mute", "muted": true}``. Sealed on the encrypted command
  channel (data name ``audio``) once the panel is paired, because play URLs
  carry Home Assistant's signed access tokens.
- ``{base}/stat/audio``: panel to Bridge, retained JSON status
  ``{"v": 1, "state": "idle"|"playing"|"paused", "volume": 0.5, "muted": false,
  "formats": ["mp3", "wav"], "max_bytes": N, "pausable": true}`` plus ``"id"``
  of the current or last clip and ``"error"`` when that clip failed. While a
  clip plays or pauses: ``"position_ms"`` at the moment of the status and
  ``"duration_ms"`` once the length is known (firmware from v0.9.4), and
  ``"stream": "tcp-ack-v1"`` when the panel takes clips decoded by the Bridge.

Such a panel gets a ``tcp://`` play URL: the Bridge's FFmpeg reads the clip
and sends PCM over the camera transport (audio_stream.py), so the panel
neither downloads nor decodes. Other panels download the http(s) URL
themselves and play MP3 at any sample rate and WAV.

A Bridge stream's play command carries ``"restart": true``: the Bridge plays
the clip again from the paused point to resume. Firmware from b512 then
closes the stream on a pause (no buffers, no FFmpeg held while paused) and
reports ``"released": true`` with the paused ``"position_ms"``; older
firmware keeps the stream and resumes it.
"""

from __future__ import annotations

import json
import math
import secrets
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

AUDIO_LEAF = "audio"
AUDIO_PROTOCOL_VERSION = 1
SPEAKER_UNIQUE_ID_SUFFIX = "_speaker"

AUDIO_STATES = frozenset({"idle", "playing", "paused"})
# Bound on a reported position or length: 100 hours.
AUDIO_MAX_TIME_MS = 100 * 3600 * 1000
AUDIO_ERRORS = frozenset({
    "invalid_url",
    "https_unsupported",
    "download_failed",
    "too_large",
    "unsupported_format",
    "decode_failed",
    "codec_unavailable",
    "out_of_memory",
})
AUDIO_UNKNOWN_ERROR = "unknown"
AUDIO_FORMATS = frozenset({"mp3", "wav"})
# Bridge-decoded clips (audio_stream.py): the transport a panel announces and
# the PCM rates. Music keeps its full range; speech, Assist answers and
# announcements need no more than 22.05 kHz and halve the traffic.
AUDIO_STREAM_TRANSPORT = "tcp-ack-v1"
AUDIO_RATE_MUSIC = 44100
AUDIO_RATE_SPEECH = 22050

AUDIO_STATUS_MAX_PAYLOAD = 1024
# Matches the panel's kMaxUrlLength; longer URLs are refused before sending.
AUDIO_MAX_URL_LENGTH = 1024
AUDIO_DEFAULT_MAX_BYTES = 4 * 1024 * 1024
# Device-announced limits beyond this are malformed, not merely large.
AUDIO_MAX_ANNOUNCED_BYTES = 64 * 1024 * 1024

_ID_CHARS = frozenset("0123456789abcdef")


def audio_command_topic(base_topic: str) -> str:
    return f"{base_topic}/cmnd/{AUDIO_LEAF}"


def audio_status_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{AUDIO_LEAF}"


def speaker_unique_id(device_id: str) -> str:
    return f"{device_id}{SPEAKER_UNIQUE_ID_SUFFIX}"


def is_speaker_unique_id(unique_id: str | None) -> bool:
    return bool(unique_id) and unique_id.endswith(SPEAKER_UNIQUE_ID_SUFFIX)


def new_request_id() -> str:
    return secrets.token_hex(8)


def valid_request_id(value: Any) -> bool:
    return (isinstance(value, str) and 16 <= len(value) <= 32
            and all(c in _ID_CHARS for c in value))


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def build_play_command(request_id: str, url: str, live: bool = False,
                       lead_ms: int | None = None, restart: bool = False,
                       timer: bool = False) -> str:
    """Play url; live (a camera's sound) keeps the panel's buffer short.

    lead_ms: how much earlier this sound leaves the Bridge than the camera's
    picture; the panel holds the picture back by its sound delay less that.
    restart: the Bridge plays this clip again from the paused point to
    resume, so the panel may close the stream on a pause (never live).
    timer: a finished voice timer's own sound; the panel plays it at its
    timer volume, unmuted (never live).
    """
    if not valid_request_id(request_id):
        raise ValueError("invalid_request_id")
    payload = {"v": AUDIO_PROTOCOL_VERSION, "op": "play", "id": request_id, "url": url}
    if live:
        payload["live"] = True
        if lead_ms is not None:
            payload["lead_ms"] = max(0, min(1000, int(lead_ms)))
    elif restart:
        payload["restart"] = True
    if timer and not live:
        payload["timer_sound"] = True
    return _encode(payload)


def build_stop_command() -> str:
    return _encode({"v": AUDIO_PROTOCOL_VERSION, "op": "stop"})


def build_pause_command(paused: bool) -> str:
    """Pause or resume the playing clip."""
    return _encode({"v": AUDIO_PROTOCOL_VERSION, "op": "pause" if paused else "resume"})


def media_title(media_id: Any, extra: Any = None) -> str | None:
    """A title for Home Assistant's player bar.

    The caller's title when it sends one, the message of a TTS media source,
    else the file name without its extension; None when nothing fits.
    """
    if isinstance(extra, dict):
        title = extra.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:255]
    if not isinstance(media_id, str) or not media_id:
        return None
    parts = urlsplit(media_id)
    if parts.query:
        for key, value in parse_qsl(parts.query):
            if key == "message" and value.strip():
                return value.strip()[:255]
    name = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = name.strip()
    return name[:255] or None


def stream_rate(media_id: Any, announce: Any = False) -> int:
    """The Bridge's PCM rate for a clip: speech at 22.05 kHz, music at 44.1 kHz."""
    if announce is True:
        return AUDIO_RATE_SPEECH
    if isinstance(media_id, str) and (media_id.startswith("media-source://tts/")
                                      or "/api/tts_proxy/" in media_id):
        return AUDIO_RATE_SPEECH
    return AUDIO_RATE_MUSIC


def _time_ms(value: Any) -> int | None:
    if type(value) is not int or not 0 <= value <= AUDIO_MAX_TIME_MS:
        return None
    return value


def clamp_volume(volume: Any) -> float:
    try:
        value = float(volume)
    except (TypeError, ValueError):
        raise ValueError("invalid_volume") from None
    if math.isnan(value):
        raise ValueError("invalid_volume")
    return min(1.0, max(0.0, value))


def build_volume_command(volume: Any) -> str:
    return _encode({"v": AUDIO_PROTOCOL_VERSION, "op": "volume",
                    "volume": round(clamp_volume(volume), 3)})


def build_mute_command(muted: bool) -> str:
    return _encode({"v": AUDIO_PROTOCOL_VERSION, "op": "mute", "muted": bool(muted)})


def playable_url_error(url: Any, https_allowed: bool = False) -> str | None:
    """Why the panel cannot fetch url, or None.

    The panel needs an absolute URL it can reach on its own. Firmware of the
    first speaker beta refused https; current firmware accepts it.
    """
    if not isinstance(url, str) or not url or len(url) > AUDIO_MAX_URL_LENGTH:
        return "invalid_url"
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in url):
        return "invalid_url"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid_url"
    if parts.scheme == "https":
        if not https_allowed:
            return "https_unsupported"
    elif parts.scheme != "http":
        return "invalid_url"
    if not parts.netloc or not parts.hostname:
        return "invalid_url"
    return None


def _decode_json_object(payload: Any, limit: int) -> dict[str, Any] | None:
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > limit:
            return None
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or len(payload) > limit:
        return None
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_status(payload: Any) -> dict[str, Any] | None:
    """Validate the retained status; None for anything malformed."""
    data = _decode_json_object(payload, AUDIO_STATUS_MAX_PAYLOAD)
    if data is None or data.get("v") != AUDIO_PROTOCOL_VERSION:
        return None
    state = data.get("state")
    if state not in AUDIO_STATES:
        return None
    volume = data.get("volume")
    if type(volume) not in (int, float) or isinstance(volume, bool) or math.isnan(volume):
        return None
    muted = data.get("muted", False)
    if type(muted) is not bool:
        return None
    max_bytes = data.get("max_bytes", AUDIO_DEFAULT_MAX_BYTES)
    if type(max_bytes) is not int or not 0 < max_bytes <= AUDIO_MAX_ANNOUNCED_BYTES:
        return None
    formats = data.get("formats", sorted(AUDIO_FORMATS))
    if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
        return None
    request_id = data.get("id")
    if request_id is not None and not valid_request_id(request_id):
        request_id = None
    error = data.get("error")
    if error is not None:
        error = error if error in AUDIO_ERRORS else AUDIO_UNKNOWN_ERROR
    return {
        "state": state,
        "volume": min(1.0, max(0.0, float(volume))),
        "muted": muted,
        "max_bytes": max_bytes,
        "formats": [f for f in formats if f in AUDIO_FORMATS],
        "id": request_id,
        "error": error,
        # Firmware before v0.9.4 reports neither.
        "pausable": data.get("pausable") is True,
        "position_ms": _time_ms(data.get("position_ms")),
        "duration_ms": _time_ms(data.get("duration_ms")) or None,
        # Firmware from b502 takes Bridge-decoded clips (audio_stream.py).
        "stream": data.get("stream") == AUDIO_STREAM_TRANSPORT,
        # Firmware from b512: paused with the stream closed; resume plays the
        # clip again at position_ms.
        "released": state == "paused" and data.get("released") is True,
    }


async def async_publish_audio_command(hass: Any, bridge: Any, paired: bool, base_topic: str,
                                      payload: str, publish) -> bool:
    """Send a speaker command; sealed through the running Bridge when paired.

    A paired panel never receives play URLs (which carry access tokens)
    unencrypted: without a running Bridge the command is dropped.
    """
    sealed_publish = getattr(bridge, "async_publish_audio_command", None)
    if sealed_publish is not None:
        await sealed_publish(payload)
        return True
    if paired:
        return False
    await publish(hass, audio_command_topic(base_topic), payload, qos=0, retain=False)
    return True
