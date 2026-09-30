"""Panels showing the same camera share one FFmpeg pipeline.

Every popup used to start its own FFmpeg: two panels on the same camera
meant two RTSP connections and two full decodes of the same stream. Panels
asking for the same camera, size and rate now share one pipeline; each
keeps only its newest frame and sends at its own pace.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from test_camera_stream_lag import ENTITY, SOURCE, FakeHass, FakeProcess
from test_local_camera_stream import jpeg, load_camera_stream_module


class SharedStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.manager = self.module.CameraStreamManager(FakeHass())
        self.connection = self.manager._tcp_connection
        self.processes = []
        self.commands = []
        self.sent = {}      # writer -> frames acknowledged by that panel
        self.controls = {}  # writer -> control messages
        self.gates = {}     # writer -> event a panel waits on before each frame

        async def stream_source(hass, entity_id):
            return SOURCE

        async def spawn(*command, **kwargs):
            self.commands.append(command)
            process = FakeProcess()
            self.processes.append(process)
            return process

        async def send_frame(reader, writer, sequence, frame):
            gate = self.gates.get(writer)
            if gate is not None:
                await gate.wait()
            self.sent.setdefault(writer, []).append(frame)
            return self.module.CameraFrameSendMetrics(1, 0.0, 0.0, 0.0, 0.0)

        async def send_control(writer, message_type, sequence):
            self.controls.setdefault(writer, []).append(message_type)

        self.patches = [
            mock.patch.object(self.module, "async_get_stream_source", stream_source),
            mock.patch.object(self.module, "get_ffmpeg_manager",
                              lambda hass: types.SimpleNamespace(binary="ffmpeg")),
            mock.patch.object(self.module.asyncio, "create_subprocess_exec", spawn),
        ]
        for patcher in self.patches:
            patcher.start()
        self.connection._async_send_frame = send_frame
        self.connection._async_send_control = send_control

    async def asyncTearDown(self):
        await self.manager.async_shutdown()
        for patcher in reversed(self.patches):
            patcher.stop()

    async def wait_for(self, predicate, timeout=3.0):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def open_panel(self, device, width=752, height=424, fps=30):
        session = await self.manager.async_create_session(device, ENTITY, width, height, fps)
        await self.manager.async_take_session(session.token)
        return asyncio.create_task(self.connection._async_stream(session, None, device))

    def frames(self, device):
        return self.sent.get(device, [])

    async def test_two_panels_on_the_same_camera_share_one_ffmpeg(self):
        a = await self.open_panel("a")
        await self.wait_for(lambda: self.processes)
        b = await self.open_panel("b")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.processes), 1)

        first = jpeg(64)
        self.processes[0].stdout.queue.put_nowait(first)
        await self.wait_for(lambda: self.frames("a") and self.frames("b"))
        self.assertEqual(self.frames("a"), [first])
        self.assertEqual(self.frames("b"), [first])

        # The first panel closes: the other keeps the running pipeline.
        await self.manager.async_stop_device("a")
        await asyncio.wait_for(a, 3)
        self.assertIsNone(self.processes[0].returncode)
        second = jpeg(65)
        self.processes[0].stdout.queue.put_nowait(second)
        await self.wait_for(lambda: len(self.frames("b")) == 2)
        self.assertEqual(self.frames("b")[-1], second)
        self.assertEqual(self.frames("a"), [first])

        # The last panel closes: FFmpeg stops.
        await self.manager.async_stop_device("b")
        await asyncio.wait_for(b, 3)
        await self.wait_for(lambda: self.processes[0].returncode is not None)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.manager._broadcasts, {})

    async def test_other_sizes_and_rates_get_their_own_pipeline(self):
        tasks = [
            await self.open_panel("a"),
            await self.open_panel("b", 640, 360),
            await self.open_panel("c", fps=24),
        ]
        await self.wait_for(lambda: len(self.processes) == 3)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.processes), 3)
        for device in ("a", "b", "c"):
            await self.manager.async_stop_device(device)
        for task in tasks:
            await asyncio.wait_for(task, 3)
        await self.wait_for(lambda: all(p.returncode is not None for p in self.processes))

    async def test_a_panel_opened_later_joins_the_running_pipeline(self):
        a = await self.open_panel("a")
        await self.wait_for(lambda: self.processes)
        self.processes[0].stdout.queue.put_nowait(jpeg(64))
        await self.wait_for(lambda: self.frames("a"))
        with self.assertLogs(self.module._LOGGER, "INFO") as logs:
            b = await self.open_panel("b")
            await asyncio.sleep(0.05)
        self.assertTrue(any("joins the running stream (2 panels)" in r.getMessage()
                            for r in logs.records))
        later = jpeg(70)
        self.processes[0].stdout.queue.put_nowait(later)
        await self.wait_for(lambda: self.frames("b"))
        self.assertEqual(self.frames("b"), [later])
        self.assertEqual(len(self.processes), 1)
        for device, task in (("a", a), ("b", b)):
            await self.manager.async_stop_device(device)
            await asyncio.wait_for(task, 3)

    async def test_a_slow_panel_skips_frames_without_holding_back_the_others(self):
        self.gates["a"] = asyncio.Event()
        a = await self.open_panel("a")
        b = await self.open_panel("b")
        await self.wait_for(lambda: self.processes)
        frames = [jpeg(80), jpeg(81), jpeg(82)]
        for index, frame in enumerate(frames, start=1):
            self.processes[0].stdout.queue.put_nowait(frame)
            await self.wait_for(lambda index=index: len(self.frames("b")) == index)
        self.assertEqual(self.frames("b"), frames)
        self.assertEqual(self.frames("a"), [])
        # The slow panel sends the frame it was holding, then only the newest.
        self.gates["a"].set()
        await self.wait_for(lambda: len(self.frames("a")) == 2)
        self.assertEqual(self.frames("a"), [frames[0], frames[2]])
        for device, task in (("a", a), ("b", b)):
            await self.manager.async_stop_device(device)
            await asyncio.wait_for(task, 3)

    async def test_a_restart_reaches_every_panel_once(self):
        a = await self.open_panel("a")
        b = await self.open_panel("b")
        await self.wait_for(lambda: self.processes)
        self.processes[0].stdout.queue.put_nowait(jpeg(64))
        await self.wait_for(lambda: self.frames("a") and self.frames("b"))
        flush = self.module.CAMERA_STREAM_MESSAGE_FLUSH
        before = {device: self.controls[device].count(flush) for device in ("a", "b")}
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            self.processes[0].terminate()  # the source ends
            await self.wait_for(lambda: len(self.processes) == 2)
        await self.wait_for(lambda: all(
            self.controls[device].count(flush) == before[device] + 1 for device in ("a", "b")))
        self.assertEqual(len(self.processes), 2)
        restarted = jpeg(66)
        self.processes[1].stdout.queue.put_nowait(restarted)
        await self.wait_for(lambda: self.frames("a")[-1:] == [restarted]
                            and self.frames("b")[-1:] == [restarted])
        for device, task in (("a", a), ("b", b)):
            await self.manager.async_stop_device(device)
            await asyncio.wait_for(task, 3)

    async def test_shutdown_stops_the_shared_pipeline_and_its_panels(self):
        a = await self.open_panel("a")
        b = await self.open_panel("b")
        await self.wait_for(lambda: self.processes)
        await self.manager.async_shutdown()
        await asyncio.wait_for(asyncio.gather(a, b), 3)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertEqual(self.manager._broadcasts, {})


if __name__ == "__main__":
    unittest.main()
