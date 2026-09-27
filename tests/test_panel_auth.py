"""Pairing push to a panel with the optional Web Admin password."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import logging
import unittest

from test_view_navigation import ROOT, load_module

AUTH = load_module("panel_auth")

# Shared with the firmware test tools/tests/web/test-web-admin-auth-browser.mjs:
# both implementations must derive exactly these values.
SALT = "a1" * 16
NONCE = "5c" * 32
PASSWORD = "Pässwort-123"
KEY = "1a1168e2a2b908f40f849a0828d7dc2120d5e3355a33cb614f66600d2a7ca954"
PROOF = "a4b17028d639885824c1cd3e586a1203edb2648daafad81c14c4f203e92f32f9"
SERVER_PROOF = "1ead99615ad5e76301fc20b5f87d4410bccd52b7df60f7464ea1b38160e8abe5"
SESSION = "0123456789abcdef0123456789abcdef"
CSRF = "fedcba9876543210fedcba9876543210"


class FakeHeaders(dict):
  def getall(self, key, default=None):
    value = self.get(key)
    if value is None:
      return default if default is not None else []
    return value if isinstance(value, list) else [value]


class FakeResponse:
  def __init__(self, status, body=None, headers=None):
    self.status = status
    self._body = body
    self.headers = FakeHeaders(headers or {})

  async def json(self, content_type=None):
    if self._body is None:
      raise ValueError("no json")
    return self._body

  async def __aenter__(self):
    return self

  async def __aexit__(self, *args):
    return False


class FakePanel:
  """Minimal HTTP model of the firmware's auth endpoints and push routes."""

  def __init__(self, *, firmware="protected", login_status=200, server_proof=SERVER_PROOF):
    self.firmware = firmware
    self.login_status = login_status
    self.server_proof = server_proof
    self.requests = []

  def get(self, url, **kwargs):
    self.requests.append(("GET", url, kwargs))
    if url.endswith("/api/auth/challenge"):
      if self.firmware == "legacy":
        return FakeResponse(404)
      if self.firmware == "open":
        return FakeResponse(200, {"enabled": False})
      return FakeResponse(200, {"enabled": True, "salt": SALT, "nonce": NONCE})
    return FakeResponse(404)

  def post(self, url, **kwargs):
    self.requests.append(("POST", url, kwargs))
    if url.endswith("/api/auth/login"):
      if self.login_status == 200 and kwargs["json"]["proof"] != PROOF:
        return FakeResponse(401, {"error": "invalid_password"})
      if self.login_status != 200:
        return FakeResponse(self.login_status, {"error": "invalid_password" if self.login_status == 401 else "too_many_attempts"})
      return FakeResponse(200, {"csrf": CSRF, "server_proof": self.server_proof},
                          {"Set-Cookie": [f"ht_session={SESSION}; Path=/; HttpOnly; SameSite=Strict"]})
    if self.firmware == "protected":
      headers = kwargs.get("headers") or {}
      if headers.get("Cookie") != f"ht_session={SESSION}" or headers.get("X-HomeTiles-CSRF") != CSRF:
        return FakeResponse(401, {"error": "auth_required"}, {"X-HomeTiles-Auth": "required"})
    return FakeResponse(303)


FORM = {"mqtt_host": "192.168.1.2", "mqtt_port": "1883", "mqtt_user": "u",
        "mqtt_pass": "broker-secret", "mqtt_base": "hometiles", "ha_prefix": "ha/statestream"}


class PanelAuthTest(unittest.IsolatedAsyncioTestCase):
  def test_derivation_matches_the_firmware_vector(self):
    key = AUTH.derive_key(bytes.fromhex(SALT), PASSWORD)
    self.assertEqual(key.hex(), KEY)
    proof = AUTH.login_proof(key, bytes.fromhex(NONCE))
    self.assertEqual(proof.hex(), PROOF)
    self.assertEqual(AUTH.server_proof(key, bytes.fromhex(NONCE), proof).hex(), SERVER_PROOF)
    self.assertEqual(KEY, hashlib.sha256(bytes.fromhex(SALT) + PASSWORD.encode()).hexdigest())
    self.assertEqual(PROOF, hmac.new(bytes.fromhex(KEY), bytes.fromhex(NONCE), hashlib.sha256).hexdigest())

  def test_session_cookie_parsing(self):
    self.assertEqual(AUTH.session_from_set_cookie(f"ht_session={SESSION}; Path=/; HttpOnly"), SESSION)
    self.assertEqual(AUTH.session_from_set_cookie(["other=1", f"ht_session={SESSION.upper()}; Path=/"]), SESSION)
    self.assertIsNone(AUTH.session_from_set_cookie("ht_session=abc; Path=/"))
    self.assertIsNone(AUTH.session_from_set_cookie("xht_session=" + SESSION))
    self.assertIsNone(AUTH.session_from_set_cookie(None))

  async def push(self, panel, password):
    return await AUTH.async_push_credentials(panel, "192.168.1.50", FORM, password, timeout=5)

  async def test_legacy_firmware_without_password_is_unchanged(self):
    panel = FakePanel(firmware="legacy")
    self.assertIsNone(await self.push(panel, ""))
    self.assertEqual([request[:2] for request in panel.requests],
                     [("POST", "http://192.168.1.50/mqtt"), ("POST", "http://192.168.1.50/restart")])
    self.assertEqual(panel.requests[0][2]["headers"], {})

  async def test_password_is_ignored_by_legacy_and_open_panels(self):
    for firmware in ("legacy", "open"):
      panel = FakePanel(firmware=firmware)
      self.assertIsNone(await self.push(panel, PASSWORD))
      self.assertEqual([request[1].rsplit("/", 1)[1] for request in panel.requests],
                       ["challenge", "mqtt", "restart"])

  async def test_protected_panel_login_then_push_with_session(self):
    panel = FakePanel()
    self.assertIsNone(await self.push(panel, PASSWORD))
    paths = [request[1].split("192.168.1.50", 1)[1] for request in panel.requests]
    self.assertEqual(paths, ["/api/auth/challenge", "/api/auth/login", "/mqtt", "/restart"])
    self.assertEqual(panel.requests[1][2]["json"], {"nonce": NONCE, "proof": PROOF})
    for method, url, kwargs in panel.requests[2:]:
      self.assertEqual(kwargs["headers"], {"Cookie": f"ht_session={SESSION}", "X-HomeTiles-CSRF": CSRF})
    # The password itself never leaves the Bridge.
    self.assertNotIn(PASSWORD, json.dumps([str(request) for request in panel.requests]))

  async def test_protected_panel_errors(self):
    self.assertEqual(await self.push(FakePanel(), ""), "panel_password_required")
    self.assertEqual(await self.push(FakePanel(), "wrong password"), "invalid_panel_password")
    self.assertEqual(await self.push(FakePanel(login_status=429), PASSWORD), "panel_locked")
    fake = FakePanel(server_proof="00" * 32)
    self.assertEqual(await self.push(fake, PASSWORD), "panel_identity_failed")
    # A device that cannot prove the password never receives the broker login.
    self.assertFalse(any(url.endswith("/mqtt") for _, url, _ in fake.requests))

  async def test_password_and_secrets_are_never_logged(self):
    with self.assertLogs(AUTH._LOGGER, level=logging.DEBUG) as logs:
      AUTH._LOGGER.debug("marker")
      await self.push(FakePanel(), "wrong password")
      await self.push(FakePanel(server_proof="00" * 32), PASSWORD)
    output = "\n".join(logs.output)
    for secret in (PASSWORD, "wrong password", KEY, PROOF, "broker-secret"):
      self.assertNotIn(secret, output)


class ConfigFlowContractTest(unittest.TestCase):
  def test_password_field_is_optional_and_never_stored(self):
    source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    step = next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_zeroconf_confirm")
    step_source = ast.get_source_segment(source, step)
    self.assertIn('vol.Optional(CONF_PROVISION_PANEL_PASSWORD, default="")', step_source)
    self.assertIn("str(user_input.get(CONF_PROVISION_PANEL_PASSWORD) or \"\")", step_source)
    # The entry data is built from topics and discovery fields only.
    data_lines = [line for line in step_source.splitlines() if "data[" in line or "data: Dict" in line]
    self.assertTrue(data_lines)
    self.assertFalse(any("PASSWORD" in line for line in data_lines))
    self.assertNotIn("_LOGGER", ast.get_source_segment(source, next(
      node for node in ast.walk(tree)
      if isinstance(node, ast.AsyncFunctionDef) and node.name == "_push_credentials_to_device")))

  def test_translations_cover_the_new_field_and_errors(self):
    for relative in ("strings.json", "translations/en.json", "translations/de.json"):
      data = json.loads((ROOT / relative).read_text(encoding="utf-8"))
      step = data["config"]["step"]["zeroconf_confirm"]
      self.assertTrue(step["data"]["panel_password"].strip())
      self.assertTrue(step["data_description"]["panel_password"].strip())
      for error in ("invalid_panel_password", "panel_password_required", "panel_locked",
                    "panel_identity_failed"):
        self.assertTrue(data["config"]["error"][error].strip(), (relative, error))


if __name__ == "__main__":
  unittest.main()
