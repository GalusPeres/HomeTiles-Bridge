"""A direct camera stream must never fall behind the live picture.

A single decoding thread could not keep up with a 1440p60 OBS stream in a
Home Assistant VM (0.9x real time). The delay grew until the RTSP server
dropped data for the slow reader, and every panel showed a smeared picture.
The Bridge now decodes on several threads, restarts FFmpeg at the live
position when it falls behind, and after repeated restarts decodes only key
frames for the rest of the popup.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from test_local_camera_stream import jpeg, load_camera_stream_module

SOURCE = "rtsp://camera.local/stream"
ENTITY = "camera.garden"


class FakeHass:
    def __init__(self):
        self.data = {}

    def async_create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro, name=name)


class ScriptedPipe:
    """stdout/stderr stand-in: returns what the test puts in, b"" at exit."""

    def __init__(self):
        self.queue = asyncio.Queue()

    async def read(self, _size):
        return await self.queue.get()

    async def readline(self):
        return await self.queue.get()


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.stdout = ScriptedPipe()
        self.stderr = ScriptedPipe()
        self.stdin = None

    def progress(self, frame, out_time_us):
        for line in (f"frame={frame}", "fps=24.0", f"out_time_us={out_time_us}",
                     "speed=0.9x", "progress=continue"):
            self.stderr.queue.put_nowait(line.encode() + b"\n")

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.queue.put_nowait(b"")
            self.stderr.queue.put_nowait(b"")
            self.exited.set()

    def terminate(self):
        self._exit(-15)

    def kill(self):
        self._exit(-9)

    async def wait(self):
        await self.exited.wait()
        return self.returncode


class LagGuardTest(unittest.TestCase):
    def setUp(self):
        self.module = load_camera_stream_module()
        self.guard = self.module.CameraStreamLagGuard(1.0)

    def feed(self, reports):
        return [self.guard.update(frame, out, now) for frame, out, now in reports]

    def test_real_time_decoding_never_triggers(self):
        reports = [(n * 12, n * 0.5, 100.0 + n * 0.5) for n in range(1, 200)]
        self.assertFalse(any(self.feed(reports)))
        self.assertEqual(self.guard.lag_seconds, 0.0)

    def test_decoding_at_ninety_percent_triggers_after_about_ten_seconds(self):
        results = self.feed([(n * 12, n * 0.45, n * 0.5) for n in range(1, 40)])
        first = results.index(True)
        # Lag grows by 0.05 s per report; 1.0 s is exceeded after 21 reports.
        self.assertEqual(first, 21)
        self.assertGreater(self.guard.lag_seconds, 1.0)

    def test_a_source_pause_without_frames_is_no_lag(self):
        reports = [(12, 0.5, 0.5)]
        # The camera sends nothing for five seconds: the frame count stands.
        reports += [(12, 0.5, 0.5 + n * 0.5) for n in range(1, 11)]
        # Then it continues in real time.
        reports += [(12 + n * 12, 0.5 + n * 0.5, 5.5 + n * 0.5) for n in range(1, 20)]
        self.assertFalse(any(self.feed(reports)))

    def test_running_ahead_restarts_the_comparison(self):
        # FFmpeg drains a burst first (ahead of the wall clock) ...
        self.feed([(24, 1.0, 0.1), (48, 2.0, 0.2)])
        # ... then decodes in real time: that is not lag.
        results = self.feed([(48 + n * 12, 2.0 + n * 0.5, 0.2 + n * 0.5) for n in range(1, 20)])
        self.assertFalse(any(results))

    def test_reports_before_the_first_frame_are_ignored(self):
        self.assertFalse(self.guard.update(0, 0.0, 0.0))
        self.assertFalse(self.guard.update(0, 0.0, 30.0))
        self.assertFalse(self.guard.update(1, 0.04, 30.04))
        self.assertEqual(self.guard.lag_seconds, 0.0)


class FfmpegCommandTest(unittest.TestCase):
    def setUp(self):
        self.module = load_camera_stream_module()

    def session(self, source):
        return types.SimpleNamespace(source=source, width=752, height=424, fps=24)

    def command(self, source=SOURCE, keyframes_only=False):
        return self.module.CameraStreamConnection._ffmpeg_command(
            "ffmpeg", self.session(source), 11, keyframes_only)

    @staticmethod
    def pairs(command):
        return [list(pair) for pair in zip(command, command[1:])]

    def test_direct_stream_decodes_on_several_threads(self):
        command = self.command()
        input_at = command.index("-i")
        self.assertNotIn("low_delay", command)  # it forces a single thread
        self.assertIn(["-threads", str(self.module.CAMERA_STREAM_DECODE_THREADS)],
                      self.pairs(command[:input_at]))
        self.assertEqual(self.module.CAMERA_STREAM_DECODE_THREADS, 4)
        self.assertIn(["-progress", "pipe:2"], self.pairs(command[:input_at]))
        self.assertIn(["-fpsmax", "24"], self.pairs(command))
        self.assertNotIn("-skip_frame", command)
        self.assertNotIn("-fps_mode", command)
        # The encoder stays single threaded, the RTSP options stay in place.
        self.assertIn(["-threads:v", "1"], self.pairs(command[input_at:]))
        for pair in (["-rtsp_transport", "tcp"], ["-fflags", "nobuffer"],
                     ["-probesize", "32"], ["-analyzeduration", "0"]):
            self.assertIn(pair, self.pairs(command[:input_at]))

    def test_keyframes_only_skips_the_rest_without_repeating_frames(self):
        command = self.command(keyframes_only=True)
        input_at = command.index("-i")
        self.assertIn(["-skip_frame", "nokey"], self.pairs(command[:input_at]))
        # -fpsmax implies a constant rate and would repeat every key frame.
        self.assertNotIn("-fpsmax", command)
        self.assertIn(["-fps_mode", "vfr"], self.pairs(command[input_at:]))

    def test_still_image_cameras_keep_their_command(self):
        command = self.command(source=None)
        self.assertEqual(command[:command.index("-i") + 2], [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "image2pipe", "-framerate", "24", "-vcodec", "mjpeg",
            "-i", "pipe:0",
        ])
        self.assertNotIn("-progress", command)
        self.assertNotIn("-fpsmax", command)
        self.assertNotIn("-skip_frame", command)


class LagRestartTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.manager = self.module.CameraStreamManager(FakeHass())
        self.connection = self.manager._tcp_connection
        self.processes = []
        self.commands = []
        self.sent = []

        async def stream_source(hass, entity_id):
            return SOURCE

        async def spawn(*command, **kwargs):
            self.commands.append(command)
            process = FakeProcess()
            self.processes.append(process)
            return process

        async def send_frame(reader, writer, sequence, frame):
            self.sent.append(frame)
            return self.module.CameraFrameSendMetrics(1, 0.0, 0.0, 0.0, 0.0)

        async def send_control(writer, message_type, sequence):
            pass

        self.patches = [
            mock.patch.object(self.module, "async_get_stream_source", stream_source),
            mock.patch.object(self.module, "get_ffmpeg_manager",
                              lambda hass: types.SimpleNamespace(binary="ffmpeg")),
            mock.patch.object(self.module.asyncio, "create_subprocess_exec", spawn),
            mock.patch.object(self.module, "CAMERA_STREAM_MAX_LAG_SECONDS", 0.05),
        ]
        for patcher in self.patches:
            patcher.start()
        self.connection._async_send_frame = send_frame
        self.connection._async_send_control = send_control

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    async def wait_for(self, predicate, timeout=3.0):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def fall_behind(self, process):
        """Frames keep coming while FFmpeg's output clock stands still."""
        process.stdout.queue.put_nowait(jpeg(64))
        await self.wait_for(lambda: self.sent)
        process.progress(1, 0)
        await asyncio.sleep(0.1)
        process.progress(5, 0)

    async def test_lagging_ffmpeg_restarts_live_then_falls_back_to_key_frames(self):
        session = await self.manager.async_create_session("viewer", ENTITY, 752, 424, 24)
        await self.manager.async_take_session(session.token)
        with self.assertLogs(self.module._LOGGER, "WARNING") as logs:
            task = asyncio.create_task(self.connection._async_stream(session, None, None))
            await self.wait_for(lambda: self.processes)
            await self.fall_behind(self.processes[0])
            await self.wait_for(lambda: len(self.processes) == 2)
            self.assertEqual(self.processes[0].returncode, -15)
            self.assertNotIn("-skip_frame", self.commands[1])
            self.assertIn("-fpsmax", self.commands[1])

            await self.fall_behind(self.processes[1])
            await self.wait_for(lambda: len(self.processes) == 3)
            self.assertIn("-skip_frame", self.commands[2])
            self.assertNotIn("-fpsmax", self.commands[2])

            # Key frames arrive seconds apart: that is not watched as lag.
            third = self.processes[2]
            third.progress(1, 0)
            await asyncio.sleep(0.1)
            third.progress(2, 0)
            await asyncio.sleep(0.1)
            self.assertIsNone(third.returncode)
            self.assertEqual(len(self.processes), 3)

            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 3)
        messages = [record.getMessage() for record in logs.records]
        self.assertTrue(any("behind the live stream; restarting at the live position" in m
                            for m in messages), messages)
        self.assertTrue(any("showing key frames only until the popup closes" in m
                            for m in messages), messages)
        self.assertFalse(any("source ended" in m for m in messages), messages)

    async def test_a_single_lag_restart_is_forgotten_after_the_window(self):
        session = await self.manager.async_create_session("viewer", ENTITY, 752, 424, 24)
        await self.manager.async_take_session(session.token)
        with mock.patch.object(self.module, "CAMERA_STREAM_LAG_WINDOW_SECONDS", 0.2), \
                self.assertLogs(self.module._LOGGER, "WARNING"):
            task = asyncio.create_task(self.connection._async_stream(session, None, None))
            await self.wait_for(lambda: self.processes)
            await self.fall_behind(self.processes[0])
            await self.wait_for(lambda: len(self.processes) == 2)
            await asyncio.sleep(0.3)  # longer than the window
            await self.fall_behind(self.processes[1])
            await self.wait_for(lambda: len(self.processes) == 3)
            self.assertNotIn("-skip_frame", self.commands[2])
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 3)

    async def test_ffmpeg_progress_lines_are_not_logged_as_warnings(self):
        process = FakeProcess()
        session = types.SimpleNamespace(entity_id=ENTITY)
        with mock.patch.object(self.module._LOGGER, "debug") as debug:
            process.progress(1, 40000)
            process.stderr.queue.put_nowait(b"[h264 @ 0x1] error while decoding MB 1 2\n")
            process.stderr.queue.put_nowait(b"")
            lag = await self.connection._async_log_ffmpeg_stderr(
                session, process, False, watch_lag=True)
        self.assertIsNone(lag)
        self.assertEqual(debug.call_count, 1)


if __name__ == "__main__":
    unittest.main()
