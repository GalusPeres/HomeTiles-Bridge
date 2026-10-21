"""Bridge #16 / HomeTiles #114: add a panel by its IP address where mDNS does not reach HA."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
import unittest

from test_panel_auth import FakeResponse
from test_view_navigation import ROOT, load_module

AUTH = load_module("panel_auth")
SOURCE = (ROOT / "config_flow.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLOW = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "Tab5ConfigFlow")

# What the firmware's GET /api/link answers (handleLinkInfo, test-link-info.mjs).
PANEL_INFO = {"device_id": "A1B2C3D4E5F6", "name": "Guition JC8012P4A1 V2", "model": "guition_jc8012p4a1_v2",
              "firmware": "v0.6.12b513", "base_topic": "tab5_e5f6", "ha_prefix": "homeassistant",
              "link": 1, "pair": True}


class InfoPanel:
  """The panel's GET /api/link."""

  def __init__(self, status=200, body="panel"):
    # "panel": the firmware's answer; None: no JSON (a Web Admin page).
    self.status = status
    self.body = dict(PANEL_INFO) if body == "panel" else body
    self.requests = []

  def get(self, url, **kwargs):
    self.requests.append((url, kwargs))
    return FakeResponse(self.status, self.body)

  def post(self, url, **kwargs):  # pragma: no cover - reading changes nothing
    raise AssertionError("GET /api/link only reads")


class LinkInfoTest(unittest.IsolatedAsyncioTestCase):
  async def info(self, panel):
    return await AUTH.async_link_info(panel, "192.168.1.50", timeout=5)

  async def test_panel_with_pair_pressed(self):
    panel = InfoPanel()
    info, error = await self.info(panel)
    self.assertIsNone(error)
    self.assertEqual(info, {key: PANEL_INFO[key] for key in
                            ("device_id", "name", "model", "firmware", "base_topic", "ha_prefix")})
    url, kwargs = panel.requests[0]
    self.assertEqual(url, "http://192.168.1.50/api/link")
    self.assertNotIn("headers", kwargs)  # no login, no password
    self.assertIs(kwargs["allow_redirects"], False)

  async def test_pair_not_pressed(self):
    self.assertEqual(await self.info(InfoPanel(body=dict(PANEL_INFO, pair=False))), (None, AUTH.ERROR_LINK_PRESS_PAIR))

  async def test_older_firmware_or_another_device(self):
    # Older firmware: only POST /api/link (404/405), its Web Admin page, or a
    # login redirect; a device without a HomeTiles id.
    for panel in (InfoPanel(404), InfoPanel(405), InfoPanel(302), InfoPanel(401), InfoPanel(200, None),
                  InfoPanel(body=dict(PANEL_INFO, link=0)), InfoPanel(body=dict(PANEL_INFO, device_id="tab5")),
                  InfoPanel(body=dict(PANEL_INFO, device_id=123456789012)), InfoPanel(body=["A1B2C3D4E5F6"])):
      self.assertEqual(await self.info(panel), (None, AUTH.ERROR_LINK_UNSUPPORTED), panel.status)
    self.assertEqual(await self.info(InfoPanel(503)), (None, AUTH.ERROR_CANNOT_CONNECT))

  async def test_texts_are_bounded(self):
    info, _ = await self.info(InfoPanel(body=dict(PANEL_INFO, name=" " + "x" * 300, model=None)))
    self.assertEqual(info["name"], "x" * 64)
    self.assertEqual(info["model"], "")


def flow_class():
  """The steps of "Add device" on a small flow, without Home Assistant."""
  names = {"async_step_user", "async_step_link_ip", "async_step_manual"}
  functions = [node for node in FLOW.body if getattr(node, "name", None) in names]
  helpers = [node for node in TREE.body if getattr(node, "name", None) in ("_panel_host_input",)]
  constants = [node for node in TREE.body
               if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None)
               in ("_LINK_CARD_STEPS", "_PANEL_HOSTNAME", "CONF_PANEL_HOST")]
  cls = ast.ClassDef(name="Flow", bases=[ast.Name("FlowBase", ast.Load())], keywords=[],
                     body=functions, decorator_list=[], type_params=[])
  module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *constants, *helpers, cls], type_ignores=[])

  package = types.ModuleType("fake_tab5_lvgl")
  package.ensured = []

  async def async_ensure_link_runtime(hass):
    package.ensured.append(hass)

  package.async_ensure_link_runtime = async_ensure_link_runtime
  sys.modules["fake_tab5_lvgl"] = package

  class FlowBase:
    _discovered_host = None
    _discovered_device_id = None
    _discovered_name = None
    _discovered_model = None
    _discovered_base_topic = None
    _discovered_ha_prefix = None
    _link_entry_id = None

    def __init__(self, info=None, error=None, existing=None):
      self.hass = types.SimpleNamespace()
      self.context = {}
      self.calls = []
      self.info = info
      self.error = error
      self.existing = existing

    def async_show_menu(self, **kwargs):
      return ("menu", kwargs)

    def async_show_form(self, **kwargs):
      return ("form", kwargs)

    def async_create_entry(self, **kwargs):
      return ("entry", kwargs)

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
      self.calls.append(("unique_id", unique_id, raise_on_progress))

    def _async_dismiss_cards(self, device_id, steps, reason):
      self.calls.append(("dismiss", device_id, steps))

    def _link_existing_entry(self, device_id):
      return self.existing

    def _validate_topic_input(self, user_input):
      return {"base_topic": user_input["base_topic"], "ha_prefix": user_input["ha_prefix"]}, {}

    async def async_step_link_confirm(self):
      return ("link_confirm", None)

    async def async_step_link_switch(self):
      return ("link_switch", None)

  async def fetch(hass, host):
    flow = fetch.flow
    flow.calls.append(("get", host))
    return flow.info, flow.error

  scope = {"FlowBase": FlowBase, "__package__": "fake_tab5_lvgl", "__name__": "fake_tab5_lvgl.config_flow",
           "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "re": __import__("re"),
           "ipaddress": __import__("ipaddress"), "urlparse": __import__("urllib.parse").parse.urlparse,
           "vol": types.SimpleNamespace(Schema=lambda schema: schema, Required=lambda key, default=None: (key, default),
                                        Optional=lambda key, default=None: (key, default)),
           "_async_panel_link_info": fetch, "_entry_title": lambda data: data.get("device_name") or "HomeTiles Panel",
           "_LOGGER": types.SimpleNamespace(info=lambda *args: None),
           "CONF_BASE_TOPIC": "base_topic", "CONF_HA_PREFIX": "ha_prefix", "CONF_DEVICE_NAME": "device_name",
           "CONF_MANUFACTURER": "manufacturer", "CONF_MODEL": "model", "DEFAULT_BASE": "hometiles",
           "DEFAULT_PREFIX": "homeassistant"}
  exec(compile(ast.fix_missing_locations(module), "config_flow.py", "exec"), scope)
  return scope, fetch, package


class AddDeviceTest(unittest.TestCase):
  def setUp(self):
    self.scope, self.fetch, self.package = flow_class()
    self.Flow = self.scope["Flow"]

  def run_step(self, flow, step, user_input=None):
    self.fetch.flow = flow
    return asyncio.run(getattr(flow, step)(user_input))

  def test_add_device_offers_ip_and_mqtt(self):
    kind, menu = self.run_step(self.Flow(), "async_step_user")
    self.assertEqual((kind, menu), ("menu", {"step_id": "user", "menu_options": ["link_ip", "manual"]}))

  def test_ip_form_and_its_checks(self):
    kind, form = self.run_step(self.Flow(), "async_step_link_ip")
    self.assertEqual((kind, form["step_id"], form["errors"]), ("form", "link_ip", {}))
    flow = self.Flow()
    kind, form = self.run_step(flow, "async_step_link_ip", {"host": "192.168.1"})
    self.assertEqual(form["errors"], {"host": "invalid_host"})
    self.assertEqual(flow.calls, [])  # nothing asked
    flow = self.Flow(error="link_press_pair")
    kind, form = self.run_step(flow, "async_step_link_ip", {"host": " 192.168.1.50 "})
    self.assertEqual(form["errors"], {"base": "link_press_pair"})
    self.assertEqual(form["data_schema"], {("host", "192.168.1.50"): str})  # the address stays
    self.assertEqual(flow.calls, [("get", "192.168.1.50")])

  def test_found_panel_goes_on_like_after_mdns(self):
    info = {key: PANEL_INFO[key] for key in ("device_id", "name", "model", "firmware", "base_topic", "ha_prefix")}
    flow = self.Flow(info=info)
    self.assertEqual(self.run_step(flow, "async_step_link_ip", {"host": "http://192.168.1.50/"}), ("link_confirm", None))
    self.assertEqual(flow.calls[:3], [("get", "192.168.1.50"), ("unique_id", "A1B2C3D4E5F6", False),
                                      ("dismiss", "A1B2C3D4E5F6", ("link_confirm", "link_switch"))])
    self.assertEqual((flow._discovered_host, flow._discovered_device_id, flow._discovered_name,
                      flow._discovered_model, flow._discovered_base_topic, flow._discovered_ha_prefix),
                     ("192.168.1.50", "A1B2C3D4E5F6", "Guition JC8012P4A1 V2", "guition_jc8012p4a1_v2",
                      "tab5_e5f6", "homeassistant"))
    self.assertIsNone(flow._link_entry_id)
    self.assertEqual(len(self.package.ensured), 1)  # the link server runs before the address goes out
    # A panel that has an entry (over MQTT) is switched to the link, its entry stays.
    existing = types.SimpleNamespace(entry_id="entry-1", title="Kitchen")
    flow = self.Flow(info=info, existing=existing)
    self.assertEqual(self.run_step(flow, "async_step_link_ip", {"host": "192.168.1.50"}), ("link_switch", None))
    self.assertEqual((flow._link_entry_id, flow.context["title_placeholders"]), ("entry-1", {"name": "Kitchen"}))

  def test_manual_over_mqtt_is_the_old_form(self):
    kind, form = self.run_step(self.Flow(), "async_step_manual")
    self.assertEqual((kind, form["step_id"]), ("form", "manual"))
    self.assertEqual([key for key, _ in form["data_schema"]],
                     ["base_topic", "ha_prefix", "device_name", "manufacturer", "model"])
    kind, entry = self.run_step(self.Flow(), "async_step_manual",
                                {"base_topic": "panel", "ha_prefix": "homeassistant", "device_name": " Hall "})
    self.assertEqual((kind, entry["data"]), ("entry", {"base_topic": "panel", "ha_prefix": "homeassistant",
                                                       "device_name": "Hall"}))

  def test_typed_addresses(self):
    host = self.scope["_panel_host_input"]
    for typed, expected in [("192.168.1.50", "192.168.1.50"), (" http://192.168.1.50/ ", "192.168.1.50"),
                            ("http://192.168.1.50:80/setup", "192.168.1.50"), ("Panel-Kitchen.local", "panel-kitchen.local"),
                            ("192.168.1", ""), ("300.1.1.1", ""), ("fe80::1", ""), ("a b", ""), ("", ""), (None, ""),
                            ("-panel", ""), ("192.168.1.50:8080", "")]:
      self.assertEqual(host(typed), expected, typed)


class AddDeviceTextsTest(unittest.TestCase):
  def test_every_language_has_the_menu_the_form_and_the_errors(self):
    english = json.loads((ROOT / "translations" / "en.json").read_text(encoding="utf-8"))["config"]
    for path in [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]:
      config = json.loads(path.read_text(encoding="utf-8"))["config"]
      step = config["step"]
      self.assertEqual(set(step["user"]["menu_options"]), {"link_ip", "manual"}, path)
      self.assertNotIn("data", step["user"], path)  # a menu, no form
      self.assertEqual(set(step["link_ip"]["data"]), {"host"}, path)
      self.assertTrue(step["link_ip"]["data_description"]["host"].strip(), path)
      self.assertEqual(set(step["manual"]["data"]), set(english["step"]["manual"]["data"]), path)
      for error in ("invalid_host", "link_press_pair", "cannot_connect", "link_unsupported"):
        self.assertTrue(config["error"][error].strip(), (path, error))
      # The hint to press Pair first and the fixed address are in the form.
      self.assertIn("192.168.1.50", step["link_ip"]["data_description"]["host"], path)
    texts = json.loads((ROOT / "translations" / "de.json").read_text(encoding="utf-8"))["config"]["step"]
    self.assertEqual(texts["user"]["menu_options"],
                     {"link_ip": "Panel per IP-Adresse (direkte Verbindung)", "manual": "Manuell über MQTT"})
    self.assertIn("Drücke vorher am Panel Koppeln (Einstellungen > System > Sicherheit)", texts["link_ip"]["description"])
    self.assertIn("feste IP-Adresse", texts["link_ip"]["description"])


if __name__ == "__main__":
  unittest.main()
