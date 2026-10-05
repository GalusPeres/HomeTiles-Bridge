"""Wire format and handshake of the direct panel link.

Protocol reference: docs-dev/bridge-link.md in the HomeTiles firmware
repository. The panel connects to the Bridge over TCP; every frame is a 4-byte
big-endian length followed by the body. Before the session keys exist the body
is ``type || payload``; afterwards it is ChaCha20-Poly1305(type || payload)
with the key of the sending direction, the nonce ``00000000 || u64be(counter)``
and the 4 length bytes as associated data.

Session keys, from the pairing key K of pairing.py:

  salt = SHA-256("HomeTiles link v1" || u16be(len(id)) || id
                 || u16be(len(base)) || base || n_p || n_b)
  panel key  = HKDF-SHA256(salt, K, "HomeTiles link panel-to-bridge v1", 32)
  bridge key = HKDF-SHA256(salt, K, "HomeTiles link bridge-to-panel v1", 32)

Nothing here performs I/O, so the tests run the exact code the integration
uses. Keys are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import struct
from typing import Any, Dict, Optional, Tuple

VERSION = 1

# Frame and payload limits (docs-dev/bridge-link.md).
MAX_FRAME = 70_000
MAX_TOPIC = 255
MAX_PAYLOAD = 65_535
MAX_STREAM = 2 * 1024 * 1024
MAX_STREAM_CHUNK = 8192
MAX_HELLO = 512
TAG_LENGTH = 16
NONCE_LENGTH = 16
KEY_LENGTH = 32

# Timing (seconds).
HANDSHAKE_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 45.0

TYPE_HELLO = 0x01
TYPE_WELCOME = 0x02
TYPE_REFUSE = 0x03
TYPE_READY = 0x04
TYPE_PUBLISH = 0x10
TYPE_SUBSCRIBE = 0x11
TYPE_UNSUBSCRIBE = 0x12
TYPE_STREAM_BEGIN = 0x13
TYPE_STREAM_DATA = 0x14
TYPE_STREAM_END = 0x15
TYPE_PING = 0x20
TYPE_PONG = 0x21

FLAG_RETAIN = 0x01

MODE_SESSION = "session"
MODE_PAIR = "pair"

REFUSE_VERSION = "version"
REFUSE_UNKNOWN = "unknown"
REFUSE_BASE = "base"
REFUSE_PAIR = "pair"
REFUSE_INVALID = "invalid"

_SALT_LABEL = b"HomeTiles link v1"
_INFO_PANEL = b"HomeTiles link panel-to-bridge v1"
_INFO_BRIDGE = b"HomeTiles link bridge-to-panel v1"

_DEVICE_ID = re.compile(r"[0-9A-Za-z_]{1,32}\Z")
_KID = re.compile(r"[0-9a-f]{16}\Z")
_NONCE_HEX = re.compile(r"[0-9a-f]{32}\Z")


class ProtocolError(Exception):
  """A malformed frame or handshake; the connection is closed."""


# ---------------------------------------------------------------------------
# Topics


def valid_topic(topic: Any) -> bool:
  """A concrete topic: 1-255 UTF-8 bytes without NUL or wildcards."""
  if not isinstance(topic, str) or not topic:
    return False
  if "\0" in topic or "+" in topic or "#" in topic:
    return False
  try:
    return len(topic.encode("utf-8")) <= MAX_TOPIC
  except UnicodeEncodeError:
    return False


def valid_filter(topic_filter: Any) -> bool:
  """An MQTT subscription filter ('+' per level, '#' only as the last level)."""
  if not isinstance(topic_filter, str) or not topic_filter or "\0" in topic_filter:
    return False
  levels = topic_filter.split("/")
  for index, level in enumerate(levels):
    if "#" in level and (level != "#" or index != len(levels) - 1):
      return False
    if "+" in level and level != "+":
      return False
  return True


def topic_matches(topic_filter: str, topic: str) -> bool:
  """MQTT topic matching for a filter with '+' and '#' wildcards."""
  if topic_filter == topic:
    return True
  filter_levels = topic_filter.split("/")
  topic_levels = topic.split("/")
  for index, level in enumerate(filter_levels):
    if level == "#":
      return True
    if index >= len(topic_levels):
      return False
    if level != "+" and level != topic_levels[index]:
      return False
  return len(filter_levels) == len(topic_levels)


# ---------------------------------------------------------------------------
# Frames


def frame_header(length: int) -> bytes:
  if length < 1 or length > MAX_FRAME:
    raise ProtocolError("frame_length")
  return struct.pack(">I", length)


def parse_frame_length(header: bytes) -> int:
  """The body length of a frame header; ProtocolError outside the limits."""
  if len(header) != 4:
    raise ProtocolError("frame_header")
  (length,) = struct.unpack(">I", header)
  if length < 1 or length > MAX_FRAME:
    raise ProtocolError("frame_length")
  return length


def plain_frame(frame_type: int, payload: bytes = b"") -> bytes:
  body = bytes([frame_type]) + payload
  return frame_header(len(body)) + body


def split_body(body: bytes) -> Tuple[int, bytes]:
  if not body:
    raise ProtocolError("empty_body")
  return body[0], body[1:]


def publish_payload(topic: str, payload: bytes, retain: bool) -> bytes:
  if not valid_topic(topic):
    raise ProtocolError("topic")
  if len(payload) > MAX_PAYLOAD:
    raise ProtocolError("payload_length")
  encoded = topic.encode("utf-8")
  return bytes([FLAG_RETAIN if retain else 0]) + struct.pack(">H", len(encoded)) + encoded + payload


def parse_publish(payload: bytes) -> Tuple[str, bytes, bool]:
  """(topic, payload, retain) of a publish frame."""
  topic, rest, retain = _parse_topic_header(payload)
  if len(rest) > MAX_PAYLOAD:
    raise ProtocolError("payload_length")
  return topic, rest, retain


def stream_begin_payload(topic: str, total: int, retain: bool = False) -> bytes:
  if not valid_topic(topic):
    raise ProtocolError("topic")
  if total < 0 or total > MAX_STREAM:
    raise ProtocolError("stream_length")
  encoded = topic.encode("utf-8")
  return (bytes([FLAG_RETAIN if retain else 0]) + struct.pack(">H", len(encoded)) + encoded
          + struct.pack(">I", total))


def parse_stream_begin(payload: bytes) -> Tuple[str, int, bool]:
  """(topic, total length, retain) of a publish-begin frame."""
  topic, rest, retain = _parse_topic_header(payload)
  if len(rest) != 4:
    raise ProtocolError("stream_header")
  (total,) = struct.unpack(">I", rest)
  if total > MAX_STREAM:
    raise ProtocolError("stream_length")
  return topic, total, retain


def _parse_topic_header(payload: bytes) -> Tuple[str, bytes, bool]:
  if len(payload) < 3:
    raise ProtocolError("publish_header")
  flags = payload[0]
  if flags & ~FLAG_RETAIN:
    raise ProtocolError("publish_flags")
  (topic_length,) = struct.unpack(">H", payload[1:3])
  if topic_length < 1 or topic_length > MAX_TOPIC or len(payload) < 3 + topic_length:
    raise ProtocolError("topic_length")
  try:
    topic = payload[3:3 + topic_length].decode("utf-8")
  except UnicodeDecodeError as err:
    raise ProtocolError("topic_encoding") from err
  if not valid_topic(topic):
    raise ProtocolError("topic")
  return topic, payload[3 + topic_length:], bool(flags & FLAG_RETAIN)


def parse_topic(payload: bytes) -> str:
  """The topic of a subscribe or unsubscribe frame."""
  try:
    topic = payload.decode("utf-8")
  except UnicodeDecodeError as err:
    raise ProtocolError("topic_encoding") from err
  if not valid_topic(topic):
    raise ProtocolError("topic")
  return topic


# ---------------------------------------------------------------------------
# Handshake


def parse_hello(payload: bytes) -> Dict[str, Any]:
  """The validated hello of a panel; ProtocolError for anything else."""
  if len(payload) > MAX_HELLO:
    raise ProtocolError("hello_length")
  try:
    hello = json.loads(payload.decode("utf-8"))
  except (UnicodeDecodeError, ValueError) as err:
    raise ProtocolError("hello_json") from err
  if not isinstance(hello, dict):
    raise ProtocolError("hello_json")
  if hello.get("v") != VERSION:
    raise ProtocolError(REFUSE_VERSION)
  device_id = hello.get("id")
  base = hello.get("base")
  mode = hello.get("mode")
  if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
    raise ProtocolError("hello_id")
  if not valid_topic(base) or len(base.encode("utf-8")) > 128:
    raise ProtocolError("hello_base")
  result: Dict[str, Any] = {"id": device_id, "base": base, "mode": mode}
  if mode == MODE_SESSION:
    kid = hello.get("kid")
    nonce = hello.get("n")
    if not isinstance(kid, str) or not _KID.match(kid):
      raise ProtocolError("hello_kid")
    if not isinstance(nonce, str) or not _NONCE_HEX.match(nonce):
      raise ProtocolError("hello_nonce")
    result["kid"] = kid
    result["n"] = bytes.fromhex(nonce)
  elif mode != MODE_PAIR:
    raise ProtocolError("hello_mode")
  return result


def build_hello(device_id: str, base: str, mode: str, kid: Optional[str] = None,
                nonce: Optional[bytes] = None) -> bytes:
  """The panel's hello (used by the tests; the firmware builds its own)."""
  hello: Dict[str, Any] = {"v": VERSION, "id": device_id, "base": base, "mode": mode}
  if mode == MODE_SESSION:
    hello["kid"] = kid
    hello["n"] = (nonce or b"").hex()
  return json.dumps(hello, separators=(",", ":")).encode("utf-8")


def build_welcome(nonce: Optional[bytes]) -> bytes:
  welcome: Dict[str, Any] = {"v": VERSION}
  if nonce is not None:
    welcome["n"] = nonce.hex()
  return json.dumps(welcome, separators=(",", ":")).encode("utf-8")


def build_refuse(reason: str) -> bytes:
  return json.dumps({"v": VERSION, "r": reason}, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Session keys and sealed frames


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
  prk = hmac.new(salt, ikm, hashlib.sha256).digest()
  output = b""
  block = b""
  counter = 1
  while len(output) < length:
    block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
    output += block
    counter += 1
  return output[:length]


def session_salt(device_id: str, base: str, panel_nonce: bytes, bridge_nonce: bytes) -> bytes:
  encoded_id = device_id.encode("utf-8")
  encoded_base = base.encode("utf-8")
  return hashlib.sha256(
    _SALT_LABEL + struct.pack(">H", len(encoded_id)) + encoded_id
    + struct.pack(">H", len(encoded_base)) + encoded_base + panel_nonce + bridge_nonce
  ).digest()


def session_keys(pairing_key: bytes, device_id: str, base: str, panel_nonce: bytes,
                 bridge_nonce: bytes) -> Tuple[bytes, bytes]:
  """(panel-to-bridge key, bridge-to-panel key) of one connection."""
  if len(pairing_key) != KEY_LENGTH or len(panel_nonce) != NONCE_LENGTH or len(bridge_nonce) != NONCE_LENGTH:
    raise ProtocolError("key_material")
  salt = session_salt(device_id, base, panel_nonce, bridge_nonce)
  return _hkdf(salt, pairing_key, _INFO_PANEL, KEY_LENGTH), _hkdf(salt, pairing_key, _INFO_BRIDGE, KEY_LENGTH)


def _aead(key: bytes):
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  return ChaCha20Poly1305(key)


def _nonce(counter: int) -> bytes:
  return b"\0\0\0\0" + struct.pack(">Q", counter)


class Sealer:
  """Seals the frames of one direction with a counter nonce."""

  def __init__(self, key: bytes) -> None:
    self._aead = _aead(key)
    self.counter = 0

  def __repr__(self) -> str:
    return f"Sealer(counter={self.counter})"

  def frame(self, frame_type: int, payload: bytes = b"") -> bytes:
    length = 1 + len(payload) + TAG_LENGTH
    header = frame_header(length)
    sealed = self._aead.encrypt(_nonce(self.counter), bytes([frame_type]) + payload, header)
    self.counter += 1
    return header + sealed


class Opener:
  """Opens the frames of the other direction in counter order."""

  def __init__(self, key: bytes) -> None:
    self._aead = _aead(key)
    self.counter = 0

  def __repr__(self) -> str:
    return f"Opener(counter={self.counter})"

  def open(self, header: bytes, body: bytes) -> Tuple[int, bytes]:
    """(type, payload); ProtocolError when the frame does not authenticate."""
    from cryptography.exceptions import InvalidTag
    if len(body) < 1 + TAG_LENGTH:
      raise ProtocolError("sealed_length")
    try:
      plaintext = self._aead.decrypt(_nonce(self.counter), body, header)
    except InvalidTag as err:
      raise ProtocolError("authentication") from err
    self.counter += 1
    return split_body(plaintext)
