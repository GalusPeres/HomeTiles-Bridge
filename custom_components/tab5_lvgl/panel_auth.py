"""Login to a panel whose Web Admin is protected by the optional password.

The pairing push (POST /mqtt and /restart) runs against the panel's Web
Admin. Since firmware with the optional Web Admin password, a protected panel
answers those requests only for a logged-in session:

  GET  /api/auth/challenge -> {"enabled": true, "salt": hex, "nonce": hex, "iter": n}
  key   = PBKDF2-HMAC-SHA256(UTF-8 password, salt, n iterations, 32 bytes)
  proof = HMAC-SHA256(key, nonce)
  POST /api/auth/login {"nonce": hex, "proof": hex}
       -> {"csrf": hex, "server_proof": hex} plus an ht_session cookie
  server_proof = HMAC-SHA256(key, "HomeTiles-Web-Admin-server-v1" || nonce || proof)

Every later request carries the session cookie and the X-HomeTiles-CSRF
header. The Bridge checks the server proof before it sends MQTT credentials,
so a device that merely claims to be the panel does not receive them.

Older firmware has no /api/auth/challenge (HTTP 404) and a panel without a
password answers {"enabled": false}; both keep the unauthenticated push.

The key is deliberately slow to derive: the login travels over plain http://,
so a recorded login must not allow fast offline guessing of the password. The
panel stores only salt, iterations and key and never derives it; the Bridge
does it once per pairing, off the event loop, and accepts only iteration counts
between KDF_MIN_ITERATIONS and KDF_MAX_ITERATIONS, so a device posing as the
panel cannot stall Home Assistant.

The password, the derived key and the proofs are never logged or stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from typing import Any, Dict, Optional

_LOGGER = logging.getLogger(__name__)

SERVER_PROOF_LABEL = b"HomeTiles-Web-Admin-server-v1"
SESSION_COOKIE = "ht_session"
CSRF_HEADER = "X-HomeTiles-CSRF"

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_PASSWORD = "invalid_panel_password"
ERROR_PASSWORD_REQUIRED = "panel_password_required"
ERROR_LOCKED = "panel_locked"
ERROR_IDENTITY = "panel_identity_failed"
# The panel's firmware has no direct link (POST /api/link answers 404).
ERROR_LINK_UNSUPPORTED = "link_unsupported"

KDF_MIN_ITERATIONS = 10_000
KDF_MAX_ITERATIONS = 1_000_000

_HEX_16 = re.compile(r"[0-9a-fA-F]{32}\Z")
_HEX_32 = re.compile(r"[0-9a-fA-F]{64}\Z")
_COOKIE = re.compile(r"(?:^|[;,]\s*)" + SESSION_COOKIE + r"=([0-9a-fA-F]{32})(?=;|,|\s|$)")


def derive_key(salt: bytes, password: str, iterations: int) -> bytes:
  return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)


def _iterations(value: object) -> Optional[int]:
  if isinstance(value, bool) or not isinstance(value, int):
    return None
  return value if KDF_MIN_ITERATIONS <= value <= KDF_MAX_ITERATIONS else None


def login_proof(key: bytes, nonce: bytes) -> bytes:
  return hmac.new(key, nonce, hashlib.sha256).digest()


def server_proof(key: bytes, nonce: bytes, proof: bytes) -> bytes:
  return hmac.new(key, SERVER_PROOF_LABEL + nonce + proof, hashlib.sha256).digest()


def session_from_set_cookie(values: Any) -> Optional[str]:
  """Return the ht_session value of one or more Set-Cookie header values."""
  if isinstance(values, str):
    values = [values]
  for value in values or []:
    match = _COOKIE.search(str(value))
    if match:
      return match.group(1).lower()
  return None


class PanelLogin:
  """Headers for the following Web Admin requests, or an error code."""

  def __init__(self, headers: Optional[Dict[str, str]] = None, error: Optional[str] = None,
               protected: bool = False) -> None:
    self.headers: Dict[str, str] = dict(headers or {})
    self.error = error
    self.protected = protected


async def _json(response: Any) -> Dict[str, Any]:
  try:
    data = await response.json(content_type=None)
  except (ValueError, TypeError):
    return {}
  return data if isinstance(data, dict) else {}


async def async_login(session: Any, host: str, password: str, timeout: Any) -> PanelLogin:
  """Open a Web Admin session on the panel when it is password protected.

  Network errors propagate to the caller, which maps them to cannot_connect.
  """
  if not password:
    # Unchanged request sequence for panels without a password; a protected
    # panel answers the /mqtt push with 401 (panel_password_required).
    return PanelLogin()
  base = f"http://{host}"
  for _attempt in range(2):
    async with session.get(f"{base}/api/auth/challenge", timeout=timeout,
                           allow_redirects=False) as response:
      if response.status == 404:
        # Firmware before the optional Web Admin password.
        return PanelLogin()
      if response.status != 200:
        return PanelLogin(error=ERROR_CANNOT_CONNECT)
      challenge = await _json(response)
    if challenge.get("enabled") is not True:
      return PanelLogin()
    salt_hex = str(challenge.get("salt") or "")
    nonce_hex = str(challenge.get("nonce") or "")
    iterations = _iterations(challenge.get("iter"))
    if not _HEX_16.match(salt_hex) or not _HEX_32.match(nonce_hex) or iterations is None:
      return PanelLogin(error=ERROR_IDENTITY, protected=True)

    nonce = bytes.fromhex(nonce_hex)
    key = await asyncio.to_thread(derive_key, bytes.fromhex(salt_hex), password, iterations)
    proof = login_proof(key, nonce)
    async with session.post(f"{base}/api/auth/login", json={"nonce": nonce_hex, "proof": proof.hex()},
                            timeout=timeout, allow_redirects=False) as response:
      status = response.status
      result = await _json(response)
      cookies = response.headers.getall("Set-Cookie", []) if hasattr(response.headers, "getall") else response.headers.get("Set-Cookie")
    if status == 429:
      return PanelLogin(error=ERROR_LOCKED, protected=True)
    if status == 401 and result.get("error") == "challenge_expired":
      continue
    if status == 401:
      return PanelLogin(error=ERROR_INVALID_PASSWORD, protected=True)
    if status != 200:
      return PanelLogin(error=ERROR_CANNOT_CONNECT, protected=True)

    expected = server_proof(key, nonce, proof).hex()
    received = str(result.get("server_proof") or "").lower()
    csrf = str(result.get("csrf") or "")
    session_id = session_from_set_cookie(cookies)
    if not hmac.compare_digest(expected, received) or not _HEX_16.match(csrf) or not session_id:
      _LOGGER.warning("HomeTiles panel at %s did not prove that it knows the Web Admin password", host)
      return PanelLogin(error=ERROR_IDENTITY, protected=True)
    return PanelLogin(
      headers={"Cookie": f"{SESSION_COOKIE}={session_id}", CSRF_HEADER: csrf},
      protected=True,
    )
  return PanelLogin(error=ERROR_CANNOT_CONNECT, protected=True)


async def async_push_link(session: Any, host: str, form: Dict[str, str],
                          password: str, timeout: Any) -> Optional[str]:
  """POST /api/link (Bridge address, base topic, prefix), logging in first.

  The panel stores the address, starts pairing over the link and restarts by
  itself. Returns None on success or an error code for the config flow form;
  ERROR_LINK_UNSUPPORTED for firmware without the direct link.
  """
  login = await async_login(session, host, password, timeout)
  if login.error:
    _LOGGER.warning("HomeTiles panel login at %s failed: %s", host, login.error)
    return login.error
  async with session.post(f"http://{host}/api/link", data=form, headers=login.headers,
                          timeout=timeout, allow_redirects=False) as response:
    if response.status in (401, 403):
      return ERROR_INVALID_PASSWORD if password else ERROR_PASSWORD_REQUIRED
    if response.status == 404:
      return ERROR_LINK_UNSUPPORTED
    if response.status != 200:
      return ERROR_CANNOT_CONNECT
  return None


async def async_push_credentials(session: Any, host: str, form: Dict[str, str],
                                 password: str, timeout: Any) -> Optional[str]:
  """POST /mqtt and /restart, logging in first on a protected panel.

  Returns None on success or an error code for the config flow form.
  """
  login = await async_login(session, host, password, timeout)
  if login.error:
    _LOGGER.warning("HomeTiles panel login at %s failed: %s", host, login.error)
    return login.error
  for path, data in (("/mqtt", form), ("/restart", {})):
    async with session.post(f"http://{host}{path}", data=data, headers=login.headers,
                            timeout=timeout, allow_redirects=False) as response:
      if response.status in (401, 403):
        # A panel whose challenge endpoint was not reachable or that enabled
        # its password in the meantime.
        return ERROR_INVALID_PASSWORD if password else ERROR_PASSWORD_REQUIRED
      if response.status not in (200, 303):
        return ERROR_CANNOT_CONNECT
  return None
