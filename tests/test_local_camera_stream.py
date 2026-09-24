"""Live stream from a panel's own camera: wire protocol, upload and viewers."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import logging
import struct
import sys
import types
import unittest
from unittest import mock

from test_local_camera_platform import (
    BASE,
    READY,
    FakeMqtt,
    entry,
    load_camera_module,
    message,
)
from test_view_navigation import ROOT

PACKAGE = "_hometiles_stream_testpkg"
SESSION = "ab" * 16
TOKEN = "cd" * 16


def jpeg(size):
    return b"\xff\xd8" + b"\x11" * (size - 4) + b"\xff\xd9"


def load_package_module(name, extra_stubs=None):
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    stubs = {PACKAGE: package, **(extra_stubs or {})}
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{PACKAGE}.{name}")


def load_camera_stream_module():
    """Import camera_stream.py (the shared TCP listener) against HA stubs."""
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    camera = types.ModuleType("homeassistant.components.camera")
    camera.async_get_image = None
    camera.async_get_stream_source = None
    ffmpeg = types.ModuleType("homeassistant.components.ffmpeg")
    ffmpeg.get_ffmpeg_manager = None
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    return load_package_module("camera_stream", {
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.camera": camera,
        "homeassistant.components.ffmpeg": ffmpeg,
        "homeassistant.core": core,
    })


STREAM = load_package_module("local_camera_stream")
CAPS = load_package_module("capabilities")


class WireProtocolTest(unittest.TestCase):
    def test_structs_and_magics_match_the_downstream_stream(self):
        tree = ast.parse((ROOT / "camera_stream.py").read_text(encoding="utf-8"))
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                constants[node.target.id] = node.value
        fmt = lambda name: constants[name].args[0].value  # noqa: E731
        self.assertEqual(STREAM.HELLO_STRUCT.format, fmt("CAMERA_STREAM_HELLO_STRUCT"))
        self.assertEqual(STREAM.FRAME_STRUCT.format, fmt("CAMERA_STREAM_FRAME_STRUCT"))
        self.assertEqual(STREAM.ACK_STRUCT.format, fmt("CAMERA_STREAM_ACK_STRUCT"))
        self.assertEqual(STREAM.HELLO_MAGIC, constants["CAMERA_STREAM_HELLO_MAGIC"].value)
        self.assertEqual(STREAM.FRAME_MAGIC, constants["CAMERA_STREAM_FRAME_MAGIC"].value)
        self.assertEqual(STREAM.ACK_MAGIC, constants["CAMERA_STREAM_ACK_MAGIC"].value)
        self.assertEqual((STREAM.HELLO_STRUCT.size, STREAM.FRAME_STRUCT.size, STREAM.ACK_STRUCT.size),
                         (8, 16, 12))
        self.assertEqual(STREAM.CHUNK_BYTES, 8192)
        self.assertEqual(STREAM.MAX_JPEG_BYTES, 131072)
        # The upload line must never be mistaken for the downstream request.
        downstream = constants["CAMERA_STREAM_REQUEST_PREFIX"].value
        self.assertFalse(STREAM.UPLOAD_REQUEST_PREFIX.startswith(downstream))
        self.assertEqual(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, 7, 20000),
                         b"HTF1\x01\x00\x00\x00" + struct.pack(">II", 7, 20000))
        self.assertEqual(STREAM.ACK_STRUCT.pack(b"HTA1", 7, 8192), b"HTA1" + struct.pack(">II", 7, 8192))

    def test_handshake_parsing(self):
        parse = STREAM.parse_upload_handshake
        self.assertEqual(parse(f"HTCAMUP/1 {SESSION} {TOKEN}\n".encode()), (SESSION, TOKEN))
        self.assertEqual(parse(f"HTCAMUP/1 {SESSION} {TOKEN}\r\n"), (SESSION, TOKEN))
        for bad in [
            f"HTCAM/1 {TOKEN}\n", f"HTCAMUP/2 {SESSION} {TOKEN}\n", f"HTCAMUP/1 {SESSION}\n",
            f"HTCAMUP/1 {SESSION.upper()} {TOKEN}\n", f"HTCAMUP/1 {SESSION}  {TOKEN}\n",
            f"HTCAMUP/1 {SESSION} {TOKEN} extra\n", f"HTCAMUP/1 abc {TOKEN}\n",
            b"HTCAMUP/1 " + b"a" * 300 + b"\n", b"HTCAMUP/1 \xff\xfe\n", None, 5,
        ]:
            self.assertIsNone(parse(bad), bad)
        self.assertTrue(STREAM.is_upload_handshake(f"HTCAMUP/1 {SESSION} {TOKEN}"))
        self.assertFalse(STREAM.is_upload_handshake(f"HTCAM/1 {TOKEN}"))

    def test_stream_requests(self):
        request = STREAM.build_stream_request(SESSION, "192.168.1.10", 8124, TOKEN)
        self.assertEqual(request, {"v": 1, "action": "stream", "session": SESSION,
                                   "host": "192.168.1.10", "port": 8124, "token": TOKEN,
                                   "width": 640, "height": 360, "fps": 25, "quality": 65,
                                   "ttl_ms": 6000})
        self.assertEqual(STREAM.build_stream_stop(SESSION),
                         {"v": 1, "action": "stream_stop", "session": SESSION})
        self.assertNotIn("op", request)
        for host, port in [("fe80::1", 8124), ("0.0.0.0", 8124), ("224.0.0.251", 8124),
                           ("homeassistant.local", 8124), ("192.168.1.10", 0), ("192.168.1.10", True)]:
            with self.assertRaises(ValueError):
                STREAM.build_stream_request(SESSION, host, port, TOKEN)
        with self.assertRaises(ValueError):
            STREAM.build_stream_stop("XYZ")
        self.assertRegex(STREAM.new_session_id(), r"^[0-9a-f]{32}$")
        self.assertRegex(STREAM.new_token(), r"^[0-9a-f]{32}$")

    def test_capability_is_never_inferred(self):
        self.assertEqual(CAPS.normalise_capabilities({"local_camera_stream": True}),
                         {"local_camera_stream": True})
        with self.assertRaises(ValueError):
            CAPS.normalise_capabilities({"local_camera_stream": 1})
        self.assertFalse(CAPS.supports({}, "local_camera_stream"))
        self.assertFalse(CAPS.supports({"capabilities": {"local_camera": True}}, "local_camera_stream"))
        self.assertTrue(CAPS.supports({"capabilities": {"local_camera_stream": True}}, "local_camera_stream"))


class Panel:
    """Minimal panel-side client that obeys the one-chunk-in-flight rule."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port, line):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(line)
        await writer.drain()
        return cls(reader, writer)

    async def hello(self):
        return STREAM.HELLO_STRUCT.unpack(await asyncio.wait_for(self.reader.readexactly(8), 2))

    async def ack(self):
        return STREAM.ACK_STRUCT.unpack(await asyncio.wait_for(self.reader.readexactly(12), 2))

    async def send_frame(self, sequence, payload):
        self.writer.write(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, sequence, len(payload)))
        acks = []
        sent = 0
        while sent < len(payload):
            chunk = payload[sent:sent + 8192]
            self.writer.write(chunk)
            await self.writer.drain()
            sent += len(chunk)
            acks.append(await self.ack())
        return acks

    async def closed(self):
        data = await asyncio.wait_for(self.reader.read(), 2)
        return data

    def close(self):
        self.writer.close()


class UploadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.camera_stream = load_camera_stream_module()
        self.manager = self.camera_stream.CameraStreamManager(hass=None)
        self.registry = self.manager.local_camera_uploads
        self.frames = []
        self.server = await asyncio.start_server(
            self.manager._tcp_connection.async_handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        self.registry.close_all()
        self.server.close()
        await self.server.wait_closed()

    async def panel(self, session=SESSION, token=TOKEN):
        client = await Panel.connect(self.port, f"HTCAMUP/1 {session} {token}\n".encode())
        self.clients.append(client)
        return client

    def register(self):
        self.registry.register(SESSION, TOKEN, self.frames.append)

    async def test_chunked_frames_are_acknowledged_one_at_a_time(self):
        self.register()
        panel = await self.panel()
        self.assertEqual(await panel.hello(), (b"HTC1", 0, 8192))
        frame = jpeg(20000)
        acks = await panel.send_frame(1, frame)
        self.assertEqual(acks, [(b"HTA1", 1, 8192), (b"HTA1", 1, 16384), (b"HTA1", 1, 20000)])
        small = jpeg(100)
        self.assertEqual(await panel.send_frame(2, small), [(b"HTA1", 2, 100)])
        exact = jpeg(8192)
        self.assertEqual(await panel.send_frame(3, exact), [(b"HTA1", 3, 8192)])
        self.assertEqual(self.frames, [frame, small, exact])
        self.assertTrue(self.registry.is_connected(SESSION))
        # A flush is accepted, an end message closes the upload cleanly.
        panel.writer.write(STREAM.FRAME_STRUCT.pack(b"HTF1", 2, 3, 0))
        panel.writer.write(STREAM.FRAME_STRUCT.pack(b"HTF1", 3, 3, 0))
        await panel.writer.drain()
        self.assertEqual(await panel.closed(), b"")
        self.assertFalse(self.registry.is_connected(SESSION))
        self.assertTrue(self.registry.is_registered(SESSION))

    async def test_no_second_chunk_is_read_before_its_ack(self):
        self.register()
        panel = await self.panel()
        await panel.hello()
        frame = jpeg(10000)
        panel.writer.write(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, 9, len(frame)) + frame[:8192])
        await panel.writer.drain()
        self.assertEqual(await panel.ack(), (b"HTA1", 9, 8192))
        # Without the rest of the frame no further ACK appears.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(panel.reader.readexactly(1), 0.2)
        panel.writer.write(frame[8192:])
        await panel.writer.drain()
        self.assertEqual(await panel.ack(), (b"HTA1", 9, 10000))
        await asyncio.sleep(0.01)
        self.assertEqual(self.frames, [frame])

    async def test_unknown_session_or_token_is_rejected(self):
        self.register()
        for session, token in [(SESSION, "ef" * 16), ("12" * 16, TOKEN)]:
            with self.assertLogs(STREAM._LOGGER, "WARNING") if session == SESSION else _no_op():
                panel = await self.panel(session, token)
                self.assertEqual(await panel.hello(), (b"HTC1", 1, 8192))
                self.assertEqual(await panel.closed(), b"")
        self.assertFalse(self.registry.is_connected(SESSION))
        self.assertEqual(self.frames, [])

    async def test_expired_session_is_rejected(self):
        now = [0.0]
        self.registry._clock = lambda: now[0]
        self.register()
        now[0] = STREAM.SESSION_EXPIRY_S + 0.1
        with self.assertLogs(STREAM._LOGGER, "WARNING"):
            panel = await self.panel()
            self.assertEqual((await panel.hello())[1], 1)
        self.assertFalse(self.registry.is_registered(SESSION))

    async def violation(self, header, payload=b""):
        self.register()
        panel = await self.panel()
        await panel.hello()
        with self.assertLogs(STREAM._LOGGER, "WARNING") as logs:
            panel.writer.write(header + payload)
            await panel.writer.drain()
            data = await panel.closed()
        self.assertEqual(self.frames, [])
        return data, logs.output[0]

    async def test_oversize_frame_is_rejected_before_any_payload(self):
        data, log = await self.violation(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, 1, 131073))
        self.assertEqual(data, b"")
        self.assertIn("frame_too_large", log)

    async def test_invalid_headers_are_rejected(self):
        data, log = await self.violation(STREAM.FRAME_STRUCT.pack(b"XXXX", 1, 1, 100))
        self.assertIn("invalid_frame_magic", log)

    async def test_missing_soi_is_rejected_without_ack(self):
        payload = b"\x00\x00" + jpeg(100)[2:]
        data, log = await self.violation(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, 1, 100), payload)
        self.assertEqual(data, b"")
        self.assertIn("jpeg_start_missing", log)

    async def test_missing_eoi_never_gets_the_final_ack(self):
        payload = jpeg(9000)[:-2] + b"\x00\x00"
        data, log = await self.violation(STREAM.FRAME_STRUCT.pack(b"HTF1", 1, 4, 9000), payload)
        # Only the first chunk was acknowledged.
        self.assertEqual(data, STREAM.ACK_STRUCT.pack(b"HTA1", 4, 8192))
        self.assertIn("jpeg_end_missing", log)

    async def test_second_connection_replaces_the_first(self):
        self.register()
        first = await self.panel()
        await first.hello()
        second = await self.panel()
        self.assertEqual((await second.hello())[1], 0)
        self.assertEqual(await first.closed(), b"")
        frame = jpeg(300)
        self.assertEqual(await second.send_frame(1, frame), [(b"HTA1", 1, 300)])
        self.assertEqual(self.frames, [frame])

    async def test_revoke_closes_the_active_upload(self):
        self.register()
        panel = await self.panel()
        await panel.hello()
        await asyncio.sleep(0.01)
        self.registry.revoke(SESSION)
        self.assertEqual(await panel.closed(), b"")
        self.assertFalse(self.registry.is_registered(SESSION))

    async def test_manager_shutdown_closes_uploads(self):
        self.register()
        panel = await self.panel()
        await panel.hello()
        await asyncio.sleep(0.01)
        self.assertTrue(self.registry.is_connected(SESSION))
        await self.manager.async_shutdown()
        self.assertEqual(await panel.closed(), b"")
        self.assertFalse(self.registry.is_registered(SESSION))

    async def test_downstream_handshake_still_uses_the_existing_path(self):
        client = await Panel.connect(self.port, b"HTCAM/1 unknown-token\n")
        self.clients.append(client)
        # Unknown downstream token: the existing reject hello, no upload path.
        self.assertEqual(await client.hello(), (b"HTC1", 1, 8192))
        self.assertEqual(await client.closed(), b"")


class _no_op:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeRegistry:
    def __init__(self):
        self.sessions = {}
        self.revoked = []
        self.touched = 0

    def register(self, session, token, on_frame):
        self.sessions[session] = (token, on_frame)

    def touch(self, session):
        self.touched += 1

    def revoke(self, session):
        if self.sessions.pop(session, None) is not None:
            self.revoked.append(session)


class LiveStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.published = []
        self.registry = FakeRegistry()
        self.is_ready = True
        self.endpoint = ("192.168.1.10", 8125)
        self.live = STREAM.LocalCameraLiveStream(
            publish=self.publish, endpoint=self.get_endpoint, registry=lambda: self.registry,
            ready=lambda: self.is_ready, log_name=BASE, keepalive_s=0.05, grace_s=0.15,
            session_factory=lambda: SESSION, token_factory=lambda: TOKEN)

    async def asyncTearDown(self):
        await self.live.async_stop()

    async def publish(self, request):
        self.published.append(request)

    async def get_endpoint(self):
        return self.endpoint

    def actions(self):
        return [request["action"] for request in self.published]

    async def test_first_viewer_starts_keepalive_and_last_stops_after_grace(self):
        self.live.acquire()
        await asyncio.sleep(0.01)
        self.assertEqual(self.published[0], STREAM.build_stream_request(SESSION, "192.168.1.10", 8125, TOKEN))
        self.assertIn(SESSION, self.registry.sessions)
        self.live.acquire()
        self.assertEqual(self.live.viewers, 2)
        await asyncio.sleep(0.12)
        # Keepalives repeat the identical message.
        self.assertGreaterEqual(len(self.published), 3)
        self.assertTrue(all(request == self.published[0] for request in self.published))
        self.live.release()
        self.live.release()
        self.live.release()  # an extra release never goes negative
        self.assertEqual(self.live.viewers, 0)
        await asyncio.sleep(0.05)
        self.assertTrue(self.live.running)
        self.assertNotIn("stream_stop", self.actions())
        await asyncio.sleep(0.2)
        self.assertFalse(self.live.running)
        self.assertEqual(self.published[-1], {"v": 1, "action": "stream_stop", "session": SESSION})
        self.assertEqual(self.registry.revoked, [SESSION])
        self.assertEqual(self.actions().count("stream_stop"), 1)

    async def test_viewer_returning_within_grace_keeps_the_stream(self):
        self.live.acquire()
        await asyncio.sleep(0.01)
        self.live.release()
        await asyncio.sleep(0.05)
        self.live.acquire()
        await asyncio.sleep(0.25)
        self.assertTrue(self.live.running)
        self.assertNotIn("stream_stop", self.actions())
        self.assertEqual(self.registry.revoked, [])

    async def test_not_ready_suspends_and_resumes(self):
        self.live.acquire()
        await asyncio.sleep(0.01)
        self.is_ready = False
        self.live.poke()
        await asyncio.sleep(0.01)
        self.assertEqual(self.registry.revoked, [SESSION])
        count = len(self.published)
        await asyncio.sleep(0.12)
        # No keepalives (and no stop over a broker that may be gone).
        self.assertEqual(len(self.published), count)
        self.is_ready = True
        self.live.poke()
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.published), count + 1)
        self.assertEqual(self.published[-1]["action"], "stream")
        self.assertIn(SESSION, self.registry.sessions)
        # A stop while not ready revokes but sends nothing.
        self.is_ready = False
        await self.live.async_stop()
        self.assertNotIn("stream_stop", self.actions())

    async def test_missing_endpoint_or_listener_publishes_nothing(self):
        self.endpoint = None
        with self.assertLogs(STREAM._LOGGER, "WARNING") as logs:
            self.live.acquire()
            await asyncio.sleep(0.15)
        self.assertEqual(len(logs.output), 1)  # rate limited
        self.assertEqual(self.published, [])
        await self.live.async_stop()
        self.assertEqual(self.published, [])
        self.live._registry = lambda: None
        with self.assertLogs(STREAM._LOGGER, "WARNING"):
            self.live.acquire()
            await asyncio.sleep(0.01)
        self.assertEqual(self.published, [])

    async def test_publish_failure_is_contained(self):
        async def failing(request):
            raise RuntimeError("broker gone")
        self.live._publish = failing
        with self.assertLogs(STREAM._LOGGER, "WARNING"):
            self.live.acquire()
            await asyncio.sleep(0.12)
        self.assertTrue(self.live.running)

    async def test_frames_are_latest_only_and_freshness_bounded(self):
        now = [100.0]
        self.live._clock = lambda: now[0]
        self.live.acquire()
        await asyncio.sleep(0.01)
        _token, on_frame = self.registry.sessions[SESSION]
        self.assertIsNone(self.live.latest())
        waiter = asyncio.create_task(self.live.async_next_frame(None, 1.0))
        await asyncio.sleep(0.01)
        first, second = jpeg(10), jpeg(20)
        on_frame(first)
        self.assertEqual(await waiter, first)
        on_frame(second)
        self.assertIs(self.live.latest(), second)
        self.assertIs(await self.live.async_next_frame(first, 1.0), second)
        self.assertIsNone(await self.live.async_next_frame(second, 0.05))
        now[0] += 1.0
        self.assertIsNone(self.live.latest())

    async def test_close_ends_waiting_viewers_and_never_restarts(self):
        self.live.acquire()
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(self.live.async_next_frame(None, 5.0))
        await asyncio.sleep(0.01)
        await self.live.async_close()
        self.assertIsNone(await asyncio.wait_for(waiter, 0.5))
        self.assertTrue(self.live.closed)
        self.assertFalse(self.live.running)
        self.assertEqual(self.published[-1]["action"], "stream_stop")
        count = len(self.published)
        self.live.acquire()
        await asyncio.sleep(0.05)
        self.assertFalse(self.live.running)
        self.assertEqual(len(self.published), count)
        self.assertIsNone(await self.live.async_next_frame(None, 0.01))
        self.live.release()


class LiveCameraEntityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.module = load_camera_module(self.mqtt)
        # The stub package is gone from sys.modules; logger names are global.
        self.stream = types.SimpleNamespace(
            _LOGGER=logging.getLogger(self.module.LocalCameraLiveStream.__module__))
        self.registry = FakeRegistry()
        self.manager = types.SimpleNamespace(tcp_port=8126, local_camera_uploads=self.registry)
        self.cameras = []

    async def asyncTearDown(self):
        for camera in self.cameras:
            await camera.async_will_remove_from_hass()

    async def created(self, config_entry):
        result = []
        await self.module.async_setup_entry(None, config_entry, result.extend)
        return result

    async def start(self, capabilities):
        [camera] = await self.created(entry(capabilities))
        camera.hass = types.SimpleNamespace(data={"tab5_lvgl": {
            "camera_stream_manager": self.manager,
            "entries": {"e1": types.SimpleNamespace(_device_ip="192.168.1.50")}}})
        await camera.async_added_to_hass()
        handler, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera"]
        await handler(message(f"{BASE}/stat/local_camera", json.dumps(READY)))
        self.cameras.append(camera)
        return camera

    async def test_capability_gating(self):
        [still] = await self.created(entry({"local_camera": True}))
        self.assertIsNone(still._live)
        self.assertEqual(still._attr_frame_interval, 2.0)
        [live] = await self.created(entry({"local_camera": True, "local_camera_stream": True}))
        self.assertIsNotNone(live._live)
        self.assertLess(live._attr_frame_interval, 0.2)
        self.assertEqual(live._attr_supported_features, 0)
        self.assertEqual(await self.created(entry({"local_camera_stream": True})), [])

    async def test_still_camera_uses_the_default_mjpeg_handler(self):
        camera = await self.start({"local_camera": True})
        with mock.patch.object(self.module.Camera, "handle_async_mjpeg_stream", create=True,
                               new=mock.AsyncMock(return_value="default")):
            self.assertEqual(await camera.handle_async_mjpeg_stream(object()), "default")
        self.assertEqual(self.mqtt.published, [])

    async def test_mjpeg_viewers_share_one_stream(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        camera._live._keepalive_s = 0.05
        camera._live._grace_s = 0.1
        closed = [False, False]

        def viewer(index):
            request = types.SimpleNamespace(images=[], closed=lambda: closed[index])
            return request, asyncio.create_task(camera.handle_async_mjpeg_stream(request))

        (first_request, first), (second_request, second) = viewer(0), viewer(1)
        await asyncio.sleep(0.02)
        self.assertEqual(camera._live.viewers, 2)
        stream_requests = [request for _t, request, _q, _r in self.mqtt.published]
        topic, request, qos, retain = self.mqtt.published[0]
        self.assertEqual((topic, qos, retain), (f"{BASE}/cmnd/local_camera", 0, False))
        self.assertEqual(request["action"], "stream")
        self.assertEqual((request["host"], request["port"]), ("192.168.1.10", 8126))
        self.assertTrue(all(item == request for item in stream_requests))
        session = request["session"]
        _token, on_frame = self.registry.sessions[session]
        frame = jpeg(50)
        on_frame(frame)
        await asyncio.sleep(0.01)
        self.assertEqual(first_request.images, [frame])
        self.assertEqual(second_request.images, [frame])
        # Still-image requests reuse the fresh live frame without MQTT snapshots.
        self.assertIs(await camera.async_camera_image(), frame)
        self.assertNotIn("snapshot", [request.get("op") for _t, request, _q, _r in self.mqtt.published])
        closed[0] = True
        on_frame(jpeg(60))
        self.assertEqual(await first, "response")
        self.assertEqual(camera._live.viewers, 1)
        closed[1] = True
        on_frame(jpeg(70))
        await second
        self.assertEqual(camera._live.viewers, 0)
        await asyncio.sleep(0.2)
        self.assertFalse(camera._live.running)
        self.assertEqual(self.mqtt.published[-1][1],
                         {"v": 1, "action": "stream_stop", "session": session})

    async def test_offline_panel_suspends_the_stream(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        camera._live.acquire()
        await asyncio.sleep(0.01)
        session = self.mqtt.published[-1][1]["session"]
        count = len(self.mqtt.published)
        handler, _ = self.mqtt.subscriptions[f"{BASE}/stat/connected"]
        await handler(message(f"{BASE}/stat/connected", "offline"))
        await asyncio.sleep(0.01)
        self.assertEqual(self.registry.revoked, [session])
        self.assertEqual(len(self.mqtt.published), count)
        camera._live.release()

    async def test_pause_suspends_the_stream_without_any_request(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        camera._live._keepalive_s = 0.02
        camera._live.acquire()
        await asyncio.sleep(0.01)
        session = self.mqtt.published[-1][1]["session"]
        count = len(self.mqtt.published)
        status, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera"]
        await status(message(f"{BASE}/stat/local_camera", '{"v":1,"state":"disabled","paused":true}'))
        # Several keepalive intervals pass: no keepalive, no stop, no snapshot.
        await asyncio.sleep(0.1)
        self.assertEqual(self.registry.revoked, [session])
        self.assertEqual(len(self.mqtt.published), count)
        self.assertTrue(camera.available)
        # A still-image request ignores even a fresh live frame while paused.
        camera._live._on_frame(jpeg(40))
        self.assertIsNone(await camera.async_camera_image())
        # A new MJPEG viewer ends at once instead of freezing a frame.
        request, task = self.viewer(camera)
        self.assertEqual(await asyncio.wait_for(task, 0.5), "response")
        self.assertEqual(request.images, [])
        self.assertEqual(len(self.mqtt.published), count)
        # Resuming restarts the stream for the viewer that is still there.
        await status(message(f"{BASE}/stat/local_camera", json.dumps(READY)))
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.mqtt.published), count + 1)
        self.assertEqual(self.mqtt.published[-1][1]["action"], "stream")
        self.assertIn(session, self.registry.sessions)
        camera._live.release()

    async def test_pause_ends_open_mjpeg_viewers(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.02):
            request, task = self.viewer(camera)
            await asyncio.sleep(0.01)
            session = self.mqtt.published[-1][1]["session"]
            frame = jpeg(50)
            self.registry.sessions[session][1](frame)
            await asyncio.sleep(0.01)
            count = len(self.mqtt.published)
            status, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera"]
            await status(message(f"{BASE}/stat/local_camera",
                                 '{"v":1,"state":"disabled","paused":true}'))
            self.assertEqual(await asyncio.wait_for(task, 0.5), "response")
        self.assertEqual(request.images, [frame])
        self.assertEqual(len(self.mqtt.published), count)
        self.assertEqual(camera._live.viewers, 0)

    async def test_stale_live_frame_falls_back_to_snapshot(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        camera._live._frame = jpeg(40)
        camera._live._frame_at = -100.0
        task = asyncio.create_task(camera.async_camera_image())
        await asyncio.sleep(0.01)
        topic, request, _qos, retain = self.mqtt.published[-1]
        self.assertEqual(set(request), {"v", "id", "op", "max_bytes"})
        image_handler, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera/image/+"]
        snapshot = jpeg(80)
        await image_handler(message(f"{BASE}/stat/local_camera/image/{request['id']}", snapshot))
        self.assertEqual(await task, snapshot)

    async def test_viewer_without_live_frames_gets_a_snapshot_then_ends(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        self.manager.tcp_port = None  # listener down: no stream request at all
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.05):
            request = types.SimpleNamespace(images=[], closed=lambda: False)
            with self.assertLogs(self.stream._LOGGER, "WARNING"):
                task = asyncio.create_task(camera.handle_async_mjpeg_stream(request))
                await asyncio.sleep(0.1)
            [(_topic, snapshot_request, _q, _r)] = self.mqtt.published
            self.assertEqual(snapshot_request["op"], "snapshot")
            error_handler, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera/error/+"]
            with self.assertLogs(self.module._LOGGER, "WARNING"):
                await error_handler(message(f"{BASE}/stat/local_camera/error/{snapshot_request['id']}",
                                            '{"v":1,"error":"busy"}'))
                self.assertEqual(await task, "response")
        self.assertEqual(request.images, [])
        self.assertEqual(camera._live.viewers, 0)

    def viewer(self, camera, closed=lambda: False):
        request = types.SimpleNamespace(images=[], closed=closed)
        return request, asyncio.create_task(camera.handle_async_mjpeg_stream(request))

    async def test_removal_ends_open_viewers(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        request, task = self.viewer(camera)
        await asyncio.sleep(0.02)
        session = self.mqtt.published[-1][1]["session"]
        frame = jpeg(50)
        self.registry.sessions[session][1](frame)
        await asyncio.sleep(0.01)
        self.assertEqual(request.images, [frame])
        self.cameras.remove(camera)
        await camera.async_will_remove_from_hass()
        # The multipart response ends instead of repeating a frozen frame.
        self.assertEqual(await asyncio.wait_for(task, 0.5), "response")
        self.assertEqual(camera._live.viewers, 0)
        self.assertEqual(self.mqtt.published[-1][1]["action"], "stream_stop")

    async def test_viewer_without_new_frames_ends_after_bounded_misses(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.02):
            request, task = self.viewer(camera)
            await asyncio.sleep(0.01)
            session = self.mqtt.published[-1][1]["session"]
            frame = jpeg(50)
            self.registry.sessions[session][1](frame)
            # The panel stops uploading; the viewer ends instead of freezing.
            self.assertEqual(await asyncio.wait_for(task, 1.0), "response")
        self.assertEqual(request.images, [frame])
        self.assertEqual(camera._live.viewers, 0)
        self.assertNotIn("snapshot", [item.get("op") for _t, item, _q, _r in self.mqtt.published])

    async def test_mqtt_disconnect_suspends_and_reconnect_resumes(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        [connection_changed] = self.mqtt.connection_callbacks
        camera._live.acquire()
        await asyncio.sleep(0.01)
        session = self.mqtt.published[-1][1]["session"]
        count = len(self.mqtt.published)
        self.mqtt.connected = False
        connection_changed(False)
        await asyncio.sleep(0.01)
        # Suspended at once (well before the 2 s keepalive tick), nothing sent.
        self.assertEqual(self.registry.revoked, [session])
        self.assertNotIn(session, self.registry.sessions)
        self.assertEqual(len(self.mqtt.published), count)
        self.mqtt.connected = True
        connection_changed(True)
        await asyncio.sleep(0.01)
        self.assertIn(session, self.registry.sessions)
        self.assertEqual(len(self.mqtt.published), count + 1)
        self.assertEqual(self.mqtt.published[-1][1]["action"], "stream")
        camera._live.release()
        self.cameras.remove(camera)
        await camera.async_will_remove_from_hass()
        self.assertEqual(self.mqtt.connection_callbacks, [])

    async def test_removal_stops_immediately(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        camera._live.acquire()
        await asyncio.sleep(0.01)
        session = self.mqtt.published[-1][1]["session"]
        self.cameras.remove(camera)
        await camera.async_will_remove_from_hass()
        self.assertFalse(camera._live.running)
        self.assertEqual(self.registry.revoked, [session])
        self.assertEqual(self.mqtt.published[-1][1]["action"], "stream_stop")


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
