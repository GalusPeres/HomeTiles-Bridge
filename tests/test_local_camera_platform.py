"""The HA camera platform for a panel's own camera, run against small HA stubs."""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import json
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT, load_module

JPEG = b"\xff\xd8" + b"\x01" * 32 + b"\xff\xd9"
BASE = "hometiles/panel"
READY = {"v": 1, "state": "ready", "width": 1280, "height": 720, "format": "jpeg",
         "max_bytes": 131072, "min_interval_ms": 1000}


class FakeMqtt(types.ModuleType):
    def __init__(self):
        super().__init__("homeassistant.components.mqtt")
        self.subscriptions = {}
        self.published = []
        self.connected = True
        self.unsubscribed = 0
        self.ReceiveMessage = types.SimpleNamespace

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscriptions[topic] = (handler, encoding)

        def unsubscribe():
            self.unsubscribed += 1
        return unsubscribe

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))

    def is_connected(self, hass):
        return self.connected


class FakeCamera:
    def __init__(self):
        self.content_type = "image/jpeg"
        self.written = 0

    @property
    def available(self):
        return self._attr_available

    def async_write_ha_state(self):
        self.written += 1

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


def load_camera_module(fake_mqtt):
    package_name = "_hometiles_camera_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    components.mqtt = fake_mqtt
    camera = types.ModuleType("homeassistant.components.camera")
    camera.Camera = FakeCamera
    camera.CameraEntityFeature = int
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    stubs = {
        package_name: package,
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.mqtt": fake_mqtt,
        "homeassistant.components.camera": camera,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.camera")


def entry(capabilities=None, **data):
    values = {"device_id": "mac", "base_topic": BASE, "model": "guition_jc8012p4a1_v2"}
    values.update(data)
    if capabilities is not None:
        values["capabilities"] = capabilities
    return types.SimpleNamespace(entry_id="e1", data=values, options={})


def message(topic, payload, retain=False):
    return types.SimpleNamespace(topic=topic, payload=payload, retain=retain)


class LocalCameraPlatformTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.module = load_camera_module(self.mqtt)

    async def created(self, config_entry):
        result = []
        await self.module.async_setup_entry(None, config_entry, result.extend)
        return result

    async def test_entity_only_for_announced_capability(self):
        self.assertEqual(await self.created(entry()), [])
        self.assertEqual(await self.created(entry({"view_navigation": True})), [])
        self.assertEqual(await self.created(entry({"local_camera": False})), [])
        # A stale option copy can never enable a camera the panel withdrew.
        stale = entry({"local_camera": False})
        stale.options = {"capabilities": {"local_camera": True}}
        self.assertEqual(await self.created(stale), [])
        [camera] = await self.created(entry({"local_camera": True}))
        self.assertEqual(camera._attr_unique_id, "mac_local_camera")
        self.assertEqual(camera._attr_translation_key, "local_camera")
        self.assertTrue(camera._attr_has_entity_name)
        self.assertEqual(camera._attr_supported_features, 0)
        self.assertEqual(camera._attr_frame_interval, 2.0)
        self.assertEqual(camera.content_type, "image/jpeg")
        self.assertIn(("tab5_lvgl", "mac"), camera._attr_device_info["identifiers"])

    async def start(self):
        [camera] = await self.created(entry({"local_camera": True}))
        camera.hass = object()
        await camera.async_added_to_hass()
        return camera

    async def deliver(self, topic_filter, topic, payload):
        handler, _ = self.mqtt.subscriptions[topic_filter]
        await handler(message(topic, payload))

    async def test_subscriptions_and_availability(self):
        camera = await self.start()
        self.assertEqual(set(self.mqtt.subscriptions), {
            f"{BASE}/stat/local_camera", f"{BASE}/stat/connected",
            f"{BASE}/stat/local_camera/image/+", f"{BASE}/stat/local_camera/error/+"})
        self.assertIsNone(self.mqtt.subscriptions[f"{BASE}/stat/local_camera/image/+"][1])
        self.assertEqual(self.mqtt.subscriptions[f"{BASE}/stat/local_camera"][1], "utf-8")
        self.assertFalse(camera.available)
        self.assertIsNone(await camera.async_camera_image())
        self.assertEqual(self.mqtt.published, [])
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", json.dumps(READY))
        self.assertTrue(camera.available)
        self.assertEqual(camera.extra_state_attributes,
                         {"panel_camera_state": "ready", "width": 1280, "height": 720})
        await self.deliver(f"{BASE}/stat/connected", f"{BASE}/stat/connected", "0")
        self.assertFalse(camera.available)
        await self.deliver(f"{BASE}/stat/connected", f"{BASE}/stat/connected", "1")
        self.assertTrue(camera.available)
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", "{bad")
        self.assertTrue(camera.available)
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera",
                           '{"v":1,"state":"disabled"}')
        self.assertFalse(camera.available)
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", json.dumps(READY))
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", "")
        self.assertFalse(camera.available)
        self.assertIsNone(camera.extra_state_attributes)
        await camera.async_will_remove_from_hass()
        self.assertEqual(self.mqtt.unsubscribed, 4)

    async def test_snapshot_request_round_trip_and_single_flight(self):
        camera = await self.start()
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", json.dumps(READY))
        first = asyncio.create_task(camera.async_camera_image())
        second = asyncio.create_task(camera.async_camera_image(640, 360))
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.mqtt.published), 1)
        topic, request, qos, retain = self.mqtt.published[0]
        self.assertEqual((topic, qos, retain), (f"{BASE}/cmnd/local_camera", 0, False))
        self.assertEqual(set(request), {"v", "id", "op", "max_bytes"})
        self.assertEqual((request["v"], request["op"], request["max_bytes"]), (1, "snapshot", 131072))
        # A foreign id and a non-JPEG answer do not complete the request.
        await self.deliver(f"{BASE}/stat/local_camera/image/+",
                           f"{BASE}/stat/local_camera/image/{'0' * 16}", JPEG)
        self.assertFalse(first.done())
        await self.deliver(f"{BASE}/stat/local_camera/image/+",
                           f"{BASE}/stat/local_camera/image/{request['id']}", JPEG)
        self.assertEqual(await first, JPEG)
        self.assertEqual(await second, JPEG)
        self.assertEqual(await camera.async_camera_image(), JPEG)
        self.assertEqual(len(self.mqtt.published), 1)

    async def test_panel_error_and_offline_release_waiters(self):
        camera = await self.start()
        status = dict(READY, min_interval_ms=0)
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", json.dumps(status))
        task = asyncio.create_task(camera.async_camera_image())
        await asyncio.sleep(0.01)
        request_id = self.mqtt.published[-1][1]["id"]
        with self.assertLogs(self.module._LOGGER, "WARNING") as logs:
            await self.deliver(f"{BASE}/stat/local_camera/error/+",
                               f"{BASE}/stat/local_camera/error/{request_id}",
                               '{"v":1,"error":"encoder_busy"}')
            self.assertIsNone(await task)
        self.assertIn("encoder_busy", logs.output[0])
        camera._snapshots._last_request_at = None
        task = asyncio.create_task(camera.async_camera_image())
        await asyncio.sleep(0.01)
        with self.assertNoLogs(self.module._LOGGER, "WARNING"):
            await self.deliver(f"{BASE}/stat/connected", f"{BASE}/stat/connected", "offline")
            self.assertIsNone(await task)
        self.assertFalse(camera.available)

    async def test_disconnected_broker_sends_nothing(self):
        camera = await self.start()
        await self.deliver(f"{BASE}/stat/local_camera", f"{BASE}/stat/local_camera", json.dumps(READY))
        self.mqtt.connected = False
        self.assertIsNone(await camera.async_camera_image())
        self.assertEqual(self.mqtt.published, [])


def bridge_methods(names, scope):
    """Execute selected Tab5Bridge methods from __init__.py without HA imports."""
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    functions = [node for node in bridge.body if getattr(node, "name", None) in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
    return {name: scope[name] for name in names}


class CameraCommandSelfLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        local_camera = load_module("local_camera")
        self.published = []
        self.sessions = []
        registry_entries = {
            "camera.panel_camera": types.SimpleNamespace(platform="tab5_lvgl", domain="camera",
                config_entry_id="e1", unique_id="mac_local_camera"),
            "camera.kitchen_panel_camera": types.SimpleNamespace(platform="tab5_lvgl", domain="camera",
                config_entry_id="e2", unique_id="kitchen_local_camera"),
        }
        registry = types.SimpleNamespace(async_get=registry_entries.get)

        async def publish(hass, topic, payload, qos=0, retain=False):
            self.published.append((topic, json.loads(payload)))

        scope = {
            "mqtt": types.SimpleNamespace(async_publish=publish),
            "er": types.SimpleNamespace(async_get=lambda hass: registry),
            "json": json, "DOMAIN": "tab5_lvgl", "CAMERA_BRIDGE_PROTOCOL_VERSION": 1,
            "CAMERA_STREAM_TRANSPORT": "tcp-ack-v1", "CAMERA_STREAM_WIDTH": 320,
            "CAMERA_STREAM_HEIGHT": 240, "CAMERA_STREAM_FPS": 5,
            "_try_parse_json": lambda raw: json.loads(raw),
            "_LOGGER": logging.getLogger(__name__),
            "is_local_camera_self_loop": local_camera.is_local_camera_self_loop,
        }
        methods = bridge_methods({"_is_local_camera_self_loop", "_async_handle_camera_command"}, scope)
        manager = types.SimpleNamespace(async_create_session=self.create_session)
        bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(data={"tab5_lvgl": {"camera_stream_manager": manager}}),
            entry=types.SimpleNamespace(entry_id="e1"), base_topic=BASE, device_id="mac",
            cameras=["camera.panel_camera", "camera.kitchen_panel_camera"], _device_ip=None,
            _resolve_target_entity=lambda requested, allowed: requested if requested in allowed else None)
        bridge._is_local_camera_self_loop = lambda entity_id: methods["_is_local_camera_self_loop"](bridge, entity_id)
        self.handle = lambda payload: methods["_async_handle_camera_command"](
            bridge, types.SimpleNamespace(payload=payload))

    async def create_session(self, *args):
        self.sessions.append(args)
        raise ValueError("camera_image_unavailable")

    async def test_own_camera_is_refused_before_any_stream_session(self):
        with self.assertLogs(logging.getLogger(__name__), "WARNING") as logs:
            await self.handle(json.dumps({"command": "open", "entity_id": "camera.panel_camera",
                                          "transport": "tcp-ack-v1"}))
        self.assertIn("own camera", logs.output[0])
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.published, [(f"{BASE}/stat/camera", {
            "status": "error", "entity_id": "camera.panel_camera",
            "error": "camera_self_loop", "protocol_version": 1})])

    async def test_other_panel_camera_still_uses_the_existing_stream_path(self):
        with self.assertLogs(logging.getLogger(__name__), "WARNING"):
            await self.handle(json.dumps({"command": "open", "entity_id": "camera.kitchen_panel_camera",
                                          "transport": "tcp-ack-v1"}))
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.published[-1][1]["error"], "camera_image_unavailable")


if __name__ == "__main__":
    unittest.main()
