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


class Entries:
    def __init__(self, entries):
        self.entries = entries

    def async_entries(self, domain):
        assert domain == "tab5_lvgl"
        return self.entries


def config_entry(base, device_id, transport=None):
    data = {"base_topic": base, "device_id": device_id}
    if transport:
        data["transport"] = transport
    return types.SimpleNamespace(data=data, options={})


class Hass:
    def __init__(self, components=("mqtt",), link=True, mqtt_entries=None, entries=()):
        self.config_entries = Entries(list(entries))
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

    async def test_linked_panel_never_sees_its_old_mqtt_retained_messages(self):
        # The V2 left MQTT with "0" retained on its connected topic. On the
        # link that stale "0" overrode the live "1" and made the view select
        # and the panel camera unavailable.
        hass = Hass(mqtt_entries=["s3"], entries=[
            config_entry("hometiles_v2", "V2ID", transport="link"),
            config_entry("hometiles_s3", "S3ID"),
        ])
        session = Session(base="hometiles_v2")
        session.device_id = "V2ID"
        hass.broker.attach(session)
        hass.broker.panel_publish(session, "hometiles_v2/stat/connected", b"1", True)
        seen = []
        await self.mqtt.async_subscribe(hass, "hometiles_v2/stat/connected", lambda msg: seen.append(msg.payload))
        await self.mqtt.async_subscribe(hass, "hometiles_s3/stat/connected", lambda msg: None)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(seen, ["1"])
        self.assertEqual(self.fake.subscribed, [("hometiles_s3/stat/connected", "utf-8")])

        # Commands for the linked panel stay on the link; others and the
        # shared states still reach the MQTT panels.
        hass.broker.panel_subscribe(session, "hometiles_v2/cmnd/light")
        await self.mqtt.async_publish(hass, "hometiles_v2/cmnd/light", "x")
        await self.mqtt.async_publish(hass, "tab5_lvgl/config/V2ID/bridge/apply", "{}", 0, True)
        await self.mqtt.async_publish(hass, "hometiles_s3/cmnd/light", "y")
        await self.mqtt.async_publish(hass, "ha/statestream/light/desk/state", "on", 0, True)
        self.assertEqual([topic for topic, _payload, _retain in self.fake.published],
                         ["hometiles_s3/cmnd/light", "ha/statestream/light/desk/state"])
        self.assertEqual(session.sent, [("hometiles_v2/cmnd/light", b"x", False)])

    async def test_wildcards_drop_mqtt_messages_of_linked_panels(self):
        hass = Hass(mqtt_entries=["s3"], entries=[
            config_entry("hometiles_v2", "V2ID", transport="link"),
            config_entry("hometiles_s3", "S3ID"),
        ])
        stored = {}

        async def subscribe(hass_, topic, handler, qos=0, encoding="utf-8"):
            stored[topic] = handler
            return lambda: None

        self.fake.async_subscribe = subscribe
        seen = []

        async def handler(msg):
            seen.append(msg.topic)

        await self.mqtt.async_subscribe(hass, "tab5_lvgl/config/+/bridge", handler)
        mqtt_handler = stored["tab5_lvgl/config/+/bridge"]
        await mqtt_handler(types.SimpleNamespace(topic="tab5_lvgl/config/V2ID/bridge", payload="{}"))
        await mqtt_handler(types.SimpleNamespace(topic="tab5_lvgl/config/S3ID/bridge", payload="{}"))
        self.assertEqual(seen, ["tab5_lvgl/config/S3ID/bridge"])

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

    async def test_a_linked_panel_loses_its_old_mqtt_announcement_only_on_mqtt(self):
        topic = "tab5_lvgl/config/8AF1E60AF6E8/bridge"
        hass = Hass(mqtt_entries=[], entries=[config_entry("hometiles_f6e8", "8AF1E60AF6E8", "link")])
        hass.broker.publish(topic, b'{"device_id":"8AF1E60AF6E8"}', True)
        await self.mqtt.async_clear_mqtt_announcement(hass, "8AF1E60AF6E8")
        # Empty and retained deletes it on the broker, although the topic
        # belongs to a linked panel; the link keeps the current announcement.
        self.assertEqual(self.fake.published, [(topic, "", True)])
        self.assertEqual(hass.broker.retained(topic), b'{"device_id":"8AF1E60AF6E8"}')

    async def test_clearing_the_announcement_waits_for_mqtt_and_never_fails(self):
        topic = "tab5_lvgl/config/8AF1E60AF6E8/bridge"
        await self.mqtt.async_clear_mqtt_announcement(Hass(components=()), "8AF1E60AF6E8")
        await self.mqtt.async_clear_mqtt_announcement(Hass(), "")
        self.assertEqual(self.fake.published, [])
        waits = []

        async def wait_for_mqtt(hass):
            waits.append(hass)
            return False

        self.fake.async_wait_for_mqtt_client = wait_for_mqtt
        await self.mqtt.async_clear_mqtt_announcement(Hass(), "8AF1E60AF6E8")
        self.assertEqual((len(waits), self.fake.published), (1, []))

        async def ready(hass):
            return True

        async def broken(*args, **kwargs):
            raise RuntimeError("MQTT is not connected")

        self.fake.async_wait_for_mqtt_client = ready
        await self.mqtt.async_clear_mqtt_announcement(Hass(), "8AF1E60AF6E8")
        self.assertEqual(self.fake.published, [(topic, "", True)])
        self.fake.async_publish = broken
        await self.mqtt.async_clear_mqtt_announcement(Hass(), "8AF1E60AF6E8")


if __name__ == "__main__":
    unittest.main()
