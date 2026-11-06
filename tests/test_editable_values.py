"""Writable value contracts and real MQTT command/history handlers."""
from __future__ import annotations
import ast
from datetime import datetime, timedelta, timezone
import json
import logging
import types
import unittest
from test_view_navigation import ROOT, load_module

E = load_module("editable_helpers")
from test_state_history import STATE_HISTORY as H
NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)


def methods(names):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    scope = dict(vars(E))
    scope.update(vars(H))
    scope.update(json=json, datetime=datetime, timedelta=timedelta, _try_parse_json=json.loads,
                 dt_util=types.SimpleNamespace(utcnow=lambda: NOW), _LOGGER=logging.getLogger(__name__))
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "editable-handlers", "exec"), scope)
    return scope


class EditableHelperTests(unittest.TestCase):
    def payload(self, entity, state="unknown", attrs=None):
        return E.build_editable_payload(entity, state, attrs or {}, "a" * 32, "Europe/Berlin")

    def test_number_bounds_zero_fraction_and_revision(self):
        for domain in E.NUMBER_DOMAINS:
            entity = domain + ".desk"
            attrs = {"min": -1, "max": 1, "step": 0.1, "unit_of_measurement": "m²", "mode": "box"}
            p = self.payload(entity, "0", attrs)
            self.assertTrue(p["writable"])
            self.assertEqual(p["unit"], "m²")
            self.assertEqual(p["revision"], self.payload(entity, "0.1", attrs)["revision"])
            self.assertNotEqual(p["revision"], self.payload(entity, "0", {**attrs, "step": 0.2})["revision"])
            for value in [-1, 0, 0.3, 1]:
                self.assertAlmostEqual(E.build_editable_service_call(entity, value, p)[2]["value"], value)
            for value in [True, None, "nan", "inf", -1.1, 1.1, 0.15, "0x10", ""]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    E.build_editable_service_call(entity, value, p)
        for attrs in [{}, {"min": 1, "max": 1, "step": 1}, {"min": 0, "max": 2, "step": 0}, {"min": False, "max": 2, "step": 1}]:
            self.assertFalse(self.payload("number.x", "0", attrs)["writable"])

    def test_select_exact_options_and_bounded_payloads(self):
        for domain in E.SELECT_DOMAINS:
            entity = domain + ".desk"
            p = self.payload(entity, "unknown", {"options": ["1x", "Heat pump + heating rod", "<warm>"]})
            self.assertTrue(p["writable"])
            self.assertEqual(E.build_editable_service_call(entity, "<warm>", p), (domain, "select_option", {"option": "<warm>"}))
            for invalid in [1, None, "1X", "missing"]:
                with self.assertRaises(ValueError): E.build_editable_service_call(entity, invalid, p)
            for options in [None, [], ["x", "x"], [""], ["a\nb"], ["x" * 256], [str(i) for i in range(65)]]:
                self.assertFalse(self.payload(entity, "x", {"options": options})["writable"])
            options = [str(i) + "é" * 100 for i in range(64)]
            bounded = self.payload(entity, options[0], {"options": options})
            self.assertTrue(bounded["writable"])
            self.assertLess(len(json.dumps(bounded, ensure_ascii=False).encode()), E.MAX_CONTROL_BYTES)

    def test_time_date_datetime_helpers_and_dst(self):
        for attributes in ({}, {"has_time": False, "has_date": False}):
            self.assertFalse(self.payload("input_datetime.x", attrs=attributes)["writable"])
        cases = [("time.x", {}, "08:30", "set_value", {"time": "08:30:00"}),
                 ("date.x", {}, "2028-02-29", "set_value", {"date": "2028-02-29"}),
                 ("input_datetime.x", {"has_time": True}, "08:30", "set_datetime", {"time": "08:30:00"}),
                 ("input_datetime.x", {"has_date": True}, "2028-02-29", "set_datetime", {"date": "2028-02-29"})]
        for entity, attrs, value, service, data in cases:
            self.assertEqual(E.build_editable_service_call(entity, value, self.payload(entity, attrs=attrs)), (entity.split(".")[0], service, data))
        for entity, value in [("time.x", "25:00"), ("date.x", "2026-02-29"), ("date.x", "2026/09/06")]:
            with self.assertRaises(ValueError): E.build_editable_service_call(entity, value, self.payload(entity))
        p = self.payload("datetime.x", "2026-09-06T06:00:00+00:00")
        self.assertEqual(p["state"], "2026-09-06 08:00:00")
        for invalid in ["2026-03-29 02:30:00", "2026-10-25 02:30:00"]:
            with self.assertRaises(ValueError): E.build_editable_service_call("datetime.x", invalid, p, "Europe/Berlin")
        self.assertEqual(E.build_editable_service_call("datetime.x", "2026-09-06 08:00", p, "Europe/Berlin")[2], {"datetime": "2026-09-06T08:00:00+02:00"})
        p = self.payload("input_datetime.x", attrs={"has_date": True, "has_time": True})
        self.assertEqual(E.build_editable_service_call("input_datetime.x", "2026-09-06 08:00", p, "Europe/Berlin")[2], {"timestamp": 1788674400.0})

    def test_missing_unavailable_and_old_config(self):
        self.assertIsNone(self.payload("number.x", None)["state"])
        self.assertFalse(self.payload("select.x", "unavailable", {"options": ["x"]})["writable"])
        self.assertEqual(E.editable_selection([], E.NUMBER_DOMAINS), [])
        self.assertEqual(E.editable_selection(["number.x", "number.x", "input_number.y"], E.NUMBER_DOMAINS), ["number.x", "input_number.y"])
        for value in [None, "number.x", ["sensor.number"], ["number.x/command"]]:
            with self.assertRaises(ValueError): E.editable_selection(value, E.NUMBER_DOMAINS)

    def test_real_config_roundtrip_missing_lists_and_shared_panel_selection(self):
        tree = ast.parse((ROOT / "config_flow.py").read_text(encoding="utf-8"))
        names = {"_convert_entity_data", "_normalise_entity_list", "_unique", "_split_weather_entities", "_parse_scene_map", "_merge_all_entities"}
        nodes = [node for node in tree.body if getattr(node, "name", None) in names]
        scope = dict(vars(load_module("const")))
        scope.update(vars(E))
        scope.update(vars(load_module("control_helpers")))
        scope["split_binary_sensor_entities"] = load_module("binary_sensor_helpers").split_binary_sensor_entities
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        scope["panel_entries"] = lambda hass: hass.config_entries.async_entries("tab5_lvgl")
        exec(compile(ast.fix_missing_locations(module), "editable-config", "exec"), scope)
        convert = scope["_convert_entity_data"]
        old = {"device_id": "panel", "sensor": [], "scene_map": {}}
        selected = {"numbers": ["number.area", "input_number.value"], "selects": ["select.mode", "input_select.mode"],
                    "datetimes": ["date.day", "time.start", "datetime.start", "input_datetime.helper"]}
        upgraded = convert(selected, old)
        for key, values in selected.items(): self.assertEqual(upgraded[key], values)
        preserved = convert({}, upgraded)
        for key, values in selected.items(): self.assertEqual(preserved[key], values)
        cleared = convert({"selects": []}, upgraded)
        self.assertEqual(cleared["selects"], [])
        self.assertEqual(cleared["numbers"], selected["numbers"])
        entries = [types.SimpleNamespace(entry_id="old", data=old, options={}),
                   types.SimpleNamespace(entry_id="new", data=upgraded, options={})]
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_entries=lambda domain: entries))
        merged = scope["_merge_all_entities"](hass, {"_entry_id": "old"})
        for key, values in selected.items(): self.assertEqual(merged[key], values)
        for key in selected:
            with self.assertRaises(ValueError): convert({key: ["sensor.wrong"]}, upgraded)

    def test_numeric_history_unknown_and_incomplete_pages_are_gaps(self):
        start = NOW - timedelta(hours=24)
        states = [{"last_changed": start + timedelta(hours=h), "state": value} for h, value in [(2, "0"), (4, "5"), (6, "unavailable"), (8, "3"), (20, "9")]]
        response = E.add_number_history({"hours": 24, "activity": [1]}, states, start, NOW, 25, complete=False, complete_until=start + timedelta(hours=10))
        self.assertEqual(response["values"][:4], [None, None, 0, 0])
        self.assertEqual(response["values"][6:9], [None, None, 3])
        self.assertEqual(response["values"][10:20], [None] * 10)
        self.assertEqual(response["values"][20:], [9] * 5)
        self.assertEqual(response["activity"], [1])


class EditableMqttTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.publishes, self.calls = [], []
        self.states = {"number.desk": types.SimpleNamespace(state="0", attributes={"min": 0, "max": 10, "step": 0.5}, last_changed=NOW)}
        async def publish(hass, topic, payload, **kwargs): self.publishes.append((topic, json.loads(payload), kwargs))
        async def call(*args, **kwargs): self.calls.append((args, kwargs))
        self.scope = methods({"_editable_payload", "_async_handle_value_command", "_async_publish_editable_state", "_async_handle_editable_history"})
        self.scope["mqtt"] = types.SimpleNamespace(async_publish=publish)
        self.bridge = types.SimpleNamespace(numbers=["number.desk"], selects=[], datetimes=[], _editable_session="a" * 32,
            _editable_seen={}, base_topic="panel", history_response_topic="panel/history/response",
            _owns_state_publish=lambda entity: True,
            _ha_topic_for_entity=lambda entity, suffix: "ha/" + entity.replace(".", "/") + "/" + suffix,
            hass=types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"), states=types.SimpleNamespace(get=self.states.get), services=types.SimpleNamespace(async_call=call)))
        for name in ["_editable_payload", "_async_publish_editable_state"]:
            setattr(self.bridge, name, types.MethodType(self.scope[name], self.bridge))

    def command(self, **changes):
        p = self.bridge._editable_payload("number.desk", self.states.get("number.desk"))
        return {"entity_id": "number.desk", "id": "request", "session": self.bridge._editable_session,
                "revision": p["revision"], "deadline": NOW.timestamp() + 10, "value": 2.5, **changes}

    async def send(self, command, retain=False):
        await self.scope["_async_handle_value_command"](self.bridge, types.SimpleNamespace(payload=json.dumps(command), retain=retain))

    async def test_real_command_ack_and_deduplication(self):
        await self.send(self.command())
        self.assertEqual(self.calls, [(("number", "set_value", {"entity_id": "number.desk", "value": 2.5}), {"blocking": True})])
        self.assertEqual(self.publishes[0][1]["status"], "ok")
        self.assertFalse(self.publishes[0][2]["retain"])
        self.assertTrue(self.publishes[1][0].endswith("/control"))
        self.assertTrue(self.publishes[1][2]["retain"])
        await self.send(self.command())
        self.assertEqual(len(self.calls), 1)

    async def test_retained_expired_restart_constraints_and_unavailable(self):
        await self.send(self.command(), retain=True)
        self.assertEqual(self.publishes, [])
        for i, change in enumerate([{"deadline": NOW.timestamp() - 1}, {"deadline": NOW.timestamp() + 100},
                                   {"session": "old"}, {"revision": "old"}, {"value": 2.1},
                                   {"entity_id": "number.other"}]):
            await self.send(self.command(id=str(i), **change))
        self.assertEqual(self.calls, [])
        old = self.command(id="old-bounds")
        self.states["number.desk"].attributes["max"] = 2
        await self.send(old)
        self.assertEqual(self.publishes[-2][1]["status"], "changed")
        self.states["number.desk"].state = "unavailable"
        await self.send(self.command(id="offline"))
        self.assertEqual(self.calls, [])
        self.states.clear()
        await self.bridge._async_publish_editable_state("number.desk", None)
        self.assertFalse(self.publishes[-1][1]["available"])

    async def test_every_editable_domain_reaches_its_fixed_service(self):
        for i, domain in enumerate(E.EDITABLE_DOMAINS):
            entity = domain + ".test"
            attributes = {"min": 0, "max": 10, "step": 1, "options": ["First", "Second"], "has_date": True, "has_time": True}
            self.states[entity] = types.SimpleNamespace(state="unknown", attributes=attributes, last_changed=NOW)
            if domain in E.NUMBER_DOMAINS: self.bridge.numbers.append(entity); value = 2
            elif domain in E.SELECT_DOMAINS: self.bridge.selects.append(entity); value = "Second"
            else:
                self.bridge.datetimes.append(entity)
                value = "08:00" if domain == "time" else "2026-09-07" if domain == "date" else "2026-09-07 08:00"
            payload = self.bridge._editable_payload(entity, self.states[entity])
            await self.send(self.command(entity_id=entity, id=str(i), revision=payload["revision"], value=value, service="delete"))
            self.assertEqual(self.calls[-1][0][0], domain)
            self.assertEqual(self.publishes[-2][1]["status"], "ok")
            self.assertIn(self.calls[-1][0][1], {"set_value", "set_datetime", "select_option"})

    async def test_real_history_uses_recorder_graph_activity_and_correlation(self):
        async def executor(function): return function()
        recorder = types.SimpleNamespace(async_add_executor_job=executor, entity_filter=lambda entity: True)
        self.scope.update(get_instance=lambda hass: recorder, state_changes_during_period=lambda *args, **kw: {}, get_last_state_changes=None)
        self.scope["fetch_bounded_state_history"] = lambda *args, **kw: ([{"state": "0", "last_changed": NOW - timedelta(hours=23)}], True, NOW)
        await self.scope["_async_handle_editable_history"](self.bridge, {"entity_id": "number.desk", "version": 1, "hours": 24, "max_transitions": 96, "request_id": "history-1"})
        response = self.publishes[-1][1]
        self.assertEqual(response["request_id"], "history-1")
        self.assertEqual(response["kind"], "number")
        self.assertEqual(len(response["values"]), 288)
        self.assertIn("activity", response)
        recorder.entity_filter = lambda entity: False
        await self.scope["_async_handle_editable_history"](self.bridge, {"entity_id": "number.desk", "version": 1})
        self.assertFalse(self.publishes[-1][1]["history_available"])
        self.assertTrue(all(v is None for v in self.publishes[-1][1]["values"]))


    async def test_datetime_history_uses_same_local_time_as_control(self):
        self.bridge.datetimes = ["datetime.start"]
        self.bridge.hass.config.time_zone = "Europe/Berlin"
        self.states["datetime.start"] = types.SimpleNamespace(state="2026-09-06T06:00:00+00:00", attributes={}, last_changed=NOW - timedelta(hours=1))
        async def executor(function): return function()
        recorder = types.SimpleNamespace(async_add_executor_job=executor, entity_filter=lambda entity: True)
        self.scope.update(get_instance=lambda hass: recorder, state_changes_during_period=lambda *args, **kw: {}, get_last_state_changes=None)
        self.scope["fetch_bounded_state_history"] = lambda *args, **kw: ([{"state": "2026-09-06T06:00:00+00:00", "last_changed": NOW - timedelta(hours=1)}], True, NOW)
        await self.scope["_async_handle_editable_history"](self.bridge, {"entity_id": "datetime.start", "version": 1, "hours": 24})
        response = self.publishes[-1][1]
        expected = "2026-09-06 08:00:00"
        self.assertEqual(response["current"], expected)
        self.assertTrue(response["activity"])
        self.assertTrue(all(item["state"] == expected for item in response["activity"]))
