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


class StreamRateTest(unittest.TestCase):
    """Firmware since b134 asks for 30 FPS and falls back to 24 on rejection."""

    def setUp(self):
        self.module = load_camera_stream_module()
        self.validate = self.module.CameraStreamManager._validate_stream_request

    def test_panels_may_ask_for_up_to_30_fps(self):
        self.assertEqual(self.module.CAMERA_STREAM_MAX_FPS, 30)
        self.assertEqual(self.validate(752, 424, 30), (752, 424, 30))
        self.assertEqual(self.validate(752, 424, 24), (752, 424, 24))
        for fps in (0, 31, 60):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                self.validate(752, 424, fps)

    def test_a_request_without_a_rate_keeps_24_fps(self):
        self.assertEqual(self.module.CAMERA_STREAM_FPS, 24)

    def test_30_fps_thins_the_source_to_30(self):
        session = types.SimpleNamespace(source=SOURCE, width=752, height=424, fps=30)
        command = self.module.CameraStreamConnection._ffmpeg_command("ffmpeg", session, 11)
        self.assertIn("gt(floor(t*30),floor(prev_selected_t*30))", command[command.index("-vf") + 1])
        self.assertIn(["-fpsmax", "30"], [list(p) for p in zip(command, command[1:])])


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

    def test_inside_keeps_the_whole_picture_without_bars(self):
        # b26: the ESP32-S3's full screen asks for "inside"; its software
        # decoder spent almost half of each 480 x 480 contain frame on bars.
        session = types.SimpleNamespace(source=SOURCE, width=480, height=480, fps=8,
                                        view="full", rotate=0, fit="inside")
        filters = self.filters(self.module.CameraStreamConnection._ffmpeg_command(
            "ffmpeg", session, 11))
        self.assertIn("scale=480:480:force_original_aspect_ratio=decrease:force_divisible_by=2:", filters)
        self.assertNotIn("pad=", filters)
        self.assertNotIn("crop=", filters)
        self.assertTrue(filters.endswith(":flags=area,setsar=1"))
        validate = self.module.CameraStreamManager._validate_view
        self.assertEqual(validate("full", 0, "inside"), ("full", 0, "inside"))
        self.assertEqual(validate("full", 0, None), ("full", 0, "contain"))
        with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
            validate("full", 0, "stretch")

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

        async def send_frame(reader, writer, sequence, frame, **_options):
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
        # One warning per stream; later restarts go to the debug log.
        self.assertEqual(len(lag), 1, warnings)
        self.assertIn("(1 in this stream)", lag[0])
        debug = [r.getMessage() for r in logs.records if r.levelname == "DEBUG"]
        self.assertTrue(any("(3 in this stream)" in m for m in debug), debug)
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



class FullScreenFrameTest(unittest.TestCase):
    """#65: full-screen frames in the panel's own framebuffer size and
    orientation, which its JPEG decoder writes straight into the framebuffer
    (no scaling or turning on the panel)."""

    def setUp(self):
        self.module = load_camera_stream_module()
        self.manager = self.module.CameraStreamManager

    def filters(self, rotate, fit, width=800, height=1280):
        session = types.SimpleNamespace(source=SOURCE, width=width, height=height, fps=30,
                                        view="full", rotate=rotate, fit=fit)
        command = self.module.CameraStreamConnection._ffmpeg_command("ffmpeg", session, 11)
        return command[command.index("-vf") + 1]

    def test_full_sizes_are_framebuffers_decoded_without_padding(self):
        validate = self.manager._validate_stream_request
        for size in ((800, 1280), (720, 1280), (1280, 800), (1280, 720)):
            self.assertEqual(validate(*size, 30, "full"), (*size, 30))
        # Sides off the 16 px decoder blocks, too small, too large.
        for size in ((1024, 600), (800, 1290), (1296, 800), (304, 1280), (1280, 1280)):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                validate(*size, 30, "full")
        # The popup keeps its own limits.
        with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
            validate(800, 1280, 30)
        self.assertEqual(validate(752, 424, 30), (752, 424, 30))

    def test_view_turn_and_placement_are_checked(self):
        view = self.manager._validate_view
        self.assertEqual(view(None, None, None), ("popup", 0, "cover"))
        self.assertEqual(view("popup", 90, "contain"), ("popup", 0, "cover"))
        self.assertEqual(view("full", 270, None), ("full", 270, "contain"))
        self.assertEqual(view("FULL", "90", "cover"), ("full", 90, "cover"))
        for args in (("wall", 0, "contain"), ("full", 45, "contain"),
                     ("full", "x", "contain"), ("full", 90, "stretch")):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                view(*args)

    def test_contain_places_the_whole_picture_upright_then_turns_it(self):
        filters = self.filters(270, "contain")
        # The landscape screen of an 800 x 1280 panel turned a quarter.
        self.assertIn("scale=1280:800:force_original_aspect_ratio=decrease:"
                      "force_divisible_by=2:", filters)
        self.assertTrue(filters.endswith(
            "pad=1280:800:(ow-iw)/2:(oh-ih)/2:color=black,transpose=2,setsar=1"))

    def test_cover_fills_the_screen_and_turns_the_other_way(self):
        filters = self.filters(90, "cover")
        self.assertIn("scale=1280:800:force_original_aspect_ratio=increase:", filters)
        self.assertTrue(filters.endswith("crop=1280:800,transpose=1,setsar=1"))

    def test_no_turn_and_a_half_turn(self):
        self.assertTrue(self.filters(0, "cover").endswith("crop=800:1280,setsar=1"))
        self.assertTrue(self.filters(180, "cover", 1280, 800).endswith(
            "crop=1280:800,hflip,vflip,setsar=1"))

    def test_the_popup_frame_is_unchanged(self):
        session = types.SimpleNamespace(source=SOURCE, width=752, height=424, fps=30)
        command = self.module.CameraStreamConnection._ffmpeg_command("ffmpeg", session, 11)
        filters = command[command.index("-vf") + 1]
        self.assertTrue(filters.endswith(
            "scale=752:424:force_original_aspect_ratio=increase:out_color_matrix=bt601:"
            "out_range=full:flags=area,crop=752:424,setsar=1"))
        self.assertNotIn("transpose", filters)
        self.assertNotIn("pad=", filters)



class ChunkWindowTest(unittest.IsolatedAsyncioTestCase):
    """#65: a full-screen panel asks for two chunks in flight; one stays the
    rule for every other stream (the P4's network memory)."""

    def setUp(self):
        self.module = load_camera_stream_module()

    async def send(self, size, window, ack_shift=0):
        module = self.module
        chunk = module.CAMERA_STREAM_CHUNK_BYTES
        jpeg = bytes(range(256)) * (size // 256) + bytes(size % 256)
        written = []
        in_flight = []
        most = 0
        acks = asyncio.Queue()

        class Writer:
            def write(self, data):
                nonlocal most
                written.append(bytes(data))
                if len(written) > 1:  # chunks after the frame header
                    sent = sum(len(d) for d in written[1:])
                    in_flight.append(sent)
                    most = max(most, len(in_flight))

            async def drain(self):
                # The panel acknowledges each chunk it has read, oldest first.
                while in_flight:
                    end = in_flight.pop(0)
                    acks.put_nowait(module.CAMERA_STREAM_ACK_STRUCT.pack(
                        module.CAMERA_STREAM_ACK_MAGIC, 7, end + ack_shift))

        class Reader:
            async def readexactly(self, n):
                return await acks.get()

        writer = Writer()
        original = writer.drain

        async def drain_after_window():
            # Hold the acknowledgements until the sender stops writing: it
            # must never have more than `window` chunks out.
            if len(in_flight) < window and sum(len(d) for d in written[1:]) < len(jpeg):
                return
            await original()

        writer.drain = drain_after_window
        metrics = await module.CameraStreamConnection._async_send_frame(
            Reader(), writer, 7, jpeg, window=window)
        return jpeg, written, metrics, most, chunk

    async def test_one_chunk_in_flight_by_default(self):
        jpeg, written, metrics, most, chunk = await self.send(50_000, 1)
        self.assertEqual(b"".join(written[1:]), jpeg)
        self.assertEqual(most, 1)
        self.assertEqual(metrics.chunks, -(-len(jpeg) // chunk))

    async def test_two_chunks_in_flight_when_asked(self):
        jpeg, written, metrics, most, chunk = await self.send(50_000, 2)
        self.assertEqual(b"".join(written[1:]), jpeg)
        self.assertEqual(most, 2)
        self.assertEqual(metrics.chunks, -(-len(jpeg) // chunk))

    async def test_four_chunks_in_flight_when_asked(self):
        jpeg, written, metrics, most, chunk = await self.send(70_000, 4)
        self.assertEqual(b"".join(written[1:]), jpeg)
        self.assertEqual(most, 4)
        self.assertEqual(metrics.chunks, -(-len(jpeg) // chunk))

    async def test_a_wrong_acknowledgement_still_ends_the_stream(self):
        with self.assertRaisesRegex(ValueError, "camera_invalid_ack"):
            await self.send(50_000, 2, ack_shift=1)

    async def test_frames_follow_without_waiting_for_the_last_acks(self):
        # b320: the next frame starts while the last chunks of the previous
        # one are in flight; never more than `window` chunks unacknowledged.
        from collections import deque
        module = self.module
        header = module.CAMERA_STREAM_FRAME_STRUCT.size
        pending = deque()
        payload = []
        most = 0

        class Writer:
            def write(self, data):
                nonlocal most
                if len(data) != header:
                    payload.append(bytes(data))
                    most = max(most, len(pending) + 1)

            async def drain(self):
                pass

        class Reader:
            async def readexactly(self, n):
                sequence, end = pending[0]
                return module.CAMERA_STREAM_ACK_STRUCT.pack(
                    module.CAMERA_STREAM_ACK_MAGIC, sequence, end)

        first = bytes(range(256)) * 200      # 51200 bytes, two 32 KB chunks
        second = bytes(range(255, -1, -1)) * 100
        send = module.CameraStreamConnection._async_send_frame
        await send(Reader(), Writer(), 1, first, window=2, chunk_bytes=32768, pending=pending)
        self.assertEqual(len(pending), 2, "the first frame's chunks stay in flight")
        await send(Reader(), Writer(), 2, second, window=2, chunk_bytes=32768, pending=pending)
        self.assertEqual(b"".join(payload), first + second)
        self.assertEqual([len(p) for p in payload], [32768, 18432, 25600])
        self.assertLessEqual(most, 2)
        self.assertEqual(list(pending), [(1, 51200), (2, 25600)][-len(pending):])

    def test_the_chunk_size_is_checked(self):
        validate = self.module.CameraStreamManager._validate_chunk
        self.assertEqual(validate(None), 8192)
        for chunk in (8192, 16384, 32768):
            self.assertEqual(validate(chunk), chunk)
        for chunk in (4096, 65536, 10000, "x"):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                validate(chunk)

    def test_the_jpeg_quality_is_checked(self):
        validate = self.module.CameraStreamManager._validate_quality
        self.assertIsNone(validate(None))
        self.assertEqual(validate(2), 2)
        self.assertEqual(validate("5"), 5)
        self.assertEqual(validate(31), 31)
        for quality in (0, 1, 32, "x", True):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                validate(quality)

    def test_the_window_is_checked(self):
        validate = self.module.CameraStreamManager._validate_window
        self.assertEqual(validate(None), 1)
        self.assertEqual(validate(2), 2)
        self.assertEqual(validate("1"), 1)
        self.assertEqual(validate(4), 4)
        for window in (0, 5, "x"):
            with self.assertRaisesRegex(ValueError, "camera_invalid_stream_request"):
                validate(window)


if __name__ == "__main__":
    unittest.main()
