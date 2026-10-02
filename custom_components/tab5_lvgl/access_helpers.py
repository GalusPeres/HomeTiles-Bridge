"""Protocol and safety rules for HomeTiles Lock and Alarm panel tiles.

Pure helpers without Home Assistant imports, so the rules are unit tested.

Rules agreed for these tiles (modelled on Home Assistant's own Google
Assistant PIN rules):
- Commands arrive only sealed from a paired panel that has a Web Admin
  password (checked by the caller).
- Home Assistant checks every code. Unlocking, opening and disarming without a
  typed code is only possible for entities the user allowed in the Bridge.
- Codes are never logged, stored or retained; this module never formats one.
- Code entry is limited per panel and entity: one at a time, at most ten
  per minute, and wrong codes lock it for a growing time.
"""

from __future__ import annotations

import hmac
import json
import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

LOCK_DOMAIN = "lock"
ALARM_DOMAIN = "alarm_control_panel"
ACCESS_DOMAINS = (LOCK_DOMAIN, ALARM_DOMAIN)

# Home Assistant LockEntityFeature.
LOCK_OPEN = 1
# Home Assistant AlarmControlPanelEntityFeature (TRIGGER = 8 is not offered).
ALARM_ARM_HOME = 1
ALARM_ARM_AWAY = 2
ALARM_ARM_NIGHT = 4
ALARM_ARM_CUSTOM_BYPASS = 16
ALARM_ARM_VACATION = 32

# action -> (service, required feature bit, opens the home)
LOCK_ACTIONS: Dict[str, Tuple[str, int, bool]] = {
  "lock": ("lock", 0, False),
  "unlock": ("unlock", 0, True),
  "open": ("open", LOCK_OPEN, True),
}
ALARM_ACTIONS: Dict[str, Tuple[str, int, bool]] = {
  "arm_home": ("alarm_arm_home", ALARM_ARM_HOME, False),
  "arm_away": ("alarm_arm_away", ALARM_ARM_AWAY, False),
  "arm_night": ("alarm_arm_night", ALARM_ARM_NIGHT, False),
  "arm_vacation": ("alarm_arm_vacation", ALARM_ARM_VACATION, False),
  "arm_custom_bypass": ("alarm_arm_custom_bypass", ALARM_ARM_CUSTOM_BYPASS, False),
  "disarm": ("alarm_disarm", 0, True),
}
ACTIONS = {LOCK_DOMAIN: LOCK_ACTIONS, ALARM_DOMAIN: ALARM_ACTIONS}

# Result statuses published on {base}/stat/lock and {base}/stat/alarm.
STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_WRONG_CODE = "wrong_code"
STATUS_CODE_REQUIRED = "code_required"
STATUS_LOCKED_OUT = "locked_out"
STATUS_BUSY = "busy"
STATUS_NOT_ALLOWED = "not_allowed"
STATUS_NOT_SECURED = "not_secured"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNAVAILABLE = "unavailable"
STATUS_EXPIRED = "expired"
STATUS_INVALID = "invalid"
STATUS_FAILED = "failed"

MAX_COMMAND_BYTES = 1024
MAX_CODE_LENGTH = 32
COMMAND_WINDOW_S = 15
_COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")
_ENTITY_ID = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
# "unknown" stays operable, as in the Home Assistant frontend.
_UNAVAILABLE = (None, "unavailable")

# Wrong codes in a row before code entry is locked, the first lock time and
# the longest one; every further wrong code doubles the time.
LOCKOUT_FREE_ATTEMPTS = 5
LOCKOUT_FIRST_S = 30.0
LOCKOUT_MAX_S = 3600.0


# Codes the Bridge itself checks per lock or alarm panel (CONF_ACCESS_CODES):
# at most this many per device, separated by commas, semicolons or spaces.
MAX_ACCESS_CODES = 10
_ACCESS_CODE_SEPARATORS = re.compile(r"[\s,;]+")


def parse_access_codes(value: Any) -> List[str]:
  """The codes of one device from the options text; ValueError for a bad one.

  Codes follow the panel's own limits (parse_access_command): printable, at
  most MAX_CODE_LENGTH characters. Repeats are dropped.
  """
  if value is None:
    return []
  if not isinstance(value, str):
    raise ValueError("codes must be text")
  codes = [code for code in _ACCESS_CODE_SEPARATORS.split(value.strip()) if code]
  if len(codes) > MAX_ACCESS_CODES:
    raise ValueError("too many codes")
  for code in codes:
    if not 1 <= len(code) <= MAX_CODE_LENGTH or not code.isprintable():
      raise ValueError("unusable code")
  return list(dict.fromkeys(codes))


def access_code_matches(code: Any, known: Sequence[str]) -> bool:
  """Whether code is one of the known codes; every one is compared in full."""
  if not isinstance(code, str) or not code:
    return False
  given = code.encode("utf-8")
  matched = False
  for entry in known:
    matched |= hmac.compare_digest(given, str(entry).encode("utf-8"))
  return matched


class AccessError(Exception):
  """A command that is answered with a status instead of a service call."""

  def __init__(self, status: str) -> None:
    super().__init__(status)
    self.status = status


class AnsweredAccessError(AccessError):
  """An AccessError that names the command it answers."""

  def __init__(self, status: str, command: Mapping[str, Any]) -> None:
    super().__init__(status)
    self.entity_id = command["entity_id"]
    self.command_id = command["id"]


def _answered(command: Mapping[str, Any], status: str) -> AnsweredAccessError:
  return AnsweredAccessError(status, command)


def _features(attributes: Mapping[str, Any]) -> int:
  value = attributes.get("supported_features")
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    return 0
  return value


def lock_code_format(value: Any) -> Optional[str]:
  """Map a lock's code_format regex to "number", "text" or None.

  Home Assistant matches the regex with re.match. A pattern that accepts a
  digit-only code of 1-12 digits can be typed on the panel keypad.
  """
  if not isinstance(value, str) or not value:
    return None
  if len(value) > 256:
    return "text"
  try:
    pattern = re.compile(value)
  except re.error:
    return "text"
  for length in range(1, 13):
    for sample in ("1" * length, ("0123456789" * 2)[:length]):
      if pattern.match(sample):
        return "number"
  return "text"


def alarm_code_format(value: Any) -> Optional[str]:
  """Home Assistant's alarm CodeFormat: "number", "text" or None."""
  if value in ("number", "text"):
    return value
  return None


def _code_needed(code_format: Optional[str], has_default_code: bool) -> bool:
  # Home Assistant fills in the entity's default code when none is sent.
  return code_format is not None and not has_default_code


def build_lock_detail(state: Optional[str], attributes: Mapping[str, Any], *,
                      has_default_code: bool, allowed_without_code: bool) -> Dict[str, Any]:
  """State for the Lock tile on {ha_prefix}/lock/<object>/detail."""
  code_format = lock_code_format(attributes.get("code_format"))
  # Home Assistant checks the code for lock, unlock and open alike.
  needed = _code_needed(code_format, has_default_code)
  return {
    "state": state,
    "supported_features": _features(attributes),
    "code_format": code_format,
    "lock_code": needed,
    "unlock_code": needed,
    "unlock_allowed": needed or allowed_without_code,
  }


def build_alarm_detail(state: Optional[str], attributes: Mapping[str, Any], *,
                       has_default_code: bool, allowed_without_code: bool) -> Dict[str, Any]:
  """State for the Alarm tile on {ha_prefix}/alarm_control_panel/<object>/detail."""
  code_format = alarm_code_format(attributes.get("code_format"))
  needed = _code_needed(code_format, has_default_code)
  # code_arm_required defaults to True in Home Assistant.
  arm_required = attributes.get("code_arm_required", True) is not False
  return {
    "state": state,
    "supported_features": _features(attributes),
    "code_format": code_format,
    "arm_code": needed and arm_required,
    "disarm_code": needed,
    "disarm_allowed": needed or allowed_without_code,
  }


def build_access_detail(domain: str, state: Optional[str], attributes: Mapping[str, Any], *,
                        has_default_code: bool, allowed_without_code: bool) -> Dict[str, Any]:
  builder = build_lock_detail if domain == LOCK_DOMAIN else build_alarm_detail
  return builder(state, attributes, has_default_code=has_default_code,
                 allowed_without_code=allowed_without_code)


def parse_access_command(payload: Any, now: float) -> Dict[str, Any]:
  """Validate a decrypted Lock/Alarm command body.

  Returns {"entity_id", "id", "action", "code", "web_auth"}. Raises AnsweredAccessError
  for a command that is answered with a status, and a plain AccessError for a
  body without a usable id or entity, which cannot be answered.
  """
  if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_COMMAND_BYTES:
    raise AccessError(STATUS_INVALID)
  try:
    command = json.loads(payload)
  except ValueError as err:
    raise AccessError(STATUS_INVALID) from err
  if not isinstance(command, dict):
    raise AccessError(STATUS_INVALID)
  command_id = command.get("id")
  entity_id = command.get("entity_id")
  if (not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id)
      or not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id)):
    raise AccessError(STATUS_INVALID)
  result = {"entity_id": entity_id, "id": command_id, "action": None, "code": None,
            "web_auth": False}
  deadline = command.get("deadline")
  if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
      or not math.isfinite(deadline) or not 0 < deadline - now <= COMMAND_WINDOW_S):
    raise _answered(result, STATUS_EXPIRED)
  action = command.get("action")
  if not isinstance(action, str):
    raise _answered(result, STATUS_INVALID)
  result["action"] = action
  # The panel states in every sealed command whether its Web Admin password
  # is set; only a sealed command's claim is fresh and authenticated.
  result["web_auth"] = command.get("web_auth") is True
  code = command.get("code")
  if code is not None:
    if (not isinstance(code, str) or not 1 <= len(code) <= MAX_CODE_LENGTH
        or not code.isprintable()):
      raise _answered(result, STATUS_INVALID)
    result["code"] = code
  return result



def plan_access_call(domain: str, command: Mapping[str, Any], state: Optional[str],
                     attributes: Mapping[str, Any], *, has_default_code: bool,
                     allowed_without_code: bool) -> Tuple[str, Dict[str, Any]]:
  """Service name and data for a validated command, or AccessError(status)."""
  actions = ACTIONS.get(domain)
  if actions is None or command.get("action") not in actions:
    # Unknown actions, including alarm "trigger", are never offered.
    raise AccessError(STATUS_NOT_ALLOWED)
  service, feature, opens = actions[command["action"]]
  if state in _UNAVAILABLE:
    raise AccessError(STATUS_UNAVAILABLE)
  if feature and not _features(attributes) & feature:
    raise AccessError(STATUS_UNSUPPORTED)
  detail = build_access_detail(domain, state, attributes, has_default_code=has_default_code,
                               allowed_without_code=allowed_without_code)
  code = command.get("code")
  if domain == LOCK_DOMAIN:
    needs_code = detail["unlock_code"] if opens else detail["lock_code"]
  else:
    needs_code = detail["disarm_code"] if opens else detail["arm_code"]
  if opens and not needs_code and not allowed_without_code:
    # Without a code Home Assistant could check (no code_format) or with only
    # the default code, opening needs the user's permission in the Bridge. A
    # code sent anyway must not bypass that for a device that ignores codes.
    if detail["code_format"] is None or not code:
      raise AccessError(STATUS_NOT_ALLOWED)
  if needs_code and not code:
    raise AccessError(STATUS_CODE_REQUIRED)
  data: Dict[str, Any] = {}
  if code:
    data["code"] = code
  return service, data


# Translation keys and texts Home Assistant and its alarm/lock integrations
# use for a rejected code. Only these count towards the lockout; a text such
# as "invalid response code" must not.
_WRONG_CODE_KEYS = frozenset({"invalid_code", "add_default_code", "invalid_alarm_code", "wrong_code"})
_WRONG_CODE_TEXT = re.compile(
  r"\b(invalid|incorrect|wrong|bad)\s+((alarm|lock|user|access|pin)\s+)?(code|pin)\b"
  r"|\b(code|pin)\s+(is\s+)?(invalid|incorrect|wrong)\b"
  r"|\b(doesn't|does not) match pattern\b",
  re.IGNORECASE,
)


def classify_service_error(error: BaseException) -> str:
  """Status for an exception raised by the lock/alarm service call."""
  key = getattr(error, "translation_key", None)
  if key == "code_arm_required":
    return STATUS_CODE_REQUIRED
  if key in _WRONG_CODE_KEYS or _WRONG_CODE_TEXT.search(str(error) or ""):
    return STATUS_WRONG_CODE
  return STATUS_FAILED


def default_code_usable(domain: str, code_format: Any, default_code: Any) -> bool:
  """Whether Home Assistant would accept the entity's default code.

  A lock checks the default code against its code_format regex like a typed
  one, so a default that does not match is treated as absent: the panel then
  asks for the code instead of sending a command HA rejects.
  """
  if not isinstance(default_code, str) or not default_code:
    return False
  if domain == LOCK_DOMAIN and isinstance(code_format, str) and code_format:
    try:
      return re.compile(code_format).match(default_code) is not None
    except re.error:
      return False
  return True


class CodeGuard:
  """Limits code entry per key (one panel and entity).

  - One command carrying a code runs at a time; others get "busy" until it
    finished (or after INFLIGHT_MAX_S if Home Assistant never answers).
  - At most RATE_MAX_ATTEMPTS commands carrying a code per RATE_WINDOW_S,
    also when Home Assistant cannot tell a wrong code from a right one.
  - After LOCKOUT_FREE_ATTEMPTS wrong codes in a row the key is locked for
    LOCKOUT_FIRST_S; every further wrong code doubles the time up to
    LOCKOUT_MAX_S. Only a successful command that carried a code resets it.
  """

  INFLIGHT_MAX_S = 60.0
  # Above the five wrong codes of the lockout, so it only slows guessing
  # where Home Assistant cannot report a wrong code.
  RATE_MAX_ATTEMPTS = 10
  RATE_WINDOW_S = 60.0

  def __init__(self, clock: Callable[[], float], *, max_keys: int = 256) -> None:
    self._clock = clock
    self._max_keys = max_keys
    self._failures: Dict[Any, int] = {}
    self._locked_until: Dict[Any, float] = {}
    self._in_flight: Dict[Any, float] = {}
    self._attempts: Dict[Any, list] = {}

  def retry_after(self, key: Any) -> int:
    """Seconds until codes are accepted again for key; 0 when not blocked."""
    now = self._clock()
    remaining = (self._locked_until.get(key) or now) - now
    attempts = [moment for moment in self._attempts.get(key, []) if moment > now - self.RATE_WINDOW_S]
    if len(attempts) >= self.RATE_MAX_ATTEMPTS:
      remaining = max(remaining, attempts[0] + self.RATE_WINDOW_S - now)
    if remaining <= 0:
      return 0
    return max(1, math.ceil(remaining))

  def admit(self, key: Any) -> Tuple[Optional[str], int]:
    """Start a command carrying a code: (None, 0) or the status to answer."""
    now = self._clock()
    blocked = self.retry_after(key)
    if blocked:
      return STATUS_LOCKED_OUT, blocked
    started = self._in_flight.get(key)
    if started is not None and now - started < self.INFLIGHT_MAX_S:
      return STATUS_BUSY, 0
    self._forget_one_if_full(key)
    self._in_flight[key] = now
    attempts = [moment for moment in self._attempts.get(key, []) if moment > now - self.RATE_WINDOW_S]
    attempts.append(now)
    self._attempts[key] = attempts
    return None, 0

  def finish(self, key: Any, status: str) -> int:
    """End an admitted command; returns a newly started lock time or 0."""
    self._in_flight.pop(key, None)
    if status == STATUS_OK:
      self._failures.pop(key, None)
      self._locked_until.pop(key, None)
      return 0
    if status != STATUS_WRONG_CODE:
      return 0
    failures = self._failures.get(key, 0) + 1
    self._failures[key] = failures
    if failures < LOCKOUT_FREE_ATTEMPTS:
      return 0
    seconds = min(LOCKOUT_MAX_S, LOCKOUT_FIRST_S * 2 ** (failures - LOCKOUT_FREE_ATTEMPTS))
    self._locked_until[key] = self._clock() + seconds
    return int(seconds)

  def _forget_one_if_full(self, key: Any) -> None:
    keys = set(self._failures) | set(self._attempts) | set(self._in_flight)
    if key in keys or len(keys) < self._max_keys:
      return
    # Bounded state: forget a key that is neither blocked nor running.
    for old in keys:
      if not self.retry_after(old) and old not in self._in_flight:
        for table in (self._failures, self._locked_until, self._attempts):
          table.pop(old, None)
        return
