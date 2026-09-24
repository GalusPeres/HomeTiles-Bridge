"""On-demand still images from a panel's own camera over MQTT.

The module has no Home Assistant imports so the request lifecycle, payload
validation and topic contract can be tested without a Home Assistant install.

MQTT contract (base = the panel base topic):
- ``{base}/cmnd/local_camera``: Bridge to panel, not retained, JSON request
  ``{"v": 1, "id": "<hex>", "op": "snapshot", "max_bytes": N}``, or the
  user's pause switch ``{"v": 1, "action": "pause"}`` /
  ``{"v": 1, "action": "resume"}``.
- ``{base}/stat/local_camera``: panel to Bridge, retained JSON status. While
  the user paused the camera it reports ``"state": "disabled"`` together
  with ``"paused": true``; the field is absent otherwise and on firmware
  without the pause switch.
- ``{base}/stat/local_camera/image/<id>``: panel to Bridge, raw JPEG bytes.
- ``{base}/stat/local_camera/error/<id>``: panel to Bridge, JSON error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import re
import secrets
from time import monotonic
from typing import Any

LOCAL_CAMERA_LEAF = "local_camera"
LOCAL_CAMERA_PROTOCOL_VERSION = 1
LOCAL_CAMERA_OP_SNAPSHOT = "snapshot"
LOCAL_CAMERA_ACTION_PAUSE = "pause"
LOCAL_CAMERA_ACTION_RESUME = "resume"
LOCAL_CAMERA_UNIQUE_ID_SUFFIX = "_local_camera"

LOCAL_CAMERA_STATES = frozenset({"ready", "disabled", "error"})
LOCAL_CAMERA_ERRORS = frozenset({
    "busy",
    "disabled",
    "sensor_unavailable",
    "encoder_busy",
    "too_large",
    "rate_limited",
})
LOCAL_CAMERA_UNKNOWN_ERROR = "unknown"

LOCAL_CAMERA_DEFAULT_MAX_BYTES = 131072
LOCAL_CAMERA_DEFAULT_MIN_INTERVAL_MS = 1000
LOCAL_CAMERA_MAX_MIN_INTERVAL_MS = 60000
LOCAL_CAMERA_MAX_DIMENSION = 8192
# Device-announced limits beyond this are malformed, not merely large.
LOCAL_CAMERA_MAX_ANNOUNCED_BYTES = 16 * 1024 * 1024
LOCAL_CAMERA_STATUS_MAX_PAYLOAD = 1024
LOCAL_CAMERA_ERROR_MAX_PAYLOAD = 256
LOCAL_CAMERA_MIN_JPEG_BYTES = 4

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16,32}$")
_TOKEN_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


class LocalCameraError(Exception):
    """A snapshot request ended without an image."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- Topics and identifiers -------------------------------------------------

def local_camera_command_topic(base_topic: str) -> str:
    return f"{base_topic}/cmnd/{LOCAL_CAMERA_LEAF}"


def local_camera_status_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{LOCAL_CAMERA_LEAF}"


def local_camera_image_prefix(base_topic: str) -> str:
    return f"{local_camera_status_topic(base_topic)}/image"


def local_camera_error_prefix(base_topic: str) -> str:
    return f"{local_camera_status_topic(base_topic)}/error"


def local_camera_unique_id(device_id: str) -> str:
    return f"{device_id}{LOCAL_CAMERA_UNIQUE_ID_SUFFIX}"


def is_local_camera_unique_id(unique_id: str | None) -> bool:
    return bool(unique_id) and str(unique_id).endswith(LOCAL_CAMERA_UNIQUE_ID_SUFFIX)


def is_local_camera_self_loop(registry_entry: Any, domain: str, config_entry_id: str) -> bool:
    """Return whether a registry entry is this panel's own local camera.

    Streaming it back to the same panel would run capture, Home Assistant,
    FFmpeg, TCP and decode against the panel's single JPEG engine.
    """
    if registry_entry is None:
        return False
    return (getattr(registry_entry, "platform", None) == domain
            and getattr(registry_entry, "domain", None) == "camera"
            and getattr(registry_entry, "config_entry_id", None) == config_entry_id
            and is_local_camera_unique_id(getattr(registry_entry, "unique_id", None)))


def new_request_id() -> str:
    return secrets.token_hex(8)


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def request_id_from_topic(prefix: str, topic: Any) -> str | None:
    """Extract the correlation id from ``<prefix>/<id>``; reject anything else."""
    if not isinstance(topic, str) or not topic.startswith(prefix + "/"):
        return None
    candidate = topic[len(prefix) + 1:]
    return candidate if valid_request_id(candidate) else None


def build_request(request_id: str, max_bytes: int) -> dict[str, Any]:
    if not valid_request_id(request_id):
        raise ValueError("invalid_request_id")
    return {
        "v": LOCAL_CAMERA_PROTOCOL_VERSION,
        "id": request_id,
        "op": LOCAL_CAMERA_OP_SNAPSHOT,
        "max_bytes": int(max_bytes),
    }


def build_pause_request(paused: bool) -> dict[str, Any]:
    """Return the command that pauses (True) or resumes (False) the camera."""
    return {
        "v": LOCAL_CAMERA_PROTOCOL_VERSION,
        "action": LOCAL_CAMERA_ACTION_PAUSE if paused else LOCAL_CAMERA_ACTION_RESUME,
    }


# --- Payload parsing --------------------------------------------------------

def valid_jpeg(payload: Any, max_bytes: int) -> bool:
    """Accept only a complete baseline JPEG container within the size limit."""
    if not isinstance(payload, (bytes, bytearray)):
        return False
    length = len(payload)
    if length < LOCAL_CAMERA_MIN_JPEG_BYTES or length > max_bytes:
        return False
    return payload[:2] == _JPEG_SOI and payload[-2:] == _JPEG_EOI


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
        decoded = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    # bool is an int subclass; a JSON true must never become 1.
    if type(value) is not int or value < minimum or value > maximum:
        return None
    return value


def _optional_token(value: Any) -> str | None:
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return None


def parse_status(payload: Any, bridge_max_bytes: int) -> dict[str, Any] | None:
    """Validate the retained status; return None for any malformed field."""
    data = _decode_json_object(payload, LOCAL_CAMERA_STATUS_MAX_PAYLOAD)
    if data is None or data.get("v") != LOCAL_CAMERA_PROTOCOL_VERSION:
        return None
    if type(data.get("v")) is not int:
        return None
    state = data.get("state")
    if state not in LOCAL_CAMERA_STATES:
        return None
    result: dict[str, Any] = {"state": state}
    for key in ("width", "height"):
        if key in data:
            value = _bounded_int(data[key], 1, LOCAL_CAMERA_MAX_DIMENSION)
            if value is None:
                return None
            result[key] = value
        elif state == "ready":
            return None
    image_format = data.get("format", "jpeg")
    if image_format != "jpeg":
        return None
    max_bytes = _bounded_int(data.get("max_bytes", LOCAL_CAMERA_DEFAULT_MAX_BYTES),
                             1, LOCAL_CAMERA_MAX_ANNOUNCED_BYTES)
    if max_bytes is None:
        return None
    # The Bridge cap wins; a panel that promises more still gets bounded input.
    result["max_bytes"] = min(max_bytes, bridge_max_bytes)
    interval = _bounded_int(data.get("min_interval_ms", LOCAL_CAMERA_DEFAULT_MIN_INTERVAL_MS),
                            0, LOCAL_CAMERA_MAX_MIN_INTERVAL_MS)
    if interval is None:
        return None
    result["min_interval_s"] = interval / 1000.0
    if (sensor := _optional_token(data.get("sensor"))) is not None:
        result["sensor"] = sensor
    if (error := _optional_token(data.get("error"))) is not None:
        result["error"] = error
    # Additive field: older firmware never sends it, so absent means false.
    paused = data.get("paused", False)
    if type(paused) is not bool:
        return None
    result["paused"] = paused
    return result


def camera_allowed(status: dict[str, Any] | None, previous: bool | None) -> bool | None:
    """Return the pause switch state (True = capture allowed) for a status.

    ``None`` means unknown: no status yet, or the panel cleared it. An error
    or a disabled camera that the user did not pause says nothing about the
    pause switch, so the last known state is kept.
    """
    if status is None:
        return None
    if status["paused"]:
        return False
    if status["state"] == "ready":
        return True
    return previous


def parse_error(payload: Any) -> str:
    """Return a known panel error code; anything else is ``unknown``."""
    data = _decode_json_object(payload, LOCAL_CAMERA_ERROR_MAX_PAYLOAD)
    if data is None:
        return LOCAL_CAMERA_UNKNOWN_ERROR
    code = data.get("error")
    return code if code in LOCAL_CAMERA_ERRORS else LOCAL_CAMERA_UNKNOWN_ERROR


def parse_connected(payload: Any) -> bool | None:
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "ignore")
    if not isinstance(payload, str):
        return None
    raw = payload.strip().lower()
    if raw in {"1", "on", "online", "true", "yes"}:
        return True
    if raw in {"0", "off", "offline", "false", "no"}:
        return False
    return None


# --- Request lifecycle ------------------------------------------------------

def _retrieve_exception(future: asyncio.Future) -> None:
    # Prevent "exception was never retrieved" noise when every waiter left.
    if not future.cancelled():
        future.exception()


class LocalCameraSnapshots:
    """Single in-flight snapshot request plus one cached frame per panel."""

    def __init__(
        self,
        *,
        timeout_s: float,
        stale_fallback_s: float,
        id_factory: Callable[[], str] = new_request_id,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._timeout_s = timeout_s
        self._stale_fallback_s = stale_fallback_s
        self._id_factory = id_factory
        self._clock = clock
        self._pending_id: str | None = None
        self._future: asyncio.Future | None = None
        self._deadline = 0.0
        self._max_bytes = 0
        self._last_request_at: float | None = None
        self._image: bytes | None = None
        self._image_at: float | None = None

    @property
    def pending_id(self) -> str | None:
        return self._pending_id

    def cached(self, now: float, max_age_s: float) -> bytes | None:
        if self._image is None or self._image_at is None or now - self._image_at >= max_age_s:
            return None
        return self._image

    def fallback(self, now: float) -> bytes | None:
        return self.cached(now, self._stale_fallback_s)

    def begin(self, now: float, min_interval_s: float, max_bytes: int):
        """Join the pending request or start a new one.

        Returns ``(future, request_id, is_new)`` or ``(None, None, False)``
        when a new request would exceed the per-panel request rate.
        """
        if self._future is not None and not self._future.done():
            if now < self._deadline:
                return self._future, self._pending_id, False
            # The owner was cancelled before it could expire the request.
            self.fail(self._pending_id, "timeout")
        if self._last_request_at is not None and now - self._last_request_at < min_interval_s:
            return None, None, False
        request_id = self._id_factory()
        if not valid_request_id(request_id):
            raise ValueError("invalid_request_id")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve_exception)
        self._future = future
        self._pending_id = request_id
        self._deadline = now + self._timeout_s
        self._max_bytes = max_bytes
        self._last_request_at = now
        return future, request_id, True

    def resolve_image(self, request_id: str | None, payload: Any, now: float) -> str:
        """Return ``accepted``, ``unsolicited`` or ``invalid``."""
        if (request_id is None or request_id != self._pending_id
                or self._future is None or self._future.done()):
            return "unsolicited"
        if not valid_jpeg(payload, self._max_bytes):
            self.fail(request_id, "invalid_image")
            return "invalid"
        image = bytes(payload)
        self._image = image
        self._image_at = now
        future = self._future
        self._clear()
        future.set_result(image)
        return "accepted"

    def resolve_error(self, request_id: str | None, code: str) -> bool:
        return self.fail(request_id, code)

    def fail(self, request_id: str | None, code: str) -> bool:
        if request_id is None or request_id != self._pending_id or self._future is None:
            return False
        future = self._future
        self._clear()
        if not future.done():
            future.set_exception(LocalCameraError(code))
        return True

    def fail_all(self, code: str) -> None:
        if self._pending_id is not None:
            self.fail(self._pending_id, code)

    def _clear(self) -> None:
        self._pending_id = None
        self._future = None
        self._deadline = 0.0

    async def async_fetch(
        self,
        publish: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        max_bytes: int,
        min_interval_s: float,
    ) -> tuple[bytes | None, str | None]:
        """Return ``(image, failure_code)``; the image may be a cached fallback.

        Concurrent callers share one MQTT request. A fresh cached frame and
        the per-panel minimum interval bound the load on the panel even when
        several dashboards and the MJPEG fallback of a dialog poll at once.
        """
        now = self._clock()
        if (image := self.cached(now, min_interval_s)) is not None:
            return image, None
        future, request_id, is_new = self.begin(now, min_interval_s, max_bytes)
        if future is None:
            return self.fallback(now), None
        if is_new:
            try:
                await publish(build_request(request_id, max_bytes))
            except Exception:  # noqa: BLE001 - the transport error type is HA-specific
                self.fail(request_id, "publish_failed")
                return self.fallback(self._clock()), "publish_failed"
        try:
            image = await asyncio.wait_for(asyncio.shield(future), self._timeout_s)
        except asyncio.TimeoutError:
            # Every waiter joined after the request started, so any waiter's
            # timeout means the request itself is overdue.
            self.fail(request_id, "timeout")
            return self.fallback(self._clock()), "timeout"
        except LocalCameraError as err:
            return self.fallback(self._clock()), err.code
        return image, None


class RateLimitedWarnings:
    """Allow one message per reason and interval, with a bounded key set."""

    def __init__(self, interval_s: float, max_keys: int = 16) -> None:
        self._interval_s = interval_s
        self._max_keys = max_keys
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self._interval_s:
            return False
        self._last[key] = now
        if len(self._last) > self._max_keys:
            oldest = min(self._last, key=self._last.get)
            self._last.pop(oldest, None)
        return True
