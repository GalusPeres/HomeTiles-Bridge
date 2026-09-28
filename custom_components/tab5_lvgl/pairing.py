"""Pairing a panel with the Bridge by comparing a six-digit number.

Protocol reference: docs-dev/command-encryption.md in the HomeTiles firmware
repository (pairing v2). The user taps "Pair" on the panel; the panel and Home
Assistant show the same six-digit number and the user confirms it on both.
Plain JSON, QoS 0, never retained, every byte field as lowercase hex:

  {base}/pair/panel   {"v":2,"t":"start","id":<8 B>,"pk":<pk_p>}
  {base}/pair/bridge  {"v":2,"t":"commit","id":...,"pk":<pk_b>,"c":<32 B>}
  {base}/pair/panel   {"v":2,"t":"nonce","id":...,"n":<n_p 16 B>}
  {base}/pair/bridge  {"v":2,"t":"nonce","id":...,"n":<n_b 16 B>}
  both                {"v":2,"t":"confirm","id":...,"m":<32 B>} after the local confirmation
  both                {"v":2,"t":"abort","id":...,"r":<reason>} (unauthenticated)

  c = SHA-256("HomeTiles pairing commit v2" || pk_b || pk_p || n_b)
  s = X25519(own sk, peer pk); an all-zero result is rejected
  T = SHA-256("HomeTiles pairing v2" || u16be(len(base)) || base || pk_p || pk_b || n_p || n_b)
  number = uint32_be(SHA-256("HomeTiles pairing number v2" || T)[0:4]) mod 1000000
  K = HKDF-SHA256(salt=T, ikm=s, info="pairing key", 32 bytes)
  m = HMAC-SHA256(K, "confirm panel" || T), or "confirm bridge" from the Bridge

The Bridge commits to n_b before it sees n_p, so a man in the middle has to
hit the number the panel shows (1 in 10^6 per attempt). The Bridge answers at
most three starts per panel in ten minutes, so the number cannot be ground in
the background while the user looks at it. K becomes the pairing key of
command_channel.py. Nothing here performs I/O; the key is never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import struct
from typing import Any, Callable, List, Optional, Tuple

VERSION = 2
PANEL_TOPIC_LEAF = "pair/panel"
BRIDGE_TOPIC_LEAF = "pair/bridge"
ATTEMPT_TIMEOUT_S = 120.0
CONFIRM_RESEND_S = 2.0
START_MIN_INTERVAL_S = 10.0
START_BUDGET = 3
START_BUDGET_WINDOW_S = 600.0
# Refused starts are answered at most this often; they cost the Bridge nothing else.
REFUSAL_MIN_INTERVAL_S = 1.0
MAX_MESSAGE = 512

# Reasons in an abort ("r"). The panel shows a matching text; an abort
# without a reason (a broken key exchange) reads "Pairing failed".
REASON_BUSY = "busy"
REASON_RATE = "rate"
REASON_PAIRED = "paired"
REASON_REJECTED = "rejected"
REASON_TIMEOUT = "timeout"
REASON_CANCEL = "cancel"
REASON_FAILED = "failed"  # Only reported to Home Assistant, never sent.

# Outcome of the user's answer in Home Assistant.
ANSWER_PAIRED = "paired"
ANSWER_WAITING = "waiting"
ANSWER_REJECTED = "rejected"
ANSWER_EXPIRED = "expired"

# Events for the caller: ("send", payload) publishes on the Bridge topic,
# ("prompt", (attempt id, number)) asks the user, ("closed", reason) ends the
# attempt without a key, ("refused", reason) reports a start that got an abort,
# and ("paired", key) hands over the 32-byte pairing key.
Event = Tuple[str, Any]

_ID = re.compile(r"[0-9a-f]{16}\Z")
_HEX = re.compile(r"[0-9a-f]*\Z")
# Panel message type -> (field, size in bytes); an abort carries no bytes.
_FIELDS = {"start": ("pk", 32), "nonce": ("n", 16), "confirm": ("m", 32), "abort": None}


def parse_message(payload: Any) -> Optional[Tuple[str, str, Optional[bytes]]]:
  """(type, id, bytes) of a panel message, or None for anything malformed."""
  if isinstance(payload, (bytes, bytearray)):
    try:
      payload = bytes(payload).decode("ascii")
    except UnicodeDecodeError:
      return None
  if not isinstance(payload, str) or len(payload) > MAX_MESSAGE:
    return None
  try:
    data = json.loads(payload)
  except ValueError:
    return None
  if not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != VERSION:
    return None
  kind, attempt_id = data.get("t"), data.get("id")
  if kind not in _FIELDS or not isinstance(attempt_id, str) or not _ID.match(attempt_id):
    return None
  field = _FIELDS[kind]
  if field is None:
    return kind, attempt_id, None
  name, size = field
  value = data.get(name)
  if not isinstance(value, str) or len(value) != 2 * size or not _HEX.match(value):
    return None
  return kind, attempt_id, bytes.fromhex(value)


def build_message(kind: str, attempt_id: str, **fields: Any) -> str:
  message = {"v": VERSION, "t": kind, "id": attempt_id}
  for name, value in fields.items():
    message[name] = value.hex() if isinstance(value, bytes) else value
  return json.dumps(message, separators=(",", ":"))


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


def commitment(pk_b: bytes, pk_p: bytes, n_b: bytes) -> bytes:
  return hashlib.sha256(b"HomeTiles pairing commit v2" + pk_b + pk_p + n_b).digest()


def transcript(base_topic: str, pk_p: bytes, pk_b: bytes, n_p: bytes, n_b: bytes) -> bytes:
  base = base_topic.encode("utf-8")
  return hashlib.sha256(
    b"HomeTiles pairing v2" + struct.pack(">H", len(base)) + base + pk_p + pk_b + n_p + n_b
  ).digest()


def pairing_number(transcript_hash: bytes) -> int:
  digest = hashlib.sha256(b"HomeTiles pairing number v2" + transcript_hash).digest()
  return struct.unpack(">I", digest[:4])[0] % 1_000_000


def format_number(number: int) -> str:
  """Always six digits, leading zeros kept: 61806 -> "061 806"."""
  digits = f"{number:06d}"
  return f"{digits[:3]} {digits[3:]}"


def pairing_key(shared: bytes, transcript_hash: bytes) -> bytes:
  return _hkdf(transcript_hash, shared, b"pairing key", 32)


def confirm_mac(key: bytes, transcript_hash: bytes, role: str) -> bytes:
  """role "panel" or "bridge"."""
  return hmac.new(key, f"confirm {role}".encode("ascii") + transcript_hash, hashlib.sha256).digest()


def x25519_keypair(secret: bytes) -> Tuple[Any, bytes]:
  """Private key from 32 raw bytes (clamped as in RFC 7748) and its public key."""
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
  private = X25519PrivateKey.from_private_bytes(secret)
  public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
  return private, public


def x25519_shared(private: Any, peer: bytes) -> Optional[bytes]:
  """The shared secret, or None for a low-order peer key (all-zero result)."""
  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
  try:
    shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
  except ValueError:
    return None
  return None if shared == bytes(32) else shared


class _Attempt:
  def __init__(self, attempt_id: str, started_at: float, private: Any,
               pk_p: bytes, pk_b: bytes, n_b: bytes) -> None:
    self.id = attempt_id
    self.started_at = started_at
    self.private = private
    self.pk_p = pk_p
    self.pk_b = pk_b
    self.n_b = n_b
    self.n_p: Optional[bytes] = None
    self.transcript: Optional[bytes] = None
    self.number: Optional[int] = None
    self.key: Optional[bytes] = None
    self.user_confirmed = False
    self.panel_confirmed = False
    self.next_resend = 0.0

  def __repr__(self) -> str:  # Never expose key material in logs or tracebacks.
    return f"_Attempt(id={self.id})"


class _Finished:
  """A completed attempt: a repeated panel confirm still gets the Bridge's."""

  def __init__(self, attempt_id: str, reply: str, expected: bytes, until: float) -> None:
    self.id = attempt_id
    self.reply = reply
    self.expected = expected
    self.until = until


class PanelPairing:
  """Bridge end of the pairing for one panel (one config entry).

  handle(), answer() and tick() return the events the caller carries out.
  """

  def __init__(self, base_topic: str, *, clock: Callable[[], float],
               random: Callable[[int], bytes] = os.urandom) -> None:
    self.base_topic = base_topic
    self.panel_topic = f"{base_topic}/{PANEL_TOPIC_LEAF}"
    self.bridge_topic = f"{base_topic}/{BRIDGE_TOPIC_LEAF}"
    self._clock = clock
    self._random = random
    self._attempt: Optional[_Attempt] = None
    self._finished: Optional[_Finished] = None
    self._starts: List[float] = []
    self._last_refusal_at: Optional[float] = None

  @property
  def active(self) -> bool:
    """An attempt runs, or a finished one still answers repeated confirms."""
    return self._attempt is not None or self._finished is not None

  def number(self, attempt_id: Optional[str]) -> Optional[str]:
    """The number to show for this attempt, while the user can still answer."""
    attempt = self._attempt
    if attempt is None or attempt.id != attempt_id or attempt.number is None:
      return None
    return format_number(attempt.number)

  def handle(self, payload: Any, *, paired: bool) -> List[Event]:
    """A message from {base}/pair/panel. ``paired`` covers a removal in progress."""
    message = parse_message(payload)
    if message is None:
      return []
    kind, attempt_id, value = message
    events = self.tick()
    if kind == "start":
      return events + self._on_start(attempt_id, value, paired)
    if kind == "nonce":
      return events + self._on_nonce(attempt_id, value)
    if kind == "confirm":
      return events + self._on_confirm(attempt_id, value)
    return events + self._on_abort(attempt_id)

  def answer(self, attempt_id: Optional[str], accept: bool) -> Tuple[str, List[Event]]:
    """The user's answer in Home Assistant; attempt_id None means the current one."""
    events = self.tick()
    finished = self._finished
    if accept and finished is not None and finished.id == attempt_id:
      return ANSWER_PAIRED, events
    attempt = self._attempt
    if attempt is None or attempt.key is None or (attempt_id is not None and attempt.id != attempt_id):
      return ANSWER_EXPIRED, events
    if not accept:
      events.append(("send", build_message("abort", attempt.id, r=REASON_REJECTED)))
      return ANSWER_REJECTED, events + self._end(REASON_REJECTED)
    if not attempt.user_confirmed:
      attempt.user_confirmed = True
      attempt.next_resend = self._clock() + CONFIRM_RESEND_S
      events.append(("send", self._confirm_message(attempt)))
    if attempt.panel_confirmed:
      return ANSWER_PAIRED, events + self._finish()
    return ANSWER_WAITING, events

  def tick(self) -> List[Event]:
    """Time out attempts and repeat the Bridge's confirm; called every second while active."""
    now = self._clock()
    finished = self._finished
    if finished is not None and now >= finished.until:
      self._finished = None
    attempt = self._attempt
    if attempt is None:
      return []
    if now - attempt.started_at >= ATTEMPT_TIMEOUT_S:
      return [("send", build_message("abort", attempt.id, r=REASON_TIMEOUT))] + self._end(REASON_TIMEOUT)
    if attempt.user_confirmed and now >= attempt.next_resend:
      # The panel's confirm is still missing: QoS 0 may have lost ours.
      attempt.next_resend = now + CONFIRM_RESEND_S
      return [("send", self._confirm_message(attempt))]
    return []

  def _on_start(self, attempt_id: str, pk_p: bytes, paired: bool) -> List[Event]:
    now = self._clock()
    attempt = self._attempt
    if (attempt is not None and attempt.id == attempt_id) or (
        self._finished is not None and self._finished.id == attempt_id):
      return []  # A repeated start of the same attempt.
    if paired or self._finished is not None:
      # A new pairing never replaces the current one: one careless click in
      # Home Assistant must not swap the key for someone else's. A pairing
      # that just finished counts until the entry reloaded with its key.
      return self._refuse(attempt_id, REASON_PAIRED, now)
    if attempt is not None:
      return self._refuse(attempt_id, REASON_BUSY, now)
    self._starts = [started for started in self._starts if now - started < START_BUDGET_WINDOW_S]
    if len(self._starts) >= START_BUDGET or (self._starts and now - self._starts[-1] < START_MIN_INTERVAL_S):
      return self._refuse(attempt_id, REASON_RATE, now)
    private, pk_b = x25519_keypair(self._random(32))
    n_b = self._random(16)
    self._starts.append(now)
    self._finished = None
    self._attempt = _Attempt(attempt_id, now, private, pk_p, pk_b, n_b)
    return [("send", build_message("commit", attempt_id, pk=pk_b, c=commitment(pk_b, pk_p, n_b)))]

  def _on_nonce(self, attempt_id: str, n_p: bytes) -> List[Event]:
    attempt = self._attempt
    if attempt is None or attempt.id != attempt_id or attempt.n_p is not None:
      # Only the first n_p counts: once n_b is out, another one would let a
      # man in the middle pick the number.
      return []
    shared = x25519_shared(attempt.private, attempt.pk_p)
    attempt.private = None
    if shared is None:
      return [("send", build_message("abort", attempt_id))] + self._end(REASON_FAILED)
    attempt.n_p = n_p
    attempt.transcript = transcript(self.base_topic, attempt.pk_p, attempt.pk_b, n_p, attempt.n_b)
    attempt.number = pairing_number(attempt.transcript)
    attempt.key = pairing_key(shared, attempt.transcript)
    return [
      ("send", build_message("nonce", attempt_id, n=attempt.n_b)),
      ("prompt", (attempt_id, format_number(attempt.number))),
    ]

  def _on_confirm(self, attempt_id: str, mac: bytes) -> List[Event]:
    finished = self._finished
    if finished is not None and finished.id == attempt_id:
      # The panel missed the Bridge's confirm and repeats its own.
      return [("send", finished.reply)] if hmac.compare_digest(mac, finished.expected) else []
    attempt = self._attempt
    if attempt is None or attempt.id != attempt_id or attempt.key is None:
      return []
    if not hmac.compare_digest(mac, confirm_mac(attempt.key, attempt.transcript, "panel")):
      return []
    attempt.panel_confirmed = True
    return self._finish() if attempt.user_confirmed else []

  def _on_abort(self, attempt_id: str) -> List[Event]:
    attempt = self._attempt
    if attempt is None or attempt.id != attempt_id:
      return []  # Also after the attempt finished: an abort rolls nothing back.
    return self._end(REASON_CANCEL)

  def _confirm_message(self, attempt: _Attempt) -> str:
    return build_message("confirm", attempt.id, m=confirm_mac(attempt.key, attempt.transcript, "bridge"))

  def _refuse(self, attempt_id: str, reason: str, now: float) -> List[Event]:
    if self._last_refusal_at is not None and now - self._last_refusal_at < REFUSAL_MIN_INTERVAL_S:
      return []
    self._last_refusal_at = now
    return [("send", build_message("abort", attempt_id, r=reason)), ("refused", reason)]

  def _end(self, reason: str) -> List[Event]:
    self._attempt = None
    return [("closed", reason)]

  def _finish(self) -> List[Event]:
    attempt = self._attempt
    self._attempt = None
    self._finished = _Finished(
      attempt.id,
      self._confirm_message(attempt),
      confirm_mac(attempt.key, attempt.transcript, "panel"),
      attempt.started_at + ATTEMPT_TIMEOUT_S,
    )
    return [("paired", attempt.key)]
