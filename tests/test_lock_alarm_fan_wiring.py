"""Executable Bridge wiring for the Lock, Alarm panel and Fan tiles."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import types
import unittest

from test_announcement_guard import extract
from test_view_navigation import ROOT, load_module

ACCESS = load_module("access_helpers")
FAN = load_module("fan_helpers")
CONTROL = load_module("control_helpers")

NOW = 1_760_000_000.0
CODE = "1234"
LOGGER_NAME = "test_lock_alarm_fan_wiring"
METHODS = {
  "_has_default_code", "_code_guard", "_detail_payload", "_async_publish_detail_state",
  "_access_secured", "_async_publish_access_result", "_notify_code_lockout",
  "_async_handle_lock_command", "_async_handle_alarm_command", "_async_handle_access_command",
  "_async_handle_fan_command", "_resolve_target_entity", "_secure_log_due", "_owns_state_publish",
  "_ha_topic_for_entity", "_async_call_access_service",
}


class _HAError(Exception):
  def __init__(self, message: str = "", translation_key: str | None = None) -> None:
    super().__init__(message)
    self.translation_key = translation_key


class _Records(logging.Handler):
  def __init__(self) -> None:
    super().__init__(logging.DEBUG)
    self.messages: list[str] = []

  def emit(self, record: logging.LogRecord) -> None:
    self.messages.append(record.getMessage())


def _state(value, **attributes):
  return types.SimpleNamespace(state=value, attributes=attributes, name=None)


class AccessWiringTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.clock = 100.0
    self.publishes: list[tuple[str, dict, bool]] = []
    self.calls: list[tuple] = []
    self.notifications: list[tuple[str, str]] = []
    self.behaviour = None
    self.registry_options: dict[str, dict] = {}
    self.states = {
      "lock.front_door": _state("locked", code_format=r"^\d{4}$", supported_features=1),
      "lock.shed": _state("unlocked"),
      "alarm_control_panel.home": _state("armed_away", supported_features=3),
      "alarm_control_panel.coded": _state("armed_away", supported_features=3, code_format="number"),
      "alarm_control_panel.porch": _state("armed_away", supported_features=3, code_format="number",
                                          code_arm_required=False),
      "fan.ceiling": _state("on", supported_features=63, preset_modes=["auto"]),
    }

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.publishes.append((topic, json.loads(payload), retain))

    async def call(domain, service, data, blocking=False):
      self.calls.append((domain, service, dict(data), blocking))
      if self.behaviour is not None:
        await self.behaviour()

    def notify(hass, message, title=None, notification_id=None):
      self.notifications.append((notification_id, message))

    self.registry_platforms: dict[str, str] = {}
    registry = types.SimpleNamespace(async_get=lambda entity_id: types.SimpleNamespace(
      options=self.registry_options.get(entity_id, {}), platform=self.registry_platforms.get(entity_id)))
    self.bus_listeners: dict[str, list] = {}

    def listen(event_type, handler):
      self.bus_listeners.setdefault(event_type, []).append(handler)
      return lambda: self.bus_listeners[event_type].remove(handler)
    self.records = _Records()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(self.records)
    self.addCleanup(logger.removeHandler, self.records)

    scope: dict = {}
    for module in (ACCESS, FAN):
      scope.update({key: value for key, value in vars(module).items() if not key.startswith("__")})
    scope.update({
      "ACCESS_COMMAND_WINDOW_S": ACCESS.COMMAND_WINDOW_S,
      "ACCESS_SERVICE_TIMEOUT_S": 0.05,
      "ALARMO_EVENT_WAIT_S": 0.01,
      "callback": lambda function: function,
      "ACCESS_SEEN_MAX": 128,
      "DOMAIN": "tab5_lvgl",
      "asyncio": asyncio,
      "json": json,
      "entity_domain": CONTROL.entity_domain,
      "resolve_control_entity": CONTROL.resolve_control_entity,
      "mqtt": types.SimpleNamespace(async_publish=publish),
      "er": types.SimpleNamespace(async_get=lambda hass: registry),
      "persistent_notification": types.SimpleNamespace(async_create=notify),
      "dt_util": types.SimpleNamespace(utcnow=lambda: types.SimpleNamespace(timestamp=lambda: NOW)),
      "monotonic": lambda: self.clock,
      "_LOGGER": logger,
    })
    extract(METHODS | {"_OpenedCommand"}, scope)
    self.sealed = scope["_OpenedCommand"]
    self.bridge_class = type("Bridge", (), {name: scope[name] for name in METHODS})
    self.hass = types.SimpleNamespace(
      data={"tab5_lvgl": {}},
      states=types.SimpleNamespace(get=lambda entity_id: self.states.get(entity_id)),
      services=types.SimpleNamespace(async_call=call),
      bus=types.SimpleNamespace(async_listen=listen),
      async_create_task=lambda coro: asyncio.ensure_future(coro),
    )
    self.bridge = self.make_bridge()
    self.ids = 0

  def make_bridge(self):
    bridge = self.bridge_class()
    bridge.hass = self.hass
    bridge.entry = types.SimpleNamespace(entry_id="entry1", title="Hall", data={}, options={})
    bridge.base_topic = "hometiles"
    bridge.ha_prefix = "ha"
    bridge._command_channel = types.SimpleNamespace(removing=False)
    bridge.locks = ["lock.front_door", "lock.shed"]
    bridge.alarm_panels = ["alarm_control_panel.home", "alarm_control_panel.coded",
                           "alarm_control_panel.porch"]
    bridge.fans = ["fan.ceiling"]
    bridge.open_without_code = []
    bridge.access_codes = {}
    bridge._access_seen = {}
    bridge._secure_log_at = {}
    return bridge

  def tearDown(self) -> None:
    # Whatever happened, no log line may contain a code.
    for message in self.records.messages:
      self.assertNotIn(CODE, message)
      self.assertNotIn("98765", message)

  def body(self, entity_id="lock.front_door", action="unlock", code=CODE, command_id=None,
           deadline=None, web_auth=True):
    self.ids += 1
    body = {"entity_id": entity_id, "id": command_id or f"c{self.ids}",
            "deadline": NOW + 5 if deadline is None else deadline, "action": action,
            "web_auth": web_auth}
    if code is not None:
      body["code"] = code
    return json.dumps(body)

  async def lock(self, *, plain=False, bridge=None, **fields):
    payload = self.body(**fields)
    message = (types.SimpleNamespace(topic="hometiles/cmnd/lock", payload=payload, retain=False)
               if plain else self.sealed("hometiles/cmnd/lock", payload))
    await (bridge or self.bridge)._async_handle_lock_command(message)
    return self.publishes[-1] if self.publishes else None

  async def alarm(self, **fields):
    fields.setdefault("entity_id", "alarm_control_panel.home")
    payload = self.body(**fields)
    await self.bridge._async_handle_alarm_command(self.sealed("hometiles/cmnd/alarm", payload))
    return self.publishes[-1]

  def guard(self):
    return self.hass.data["tab5_lvgl"]["_code_guards"]["entry1"]

  async def test_unsecured_commands_are_refused_without_a_call(self) -> None:
    topic, result, retain = await self.lock(plain=True)
    self.assertEqual((topic, result["status"], retain), ("hometiles/stat/lock", "not_secured", False))
    for claim in (False, None, "true", 1):
      self.assertEqual((await self.lock(web_auth=claim))[1]["status"], "not_secured")
    self.bridge._command_channel = types.SimpleNamespace(removing=True)
    self.assertEqual((await self.lock())[1]["status"], "not_secured")
    self.bridge._command_channel = None
    self.assertEqual((await self.lock())[1]["status"], "not_secured")
    self.assertEqual(self.calls, [])

  async def test_unlock_with_code_runs_blocking_and_answers_ok(self) -> None:
    topic, result, retain = await self.lock()
    self.assertEqual(self.calls, [("lock", "unlock", {"entity_id": "lock.front_door", "code": CODE}, True)])
    self.assertEqual((topic, retain), ("hometiles/stat/lock", False))
    self.assertEqual(result, {"entity_id": "lock.front_door", "id": "c1", "status": "ok"})

  async def test_commands_run_once_and_only_within_their_deadline(self) -> None:
    await self.lock(command_id="same")
    await self.lock(command_id="same")
    self.assertEqual(len(self.calls), 1)
    self.assertEqual(len(self.publishes), 1)
    result = (await self.lock(deadline=NOW - 1))[1]
    self.assertEqual(result["status"], "expired")
    self.assertEqual(len(self.calls), 1)

  async def test_unconfigured_entities_and_actions_are_not_allowed(self) -> None:
    self.states["lock.garage"] = _state("locked")
    self.assertEqual((await self.lock(entity_id="lock.garage"))[1]["status"], "not_allowed")
    self.assertEqual((await self.alarm(entity_id="lock.front_door", action="disarm"))[1]["status"],
                     "not_allowed")
    self.assertEqual((await self.alarm(action="trigger"))[1]["status"], "not_allowed")
    self.assertEqual(self.calls, [])

  async def test_code_required_and_lock_without_code(self) -> None:
    self.assertEqual((await self.lock(code=None))[1]["status"], "code_required")
    self.assertEqual((await self.lock(code=None, action="lock"))[1]["status"], "code_required")
    self.assertEqual(self.calls, [])
    self.assertEqual((await self.lock(entity_id="lock.shed", action="lock", code=None))[1]["status"], "ok")
    self.assertEqual(self.calls, [("lock", "lock", {"entity_id": "lock.shed"}, True)])
    # Unlocking a lock without its own code needs the user's permission.
    self.assertEqual((await self.lock(entity_id="lock.shed", code=None))[1]["status"], "not_allowed")
    self.assertEqual((await self.lock(entity_id="lock.shed"))[1]["status"], "not_allowed")
    self.bridge.open_without_code = ["lock.shed"]
    self.assertEqual((await self.lock(entity_id="lock.shed", code=None))[1]["status"], "ok")

  async def test_default_code_needs_permission_to_skip_the_code(self) -> None:
    self.registry_options["lock.front_door"] = {"lock": {"default_code": "0000"}}
    self.assertEqual((await self.lock(code=None))[1]["status"], "not_allowed")
    self.assertEqual((await self.lock(code=None, action="lock"))[1]["status"], "ok")
    self.assertEqual((await self.lock())[1]["status"], "ok")
    self.assertEqual(self.calls[-1][2], {"entity_id": "lock.front_door", "code": CODE})

  async def test_a_default_code_outside_the_format_is_ignored(self) -> None:
    self.registry_options["lock.front_door"] = {"lock": {"default_code": "12"}}
    await self.bridge._async_publish_detail_state("lock.front_door", self.states["lock.front_door"])
    self.assertTrue(self.publishes[-1][1]["lock_code"])
    self.assertEqual((await self.lock(code=None, action="lock"))[1]["status"], "code_required")
    self.assertEqual(self.calls, [])

  async def test_wrong_codes_lock_code_entry_and_notify(self) -> None:
    async def reject():
      raise _HAError("Invalid code", "invalid_code")

    self.behaviour = reject
    statuses = [(await self.lock())[1] for _ in range(5)]
    self.assertEqual([item["status"] for item in statuses], ["wrong_code"] * 5)
    self.assertNotIn("retry_after", statuses[3])
    self.assertEqual(statuses[4]["retry_after"], 30)
    self.assertEqual(len(self.notifications), 1)
    self.assertIn("lock.front_door", self.notifications[0][0])
    self.assertNotIn(CODE, self.notifications[0][1])
    calls = len(self.calls)
    blocked = (await self.lock())[1]
    self.assertEqual((blocked["status"], blocked["retry_after"]), ("locked_out", 30))
    self.assertEqual((await self.lock(code=None))[1]["status"], "locked_out")
    self.assertEqual(len(self.calls), calls)
    # Other entities and commands without a code keep working.
    self.behaviour = None
    self.assertEqual((await self.lock(entity_id="lock.shed", action="lock", code=None))[1]["status"], "ok")
    self.clock += 31
    self.behaviour = reject
    self.assertEqual((await self.lock())[1]["retry_after"], 60)
    self.behaviour = None
    self.clock += 61
    self.assertEqual((await self.lock())[1]["status"], "ok")
    self.assertEqual(self.guard().retry_after("lock.front_door"), 0)

  async def test_codes_in_the_bridge_are_checked_before_home_assistant(self) -> None:
    # A device that ignores a wrong code silently (Home Assistant answers
    # ok): with its codes in the Bridge options a wrong one never reaches
    # Home Assistant, is reported and counts towards the lockout.
    self.bridge.access_codes = {"lock.front_door": [CODE, "4321"]}
    statuses = [(await self.lock(code="5555"))[1] for _ in range(5)]
    self.assertEqual([item["status"] for item in statuses], ["wrong_code"] * 5)
    self.assertNotIn("retry_after", statuses[3])
    self.assertEqual(statuses[4]["retry_after"], 30)
    self.assertEqual(self.calls, [])
    self.assertEqual(len(self.notifications), 1)
    self.assertEqual((await self.lock())[1]["status"], "locked_out")
    self.clock += 31
    self.assertEqual((await self.lock(code="4321"))[1]["status"], "ok")
    self.assertEqual(self.calls, [("lock", "unlock", {"entity_id": "lock.front_door", "code": "4321"}, True)])
    self.assertEqual(self.guard().retry_after("lock.front_door"), 0)
    # A device without codes in the Bridge keeps the check of Home Assistant.
    self.bridge.access_codes = {}
    self.assertEqual((await self.lock(code="5555"))[1]["status"], "ok")

  def fire(self, event_type: str, **data) -> None:
    for handler in list(self.bus_listeners.get(event_type, [])):
      handler(types.SimpleNamespace(event_type=event_type, data=data))

  async def test_alarmo_reports_a_wrong_code_with_an_event(self) -> None:
    # Alarmo answers a code with an event right after its service returns
    # (alarmo_failed_to_arm, reason invalid_code), never with an error.
    entity = "alarm_control_panel.coded"
    self.registry_platforms[entity] = "alarmo"
    # Real waits: the loop clock on Windows ticks in about 15 ms steps, so a
    # few milliseconds could expire before the event's loop turn.
    shared = type(self.bridge)._async_call_access_service.__globals__
    shared["ALARMO_EVENT_WAIT_S"] = 0.5
    shared["ACCESS_SERVICE_TIMEOUT_S"] = 2.0
    verdicts: list[str] = []

    async def alarmo():
      verdict = verdicts.pop(0)

      async def later():
        await asyncio.sleep(0)
        if verdict == "success":
          self.fire("alarmo_command_success", entity_id=entity, action="disarm")
        elif verdict:
          self.fire("alarmo_failed_to_arm", entity_id="alarm_control_panel.other", reason="invalid_code")
          self.fire("alarmo_failed_to_arm", entity_id=entity, reason=verdict)

      asyncio.ensure_future(later())

    self.behaviour = alarmo
    verdicts.extend(["invalid_code"] * 5)
    statuses = [(await self.alarm(entity_id=entity, action="disarm"))[1] for _ in range(5)]
    self.assertEqual([item["status"] for item in statuses], ["wrong_code"] * 5)
    self.assertEqual(statuses[4]["retry_after"], 30)
    self.assertEqual((await self.alarm(entity_id=entity, action="disarm"))[1]["status"], "locked_out")
    self.clock += 31
    verdicts.append("open_sensors")
    self.assertEqual((await self.alarm(entity_id=entity, action="arm_home"))[1]["status"], "failed")
    verdicts.append("success")
    self.assertEqual((await self.alarm(entity_id=entity, action="disarm"))[1]["status"], "ok")
    self.assertEqual(self.guard().retry_after(entity), 0)
    # No verdict in time counts as ok, like any other integration.
    shared["ALARMO_EVENT_WAIT_S"] = 0.05
    verdicts.append("")
    self.assertEqual((await self.alarm(entity_id=entity, action="disarm"))[1]["status"], "ok")
    self.assertTrue(all(not handlers for handlers in self.bus_listeners.values()))
    # Other alarm panels never wait for Alarmo's events.
    self.behaviour = None
    self.assertEqual((await self.alarm(entity_id="alarm_control_panel.porch", action="disarm"))[1]["status"], "ok")
    self.assertEqual(self.bus_listeners.get("alarmo_failed_to_arm", []), [])

  async def test_a_command_without_code_does_not_lift_the_lockout(self) -> None:
    async def reject_disarm():
      if self.calls[-1][1] == "alarm_disarm":
        raise _HAError("Invalid code", "invalid_code")

    self.behaviour = reject_disarm
    for _ in range(5):
      await self.alarm(entity_id="alarm_control_panel.porch", action="disarm")
    self.assertEqual((await self.alarm(entity_id="alarm_control_panel.porch", action="arm_home",
                                       code=None))[1]["status"], "ok")
    result = (await self.alarm(entity_id="alarm_control_panel.porch", action="disarm"))[1]
    self.assertEqual(result["status"], "locked_out")

  async def test_parallel_codes_run_one_at_a_time(self) -> None:
    async def slow():
      await asyncio.sleep(0.02)

    self.behaviour = slow
    await asyncio.gather(*(self.lock() for _ in range(4)))
    statuses = sorted(item[1]["status"] for item in self.publishes)
    self.assertEqual(statuses, ["busy", "busy", "busy", "ok"])
    self.assertEqual(len(self.calls), 1)

  async def test_the_lockout_survives_an_entry_reload(self) -> None:
    async def reject():
      raise _HAError("Invalid code", "invalid_code")

    self.behaviour = reject
    for _ in range(5):
      await self.lock()
    reloaded = self.make_bridge()
    self.behaviour = None
    self.assertEqual((await self.lock(bridge=reloaded))[1]["status"], "locked_out")
    self.assertEqual(len(self.calls), 5)

  async def test_slow_home_assistant_answers_pending_and_still_counts_wrong_codes(self) -> None:
    async def slow_reject():
      await asyncio.sleep(0.15)
      raise _HAError("Invalid code", "invalid_code")

    self.behaviour = slow_reject
    self.assertEqual((await self.lock())[1]["status"], "pending")
    self.assertEqual((await self.lock())[1]["status"], "busy")
    await asyncio.sleep(0.3)
    self.assertEqual(self.guard()._failures.get("lock.front_door"), 1)
    self.assertNotIn("lock.front_door", self.guard()._in_flight)

  async def test_a_fast_timeout_error_is_a_failure_not_pending(self) -> None:
    async def timeout():
      raise TimeoutError("device timed out")

    self.behaviour = timeout
    self.assertEqual((await self.lock())[1]["status"], "failed")

  async def test_other_failures_log_only_the_exception_type(self) -> None:
    async def fail():
      raise RuntimeError(f"device refused code {CODE} and 98765")

    self.behaviour = fail
    self.assertEqual((await self.lock())[1]["status"], "failed")
    self.assertTrue(any("RuntimeError" in message for message in self.records.messages))
    self.assertEqual(self.guard().retry_after("lock.front_door"), 0)

  async def test_alarm_rules(self) -> None:
    self.assertEqual((await self.alarm(action="arm_home", code=None))[1]["status"], "ok")
    self.assertEqual(self.calls[-1], ("alarm_control_panel", "alarm_arm_home",
                                      {"entity_id": "alarm_control_panel.home"}, True))
    self.assertEqual((await self.alarm(action="arm_night", code=None))[1]["status"], "unsupported")
    self.assertEqual((await self.alarm(action="disarm", code=None))[1]["status"], "not_allowed")
    self.bridge.open_without_code = ["alarm_control_panel.home"]
    self.assertEqual((await self.alarm(action="disarm", code=None))[1]["status"], "ok")
    coded = (await self.alarm(entity_id="alarm_control_panel.coded", action="disarm", code=None))[1]
    self.assertEqual(coded["status"], "code_required")
    self.assertEqual((await self.alarm(entity_id="alarm_control_panel.coded", action="disarm"))[1]["status"], "ok")
    self.assertEqual(self.publishes[-1][0], "hometiles/stat/alarm")

  async def test_malformed_sealed_bodies_are_dropped_quietly(self) -> None:
    for payload in ("", "{", json.dumps({"code": CODE}), "x" * 4000):
      await self.bridge._async_handle_lock_command(self.sealed("hometiles/cmnd/lock", payload))
    self.assertEqual((self.publishes, self.calls), ([], []))

  async def test_detail_state_is_retained_on_the_additive_topic(self) -> None:
    await self.bridge._async_publish_detail_state("lock.front_door", self.states["lock.front_door"])
    topic, detail, retain = self.publishes[-1]
    self.assertEqual((topic, retain), ("ha/lock/front_door/detail", True))
    self.assertEqual((detail["state"], detail["lock_code"], detail["unlock_allowed"]), ("locked", True, True))
    self.registry_options["lock.front_door"] = {"lock": {"default_code": "0000"}}
    await self.bridge._async_publish_detail_state("lock.front_door", self.states["lock.front_door"])
    detail = self.publishes[-1][1]
    self.assertEqual((detail["lock_code"], detail["unlock_allowed"]), (False, False))
    self.assertNotIn("0000", json.dumps(detail))
    await self.bridge._async_publish_detail_state("alarm_control_panel.gone", None)
    self.assertEqual(self.publishes[-1][1]["state"], None)
    await self.bridge._async_publish_detail_state("fan.ceiling", self.states["fan.ceiling"])
    self.assertEqual(self.publishes[-1][0], "ha/fan/ceiling/detail")
    self.assertEqual(self.publishes[-1][1]["preset_modes"], ["auto"])

  async def test_fan_commands_are_validated_and_not_blocking(self) -> None:
    async def fan(payload, sealed=False):
      text = json.dumps(payload)
      message = (self.sealed("hometiles/cmnd/fan", text) if sealed
                 else types.SimpleNamespace(topic="hometiles/cmnd/fan", payload=text, retain=False))
      await self.bridge._async_handle_fan_command(message)

    await fan({"entity_id": "fan.ceiling", "action": "set_percentage", "percentage": 130})
    await fan({"entity_id": "fan.ceiling", "action": "set_preset_mode", "preset_mode": "auto"}, sealed=True)
    await fan({"entity_id": "fan.ceiling", "action": "set_preset_mode", "preset_mode": "turbo"})
    await fan({"entity_id": "fan.other", "action": "turn_on"})
    self.assertEqual(self.calls, [
      ("fan", "set_percentage", {"entity_id": "fan.ceiling", "percentage": 100}, False),
      ("fan", "set_preset_mode", {"entity_id": "fan.ceiling", "preset_mode": "auto"}, False),
    ])


class SourceContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls) -> None:
    cls.source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    cls.tree = ast.parse(cls.source)

  def function(self, name: str) -> ast.AST:
    return next(node for node in ast.walk(self.tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)

  def segment(self, name: str) -> str:
    return ast.get_source_segment(self.source, self.function(name))

  def test_lock_and_alarm_have_no_plain_command_topic(self) -> None:
    self.assertNotIn("cmnd/lock", self.source)
    self.assertNotIn("cmnd/alarm", self.source)
    self.assertIn('f"{self.base_topic}/cmnd/fan"', self.segment("_async_setup_plain_commands"))
    self.assertIn("self._unsub_fan()", self.segment("async_unload"))
    handlers = self.segment("_command_handlers")
    for leaf, handler in (("lock", "_async_handle_lock_command"), ("alarm", "_async_handle_alarm_command"),
                          ("fan", "_async_handle_fan_command")):
      self.assertIn(f'"{leaf}": self.{handler}', handlers)

  def test_access_logs_show_only_allowed_values(self) -> None:
    # Everything a Lock/Alarm log line may print; a code can reach none of it.
    allowed = {
      "leaf", "self.base_topic", "entity_id", "seconds", "status", "command['action']",
      "'cancelled' if task.cancelled() else type(task.exception()).__name__",
    }
    for name in ("_async_handle_access_command", "_notify_code_lockout"):
      for node in ast.walk(self.function(name)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "_LOGGER"):
          continue
        self.assertIsInstance(node.args[0], ast.Constant)
        for argument in node.args[1:]:
          self.assertIn(ast.unparse(argument), allowed)
        self.assertEqual(node.keywords, [])

  def test_config_payload_and_tracking_include_the_new_lists(self) -> None:
    config = self.segment("async_publish_config_to_device")
    for key in ("CONF_LOCKS", '"lock_meta"', "CONF_ALARM_PANELS", '"alarm_panel_meta"', "CONF_FANS", '"fan_meta"'):
      self.assertIn(key, config)
    refresh = self.segment("_refresh_runtime_entity_lists")
    for name in ("self.locks", "self.alarm_panels", "self.fans"):
      self.assertIn(f"+ {name}", refresh)
    self.assertIn("_async_publish_detail_state", self.segment("_async_publish_entity_state"))
    self.assertIn("_async_publish_detail_state", self.segment("async_publish_snapshot"))
    self.assertIn("_async_publish_detail_state", self.segment("_handle_state_event"))


class ConfigFlowTest(unittest.TestCase):
  def test_panel_lists_accept_only_their_domains(self) -> None:
    for key, valid, foreign in (("locks", "lock.door", "switch.door"),
                                ("alarm_panels", "alarm_control_panel.home", "lock.door"),
                                ("fans", "fan.ceiling", "switch.fan")):
      self.assertEqual(CONTROL.panel_entity_list([valid], key), [valid])
      with self.assertRaises(ValueError):
        CONTROL.panel_entity_list([valid, foreign], key)

  def test_options_form_filters_domains_and_keeps_lists(self) -> None:
    tree = ast.parse((ROOT / "config_flow.py").read_text(encoding="utf-8"))
    names = {"_convert_entity_data", "_normalise_entity_list", "_unique", "_split_weather_entities",
             "_parse_scene_map", "_merge_all_entities"}
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    scope = dict(vars(load_module("const")))
    scope.update(vars(load_module("editable_helpers")))
    scope.update(vars(CONTROL))
    scope["split_binary_sensor_entities"] = load_module("binary_sensor_helpers").split_binary_sensor_entities
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "access-config", "exec"), scope)
    convert = scope["_convert_entity_data"]
    selected = {
      "locks": ["lock.door", "switch.door"],
      "alarm_panels": ["alarm_control_panel.home", "lock.door"],
      "fans": ["fan.ceiling", "light.fan"],
      "open_without_code": ["lock.door", "switch.x", "alarm_control_panel.home"],
    }
    updated = convert(selected, {"scene_map": {}})
    self.assertEqual(updated["locks"], ["lock.door"])
    self.assertEqual(updated["alarm_panels"], ["alarm_control_panel.home"])
    self.assertEqual(updated["fans"], ["fan.ceiling"])
    self.assertEqual(updated["open_without_code"], ["lock.door", "alarm_control_panel.home"])
    kept = convert({}, updated)
    for key in selected:
      self.assertEqual(kept[key], updated[key])
    entries = [types.SimpleNamespace(entry_id="a", data=updated, options={}),
               types.SimpleNamespace(entry_id="b", data={"locks": ["lock.back"]}, options={})]
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_entries=lambda domain: entries))
    merged = scope["_merge_all_entities"](hass, {"_entry_id": "a", **updated})
    self.assertEqual(merged["locks"], ["lock.door", "lock.back"])

  def test_translations_explain_the_codes_the_bridge_checks(self) -> None:
    for filename in ("strings.json", "translations/de.json", "translations/en.json"):
      options = json.loads((ROOT / filename).read_text(encoding="utf-8"))["options"]
      self.assertTrue(options["step"]["init"]["menu_options"]["access_codes"].strip(), filename)
      self.assertTrue(options["step"]["access_codes"]["description"].strip(), filename)
      self.assertTrue(options["error"]["invalid_access_codes"].strip(), filename)
      self.assertTrue(options["abort"]["no_access_entities"].strip(), filename)

  def test_translations_name_the_lists_and_warn_about_opening_without_code(self) -> None:
    for filename in ("strings.json", "translations/de.json", "translations/en.json"):
      step = json.loads((ROOT / filename).read_text(encoding="utf-8"))["options"]["step"]["entities"]
      for key in ("locks", "alarm_panels", "fans", "open_without_code"):
        self.assertTrue(step["data"][key].strip(), (filename, key))
      self.assertTrue(step["data_description"]["open_without_code"].strip(), filename)


if __name__ == "__main__":
  unittest.main()
