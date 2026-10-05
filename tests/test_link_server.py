"""Direct link server, broker and setup pairing over real sockets."""

from __future__ import annotations

import asyncio
import json
import os
import types
import unittest

from test_link_protocol import NEEDS_CRYPTOGRAPHY, load_link_module
from test_pairing import BASE, Panel

LINK = load_link_module("link_protocol")
BROKER = load_link_module("link_broker")
SERVER = load_link_module("link_server")
RUNTIME = load_link_module("link_runtime")
CC = load_link_module("command_channel")

DEVICE_ID = "A1B2C3D4E5F6"
PREFIX = "ha/statestream"
KEY = bytes([0x5A]) * 32


def entry(device_id=DEVICE_ID, base=BASE, key=KEY, prefix=PREFIX):
    data = {"device_id": device_id, "base_topic": base, "ha_prefix": prefix, "transport": "link"}
    if key is not None:
        data = CC.with_pairing_key(data, key)
    return types.SimpleNamespace(data=data, options={}, entry_id=f"entry-{device_id}")


class PanelClient:
    """A panel written from docs-dev/bridge-link.md, on a real socket."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader = None
        self.writer = None
        self.sealer = None
        self.opener = None

    async def open(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)

    async def _read(self, timeout=2.0):
        header = await asyncio.wait_for(self.reader.readexactly(4), timeout)
        length = LINK.parse_frame_length(header)
        body = await asyncio.wait_for(self.reader.readexactly(length), timeout)
        if self.opener is not None:
            return self.opener.open(header, body)
        return LINK.split_body(body)

    async def hello(self, mode, device_id=DEVICE_ID, base=BASE, key=None, kid=None):
        await self.open()
        nonce = os.urandom(16)
        self.writer.write(LINK.plain_frame(LINK.TYPE_HELLO, LINK.build_hello(device_id, base, mode, kid, nonce)))
        await self.writer.drain()
        frame_type, payload = await self._read()
        answer = json.loads(payload)
        if frame_type != LINK.TYPE_WELCOME:
            return frame_type, answer
        if mode == LINK.MODE_SESSION:
            panel_key, bridge_key = LINK.session_keys(key, device_id, base, nonce, bytes.fromhex(answer["n"]))
            self.sealer = LINK.Sealer(panel_key)
            self.opener = LINK.Opener(bridge_key)
            self.writer.write(self.sealer.frame(LINK.TYPE_READY))
            await self.writer.drain()
            ready_type, _ = await self._read()
            assert ready_type == LINK.TYPE_READY
        return frame_type, answer

    async def send(self, frame_type, payload=b""):
        frame = self.sealer.frame(frame_type, payload) if self.sealer else LINK.plain_frame(frame_type, payload)
        self.writer.write(frame)
        await self.writer.drain()

    async def publish(self, topic, payload, retain=False):
        await self.send(LINK.TYPE_PUBLISH, LINK.publish_payload(topic, payload, retain))

    async def subscribe(self, topic):
        await self.send(LINK.TYPE_SUBSCRIBE, topic.encode())

    async def receive_publish(self, timeout=2.0):
        frame_type, payload = await self._read(timeout)
        assert frame_type == LINK.TYPE_PUBLISH, frame_type
        return LINK.parse_publish(payload)

    async def closed(self, timeout=2.0):
        try:
            data = await asyncio.wait_for(self.reader.read(), timeout)
        except (ConnectionError, OSError):
            return True
        return data == b""

    def close(self):
        if self.writer is not None:
            self.writer.close()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


@NEEDS_CRYPTOGRAPHY
class LinkServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entries = [entry()]
        self.runtime = RUNTIME.LinkRuntime(lambda: self.entries)
        self.port = await self.runtime.server.async_start("127.0.0.1", [0])
        self.broker = self.runtime.broker
        self.received = []
        self.broker.subscribe("#", lambda topic, payload, retained: self.received.append((topic, payload, retained)))
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        await self.runtime.async_stop()

    def client(self):
        client = PanelClient(self.port)
        self.clients.append(client)
        return client

    async def session(self, **kwargs):
        client = self.client()
        frame_type, _ = await client.hello(LINK.MODE_SESSION, key=KEY, kid=CC.key_id_for_key(KEY), **kwargs)
        self.assertEqual(frame_type, LINK.TYPE_WELCOME)
        await settle()
        return client

    async def test_session_publish_subscribe_retained_and_last_will(self):
        self.broker.publish(f"{PREFIX}/light/desk/state", b"on", retain=True)
        panel = await self.session()
        self.assertTrue(self.runtime.server.connected(DEVICE_ID))
        await panel.subscribe(f"{PREFIX}/light/desk/state")
        self.assertEqual(await panel.receive_publish(), (f"{PREFIX}/light/desk/state", b"on", True))
        await settle()
        self.broker.publish(f"{PREFIX}/light/desk/state", b"off", retain=True)
        self.assertEqual(await panel.receive_publish(), (f"{PREFIX}/light/desk/state", b"off", False))
        # Not subscribed: nothing arrives for another topic.
        self.broker.publish(f"{PREFIX}/light/hall/state", b"on")

        await panel.publish(f"{BASE}/stat/connected", b"1", retain=True)
        await panel.publish(f"{BASE}/cmnd/light", b'{"entity_id":"light.desk"}')
        await settle()
        self.assertIn((f"{BASE}/stat/connected", b"1", False), self.received)
        self.assertIn((f"{BASE}/cmnd/light", b'{"entity_id":"light.desk"}', False), self.received)
        self.assertEqual(self.broker.retained(f"{BASE}/stat/connected"), b"1")

        panel.close()
        for _ in range(50):
            if not self.runtime.server.connected(DEVICE_ID):
                break
            await asyncio.sleep(0.02)
        await settle()
        self.assertFalse(self.runtime.server.connected(DEVICE_ID))
        self.assertEqual(self.received[-1], (f"{BASE}/stat/connected", b"0", False))
        self.assertEqual(self.broker.retained(f"{BASE}/stat/connected"), b"0")

    async def test_panel_cannot_use_foreign_topics(self):
        panel = await self.session()
        await panel.publish("other/cmnd/light", b"x")
        await panel.publish("homeassistant/sensor/x/config", b"")
        await panel.publish(f"tab5_lvgl/config/{DEVICE_ID}/bridge", b"{}", retain=True)
        await panel.publish("tab5_lvgl/config/OTHER/bridge", b"{}")
        await panel.subscribe("other/stat/camera")
        await settle()
        topics = [topic for topic, _payload, _retained in self.received]
        self.assertEqual(topics, [f"tab5_lvgl/config/{DEVICE_ID}/bridge"])
        self.broker.publish("other/stat/camera", b"token")
        await panel.send(LINK.TYPE_PING)
        frame_type, _ = await panel._read()
        self.assertEqual(frame_type, LINK.TYPE_PONG)

    async def test_streamed_publish_is_reassembled(self):
        panel = await self.session()
        data = bytes(range(256)) * 80
        await panel.send(LINK.TYPE_STREAM_BEGIN, LINK.stream_begin_payload(f"{BASE}/stat/local_camera/image", len(data)))
        for offset in range(0, len(data), LINK.MAX_STREAM_CHUNK):
            await panel.send(LINK.TYPE_STREAM_DATA, data[offset:offset + LINK.MAX_STREAM_CHUNK])
        await panel.send(LINK.TYPE_STREAM_END)
        await settle()
        self.assertEqual(self.received[-1], (f"{BASE}/stat/local_camera/image", data, False))

    async def test_stream_overrun_closes_the_connection(self):
        panel = await self.session()
        await panel.send(LINK.TYPE_STREAM_BEGIN, LINK.stream_begin_payload(f"{BASE}/x", 4))
        await panel.send(LINK.TYPE_STREAM_DATA, b"12345")
        self.assertTrue(await panel.closed())

    async def test_unknown_key_wrong_base_and_bad_hello_are_refused(self):
        client = self.client()
        frame_type, answer = await client.hello(LINK.MODE_SESSION, key=KEY, kid="0" * 16)
        self.assertEqual((frame_type, answer), (LINK.TYPE_REFUSE, {"v": 1, "r": "unknown"}))
        client = self.client()
        frame_type, answer = await client.hello(LINK.MODE_SESSION, base="hometiles/other", key=KEY,
                                                kid=CC.key_id_for_key(KEY))
        self.assertEqual(answer["r"], "base")
        client = self.client()
        await client.open()
        client.writer.write(LINK.plain_frame(LINK.TYPE_HELLO, b'{"v":9}'))
        frame_type, payload = await client._read()
        self.assertEqual((frame_type, json.loads(payload)["r"]), (LINK.TYPE_REFUSE, "version"))
        self.assertFalse(self.runtime.server.connected(DEVICE_ID))

    async def test_wrong_key_never_becomes_a_session(self):
        client = self.client()
        await client.open()
        nonce = os.urandom(16)
        client.writer.write(LINK.plain_frame(LINK.TYPE_HELLO, LINK.build_hello(
            DEVICE_ID, BASE, LINK.MODE_SESSION, CC.key_id_for_key(KEY), nonce)))
        frame_type, payload = await client._read()
        self.assertEqual(frame_type, LINK.TYPE_WELCOME)
        bridge_nonce = bytes.fromhex(json.loads(payload)["n"])
        wrong_panel_key, _ = LINK.session_keys(bytes(32), DEVICE_ID, BASE, nonce, bridge_nonce)
        client.writer.write(LINK.Sealer(wrong_panel_key).frame(LINK.TYPE_READY))
        await client.reader.readexactly(4 + 17)  # The Bridge's ready.
        self.assertTrue(await client.closed())
        self.assertFalse(self.runtime.server.connected(DEVICE_ID))
        self.assertEqual(self.received, [])

    async def test_newer_connection_replaces_the_older_one(self):
        first = await self.session()
        second = await self.session()
        self.assertTrue(await first.closed())
        # The older socket must not publish the last will of the newer session.
        self.assertNotIn((f"{BASE}/stat/connected", b"0", False), self.received)
        await second.publish(f"{BASE}/stat/connected", b"1")
        await settle()
        self.assertEqual(self.received[-1], (f"{BASE}/stat/connected", b"1", False))

    async def test_pair_mode_only_carries_pairing_topics(self):
        client = self.client()
        frame_type, answer = await client.hello(LINK.MODE_PAIR)
        self.assertEqual((frame_type, answer), (LINK.TYPE_WELCOME, {"v": 1}))
        await settle()
        self.assertFalse(self.runtime.server.connected(DEVICE_ID))
        await client.subscribe(f"{BASE}/pair/bridge")
        await client.subscribe(f"{PREFIX}/light/desk/state")
        await client.publish(f"{BASE}/cmnd/light", b"x")
        await client.publish(f"{BASE}/pair/panel", b"{}", retain=True)
        await settle()
        self.assertEqual(self.received, [(f"{BASE}/pair/panel", b"{}", False)])
        self.broker.publish(f"{PREFIX}/light/desk/state", b"on")
        self.broker.publish(f"{BASE}/pair/bridge", b"answer")
        self.assertEqual(await client.receive_publish(), (f"{BASE}/pair/bridge", b"answer", False))

    async def test_pair_mode_needs_a_waiting_dialog_or_an_entry(self):
        client = self.client()
        frame_type, answer = await client.hello(LINK.MODE_PAIR, device_id="FFFFFFFFFFFF")
        self.assertEqual((frame_type, answer["r"]), (LINK.TYPE_REFUSE, "pair"))


@NEEDS_CRYPTOGRAPHY
class SetupPairingTest(unittest.IsolatedAsyncioTestCase):
    """A setup dialog pairs a new panel and the panel then connects encrypted."""

    async def asyncSetUp(self):
        self.entries = []
        self.runtime = RUNTIME.LinkRuntime(lambda: self.entries)
        self.port = await self.runtime.server.async_start("127.0.0.1", [0])

    async def asyncTearDown(self):
        await self.runtime.async_stop()

    async def test_dialog_pairs_and_the_panel_connects_with_the_key(self):
        pending = self.runtime.begin_setup(DEVICE_ID, BASE, PREFIX)
        panel_client = PanelClient(self.port)
        self.assertEqual((await panel_client.hello(LINK.MODE_PAIR))[0], LINK.TYPE_WELCOME)
        await panel_client.subscribe(f"{BASE}/pair/bridge")
        panel = Panel()
        await panel_client.publish(f"{BASE}/pair/panel", panel.start().encode())
        _topic, commit, _ = await panel_client.receive_publish()
        await panel_client.publish(f"{BASE}/pair/panel", panel.send_nonce().encode())
        _topic, nonce, _ = await panel_client.receive_publish()
        result = panel.finish(json.loads(commit), json.loads(nonce))
        await asyncio.wait_for(pending.prompted.wait(), 2)
        self.assertEqual(pending.number, f"{result['number'][:3]} {result['number'][3:]}")

        self.assertEqual(pending.answer(True), "waiting")
        _topic, confirm, _ = await panel_client.receive_publish()
        self.assertEqual(json.loads(confirm)["m"], result["m_bridge"])
        await panel_client.publish(f"{BASE}/pair/panel", panel.confirm(result["m_panel"]).encode())
        await asyncio.wait_for(pending.finished.wait(), 2)
        self.assertEqual(pending.key, result["key"])
        panel_client.close()

        # The dialog creates the entry with the key; the panel reconnects.
        self.entries.append(entry(key=pending.key))
        pending.close()
        self.assertEqual(self.runtime.pending, {})
        session = PanelClient(self.port)
        frame_type, _ = await session.hello(LINK.MODE_SESSION, key=result["key"],
                                            kid=CC.key_id_for_key(result["key"]))
        self.assertEqual(frame_type, LINK.TYPE_WELCOME)
        await settle()
        self.assertTrue(self.runtime.server.connected(DEVICE_ID))
        session.close()

    async def test_rejected_or_timed_out_dialog_reports_failure(self):
        pending = self.runtime.begin_setup(DEVICE_ID, BASE, PREFIX)
        self.assertEqual(pending.answer(True), "expired")
        panel_client = PanelClient(self.port)
        await panel_client.hello(LINK.MODE_PAIR)
        await panel_client.subscribe(f"{BASE}/pair/bridge")
        panel = Panel()
        await panel_client.publish(f"{BASE}/pair/panel", panel.start().encode())
        await panel_client.receive_publish()
        await panel_client.publish(f"{BASE}/pair/panel", panel.send_nonce().encode())
        await panel_client.receive_publish()
        await asyncio.wait_for(pending.prompted.wait(), 2)
        self.assertEqual(pending.answer(False), "rejected")
        _topic, abort, _ = await panel_client.receive_publish()
        self.assertEqual(json.loads(abort)["r"], "rejected")
        self.assertTrue(pending.finished.is_set())
        self.assertIsNone(pending.key)
        self.assertEqual(pending.failure, "rejected")
        panel_client.close()
        pending.close()

    async def test_a_new_dialog_replaces_the_old_one(self):
        first = self.runtime.begin_setup(DEVICE_ID, BASE, PREFIX)
        second = self.runtime.begin_setup(DEVICE_ID, BASE, PREFIX)
        self.assertIs(self.runtime.pending[DEVICE_ID], second)
        first.close()  # Closing the replaced dialog keeps the new one.
        self.assertIs(self.runtime.pending[DEVICE_ID], second)
        self.assertEqual(self.runtime.accept_pairing(DEVICE_ID, BASE), PREFIX)
        self.assertIsNone(self.runtime.accept_pairing(DEVICE_ID, "hometiles/other"))
        second.close()
        self.assertIsNone(self.runtime.accept_pairing(DEVICE_ID, BASE))


if __name__ == "__main__":
    unittest.main()
