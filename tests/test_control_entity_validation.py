"""Light, climate, media and camera commands must name a configured entity."""

from __future__ import annotations

import ast
import json
import logging
import types
import unittest

from test_view_navigation import ROOT, load_module

CONTROL = load_module("control_helpers")
CLIMATE = load_module("climate_helpers")

MODULE_FUNCTIONS = {
    "_try_parse_json", "_normalise_command", "_normalise_media_command",
    "_parse_simple_command", "_extract_light_service_data", "_coerce_float",
    "_coerce_bool",
}
MODULE_CONSTANTS = {"LIGHT_SERVICE_FIELDS", "MEDIA_COMMAND_ALIASES"}
HANDLERS = {
    "_resolve_target_entity", "_async_handle_light_command",
    "_async_handle_climate_command", "_async_handle_media_command",
    "_async_handle_camera_command",
}


def bridge_methods(scope):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HANDLERS
    ]
    for node in nodes:
        node.decorator_list = []
    for node in tree.body:
        if getattr(node, "name", None) in MODULE_FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in MODULE_CONSTANTS for target in node.targets
        ):
            nodes.insert(0, node)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[],
    )
    namespace = dict(scope)
    exec(compile(ast.fix_missing_locations(module), "bridge-control-validation", "exec"), namespace)
    return namespace


class ControlEntityValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.published = []
        self.sessions = []
        climate_state = types.SimpleNamespace(
            state="off", attributes={"hvac_modes": ["off", "heat"]}
        )
        # Both climates support the mode, so only the allow-list can reject one.
        self.states = {"climate.living_room": climate_state, "climate.bedroom": climate_state}

        async def call(*args, **kwargs):
            self.calls.append(args)

        async def publish(_hass, topic, payload, **_kwargs):
            self.published.append((topic, json.loads(payload)))

        async def create_session(*args):
            self.sessions.append(args)
            raise AssertionError("an unconfigured camera must not open a stream")

        scope = {
            **vars(CONTROL),
            **vars(CLIMATE),
            "json": json,
            "_LOGGER": logging.getLogger(__name__),
            "mqtt": types.SimpleNamespace(async_publish=publish),
            "DOMAIN": "tab5_lvgl",
            "CAMERA_BRIDGE_PROTOCOL_VERSION": 1,
            "CAMERA_STREAM_TRANSPORT": "tcp-ack-v1",
            "CAMERA_STREAM_WIDTH": 752,
            "CAMERA_STREAM_HEIGHT": 424,
            "CAMERA_STREAM_FPS": 24,
        }
        self.methods = bridge_methods(scope)
        manager = types.SimpleNamespace(async_create_session=create_session)
        self.bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(
                states=types.SimpleNamespace(get=self.states.get),
                services=types.SimpleNamespace(async_call=call),
                data={"tab5_lvgl": {"camera_stream_manager": manager}},
            ),
            lights=["light.kitchen"],
            climates=["climate.living_room"],
            media_players=["media_player.speaker"],
            cameras=["camera.door"],
            base_topic="hometiles/panel",
            device_id="panel",
        )
        self.bridge._resolve_target_entity = (
            lambda entity_id, candidates: self.methods["_resolve_target_entity"](
                self.bridge, entity_id, candidates
            )
        )
        self.bridge._is_local_camera_self_loop = lambda _entity: False

    async def command(self, kind, payload):
        await self.methods[f"_async_handle_{kind}_command"](
            self.bridge, types.SimpleNamespace(payload=payload, retain=False)
        )

    def test_resolver_accepts_only_configured_entities(self):
        resolve = lambda entity, candidates: self.methods["_resolve_target_entity"](
            self.bridge, entity, candidates
        )
        self.assertEqual(resolve(" light.kitchen ", ["light.kitchen"]), "light.kitchen")
        self.assertIsNone(resolve("light.garage", ["light.kitchen"]))
        self.assertIsNone(resolve("lock.front_door", ["light.kitchen"]))
        self.assertIsNone(resolve("kitchen", ["light.kitchen"]))
        # Legacy payloads without an entity keep working for a single target.
        self.assertEqual(resolve(None, ["light.kitchen"]), "light.kitchen")
        self.assertIsNone(resolve(None, ["light.kitchen", "light.hall"]))
        self.assertIsNone(resolve(None, []))

    async def test_configured_entities_are_still_controlled(self):
        await self.command("light", json.dumps({"entity_id": "light.kitchen", "state": "on"}))
        await self.command("light", "light.kitchen toggle")
        await self.command("climate", json.dumps({"entity_id": "climate.living_room", "hvac_mode": "heat"}))
        await self.command("media", json.dumps({"entity_id": "media_player.speaker", "command": "play"}))

        self.assertEqual(
            [(domain, service, data["entity_id"]) for domain, service, data in self.calls],
            [
                ("light", "turn_on", "light.kitchen"),
                ("light", "toggle", "light.kitchen"),
                ("climate", "set_hvac_mode", "climate.living_room"),
                ("media_player", "media_play", "media_player.speaker"),
            ],
        )

    async def test_unconfigured_entities_never_reach_home_assistant(self):
        await self.command("light", json.dumps({"entity_id": "light.garage", "state": "on"}))
        await self.command("light", "lock.front_door on")
        await self.command("climate", json.dumps({"entity_id": "climate.bedroom", "hvac_mode": "heat"}))
        await self.command("media", json.dumps({"entity_id": "media_player.tv", "command": "play"}))

        self.assertEqual(self.calls, [])

    async def test_unconfigured_camera_is_refused_before_a_stream_opens(self):
        await self.command("camera", json.dumps({
            "command": "open", "entity_id": "camera.garden", "transport": "tcp-ack-v1",
        }))

        self.assertEqual(self.sessions, [])
        self.assertEqual(len(self.published), 1)
        topic, payload = self.published[0]
        self.assertEqual(topic, "hometiles/panel/stat/camera")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], "unknown_camera")
        self.assertEqual(payload["entity_id"], "camera.garden")


if __name__ == "__main__":
    unittest.main()
