"""Exercise configured on/off and momentary actions through real MQTT handlers."""

from __future__ import annotations

import ast
import json
import logging
import types
import unittest

from test_view_navigation import ROOT, load_module

C = load_module("control_helpers")


def methods(names):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(vars(C), json=json, _LOGGER=logging.getLogger(__name__))
    nodes += [node for node in tree.body if getattr(node, "name", None) in {"_parse_simple_command", "_normalise_command", "_try_parse_json"}]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "bridge-controls", "exec"), namespace)
    return namespace


class CompatibleControlsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.states = {}
        async def call(*args, **kwargs): self.calls.append((args, kwargs))
        self.bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(states=types.SimpleNamespace(get=self.states.get), services=types.SimpleNamespace(async_call=call)),
            switches=[], scene_map={},
        )
        self.functions = methods({"_async_handle_switch_command", "_async_handle_scene_command", "_build_state_payload", "async_publish_snapshot", "_async_publish_switch_absent_state", "_handle_state_event"})

    async def command(self, kind, payload, retain=False):
        await self.functions[f"_async_handle_{kind}_command"](self.bridge, types.SimpleNamespace(payload=payload, retain=retain))

    def state(self, entity, value="off", features=63):
        self.states[entity] = types.SimpleNamespace(state=value, attributes={"supported_features": features})

    async def test_every_switch_domain_and_action_uses_its_own_service(self):
        for domain in C.SWITCH_DOMAINS:
            entity = f"{domain}.desk"
            self.bridge.switches = [entity]
            self.state(entity)
            for command, service in [("on", "turn_on"), ("off", "turn_off"), ("toggle", "toggle")]:
                await self.command("switch", json.dumps({"entity_id": entity, "state": command, "service": "delete"}))
                self.assertEqual(self.calls[-1], ((domain, service, {"entity_id": entity}), {"blocking": False}))
            await self.command("switch", "ON")
            self.assertEqual(self.calls[-1][0][0:2], (domain, "turn_on"))

    async def test_every_action_domain_supports_aliases_and_direct_configured_ids(self):
        for domain in C.ACTION_DOMAINS:
            entity = f"{domain}.desk"
            self.bridge.scene_map = {"desk": entity}
            # Never-pressed buttons/scenes have an unknown last-activation time.
            self.state(entity, "unknown")
            for payload in ["DESK", entity]:
                await self.command("scene", payload)
                self.assertEqual(self.calls[-1][0], (domain, C.ACTION_SERVICES[domain], {"entity_id": entity}))

    async def test_unconfigured_invalid_unavailable_and_retained_commands_are_ignored(self):
        self.bridge.switches = ["automation.desk"]
        self.bridge.scene_map = {"desk": "button.desk", "unsafe": "lock.front_door"}
        for entity in ["automation.desk", "automation.other", "button.desk", "button.other", "lock.front_door"]:
            self.state(entity)
        for payload in ['{"entity_id":"automation.other","state":"on"}', '{"entity_id":"automation.desk","state":"trigger"}', '{"entity_id":"light.desk","state":"on"}']:
            await self.command("switch", payload)
        for payload in ["button.other", "unsafe", "lock.front_door", "button.desk.extra", ""]:
            await self.command("scene", payload)
        for value in ["unavailable", "unknown"]:
            self.state("automation.desk", value)
            await self.command("switch", "ON")
        self.state("button.desk", "unavailable")
        await self.command("scene", "desk")
        self.states.clear()
        await self.command("scene", "desk")
        await self.command("switch", "ON")
        self.state("automation.desk")
        self.state("button.desk")
        await self.command("switch", "ON", retain=True)
        await self.command("scene", "desk", retain=True)
        self.assertEqual(self.calls, [])

    def test_all_fan_and_siren_feature_combinations_and_malformed_masks(self):
        for domain, required in C.SWITCH_FEATURES.items():
            entity = f"{domain}.test"
            for features in [*range(64), None, "63", -1, True]:
                expected = isinstance(features, int) and not isinstance(features, bool) and features >= 0 and features & required == required
                self.state(entity, features=features)
                self.assertEqual(C.switch_supported(entity, self.states[entity].attributes), expected)
                for command in ["on", "off", "toggle"]:
                    self.assertEqual(C.build_switch_service_call(entity, command, [entity], self.states[entity]) is not None, expected)

    def test_real_payload_builder_preserves_state_and_disables_unknown_or_unsupported(self):
        for domain in C.SWITCH_DOMAINS:
            entity = f"{domain}.test"
            for value in ["on", "off", "unknown", "unavailable"]:
                self.state(entity, value)
                raw = self.functions["_build_state_payload"](self.bridge, entity, self.states[entity])
                if domain == "switch" and value in ("on", "off", "unavailable"):
                    self.assertEqual(raw, value)
                    continue
                payload = json.loads(raw)
                self.assertEqual(payload["state"], value)
                self.assertEqual(payload["available"], value in ("on", "off"))
        self.assertFalse(C.build_switch_state_payload("fan.test", "on", {"supported_features": 1})["available"])

    def test_aliases_survive_selection_order_collisions_and_removal(self):
        old = {"desk": "scene.desk", "desk2": "script.desk", "custom": "button.desk"}
        selected = ["input_button.desk", "button.desk", "script.desk", "scene.desk"]
        updated = C.build_action_map(selected, {"custom": "button.desk"}, old)
        self.assertEqual(updated, {**old, "desk3": "input_button.desk"})
        self.assertEqual(C.build_action_map(reversed(selected), {"custom": "button.desk"}, updated), updated)
        updated = C.build_action_map(["script.desk"], {}, updated)
        self.assertEqual(updated, {"desk2": "script.desk"})
        self.assertEqual(C.build_action_map([], {}, {}), {})

    def test_real_config_conversion_preserves_existing_aliases_and_round_trips_new_domains(self):
        source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {"_convert_entity_data", "_normalise_entity_list", "_unique", "_split_weather_entities", "_parse_scene_map"}
        nodes = [node for node in tree.body if getattr(node, "name", None) in names]
        namespace = dict(vars(load_module("const")))
        namespace.update(vars(C))
        namespace.update(vars(load_module("editable_helpers")))
        namespace["split_binary_sensor_entities"] = load_module("binary_sensor_helpers").split_binary_sensor_entities
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "control-config", "exec"), namespace)
        convert = namespace["_convert_entity_data"]
        old = {"scene_map": {"desk": "scene.desk", "desk2": "script.desk"}, "device_id": "panel", "capabilities": {"view_navigation": True}}
        selected = {"switches": [f"{domain}.desk" for domain in C.SWITCH_DOMAINS], "scene_entities": ["button.desk", "script.desk", "scene.desk", "input_button.desk"]}
        upgraded = convert(selected, old)
        self.assertEqual(upgraded["scene_map"], {"desk": "scene.desk", "desk2": "script.desk", "desk3": "button.desk", "desk4": "input_button.desk"})
        self.assertEqual(upgraded["switches"], selected["switches"])
        self.assertEqual(upgraded["capabilities"], old["capabilities"])
        self.assertEqual(convert(selected, upgraded), upgraded)
        fresh = convert(selected, {})
        self.assertEqual(set(fresh["scene_map"].values()), set(selected["scene_entities"]))
        self.assertEqual(convert(selected, fresh), fresh)
        legacy = convert({"switches": ["switch.old"], "scene_entities": ["scene.desk", "script.desk"]}, old)
        self.assertEqual(legacy["scene_map"], old["scene_map"])
        self.assertIn("domain=list(SWITCH_DOMAINS)", source)
        self.assertIn("domain=list(ACTION_DOMAINS)", source)
        for filename in ["strings.json", "translations/de.json", "translations/en.json"]:
            step = json.loads((ROOT / filename).read_text(encoding="utf-8"))["options"]["step"]["entities"]
            self.assertIn("Button", step["data"]["scene_entities"])
            self.assertIn("automation", step["description"].lower())

    async def test_missing_switch_replaces_retained_state_at_startup_and_removal(self):
        self.bridge.switches = ["automation.deleted"]
        self.bridge.tracked_entities = self.bridge.switches
        self.bridge.binary_sensors = []
        self.bridge._owns_state_publish = lambda entity: True
        self.bridge._detail_entities = lambda: []
        self.bridge._ha_topic_for_entity = lambda entity, suffix: "ha/statestream/" + entity.replace(".", "/") + "/" + suffix
        published = []
        async def publish(*args, **kwargs): published.append((args, kwargs))
        self.functions["mqtt"] = types.SimpleNamespace(async_publish=publish)
        self.bridge._async_publish_switch_absent_state = types.MethodType(self.functions["_async_publish_switch_absent_state"], self.bridge)
        await self.functions["async_publish_snapshot"](self.bridge)
        self.assertEqual(json.loads(published[0][0][2])["state"], None)
        self.assertFalse(json.loads(published[0][0][2])["available"])
        self.assertTrue(published[0][1]["retain"])
        tasks = []
        self.bridge.hass.async_create_task = tasks.append
        self.functions["_handle_state_event"](self.bridge, types.SimpleNamespace(data={"entity_id": "automation.deleted", "new_state": None}))
        await tasks[0]
        self.assertEqual(published[0], published[1])


if __name__ == "__main__":
    unittest.main()
