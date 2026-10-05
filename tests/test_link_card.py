"""The card of a panel whose Pair button was pressed ends with its pair window."""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT

SOURCE = (ROOT / "config_flow.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLOW = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "Tab5ConfigFlow")


def method(name):
  return next(node for node in FLOW.body if getattr(node, "name", None) == name)


def link_card_flow_class():
  """The card steps that end a link card, on a small flow, without HA."""
  names = {"_link_setup_running", "_async_dismiss_link_cards", "_async_dismiss_cards",
           "_async_link_card_expired", "async_remove", "_close_link", "_async_watch_link_number"}
  functions = [node for node in FLOW.body if getattr(node, "name", None) in names]
  constants = [node for node in TREE.body
               if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None)
               in ("_LINK_CARD_STEPS", "_MQTT_CARD_STEPS", "LINK_CARD_TIMEOUT_S")]
  runtime = next(node for node in TREE.body if getattr(node, "name", None) == "_link_runtime")
  cls = ast.ClassDef(name="Flow", bases=[ast.Name("FlowBase", ast.Load())], keywords=[],
                     body=functions, decorator_list=[], type_params=[])
  module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *constants, runtime, cls], type_ignores=[])

  class FlowBase:
    _link_card_unsub = None
    _link_number_watch = None
    _link_pending = None
    _link_task = None
    _discovered_device_id = None
    cur_step = None

    def __init__(self, hass, flow_id, step_id=None, device_id="A1B2C3D4E5F6"):
      self.hass = hass
      self.flow_id = flow_id
      self._discovered_device_id = device_id
      if step_id:
        self.cur_step = {"step_id": step_id}

  scope = {"FlowBase": FlowBase, "DOMAIN": "tab5_lvgl", "DATA_LINK": "link", "Any": object, "Tuple": tuple,
           "HomeAssistant": object, "callback": lambda fn: fn, "_LOGGER": logging.getLogger("test_link_card")}
  exec(compile(ast.fix_missing_locations(module), "config_flow.py", "exec"), scope)
  return scope["Flow"], scope


class FakeFlowManager:
  def __init__(self):
    self.flows = []
    self.aborted = []

  def add(self, flow, unique_id="A1B2C3D4E5F6"):
    self.flows.append((flow, unique_id))

  def async_progress_by_handler(self, handler, include_uninitialized=False, match_context=None):
    assert handler == "tab5_lvgl"
    wanted = (match_context or {}).get("unique_id")
    return [{"flow_id": flow.flow_id, "handler": handler, "step_id": (flow.cur_step or {}).get("step_id"),
             "context": {"unique_id": unique_id}}
            for flow, unique_id in self.flows
            if flow.cur_step is not None and (wanted is None or unique_id == wanted)]

  def async_abort(self, flow_id):
    for item in list(self.flows):
      if item[0].flow_id == flow_id:
        self.flows.remove(item)
        self.aborted.append(flow_id)
        item[0].async_remove()
        return
    raise KeyError(flow_id)


class LinkCardTest(unittest.TestCase):
  def setUp(self):
    self.Flow, self.scope = link_card_flow_class()
    self.manager = FakeFlowManager()
    self.runtime = types.SimpleNamespace(pending={})
    self.hass = types.SimpleNamespace(data={"tab5_lvgl": {"link": self.runtime}},
                                      config_entries=types.SimpleNamespace(flow=self.manager))

  def card(self, flow_id, step_id="link_confirm", device_id="A1B2C3D4E5F6"):
    flow = self.Flow(self.hass, flow_id, step_id, device_id)
    self.manager.add(flow, device_id)
    return flow

  def test_closed_window_removes_the_cards_that_wait_for_add(self):
    self.card("old")
    self.card("switch", "link_switch")
    other_panel = self.card("other", device_id="0F0E0D0C0B0A")
    announcement = self.Flow(self.hass, "new")
    announcement._async_dismiss_link_cards("A1B2C3D4E5F6")
    self.assertEqual(self.manager.aborted, ["old", "switch"])
    self.assertEqual([flow for flow, _ in self.manager.flows], [other_panel])

  def test_a_running_setup_keeps_its_card(self):
    self.card("old")
    self.runtime.pending["A1B2C3D4E5F6"] = object()
    self.Flow(self.hass, "new")._async_dismiss_link_cards("A1B2C3D4E5F6")
    self.assertEqual(self.manager.aborted, [])

  def test_old_mqtt_cards_of_the_panel_are_removed(self):
    self.card("mqtt", "discovery_confirm")
    self.card("adopt", "adopt_confirm")
    self.card("link", "link_confirm")
    self.card("other", "discovery_confirm", device_id="0F0E0D0C0B0A")
    self.Flow(self.hass, "new")._async_dismiss_cards(
      "A1B2C3D4E5F6", self.scope["_MQTT_CARD_STEPS"], "it no longer uses MQTT")
    self.assertEqual(self.manager.aborted, ["mqtt", "adopt"])

  def test_later_steps_are_not_cards(self):
    for step_id in ("link_wait", "link_number", "link_finish", "zeroconf_confirm"):
      self.card(step_id, step_id)
    self.Flow(self.hass, "new")._async_dismiss_link_cards("A1B2C3D4E5F6")
    self.assertEqual(self.manager.aborted, [])

  def test_the_card_ends_with_the_window(self):
    flow = self.card("card")
    cancelled = []
    flow._link_card_unsub = lambda: cancelled.append(True)
    flow._async_link_card_expired()
    self.assertEqual(self.manager.aborted, ["card"])
    # The timer has fired; removing the flow does not cancel it again.
    self.assertEqual(cancelled, [])
    self.assertIsNone(flow._link_card_unsub)

  def test_the_window_end_spares_a_running_setup(self):
    flow = self.card("card")
    self.runtime.pending["A1B2C3D4E5F6"] = object()
    flow._async_link_card_expired()
    flow.cur_step = {"step_id": "link_number"}
    self.runtime.pending.clear()
    flow._async_link_card_expired()
    self.assertEqual(self.manager.aborted, [])

  def test_closing_the_card_cancels_its_timer(self):
    flow = self.card("card")
    cancelled = []
    flow._link_card_unsub = lambda: cancelled.append(True)
    flow.async_remove()
    self.assertEqual(cancelled, [True])
    self.assertIsNone(flow._link_card_unsub)

  def test_timeout_covers_the_firmware_window(self):
    self.assertGreaterEqual(self.scope["LINK_CARD_TIMEOUT_S"], 120.0)
    self.assertEqual(self.scope["_LINK_CARD_STEPS"], ("link_confirm", "link_switch"))
    self.assertEqual(self.scope["_MQTT_CARD_STEPS"], ("discovery_confirm", "adopt_confirm"))


class ZeroconfOrderTest(unittest.TestCase):
  def test_closed_window_is_handled_before_the_unique_id_check(self):
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    dismiss = source.index("self._async_dismiss_link_cards(device_id)")
    self.assertLess(dismiss, source.index("await self.async_set_unique_id(device_id)"),
                    "otherwise the announcement aborts as already_in_progress and the old card stays")
    self.assertLess(source.index("if link and not pairing:"), dismiss)

  def test_old_mqtt_cards_and_announcements_go(self):
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    unique = source.index("await self.async_set_unique_id(device_id)")
    closed = source[source.index("if link and not pairing:"):source.index("elif pairing:")]
    # The panel has neither MQTT nor the link: its MQTT card and announcement are old.
    self.assertIn('self._async_dismiss_cards(device_id, _MQTT_CARD_STEPS, "it no longer uses MQTT")', closed)
    self.assertIn("link_mqtt.async_clear_mqtt_announcement(self.hass, device_id)", closed)
    self.assertIn("async_create_background_task", closed)
    pressed = source[source.index("elif pairing:"):unique]
    self.assertIn('self._async_dismiss_cards(device_id, _MQTT_CARD_STEPS, "Pair was pressed on it")', pressed)
    self.assertLess(source.index("elif pairing:"), unique)

  def test_linked_entries_clear_their_mqtt_announcement_without_delaying_the_start(self):
    init = (ROOT / "__init__.py").read_text(encoding="utf-8")
    setup = init[init.index("async def async_setup_entry("):init.index("async def async_migrate_entry(")]
    linked = setup[setup.index("if entry_transport(entry) == TRANSPORT_LINK:"):setup.index("  else:")]
    self.assertIn("hass.async_create_background_task(", linked)
    self.assertIn("mqtt.async_clear_mqtt_announcement(hass, entry_device_id(entry))", linked)

  def test_a_leftover_mqtt_card_cannot_follow_at_start_up(self):
    # Both discoveries wait for the end of the start; the MQTT one may run
    # after the mDNS one and must then not open a card.
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    closed = source[source.index("if link and not pairing:"):source.index("elif pairing:")]
    self.assertIn("_link_only_panels(self.hass).add(device_id.upper())", closed)
    discovery = ast.get_source_segment(SOURCE, method("async_step_integration_discovery"))
    blocked = discovery.index("if retained and (str(device_id).upper() in _link_only_panels(self.hass)")
    self.assertIn("or await _async_mdns_new_link_panel(self.hass, str(device_id))", discovery[blocked:])
    self.assertLess(discovery.index("return await self._async_start_pairing_card(discovery_info)"), blocked)
    self.assertLess(blocked, discovery.index("await self.async_set_unique_id(device_id)"))
    rest = discovery[blocked:discovery.index("await self.async_set_unique_id(device_id)")]
    self.assertIn("link_mqtt.async_clear_mqtt_announcement(self.hass, str(device_id))", rest)
    self.assertIn('return self.async_abort(reason="link_press_pair")', rest)
    # The marker never reaches the entry data.
    self.assertIn("self._discovered_data.pop(DISCOVERY_RETAINED, None)", discovery)

  def test_a_live_mqtt_announcement_makes_the_card_real_again(self):
    init = (ROOT / "__init__.py").read_text(encoding="utf-8")
    handler = init[init.index("async def _handle_bridge_config("):init.index('if "_config_unsub" not in domain_data:')]
    self.assertIn('retained=bool(getattr(msg, "retain", False))', handler)
    process = init[init.index("async def _async_process_bridge_config("):init.index("def _announcement_log_due(")]
    live = process.index("if link_only and not retained:")
    self.assertIn("link_only.discard(str(device_id).upper())", process[live:live + 200])
    # Only for panels without an entry, before a card can be created.
    self.assertLess(process.index("  if entry:"), live)
    self.assertLess(live, process.index("discovery_flow.async_create_flow("))
    marked = process.index("data[DISCOVERY_RETAINED] = True")
    self.assertLess(process.index("  if retained:"), marked)
    self.assertLess(marked, process.index("discovery_flow.async_create_flow("))

  def test_a_new_card_arms_its_timer(self):
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    branch = source[source.index("if pairing:"):]
    self.assertIn("self._link_card_unsub = async_call_later(self.hass, LINK_CARD_TIMEOUT_S, "
                  "self._async_link_card_expired)", branch)
    self.assertLess(branch.index("async_call_later"), branch.index("return await self.async_step_link_confirm()"))


def mdns_helper(properties=None, cached=True, fail=False):
  """_async_mdns_new_link_panel against a fake Home Assistant mDNS cache."""
  node = next(node for node in TREE.body if getattr(node, "name", None) == "_async_mdns_new_link_panel")
  constant = next(node for node in TREE.body if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", None) == "ZEROCONF_SERVICE_TYPE")
  module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            constant, node], type_ignores=[])
  asked = []

  class AsyncServiceInfo:
    def __init__(self, type_, name):
      self.name = name
      self.properties = properties

    def load_from_cache(self, zc):
      assert zc == "zeroconf"
      asked.append(self.name)
      return cached and self.name.startswith("8AF1E60AF6E8.")

  async def async_get_async_instance(hass):
    if fail:
      raise RuntimeError("zeroconf is not set up")
    return types.SimpleNamespace(zeroconf="zeroconf")

  ha_zeroconf = types.ModuleType("homeassistant.components.zeroconf")
  ha_zeroconf.async_get_async_instance = async_get_async_instance
  components = types.ModuleType("homeassistant.components")
  components.zeroconf = ha_zeroconf
  zc_asyncio = types.ModuleType("zeroconf.asyncio")
  zc_asyncio.AsyncServiceInfo = AsyncServiceInfo
  stubs = {"homeassistant": types.ModuleType("homeassistant"), "homeassistant.components": components,
           "homeassistant.components.zeroconf": ha_zeroconf, "zeroconf": types.ModuleType("zeroconf"),
           "zeroconf.asyncio": zc_asyncio}
  scope = {"HomeAssistant": object, "_LOGGER": logging.getLogger("test_link_card")}
  exec(compile(ast.fix_missing_locations(module), "config_flow.py", "exec"), scope)

  async def run(device_id):
    with mock.patch.dict(sys.modules, stubs):
      return await scope["_async_mdns_new_link_panel"](None, device_id)

  return run, asked


class MdnsCacheTest(unittest.IsolatedAsyncioTestCase):
  async def test_a_new_link_panel_is_recognised(self):
    run, asked = mdns_helper({b"link": b"1", b"device_id": b"8AF1E60AF6E8"})
    self.assertTrue(await run("8af1e60af6e8"))
    # The instance is named after the device id; both spellings are tried.
    self.assertEqual(asked, ["8af1e60af6e8._hometiles._tcp.local.", "8AF1E60AF6E8._hometiles._tcp.local."])

  async def test_everything_else_keeps_the_card(self):
    for properties in ({b"link": b"1", b"pair": b"1"}, {b"device_id": b"8AF1E60AF6E8"}, {}, None):
      run, _asked = mdns_helper(properties)
      self.assertFalse(await run("8AF1E60AF6E8"), properties)
    run, _asked = mdns_helper({b"link": b"1"}, cached=False)
    self.assertFalse(await run("8AF1E60AF6E8"))
    run, _asked = mdns_helper({b"link": b"1"}, fail=True)
    self.assertFalse(await run("8AF1E60AF6E8"))


class LinkNumberCardTest(unittest.IsolatedAsyncioTestCase):
  """Cancel on the display while Home Assistant shows the number."""

  def setUp(self):
    self.Flow, _scope = link_card_flow_class()
    self.manager = FakeFlowManager()
    self.hass = types.SimpleNamespace(data={"tab5_lvgl": {"link": types.SimpleNamespace(pending={})}},
                                      config_entries=types.SimpleNamespace(flow=self.manager))

  def number_card(self):
    flow = self.Flow(self.hass, "card", "link_number")
    self.manager.add(flow)
    pending = types.SimpleNamespace(finished=asyncio.Event(), key=None, closed=False)
    pending.close = lambda: setattr(pending, "closed", True)
    flow._link_pending = pending
    flow._link_number_watch = asyncio.get_running_loop().create_task(flow._async_watch_link_number(pending))
    return flow, pending

  async def test_the_card_goes_when_the_panel_ends_the_pairing(self):
    flow, pending = self.number_card()
    await asyncio.sleep(0)
    self.assertEqual(self.manager.aborted, [])
    pending.finished.set()  # The panel sent abort: cancel, timeout or error.
    await asyncio.sleep(0)
    self.assertEqual(self.manager.aborted, ["card"])
    self.assertTrue(pending.closed)
    self.assertIsNone(flow._link_number_watch)

  async def test_a_paired_or_moved_on_card_stays(self):
    flow, pending = self.number_card()
    pending.key = b"k" * 32
    pending.finished.set()
    await asyncio.sleep(0)
    flow, pending = self.number_card()
    flow.cur_step = {"step_id": "link_finish"}
    pending.finished.set()
    await asyncio.sleep(0)
    self.assertEqual(self.manager.aborted, [])

  async def test_closing_the_card_stops_watching(self):
    flow, pending = self.number_card()
    watch = flow._link_number_watch
    flow._close_link()
    await asyncio.sleep(0)
    self.assertTrue(watch.cancelled())
    self.assertIsNone(flow._link_number_watch)


class LinkNumberWiringTest(unittest.TestCase):
  def test_the_number_menu_starts_watching_once(self):
    source = ast.get_source_segment(SOURCE, method("async_step_link_number"))
    watch = source.index("if self._link_number_watch is None:")
    self.assertLess(watch, source.index("return self.async_show_menu("))
    self.assertIn("self.hass.async_create_background_task(", source[watch:])


if __name__ == "__main__":
  unittest.main()
