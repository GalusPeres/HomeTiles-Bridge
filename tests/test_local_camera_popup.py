"""Another panel's camera popup joins a panel camera's live upload as a viewer."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import types
import unittest
from unittest import mock

from test_local_camera_platform import BASE, READY, FakeMqtt, entry, load_camera_module, message
from test_local_camera_stream import (
    SESSION,
    STREAM,
    TOKEN,
    FakeRegistry,
    jpeg,
    load_camera_stream_module,
)

ENTITY = "camera.kitchen_panel_camera"
OTHER = "camera.garden"
SNAPSHOT = jpeg(90)


class FakeHass:
    def __init__(self):
        self.data = {}

    def async_create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro, name=name)


class FakeStdin:
    def __init__(self, process):
        self._process = process
        self._closing = False

    def write(self, data):
        self._process.written.append(data)
        if self._process.echo:
            # FFmpeg stand-in: every input JPEG becomes one output JPEG.
            self._process.stdout.queue.put_nowait(data)

    async def drain(self):
        await asyncio.sleep(0)

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True

    async def wait_closed(self):
        pass


class FakeStdout:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def read(self, _size):
        return await self.queue.get()


class FakeStderr:
    def __init__(self, process):
        self._process = process

    async def readline(self):
        await self._process.exited.wait()
        return b""


class FakeProcess:
    def __init__(self, echo=True):
        self.echo = echo
        self.written = []
        self.returncode = None
        self.exited = asyncio.Event()
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.stderr = FakeStderr(self)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.queue.put_nowait(b"")
            self.exited.set()

    def terminate(self):
        self._exit(-15)

    def kill(self):
        self._exit(-9)

    async def wait(self):
        await self.exited.wait()
        return self.returncode


def live_stream(published):
    registry = FakeRegistry()

    async def publish(request):
        published.append(request)

    async def endpoint():
        return ("192.168.1.10", 8125)

    live = STREAM.LocalCameraLiveStream(
        publish=publish, endpoint=endpoint, registry=lambda: registry, ready=lambda: True,
        log_name=BASE, keepalive_s=0.05, grace_s=0.05,
        session_factory=lambda: SESSION, token_factory=lambda: TOKEN)
    return live, registry


class PopupLiveViewerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.hass = FakeHass()
        self.manager = self.module.CameraStreamManager(self.hass)
        self.connection = self.manager._tcp_connection
        self.published = []
        self.live, self.registry = live_stream(self.published)
        self.panel = types.SimpleNamespace(entity_id=ENTITY, live_stream=self.live)
        self.unregister = self.manager.register_live_camera(self.panel)
        self.image_calls = []
        self.processes = []
        self.commands = []
        self.sent = []
        self.send_error = None
        self.patches = [
            mock.patch.object(self.module, "async_get_stream_source", self.stream_source),
            mock.patch.object(self.module, "async_get_image", self.get_image),
            mock.patch.object(self.module, "get_ffmpeg_manager",
                              lambda hass: types.SimpleNamespace(binary="ffmpeg")),
            mock.patch.object(self.module.asyncio, "create_subprocess_exec", self.spawn),
        ]
        for patcher in self.patches:
            patcher.start()
        self.connection._async_send_frame = self.send_frame
        self.connection._async_send_control = self.send_control

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        await self.live.async_close()

    async def stream_source(self, hass, entity_id):
        return None

    async def get_image(self, hass, entity_id, **kwargs):
        self.image_calls.append(entity_id)
        return types.SimpleNamespace(content=SNAPSHOT)

    async def spawn(self, *command, **kwargs):
        self.commands.append(command)
        process = FakeProcess()
        self.processes.append(process)
        return process

    async def send_frame(self, reader, writer, sequence, frame):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(frame)
        return self.module.CameraFrameSendMetrics(1, 0.0, 0.0, 0.0, 0.0)

    async def send_control(self, writer, message_type, sequence):
        pass

    async def start(self, entity_id=ENTITY, fps=24):
        session = await self.manager.async_create_session("viewer", entity_id, 752, 424, fps)
        self.assertIs(await self.manager.async_take_session(session.token), session)
        task = asyncio.create_task(self.connection._async_stream(session, None, None))
        return session, task

    async def wait_for(self, predicate, timeout=1.0):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    def upload(self, frame):
        # The panel's acknowledged upload delivers frames through the registry.
        self.registry.sessions[SESSION][1](frame)

    # Session resolution ------------------------------------------------

    async def test_registry_finds_only_open_live_cameras_by_entity_id(self):
        self.assertIs(self.manager.live_camera_stream(ENTITY), self.live)
        self.assertIsNone(self.manager.live_camera_stream(OTHER))
        self.unregister()
        self.unregister()  # idempotent
        self.assertIsNone(self.manager.live_camera_stream(ENTITY))
        self.manager.register_live_camera(self.panel)
        await self.live.async_close()
        self.assertIsNone(self.manager.live_camera_stream(ENTITY))

    async def test_live_panel_camera_session_uses_the_live_cadence(self):
        session = await self.manager.async_create_session("viewer", ENTITY, 752, 424, 24)
        self.assertIs(session.live, self.live)
        # The popup asks for 24 fps; the live upload is not the limit any more.
        self.assertEqual(session.fps, min(24, STREAM.STREAM_FPS))
        self.assertEqual(session.fps, 24)
        self.assertEqual(session.first_image, SNAPSHOT)
        # Creating a session alone never starts the panel upload.
        self.assertEqual(self.live.viewers, 0)
        self.assertEqual(self.published, [])

    async def test_other_still_cameras_use_the_capped_still_rate(self):
        session = await self.manager.async_create_session("viewer", OTHER, 752, 424, 24)
        self.assertIsNone(session.live)
        self.assertEqual(self.module.CAMERA_STILL_MAX_FPS, 10)
        self.assertEqual(session.fps, self.module.CAMERA_STILL_MAX_FPS)
        slower = await self.manager.async_create_session("viewer", OTHER, 752, 424, 5)
        self.assertEqual(slower.fps, 5)

    async def test_stream_source_cameras_keep_the_direct_stream_session(self):
        async def stream_source(hass, entity_id):
            return "rtsp://camera.local/stream"

        with mock.patch.object(self.module, "async_get_stream_source", stream_source), \
                mock.patch.object(self.connection, "_async_feed_camera_images") as feeder:
            session = await self.manager.async_create_session("viewer", OTHER, 752, 424, 24)
            self.assertEqual(session.fps, 24)
            self.assertIsNone(session.first_image)
            self.assertIsNone(session.live)
            self.assertEqual(self.image_calls, [])
            await self.manager.async_take_session(session.token)
            task = asyncio.create_task(self.connection._async_stream(session, None, None))
            await self.wait_for(lambda: self.commands)
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 2)
        feeder.assert_not_called()
        pairs = [list(pair) for pair in zip(self.commands[0], self.commands[0][1:])]
        self.assertIn(["-fpsmax", "24"], pairs)
        self.assertIn(["-q:v", str(self.module.CAMERA_STREAM_JPEG_QUALITY)], pairs)
        self.assertNotIn("-framerate", self.commands[0])

    # Viewer lifetime ---------------------------------------------------

    async def test_popup_is_a_live_viewer_until_the_popup_closes(self):
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 5.0):
            session, task = await self.start()
            await self.wait_for(lambda: self.live.viewers == 1 and self.published)
            self.assertEqual(self.published[0]["action"], "stream")
            await self.wait_for(lambda: SNAPSHOT in self.sent)
            frames = [jpeg(40), jpeg(41), jpeg(42)]
            for frame in frames:
                self.upload(frame)
                await self.wait_for(lambda frame=frame: frame in self.sent)
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)
        self.assertEqual(self.image_calls, [ENTITY])  # only the first snapshot
        # running turns False when the stop is requested, before the run
        # task's cleanup publishes stream_stop, so wait for the publish.
        stop = {"v": 1, "action": "stream_stop", "session": SESSION}
        await self.wait_for(lambda: self.published[-1] == stop)
        self.assertFalse(self.live.running)
        self.assertIn(["-q:v", str(self.module.CAMERA_STREAM_JPEG_QUALITY)],
                      [list(pair) for pair in zip(self.commands[0], self.commands[0][1:])])

    async def test_send_error_releases_the_viewer(self):
        self.send_error = ConnectionResetError()
        _session, task = await self.start()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    async def test_cancel_releases_the_viewer(self):
        _session, task = await self.start()
        await self.wait_for(lambda: self.live.viewers == 1 and self.sent)
        task.cancel()
        # The stream swallows the cancellation after its cleanup ran.
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)

    async def test_ffmpeg_exit_keeps_one_viewer_across_restarts(self):
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 5.0), \
                self.assertLogs(self.module._LOGGER, "WARNING"):
            _session, task = await self.start()
            await self.wait_for(lambda: self.sent)
            self.processes[0].terminate()  # FFmpeg exits on its own
            await self.wait_for(lambda: len(self.processes) == 2, timeout=2)
            self.assertEqual(self.live.viewers, 1)
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)

    async def test_replaced_session_and_shutdown_release_the_viewer(self):
        _first, task = await self.start()
        await self.wait_for(lambda: self.live.viewers == 1)
        # Opening another camera on the same display replaces the session.
        second = await self.manager.async_create_session("viewer", ENTITY, 752, 424, 24)
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)
        await self.manager.async_take_session(second.token)
        task = asyncio.create_task(self.connection._async_stream(second, None, None))
        await self.wait_for(lambda: self.live.viewers == 1)
        await self.manager.async_shutdown()
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.live.viewers, 0)

    async def test_reloaded_panel_camera_moves_the_viewer_to_the_new_stream(self):
        published = []
        reloaded, registry = live_stream(published)
        try:
            with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 5.0):
                session, task = await self.start()
                await self.wait_for(lambda: self.live.viewers == 1 and self.sent)
                # Entity reload: the old stream closes, the new entity registers.
                self.unregister()
                self.manager.register_live_camera(
                    types.SimpleNamespace(entity_id=ENTITY, live_stream=reloaded))
                await self.live.async_close()
                await self.wait_for(lambda: reloaded.viewers == 1, timeout=2)
                self.assertEqual(self.live.viewers, 0)
                self.assertIs(session.live, reloaded)
                await self.wait_for(lambda: SESSION in registry.sessions)
                frame = jpeg(55)
                registry.sessions[SESSION][1](frame)
                await self.wait_for(lambda: frame in self.sent)
                await self.manager.async_stop_device("viewer")
                await asyncio.wait_for(task, 2)
            self.assertEqual(reloaded.viewers, 0)
            self.assertEqual(self.live.viewers, 0)
        finally:
            await reloaded.async_close()

    async def test_non_panel_camera_keeps_the_still_image_pipeline(self):
        with mock.patch.object(self.connection, "_async_feed_camera_images",
                               wraps=self.connection._async_feed_camera_images) as feeder, \
                mock.patch.object(self.connection, "_async_feed_live_frames") as live_feeder:
            _session, task = await self.start(OTHER)
            await self.wait_for(lambda: self.sent)
            await self.manager.async_stop_device("viewer")
            await asyncio.wait_for(task, 2)
        feeder.assert_called_once()
        live_feeder.assert_not_called()
        self.assertEqual(self.live.viewers, 0)
        self.assertEqual(self.published, [])
        pairs = [list(pair) for pair in zip(self.commands[0], self.commands[0][1:])]
        self.assertIn(["-q:v", str(self.module.CAMERA_STILL_JPEG_QUALITY)], pairs)
        self.assertIn(["-framerate", str(self.module.CAMERA_STILL_MAX_FPS)], pairs)


class LiveFeederTest(unittest.IsolatedAsyncioTestCase):
    """The FFmpeg feeder for a live panel camera, driven directly."""

    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.hass = FakeHass()
        self.manager = self.module.CameraStreamManager(self.hass)
        self.connection = self.manager._tcp_connection
        self.published = []
        self.live, self.registry = live_stream(self.published)
        self.image_calls = []
        self.image_content = SNAPSHOT
        self.patches = [
            mock.patch.object(self.module, "async_get_image", self.get_image),
            # Idle repeats every 50 ms instead of 500 ms.
            mock.patch.object(self.module, "CAMERA_STILL_REPEAT_SECONDS", 0.05),
            mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.2),
        ]
        for patcher in self.patches:
            patcher.start()

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        await self.live.async_close()

    async def get_image(self, hass, entity_id, **kwargs):
        self.image_calls.append(entity_id)
        return types.SimpleNamespace(content=self.image_content)

    def session(self, first=b"first", fps=15):
        return types.SimpleNamespace(entity_id=ENTITY, first_image=first, width=752, height=424,
                                     fps=fps, live=self.live, latest_image=None)

    async def feed(self, session, seconds):
        process = FakeProcess(echo=False)
        task = asyncio.create_task(self.connection._async_feed_live_frames(session, process))
        await asyncio.sleep(seconds)
        process.terminate()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return process

    async def test_keeps_the_first_snapshot_without_busy_looping(self):
        self.live.acquire()
        process = await self.feed(self.session(), 0.15)
        self.assertTrue(process.written)
        self.assertEqual(set(process.written), {b"first"})
        # Idle repeats at the still cadence (50 ms here), not a busy loop.
        self.assertLessEqual(len(process.written), 5)
        self.assertEqual(self.image_calls, [])
        self.live.release()

    async def test_new_live_frames_are_written_once_at_the_popup_cadence(self):
        # Real idle cadence (500 ms): frames 100 ms apart are never repeated.
        self.patches.append(mock.patch.object(self.module, "CAMERA_STILL_REPEAT_SECONDS", 0.5))
        self.patches[-1].start()
        self.live.acquire()
        await asyncio.sleep(0.01)
        process = FakeProcess(echo=False)
        task = asyncio.create_task(self.connection._async_feed_live_frames(self.session(), process))
        frames = [jpeg(30 + index) for index in range(4)]
        for frame in frames:
            await asyncio.sleep(0.1)
            self.registry.sessions[SESSION][1](frame)
        await asyncio.sleep(0.1)
        process.terminate()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.live.release()
        written = [frame for frame in process.written if frame != b"first"]
        self.assertEqual(written, frames)
        self.assertEqual(self.image_calls, [])

    async def test_restart_resumes_from_the_newest_live_frame(self):
        self.live.acquire()
        await asyncio.sleep(0.01)
        frame = jpeg(33)
        self.registry.sessions[SESSION][1](frame)
        process = await self.feed(self.session(), 0.05)
        self.live.release()
        self.assertEqual(process.written[0], frame)
        self.assertNotIn(b"first", process.written)

    async def test_late_snapshot_failure_does_not_count_after_live_resumed(self):
        snapshot_started = asyncio.Event()
        snapshot_release = asyncio.Event()

        async def slow_empty_image(hass, entity_id, **kwargs):
            snapshot_started.set()
            await snapshot_release.wait()
            return types.SimpleNamespace(content=b"")

        self.live.acquire()
        await asyncio.sleep(0.01)
        process = FakeProcess(echo=False)
        with mock.patch.object(self.module, "async_get_image", slow_empty_image), \
                mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.1), \
                mock.patch.object(self.module, "CAMERA_IMAGE_FAILURE_LIMIT", 1):
            task = asyncio.create_task(
                self.connection._async_feed_live_frames(self.session(), process))
            await asyncio.wait_for(snapshot_started.wait(), 1)
            for index in range(4):
                self.registry.sessions[SESSION][1](jpeg(60 + index))
                await asyncio.sleep(0.02)
            snapshot_release.set()
            # A live frame written after the empty snapshot resolved proves the
            # feeder handled that result without ending the stream.
            async with asyncio.timeout(2):
                index = 0
                while not any(len(frame) >= 70 for frame in process.written):
                    self.assertFalse(task.done())
                    self.registry.sessions[SESSION][1](jpeg(70 + index % 10))
                    index += 1
                    await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            process.terminate()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.live.release()

    async def test_restart_without_a_fresh_live_frame_resumes_from_the_newest_frame(self):
        session = self.session()
        session.latest_image = jpeg(34)
        process = await self.feed(session, 0.05)
        self.assertEqual(process.written[0], jpeg(34))
        self.assertNotIn(b"first", process.written)

    async def test_stale_live_frames_are_not_reused(self):
        self.live._frame = jpeg(20)
        self.live._frame_at = -100.0
        process = await self.feed(self.session(), 0.1)
        self.assertEqual(set(process.written), {b"first"})

    async def test_falls_back_to_snapshots_when_no_live_frame_arrives(self):
        # No viewer loop running: the upload never delivers a frame.
        process = await self.feed(self.session(), 0.45)
        self.assertEqual(process.written[0], b"first")
        self.assertIn(SNAPSHOT, process.written)
        self.assertTrue(self.image_calls)
        self.assertTrue(all(entity == ENTITY for entity in self.image_calls))

    async def test_closed_live_stream_falls_back_without_busy_looping(self):
        await self.live.async_close()
        process = await self.feed(self.session(), 0.45)
        self.assertLessEqual(len(process.written), 12)
        self.assertIn(SNAPSHOT, process.written)

    async def test_failing_fallback_ends_after_the_failure_limit(self):
        self.image_content = b""
        process = FakeProcess(echo=False)
        with mock.patch.object(self.module, "LIVE_FRAME_WAIT_S", 0.0), \
                mock.patch.object(self.module, "CAMERA_IMAGE_FAILURE_LIMIT", 3), \
                self.assertLogs(self.module._LOGGER, "WARNING"):
            await asyncio.wait_for(
                self.connection._async_feed_live_frames(self.session(), process), 2)
        self.assertTrue(process.stdin.is_closing())


class StillFeederTest(unittest.IsolatedAsyncioTestCase):
    """The adaptive FFmpeg feeder for still-image cameras, driven directly."""

    async def asyncSetUp(self):
        self.module = load_camera_stream_module()
        self.manager = self.module.CameraStreamManager(FakeHass())
        self.connection = self.manager._tcp_connection
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.returned = []
        self.cancelled = 0
        self.delay = 0.0
        self.content = None  # None: a new distinct JPEG per request
        self.error = None
        self.kwargs = []
        self.patcher = mock.patch.object(self.module, "async_get_image", self.get_image)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()

    async def get_image(self, hass, entity_id, **kwargs):
        self.kwargs.append(kwargs)
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            content = self.content if self.content is not None else jpeg(100 + self.calls)
            self.returned.append(content)
            return types.SimpleNamespace(content=content)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1

    def session(self, fps=10):
        return self.module.CameraStreamSession(
            token="t", device_id="viewer", entity_id=OTHER, source=None,
            first_image=b"first", width=752, height=424, fps=fps, expires_at=0.0,
            stop_event=asyncio.Event())

    async def feed(self, session, seconds):
        process = FakeProcess(echo=False)
        task = asyncio.create_task(self.connection._async_feed_camera_images(session, process))
        await asyncio.sleep(seconds)
        process.terminate()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return process

    async def test_fast_camera_is_fetched_back_to_back_up_to_the_rate_cap(self):
        process = await self.feed(self.session(fps=24), 0.55)
        # 10 fps cap: about one request per 100 ms, never two at once.
        self.assertGreaterEqual(self.calls, 4)
        self.assertLessEqual(self.calls, 7)
        self.assertEqual(self.max_in_flight, 1)
        # Every new image is written exactly once, in order, without repeats.
        self.assertEqual(process.written[0], b"first")
        self.assertEqual(process.written[1:], self.returned[:len(process.written) - 1])
        self.assertGreaterEqual(len(process.written), self.calls)
        self.assertEqual(len(set(process.written)), len(process.written))
        self.assertTrue(process.stdin.is_closing())

    async def test_still_fetch_leaves_scaling_to_ffmpeg(self):
        # A width/height request would make Home Assistant rescale every JPEG
        # on its event loop, up to 10 times per second per popup.
        await self.feed(self.session(), 0.25)
        self.assertGreaterEqual(len(self.kwargs), 1)
        for kwargs in self.kwargs:
            self.assertNotIn("width", kwargs)
            self.assertNotIn("height", kwargs)

    async def test_lower_session_rate_is_respected(self):
        await self.feed(self.session(fps=4), 0.6)
        self.assertGreaterEqual(self.calls, 2)
        self.assertLessEqual(self.calls, 4)

    async def test_slow_camera_keeps_its_own_pace_without_piling_up_requests(self):
        self.delay = 0.3
        process = await self.feed(self.session(), 1.0)
        # Requests start at 0, 0.3, 0.6 and 0.9 s: one per answer, not per 100 ms.
        self.assertGreaterEqual(self.calls, 3)
        self.assertLessEqual(self.calls, 4)
        self.assertEqual(self.max_in_flight, 1)
        self.assertEqual(self.cancelled, 1)  # the pending request ends with the feeder
        new = [frame for frame in process.written if frame != b"first"]
        self.assertEqual(new, self.returned)

    async def test_pending_request_repeats_the_previous_frame_every_repeat_interval(self):
        self.delay = 0.35
        with mock.patch.object(self.module, "CAMERA_STILL_REPEAT_SECONDS", 0.1):
            process = await self.feed(self.session(), 0.6)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.max_in_flight, 1)
        first = self.returned[0]
        # "first" at 0 s plus repeats every 100 ms until the answer at 350 ms.
        repeats = process.written[:process.written.index(first)]
        self.assertEqual(set(repeats), {b"first"})
        self.assertGreaterEqual(len(repeats), 3)
        self.assertLessEqual(len(repeats), 5)
        # The new frame is written once on arrival, then repeated the same way.
        after = process.written[process.written.index(first):]
        self.assertEqual(set(after), {first})
        self.assertGreaterEqual(len(after), 2)
        self.assertLessEqual(len(after), 4)

    async def test_real_repeat_interval_is_half_a_second(self):
        self.assertEqual(self.module.CAMERA_STILL_REPEAT_SECONDS, 0.5)
        self.delay = 10.0
        process = await self.feed(self.session(), 1.2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(process.written, [b"first"] * 3)  # 0, 0.5 and 1.0 s

    async def test_unchanged_image_is_not_written_as_a_new_frame(self):
        self.content = jpeg(77)
        with mock.patch.object(self.module, "CAMERA_STILL_REPEAT_SECONDS", 0.1):
            process = await self.feed(self.session(), 0.45)
        self.assertGreaterEqual(self.calls, 3)
        self.assertEqual(process.written[0], b"first")
        self.assertEqual(set(process.written[1:]), {jpeg(77)})
        # Written once, then repeated at 100 ms instead of at the 10 fps fetch rate.
        self.assertLessEqual(len(process.written), 6)

    async def test_failure_limit_is_unchanged_and_fast_failures_back_off(self):
        self.assertEqual(self.module.CAMERA_IMAGE_FAILURE_LIMIT, 10)
        self.assertEqual(self.module.CAMERA_STILL_RETRY_SECONDS, 0.5)
        self.error = RuntimeError("camera offline")
        process = FakeProcess(echo=False)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with mock.patch.object(self.module, "CAMERA_IMAGE_FAILURE_LIMIT", 3), \
                mock.patch.object(self.module, "CAMERA_STILL_RETRY_SECONDS", 0.1), \
                self.assertLogs(self.module._LOGGER, "WARNING") as logs:
            await asyncio.wait_for(
                self.connection._async_feed_camera_images(self.session(), process), 2)
        # Three consecutive failures spaced by the retry interval, not 100 ms fetches.
        self.assertEqual(self.calls, 3)
        self.assertGreaterEqual(loop.time() - started, 0.18)
        self.assertTrue(any("stopped responding" in line for line in logs.output))
        self.assertTrue(process.stdin.is_closing())

    async def test_empty_images_count_and_a_good_image_resets_the_failures(self):
        answers = [b"", b"", jpeg(50), b"", b"", jpeg(51), b"", b""]

        async def scripted(hass, entity_id, **kwargs):
            self.calls += 1
            content = answers.pop(0) if answers else b""
            return types.SimpleNamespace(content=content)

        process = FakeProcess(echo=False)
        with mock.patch.object(self.module, "async_get_image", scripted), \
                mock.patch.object(self.module, "CAMERA_IMAGE_FAILURE_LIMIT", 3), \
                mock.patch.object(self.module, "CAMERA_STILL_RETRY_SECONDS", 0.01), \
                self.assertLogs(self.module._LOGGER, "WARNING"):
            await asyncio.wait_for(
                self.connection._async_feed_camera_images(self.session(), process), 3)
        # Two failures before each good image never reach the limit of three.
        self.assertEqual(self.calls, 9)
        self.assertIn(jpeg(50), process.written)
        self.assertIn(jpeg(51), process.written)

    async def test_restart_resumes_from_the_newest_still_image(self):
        session = self.session()
        await self.feed(session, 0.25)
        newest = self.returned[-1]
        self.assertEqual(session.latest_image, newest)
        self.delay = 10.0
        process = await self.feed(session, 0.05)
        self.assertEqual(process.written, [newest])
        self.assertEqual(session.first_image, b"first")

    async def test_measured_still_rate_is_logged_once_per_session(self):
        session = self.session()
        with mock.patch.object(self.module, "CAMERA_STREAM_DIAGNOSTIC_INTERVAL_SECONDS", 0.15), \
                self.assertLogs(self.module._LOGGER, "INFO") as logs:
            await self.feed(session, 0.3)
            await self.feed(session, 0.3)  # an FFmpeg restart does not log again
        diag = [line for line in logs.output if "[CameraDiag]" in line]
        self.assertEqual(len(diag), 1)
        self.assertIn(OTHER, diag[0])
        self.assertIn("still=", diag[0])
        self.assertIn("cap=10 fps", diag[0])
        self.assertTrue(session.still_rate_logged)


class LocalCameraRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = FakeMqtt()
        self.module = load_camera_module(self.mqtt)
        self.streams = load_camera_stream_module()
        self.manager = self.streams.CameraStreamManager(FakeHass())

    async def start(self, capabilities):
        result = []
        await self.module.async_setup_entry(None, entry(capabilities), result.extend)
        [camera] = result
        camera.entity_id = ENTITY
        camera.hass = types.SimpleNamespace(data={"tab5_lvgl": {
            "camera_stream_manager": self.manager,
            "entries": {"e1": types.SimpleNamespace(_device_ip="192.168.1.50")}}})
        await camera.async_added_to_hass()
        handler, _ = self.mqtt.subscriptions[f"{BASE}/stat/local_camera"]
        await handler(message(f"{BASE}/stat/local_camera", json.dumps(READY)))
        return camera

    async def test_live_camera_registers_for_popups_until_removed(self):
        camera = await self.start({"local_camera": True, "local_camera_stream": True})
        self.assertIs(self.manager.live_camera_stream(ENTITY), camera.live_stream)
        await camera.async_will_remove_from_hass()
        self.assertIsNone(self.manager.live_camera_stream(ENTITY))
        self.assertEqual(self.manager._live_cameras, [])

    async def test_still_only_camera_is_not_registered(self):
        camera = await self.start({"local_camera": True})
        self.assertIsNone(camera.live_stream)
        self.assertIsNone(self.manager.live_camera_stream(ENTITY))
        await camera.async_will_remove_from_hass()


if __name__ == "__main__":
    unittest.main()
