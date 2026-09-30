"""A direct camera stream must never fall behind the live picture.

FFmpeg could not keep up with a 1440p60 OBS stream in a Home Assistant VM
(0.9x real time). The delay grew until the RTSP server dropped data for the
slow reader, and every panel showed a smeared picture. The larger part of
the work was scaling all 60 frames per second on one thread before -fpsmax
dropped most of them. The Bridge now thins the frames to the session rate
before scaling, decodes on several threads, and restarts FFmpeg at the live
position when it still falls behind. v0.7.1b7 switched to key frames only
after two restarts, which left the popup at one picture per second; that
fallback is gone.
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

    def command(self, source=SOURCE):
        return self.module.CameraStreamConnection._ffmpeg_command(
            "ffmpeg", self.session(source), 11)

    def filters(self, command):
        return command[command.index("-vf") + 1]

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
        # No key frames only fallback (v0.7.1b7: one picture per second).
        self.assertNotIn("-skip_frame", command)
        self.assertNotIn("-fps_mode", command)
        # The encoder stays single threaded, the RTSP options stay in place.
        self.assertIn(["-threads:v", "1"], self.pairs(command[input_at:]))
        for pair in (["-rtsp_transport", "tcp"], ["-fflags", "nobuffer"],
                     ["-probesize", "32"], ["-analyzeduration", "0"]):
            self.assertIn(pair, self.pairs(command[:input_at]))

    def test_frames_are_thinned_to_the_session_rate_before_scaling(self):
        filters = self.filters(self.command()).split(",scale=")
        self.assertEqual(len(filters), 2)
        # First frame of each 1/24 s slot: 60 and 30 FPS become 24, a 10 FPS
        # source keeps its 10 without repeated frames (checked with FFmpeg).
        self.assertEqual(
            filters[0],
            "select='isnan(prev_selected_t)+gt(floor(t*24),floor(prev_selected_t*24))'")
        self.assertIn(":flags=area", filters[1])
        self.assertTrue(filters[1].endswith("crop=752:424,setsar=1"))

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
        self.assertNotIn("select=", self.filters(command))
        self.assertNotIn("flags=area", self.filters(command))


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

    async def test_lagging_ffmpeg_restarts_live_and_keeps_the_full_stream(self):
        session = await self.manager.async_create_session("viewer", ENTITY, 752, 424, 24)
        await self.manager.async_take_session(session.token)
        with self.assertLogs(self.module._LOGGER, "DEBUG") as logs:
            task = asyncio.create_task(self.connection._async_stream(session, None, None))
            await self.wait_for(lambda: self.processes)
            for restart in range(1, 4):
                await self.fall_behind(self.processes[restart - 1])
                await self.wait_for(lambda restart=restart: len(self.processes) == restart + 1)
                self.assertEqual(self.processes[restart - 1].returncode, -15)
            # Every restart runs the same full-rate command, never key frames only.
            self.assertTrue(all(command == self.commands[0] for command in self.commands))
            self.assertIn("-fpsmax", self.commands[-1])
            self.assertNotIn("-skip_frame", self.commands[-1])
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 3)
        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        lag = [m for m in warnings if "behind the live stream; restarting at the live position" in m]
        # One warning per popup; later restarts go to the debug log.
        self.assertEqual(len(lag), 1, warnings)
        self.assertIn("(1 in this popup)", lag[0])
        debug = [r.getMessage() for r in logs.records if r.levelname == "DEBUG"]
        self.assertTrue(any("(3 in this popup)" in m for m in debug), debug)
        self.assertFalse(any("source ended" in m for m in warnings), warnings)

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
