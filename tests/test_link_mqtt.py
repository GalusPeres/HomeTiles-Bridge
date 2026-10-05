"""link_mqtt.py: the integration's MQTT calls with and without linked panels."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from unittest import mock

from test_link_protocol import PACKAGE, load_link_module

BROKER = load_link_module("link_broker")


class FakeMqtt(types.ModuleType):
    def __init__(self):
        super().__init__("homeassistant.components.mqtt")
        self.published = []
        self.subscribed = []
        self.connected = True

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscribed.append((topic, encoding))
        return lambda: self.subscribed.remove((topic, encoding))

    def is_connected(self, hass):
        return self.connected


def load_shim(fake):
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    components.mqtt = fake
    stubs = {"homeassistant": ha, "homeassistant.components": components,
             "homeassistant.components.mqtt": fake}
    sys.modules.pop(f"{PACKAGE}.link_mqtt", None)
    with mock.patch.dict(sys.modules, stubs):
        module = importlib.import_module(f"{PACKAGE}.link_mqtt")
    sys.modules.pop(f"{PACKAGE}.link_mqtt", None)
    return module


class Hass:
    def __init__(self, components=("mqtt",), link=True, mqtt_entries=None):
        self.config = types.SimpleNamespace(components=set(components))
        self.data = {"tab5_lvgl": {}}
        self.broker = BROKER.LinkBroker() if link else None
        if link:
            self.data["tab5_lvgl"]["link"] = types.SimpleNamespace(broker=self.broker)
        if mqtt_entries is not None:
            self.data["tab5_lvgl"]["mqtt_entries"] = set(mqtt_entries)
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task


class Session:
    def __init__(self, base="hometiles"):
        self.device_id = "A1"
        self.base = base
        self.ha_prefix = "ha/statestream"
        self.mode = "session"
        self.subscriptions = set()
        self.sent = []

    def send_publish(self, topic, payload, retain):
        self.sent.append((topic, payload, retain))


class LinkMqttTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeMqtt()
        self.mqtt = load_shim(self.fake)

    async def test_link_only_publishes_never_reach_mqtt(self):
        hass = Hass(mqtt_entries=[])
        session = Session()
        hass.broker.attach(session)
        hass.broker.panel_subscribe(session, "hometiles/cmnd/display_brightness")
        await self.mqtt.async_publish(hass, "hometiles/cmnd/display_brightness", "40", qos=0, retain=False)
        self.assertEqual(session.sent, [("hometiles/cmnd/display_brightness", b"40", False)])
        self.assertEqual(self.fake.published, [])
        self.assertTrue(self.mqtt.is_connected(hass))
        self.fake.connected = False
        self.assertTrue(self.mqtt.is_connected(hass))

    async def test_mqtt_panels_still_get_every_publish(self):
        hass = Hass(mqtt_entries=["entry"])
        await self.mqtt.async_publish(hass, "ha/statestream/light/desk/state", b"on", 0, True)
        self.assertEqual(self.fake.published, [("ha/statestream/light/desk/state", b"on", True)])
        self.assertEqual(hass.broker.retained("ha/statestream/light/desk/state"), b"on")
        self.fake.connected = False
        self.assertFalse(self.mqtt.is_connected(hass))

    async def test_without_the_mqtt_integration_only_the_link_is_used(self):
        hass = Hass(components=(), mqtt_entries=["entry"])
        await self.mqtt.async_publish(hass, "hometiles/cmnd/light", "x")
        unsubscribe = await self.mqtt.async_subscribe(hass, "hometiles/stat/connected", lambda msg: None)
        self.assertEqual((self.fake.published, self.fake.subscribed), ([], []))
        unsubscribe()

    async def test_unknown_setup_behaves_like_before(self):
        hass = Hass(link=False)
        await self.mqtt.async_publish(hass, "a/b", "x", retain=True)
        unsubscribe = await self.mqtt.async_subscribe(hass, "a/b", lambda msg: None, encoding=None)
        self.assertEqual(self.fake.published, [("a/b", "x", True)])
        self.assertEqual(self.fake.subscribed, [("a/b", None)])
        unsubscribe()
        self.assertEqual(self.fake.subscribed, [])

    async def test_subscriptions_decode_flag_retained_and_run_coroutines(self):
        hass = Hass(mqtt_entries=[])
        session = Session()
        hass.broker.attach(session)
        hass.broker.panel_publish(session, "hometiles/stat/connected", b"1", True)
        received = []

        async def handler(msg):
            received.append(("async", msg.topic, msg.payload, msg.retain))

        unsubscribe = await self.mqtt.async_subscribe(hass, "hometiles/stat/+", handler)
        raw = []
        await self.mqtt.async_subscribe(hass, "hometiles/stat/image", lambda msg: raw.append(msg.payload),
                                        encoding=None)
        hass.broker.panel_publish(session, "hometiles/stat/image", b"\xff\xd8", False)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(received, [("async", "hometiles/stat/connected", "1", True),
                                    ("async", "hometiles/stat/image", "��", False)])
        self.assertEqual(raw, [b"\xff\xd8"])
        self.assertEqual(self.fake.subscribed, [("hometiles/stat/+", "utf-8"), ("hometiles/stat/image", None)])
        unsubscribe()
        hass.broker.panel_publish(session, "hometiles/stat/connected", b"0", True)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(received), 2)


if __name__ == "__main__":
    unittest.main()
