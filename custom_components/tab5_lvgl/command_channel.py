"""Encrypted, authenticated commands between a panel and the Bridge.

Protocol reference: docs-dev/command-encryption.md in the HomeTiles firmware
repository. The pairing code is shown on the panel and entered in the Bridge
options (Security); both sides derive the same keys from it:

  HKDF-SHA256(salt="HomeTiles command pairing v1", ikm=<25 code symbols>,
              info="panel-to-bridge" | "bridge-to-panel" | "announce" (32 bytes) |
                   "key-id" (8 bytes))

Envelope, published on {base}/secure/panel (from the panel) and
{base}/secure/bridge (from the Bridge):

  {"v":1,"k":"<key id hex>","n":"<nonce hex>","d":"<hex ciphertext || tag>"}

ChaCha20-Poly1305 with the direction's key, a random 96-bit nonce and the MQTT
topic as associated data. Plaintext: "<type> <session|-> <seq> <name|->\\n<body>".

Nothing here performs I/O, so the tests run the exact code the integration uses.
The pairing code and the keys are never logged.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 25
KDF_SALT = b"HomeTiles command pairing v1"
MAX_BODY = 2048
MAX_NAME = 32
REPLAY_WINDOW = 64
SESSION_MIN_INTERVAL_S = 2.0
REKEY_MIN_INTERVAL_S = 5.0

PANEL_TOPIC_LEAF = "secure/panel"
BRIDGE_TOPIC_LEAF = "secure/bridge"
STATUS_TOPIC_LEAF = "stat/secure"
# Same value as const.CONF_COMMAND_PAIRING; this module stays import-free.
CONF_PAIRING_KEY = "command_pairing_code"
# A code removed in Home Assistant is kept here until the panel confirmed that
# it turned pairing off (same value as const.CONF_COMMAND_PAIRING_REMOVING).
CONF_REMOVING_KEY = "command_pairing_removing"

# Panel command topics that arrive sealed once pairing is active.
SEALED_COMMANDS = frozenset({"scene", "light", "switch", "media", "climate", "cover", "camera", "value"})
# Bridge-to-panel messages that carry stream tokens and travel sealed.
SEALED_DATA = frozenset({"camera", "local_camera"})

TYPE_HELLO = "hello"
TYPE_SESSION = "session"
TYPE_REKEY = "rekey"
TYPE_COMMAND = "cmd"
TYPE_DATA = "data"
# Either side turns pairing off; numbered in the sender's session like cmd/data.
TYPE_UNPAIR = "unpair"
_TYPES = frozenset({TYPE_HELLO, TYPE_SESSION, TYPE_REKEY, TYPE_COMMAND, TYPE_DATA, TYPE_UNPAIR})

_NAME = re.compile(r"[a-z0-9_]{1,32}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_SEQ = re.compile(r"(0|[1-9][0-9]{0,9})\Z")
_HEX = re.compile(r"[0-9a-fA-F]*\Z")
_MAX_ENVELOPE = 200 + 2 * (MAX_BODY + 128)


def _symbol(char: str) -> Optional[str]:
  char = char.upper()
  if char == "O":
    char = "0"
  elif char in ("I", "L"):
    char = "1"
  return char if char in ALPHABET else None


def normalize_code(text: Any) -> Optional[str]:
  """Return the 25 canonical symbols, or None for anything else."""
  if not isinstance(text, str):
    return None
  symbols = []
  for char in text:
    if char in "- \t":
      continue
    symbol = _symbol(char)
    if symbol is None:
      return None
    symbols.append(symbol)
  return "".join(symbols) if len(symbols) == CODE_LENGTH else None


def format_code(code: str) -> str:
  return "-".join(code[i:i + 5] for i in range(0, CODE_LENGTH, 5))


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
  prk = hmac.new(KDF_SALT, ikm, hashlib.sha256).digest()
  output = b""
  block = b""
  counter = 1
  while len(output) < length:
    block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
    output += block
    counter += 1
  return output[:length]


class Keys:
  """Direction keys and the public key id derived from one pairing code."""

  def __init__(self, code: str) -> None:
    canonical = normalize_code(code)
    if canonical is None:
      raise ValueError("invalid_pairing_code")
    ikm = canonical.encode("ascii")
    self.panel_to_bridge = _hkdf(ikm, b"panel-to-bridge", 32)
    self.bridge_to_panel = _hkdf(ikm, b"bridge-to-panel", 32)
    # Signs the panel's announcement (announcement_guard.py).
    self.announce = _hkdf(ikm, b"announce", 32)
    self.key_id = _hkdf(ikm, b"key-id", 8).hex()

  def __repr__(self) -> str:  # Never expose key material in logs or tracebacks.
    return f"Keys(key_id={self.key_id})"


def _aead(key: bytes):
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  return ChaCha20Poly1305(key)


class Message:
  def __init__(self, kind: str, session: Optional[str], seq: int, name: Optional[str], body: bytes) -> None:
    self.kind = kind
    self.session = session
    self.seq = seq
    self.name = name
    self.body = body


def build_plaintext(kind: str, session: Optional[str], seq: int, name: Optional[str], body: bytes = b"") -> bytes:
  if kind not in _TYPES or len(body) > MAX_BODY or not 0 <= seq <= 0xFFFFFFFF:
    raise ValueError("invalid_message")
  if session is not None and not _HEX32.match(session):
    raise ValueError("invalid_session")
  if name is not None and not _NAME.match(name):
    raise ValueError("invalid_name")
  header = f"{kind} {session or '-'} {seq} {name or '-'}\n"
  return header.encode("ascii") + body


def parse_plaintext(plaintext: bytes) -> Optional[Message]:
  line, separator, body = plaintext.partition(b"\n")
  if not separator or len(body) > MAX_BODY:
    return None
  try:
    fields = line.decode("ascii").split(" ")
  except UnicodeDecodeError:
    return None
  if len(fields) != 4:
    return None
  kind, session, seq, name = fields
  if kind not in _TYPES or not _SEQ.match(seq):
    return None
  seq_value = int(seq)
  if seq_value > 0xFFFFFFFF:
    return None
  if session != "-" and not _HEX32.match(session):
    return None
  if name != "-" and not _NAME.match(name):
    return None
  return Message(kind, None if session == "-" else session, seq_value,
                 None if name == "-" else name, body)


def seal(key: bytes, key_id: str, topic: str, plaintext: bytes, nonce: Optional[bytes] = None) -> str:
  nonce = nonce if nonce is not None else os.urandom(12)
  data = _aead(key).encrypt(nonce, plaintext, topic.encode("utf-8"))
  return json.dumps({"v": 1, "k": key_id, "n": nonce.hex(), "d": data.hex()}, separators=(",", ":"))


OPEN_OK = "ok"
OPEN_MALFORMED = "malformed"
OPEN_OTHER_KEY = "other_key"
OPEN_REJECTED = "rejected"


def open_envelope(key: bytes, key_id: str, topic: str, payload: Any) -> Tuple[str, Optional[bytes]]:
  if isinstance(payload, (bytes, bytearray)):
    try:
      payload = bytes(payload).decode("ascii")
    except UnicodeDecodeError:
      return OPEN_MALFORMED, None
  if not isinstance(payload, str) or len(payload) > _MAX_ENVELOPE:
    return OPEN_MALFORMED, None
  try:
    envelope = json.loads(payload)
  except ValueError:
    return OPEN_MALFORMED, None
  if not isinstance(envelope, dict) or envelope.get("v") != 1:
    return OPEN_MALFORMED, None
  kid, nonce, data = envelope.get("k"), envelope.get("n"), envelope.get("d")
  if (not isinstance(kid, str) or len(kid) != 16 or not _HEX.match(kid)
      or not isinstance(nonce, str) or len(nonce) != 24 or not _HEX.match(nonce)
      or not isinstance(data, str) or len(data) % 2 or len(data) < 32 or not _HEX.match(data)):
    return OPEN_MALFORMED, None
  if not hmac.compare_digest(kid.lower(), key_id):
    return OPEN_OTHER_KEY, None
  try:
    from cryptography.exceptions import InvalidTag
  except ImportError:  # pragma: no cover - cryptography ships with Home Assistant
    return OPEN_REJECTED, None
  try:
    plaintext = _aead(key).decrypt(bytes.fromhex(nonce), bytes.fromhex(data), topic.encode("utf-8"))
  except InvalidTag:
    return OPEN_REJECTED, None
  return OPEN_OK, plaintext


class ReplayWindow:
  """Accepts every sequence number once, within 64 behind the highest."""

  def __init__(self) -> None:
    self.highest = 0
    self.seen = 0

  def accept(self, seq: int) -> bool:
    if seq <= 0:
      return False
    if seq > self.highest:
      shift = seq - self.highest
      self.seen = 0 if shift >= 64 else (self.seen << shift) & 0xFFFFFFFFFFFFFFFF
      self.seen |= 1
      self.highest = seq
      return True
    offset = self.highest - seq
    if offset >= REPLAY_WINDOW:
      return False
    bit = 1 << offset
    if self.seen & bit:
      return False
    self.seen |= bit
    return True


def parse_status(payload: Any) -> Dict[str, Any]:
  """Retained {base}/stat/secure: {"state": "off"|"pending"|"active", "kid"}."""
  if isinstance(payload, (bytes, bytearray)):
    payload = bytes(payload).decode("utf-8", "ignore")
  if not isinstance(payload, str) or not payload.strip() or len(payload) > 256:
    return {"state": "off", "kid": None}
  try:
    data = json.loads(payload)
  except ValueError:
    return {"state": "unknown", "kid": None}
  if not isinstance(data, dict):
    return {"state": "unknown", "kid": None}
  state = data.get("state")
  kid = data.get("kid")
  if state not in ("pending", "active", "off"):
    state = "unknown"
  if not (isinstance(kid, str) and len(kid) == 16 and _HEX.match(kid)):
    kid = None
  return {"state": state, "kid": kid.lower() if kid else None}


class BridgeChannel:
  """Bridge end of the channel for one panel (one config entry).

  With ``removing`` the pairing was removed in Home Assistant: the channel
  still answers a hello, so the Bridge can send the panel an unpair, but it
  runs no sealed commands.
  """

  def __init__(self, code: str, base_topic: str, *, clock: Callable[[], float],
               random: Callable[[int], bytes] = os.urandom, removing: bool = False) -> None:
    self.keys = Keys(code)
    self.removing = removing
    self.base_topic = base_topic
    self.panel_topic = f"{base_topic}/{PANEL_TOPIC_LEAF}"
    self.bridge_topic = f"{base_topic}/{BRIDGE_TOPIC_LEAF}"
    self._clock = clock
    self._random = random
    self.session: Optional[str] = None
    self._window = ReplayWindow()
    self._next_seq = 1
    self._last_session_at: Optional[float] = None
    self._last_rekey_at: Optional[float] = None

  def _seal(self, plaintext: bytes) -> Tuple[str, str]:
    return self.bridge_topic, seal(self.keys.bridge_to_panel, self.keys.key_id, self.bridge_topic,
                                   plaintext, self._random(12))

  def rekey(self, force: bool = False) -> Optional[Tuple[str, str]]:
    now = self._clock()
    if not force and self._last_rekey_at is not None and now - self._last_rekey_at < REKEY_MIN_INTERVAL_S:
      return None
    self._last_rekey_at = now
    return self._seal(build_plaintext(TYPE_REKEY, None, 0, None))

  def handle_panel_message(self, payload: Any) -> Tuple[str, Any]:
    """Return ("command", (leaf, body)), ("reply", (topic, payload)), ("unpair", None) or ("ignore", reason)."""
    status, plaintext = open_envelope(self.keys.panel_to_bridge, self.keys.key_id, self.panel_topic, payload)
    if status != OPEN_OK:
      return "ignore", status
    message = parse_plaintext(plaintext)
    if message is None:
      return "ignore", OPEN_MALFORMED
    if message.kind == TYPE_HELLO:
      if message.session is not None or message.name is None or not _HEX32.match(message.name) or message.body:
        return "ignore", "invalid_hello"
      now = self._clock()
      if self._last_session_at is not None and now - self._last_session_at < SESSION_MIN_INTERVAL_S:
        return "ignore", "session_rate_limited"
      self._last_session_at = now
      self.session = self._random(16).hex()
      self._window = ReplayWindow()
      self._next_seq = 1
      return "reply", self._seal(build_plaintext(TYPE_SESSION, self.session, 0, message.name))
    if message.kind == TYPE_UNPAIR:
      if message.name is not None or message.body:
        return "ignore", "invalid_unpair"
      if self.session is None or message.session is None or not hmac.compare_digest(message.session, self.session):
        return "ignore", "stale_session"
      if not self._window.accept(message.seq):
        return "ignore", "replayed"
      return "unpair", None
    if message.kind != TYPE_COMMAND:
      return "ignore", "unexpected_type"
    if message.name not in SEALED_COMMANDS:
      return "ignore", "unknown_command"
    if self.session is None or message.session is None or not hmac.compare_digest(message.session, self.session):
      rekey = self.rekey()
      return ("reply", rekey) if rekey else ("ignore", "stale_session")
    if self.removing:
      # The unpair follows the session; commands of this panel go plain again.
      return "ignore", "removing"
    if not self._window.accept(message.seq):
      return "ignore", "replayed"
    return "command", (message.name, message.body)

  def _next(self) -> Optional[int]:
    if self.session is None:
      return None
    if self._next_seq >= 0xFFFFFFFF:
      # Sequence space exhausted: the panel needs a new session first.
      self.session = None
      return None
    seq = self._next_seq
    self._next_seq += 1
    return seq

  def seal_data(self, kind: str, body: bytes) -> Optional[Tuple[str, str]]:
    """Seal a stream-token message for the panel; None without a session."""
    if kind not in SEALED_DATA or len(body) > MAX_BODY:
      return None
    seq = self._next()
    if seq is None:
      return None
    return self._seal(build_plaintext(TYPE_DATA, self.session, seq, kind, body))

  def seal_unpair(self) -> Optional[Tuple[str, str]]:
    """Ask the panel to turn pairing off; None without a session."""
    seq = self._next()
    if seq is None:
      return None
    return self._seal(build_plaintext(TYPE_UNPAIR, self.session, seq, None))


def _entry_code(entry: Any, key: str) -> Optional[str]:
  # Home Assistant hands out entry data and options as read-only
  # MappingProxyType, which is a Mapping but not a dict.
  for source in (getattr(entry, "options", None), getattr(entry, "data", None)):
    if isinstance(source, Mapping) and source.get(key):
      return normalize_code(source.get(key))
  return None


def entry_pairing_code(entry: Any) -> Optional[str]:
  """The stored pairing code of a config entry (options override data)."""
  return _entry_code(entry, CONF_PAIRING_KEY)


def entry_removing_code(entry: Any) -> Optional[str]:
  """A code removed in Home Assistant that the panel still has to turn off."""
  if entry_pairing_code(entry):
    return None
  return _entry_code(entry, CONF_REMOVING_KEY)


def without_pairing(values: Any) -> Dict[str, Any]:
  """Entry data or options without any pairing code."""
  return {key: value for key, value in dict(values or {}).items()
          if key not in (CONF_PAIRING_KEY, CONF_REMOVING_KEY)}


def check_pairing_code(text: Any, panel_status: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
  """Validate a code entered in the Bridge against the panel's retained status.

  Returns (canonical code, None) or (None, error key). Only a panel that shows
  a code (pending or active) with the same key id can be paired, so a typo or
  a panel with old firmware cannot silence its own commands.
  """
  code = normalize_code(text)
  if code is None:
    return None, "invalid_pairing_code"
  status = panel_status or {}
  kid = status.get("kid")
  if status.get("state") not in ("pending", "active") or not isinstance(kid, str):
    return None, "pairing_not_started"
  if not hmac.compare_digest(Keys(code).key_id, kid):
    return None, "pairing_code_mismatch"
  return code, None


def key_id_for_code(code: str) -> Optional[str]:
  try:
    return Keys(code).key_id
  except ValueError:
    return None


def status_topic(base_topic: str) -> str:
  return f"{base_topic}/{STATUS_TOPIC_LEAF}"


def command_topics(base_topic: str) -> List[str]:
  return [f"{base_topic}/cmnd/{leaf}" for leaf in sorted(SEALED_COMMANDS)]
