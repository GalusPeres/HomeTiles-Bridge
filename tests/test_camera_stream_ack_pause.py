"""A short pause of the panel must not end its camera stream (#116).

The Bridge waited 5 s for each chunk acknowledgement and then closed the
connection without a word (debug log only). A panel whose Wi-Fi stalled for
a few seconds lost its picture for good: the popup showed "Stream connection
ended" after 18-60 minutes, and the Home Assistant log showed nothing. A
panel now counts as gone only after the direct link's own idle limit, and the
Bridge says so in the log when it ends a stream for that reason.
"""

from __future__ import annotations

import asyncio
import struct
import types
import unittest
from unittest import mock

from test_camera_stream_lag import ENTITY, SOURCE, FakeHass, FakeProcess
from test_local_camera_stream import jpeg, load_camera_stream_module, load_package_module

# Longer than the former 5 s limit, like the stalls seen in #116.
PAUSE_S = 6.0


class AckPauseTest(unittest.IsolatedAsyncioTestCase):
    """Real sockets: the panel acknowledges a chunk only after a pause."""

    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        accepted = asyncio.get_running_loop().create_future()

        async def accept(reader, writer):
            accepted.set_result((reader, writer))

        self.server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.panel_reader, self.panel_writer = await asyncio.open_connection("127.0.0.1", port)
        self.reader, self.writer = await accepted

    async def asyncTearDown(self):
        for writer in (self.writer, self.panel_writer):
            writer.close()
        self.server.close()
        await self.server.wait_closed()

    async def acknowledge_after(self, pause):
        """The panel's side of one frame (camera_stream.cpp), stalled before each ACK."""
        header = await self.panel_reader.readexactly(self.module.CAMERA_STREAM_FRAME_STRUCT.size)
        _magic, _kind, sequence, size = self.module.CAMERA_STREAM_FRAME_STRUCT.unpack(header)
        received = 0
        while received < size:
            part = min(self.module.CAMERA_STREAM_CHUNK_BYTES, size - received)
            await self.panel_reader.readexactly(part)
            received += part
            await asyncio.sleep(pause)
            self.panel_writer.write(struct.pack(">4sII", b"HTA1", sequence, received))
            await self.panel_writer.drain()

    async def test_a_pause_of_six_seconds_keeps_the_stream(self):
        panel = asyncio.create_task(self.acknowledge_after(PAUSE_S))
        metrics = await self.module.CameraStreamConnection._async_send_frame(
            self.reader, self.writer, 1, jpeg(4096))
        await panel
        self.assertEqual(metrics.chunks, 1)
        self.assertGreaterEqual(metrics.max_ack_wait_seconds, PAUSE_S - 0.5)

    def test_the_panel_counts_as_gone_after_the_link_idle_limit(self):
        link = load_package_module("link_protocol")
        self.assertEqual(self.module.CAMERA_STREAM_ACK_TIMEOUT_SECONDS, link.IDLE_TIMEOUT_S)


class SilentPanelTest(unittest.IsolatedAsyncioTestCase):
    """A panel silent past the limit ends its stream with one warning."""

    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.module.CAMERA_PIPELINE_LINGER_SECONDS = 0
        self.manager = self.module.CameraStreamManager(FakeHass())
        self.connection = self.manager._tcp_connection
        self.processes = []

        async def stream_source(hass, entity_id):
            return SOURCE

        async def spawn(*command, **kwargs):
            process = FakeProcess()
            self.processes.append(process)
            return process

        async def silent_panel(reader, writer, sequence, frame, **_options):
            raise TimeoutError

        async def send_control(writer, message_type, sequence):
            pass

        self.patches = [
            mock.patch.object(self.module, "async_get_stream_source", stream_source),
            mock.patch.object(self.module, "get_ffmpeg_manager",
                              lambda hass: types.SimpleNamespace(binary="ffmpeg")),
            mock.patch.object(self.module.asyncio, "create_subprocess_exec", spawn),
        ]
        for patcher in self.patches:
            patcher.start()
        self.connection._async_send_frame = silent_panel
        self.connection._async_send_control = send_control

    async def asyncTearDown(self):
        await self.manager.async_shutdown()
        for patcher in reversed(self.patches):
            patcher.stop()

    async def test_the_stream_ends_with_one_warning(self):
        session = await self.manager.async_create_session("panel", ENTITY)
        await self.manager.async_take_session(session.token)
        with self.assertLogs(self.module._LOGGER, "WARNING") as logs:
            stream = asyncio.create_task(self.connection._async_stream(session, None, "panel"))
            async with asyncio.timeout(3):
                while not self.processes:
                    await asyncio.sleep(0.005)
            self.processes[0].stdout.queue.put_nowait(jpeg(64))
            await asyncio.wait_for(stream, 3)
        warnings = [line for line in logs.output if "acknowledged nothing" in line]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn(ENTITY, warnings[0])


if __name__ == "__main__":
    unittest.main()
