"""The card of a panel whose Pair button was pressed ends with its pair window."""

from __future__ import annotations

import ast
import logging
import types
import unittest

from test_view_navigation import ROOT

SOURCE = (ROOT / "config_flow.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLOW = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "Tab5ConfigFlow")


def method(name):
  return next(node for node in FLOW.body if getattr(node, "name", None) == name)


def link_card_flow_class():
  """The card steps that end a link card, on a small flow, without HA."""
  names = {"_link_setup_running", "_async_dismiss_link_cards", "_async_link_card_expired",
           "async_remove", "_close_link"}
  functions = [node for node in FLOW.body if getattr(node, "name", None) in names]
  constants = [node for node in TREE.body
               if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None)
               in ("_LINK_CARD_STEPS", "LINK_CARD_TIMEOUT_S")]
  runtime = next(node for node in TREE.body if getattr(node, "name", None) == "_link_runtime")
  cls = ast.ClassDef(name="Flow", bases=[ast.Name("FlowBase", ast.Load())], keywords=[],
                     body=functions, decorator_list=[], type_params=[])
  module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *constants, runtime, cls], type_ignores=[])

  class FlowBase:
    _link_card_unsub = None
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

  scope = {"FlowBase": FlowBase, "DOMAIN": "tab5_lvgl", "DATA_LINK": "link", "Any": object,
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


class ZeroconfOrderTest(unittest.TestCase):
  def test_closed_window_is_handled_before_the_unique_id_check(self):
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    dismiss = source.index("self._async_dismiss_link_cards(device_id)")
    self.assertLess(dismiss, source.index("await self.async_set_unique_id(device_id)"),
                    "otherwise the announcement aborts as already_in_progress and the old card stays")
    self.assertLess(source.index("if link and not pairing:"), dismiss)

  def test_a_new_card_arms_its_timer(self):
    source = ast.get_source_segment(SOURCE, method("async_step_zeroconf"))
    branch = source[source.index("if pairing:"):]
    self.assertIn("self._link_card_unsub = async_call_later(self.hass, LINK_CARD_TIMEOUT_S, "
                  "self._async_link_card_expired)", branch)
    self.assertLess(branch.index("async_call_later"), branch.index("return await self.async_step_link_confirm()"))


if __name__ == "__main__":
  unittest.main()
