"""Streams from the Bridge to a panel over the direct link.

docs-dev/bridge-link.md: a panel announces in its session hello ("rx") the
largest message it takes as a stream. Messages above the normal publish limit
then go out as begin, data (8 KiB) and end frames, with other frames between
the pieces; a panel without the announcement gets none.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

from test_link_protocol import NEEDS_CRYPTOGRAPHY, load_link_module
from test_link_server import DEVICE_ID, KEY, PREFIX, PanelClient, entry, settle
from test_pairing import BASE

LINK = load_link_module("link_protocol")
SERVER = load_link_module("link_server")
RUNTIME = load_link_module("link_runtime")
CC = load_link_module("command_channel")


class HelloRxTest(unittest.TestCase):
    def test_rx_is_read_only_within_the_stream_range(self):
        def hello(rx):
            data = {"v": 1, "id": DEVICE_ID, "base": BASE, "mode": "session",
                    "kid": "0" * 16, "n": "1" * 32}
            if rx is not None:
                data["rx"] = rx
            return LINK.parse_hello(json.dumps(data).encode())

        self.assertEqual(hello(None)["rx"], 0)
        self.assertEqual(hello(524288)["rx"], 524288)
        self.assertEqual(hello(LINK.MAX_STREAM)["rx"], LINK.MAX_STREAM)
        for bad in (LINK.MAX_PAYLOAD, LINK.MAX_STREAM + 1, -1, "524288", True, 1.5):
            self.assertEqual(hello(bad)["rx"], 0, bad)
        pair = LINK.parse_hello(json.dumps({"v": 1, "id": DEVICE_ID, "base": BASE, "mode": "pair",
                                            "rx": 524288}).encode())
        self.assertNotIn("rx", pair)
        built = json.loads(LINK.build_hello(DEVICE_ID, BASE, "session", "0" * 16, bytes(16), rx=300000))
        self.assertEqual(built["rx"], 300000)


class _Writer:
    """Collects what a connection writes; frames stay plain without keys."""

    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        pass

    def frames(self):
        frames, offset = [], 0
        while offset < len(self.data):
            length = LINK.parse_frame_length(bytes(self.data[offset:offset + 4]))
            frames.append(LINK.split_body(bytes(self.data[offset + 4:offset + 4 + length])))
            offset += 4 + length
        return frames


class _Server:
    def log_due(self, _key):
        return True


class ConnectionStreamTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, connection, writer):
        connection.start_writer()
        for _ in range(400):
            await asyncio.sleep(0)
            if connection._queue.empty() and not connection._streams:
                break
        await settle()
        connection.close()
        await settle()
        return writer.frames()

    async def test_newer_picture_replaces_a_waiting_one(self):
        writer = _Writer()
        connection = SERVER._Connection(_Server(), writer, "test")
        connection.rx_max = 524288
        old, new = b"o" * 70000, b"n" * 90000
        connection.send_publish("a/image/480x480", old, True)
        connection.send_publish("a/image/480x480", new, True)
        frames = await self._run(connection, writer)
        begins = [LINK.parse_stream_begin(p) for t, p in frames if t == LINK.TYPE_STREAM_BEGIN]
        self.assertEqual(begins, [("a/image/480x480", len(new), True)])
        data = b"".join(p for t, p in frames if t == LINK.TYPE_STREAM_DATA)
        self.assertEqual(data, new)
        self.assertEqual(connection._queued_bytes, 0)

    async def test_without_room_large_messages_are_dropped(self):
        writer = _Writer()
        connection = SERVER._Connection(_Server(), writer, "test")
        connection.send_publish("a/image/480x480", b"x" * 70000, True)
        connection.send_publish("a/state", b"on", False)
        frames = await self._run(connection, writer)
        self.assertEqual([t for t, _ in frames], [LINK.TYPE_PUBLISH])
        self.assertEqual(LINK.parse_publish(frames[0][1]), ("a/state", b"on", False))

    async def test_messages_pass_between_the_pieces(self):
        writer = _Writer()
        connection = SERVER._Connection(_Server(), writer, "test")
        connection.rx_max = LINK.MAX_STREAM
        connection.send_publish("a/image/1280x800", bytes(range(256)) * 4096, True)
        connection.start_writer()
        for _ in range(5):
            await asyncio.sleep(0)
        connection.send_publish("a/state", b"on", False)
        frames = await self._run(connection, writer)
        types = [t for t, _ in frames]
        self.assertEqual(types[0], LINK.TYPE_STREAM_BEGIN)
        self.assertEqual(types[-1], LINK.TYPE_STREAM_END)
        self.assertIn(LINK.TYPE_PUBLISH, types)
        self.assertLess(types.index(LINK.TYPE_PUBLISH), len(types) - 1, "the state passed before the end")
        chunks = [p for t, p in frames if t == LINK.TYPE_STREAM_DATA]
        self.assertTrue(all(len(c) <= LINK.MAX_STREAM_CHUNK for c in chunks))
        self.assertEqual(b"".join(chunks), bytes(range(256)) * 4096)


@NEEDS_CRYPTOGRAPHY
class SealedStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entries = [entry()]
        self.runtime = RUNTIME.LinkRuntime(lambda: self.entries)
        self.port = await self.runtime.server.async_start("127.0.0.1", [0])
        self.broker = self.runtime.broker
        self.client = None

    async def asyncTearDown(self):
        if self.client is not None:
            self.client.close()
        await self.runtime.async_stop()

    async def _session(self, rx):
        client = PanelClient(self.port)
        self.client = client
        await client.open()
        nonce = os.urandom(16)
        hello = LINK.build_hello(DEVICE_ID, BASE, LINK.MODE_SESSION, CC.key_id_for_key(KEY), nonce, rx=rx)
        client.writer.write(LINK.plain_frame(LINK.TYPE_HELLO, hello))
        await client.writer.drain()
        frame_type, payload = await client._read()
        self.assertEqual(frame_type, LINK.TYPE_WELCOME)
        answer = json.loads(payload)
        panel_key, bridge_key = LINK.session_keys(KEY, DEVICE_ID, BASE, nonce, bytes.fromhex(answer["n"]))
        client.sealer = LINK.Sealer(panel_key)
        client.opener = LINK.Opener(bridge_key)
        await client.send(LINK.TYPE_READY)
        ready, _ = await client._read()
        self.assertEqual(ready, LINK.TYPE_READY)
        await settle()
        return client

    async def test_retained_picture_arrives_sealed_in_pieces(self):
        topic = f"{PREFIX}/media_player/tv/image/480x480"
        picture = os.urandom(200000)
        self.broker.publish(topic, picture, retain=True)
        panel = await self._session(rx=524288)
        await panel.subscribe(topic)
        frame_type, payload = await panel._read()
        self.assertEqual(frame_type, LINK.TYPE_STREAM_BEGIN)
        self.assertEqual(LINK.parse_stream_begin(payload), (topic, len(picture), True))
        data = bytearray()
        while True:
            frame_type, payload = await panel._read()
            if frame_type == LINK.TYPE_STREAM_END:
                break
            self.assertEqual(frame_type, LINK.TYPE_STREAM_DATA)
            data.extend(payload)
        self.assertEqual(bytes(data), picture)
        # The counter order held: the next frame still opens.
        await panel.send(LINK.TYPE_PING)
        self.assertEqual((await panel._read())[0], LINK.TYPE_PONG)


if __name__ == "__main__":
    unittest.main()
