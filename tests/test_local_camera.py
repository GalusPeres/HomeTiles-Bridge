"""Snapshot request lifecycle and payload validation for a panel's own camera."""

from __future__ import annotations

import asyncio
import json
import unittest

from test_view_navigation import load_module

LC = load_module("local_camera")
CONST = load_module("const")

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
ID_A = "0123456789abcdef"
ID_B = "fedcba9876543210"


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def snapshots(clock, ids=(ID_A, ID_B), timeout_s=0.2):
    queue = list(ids)
    return LC.LocalCameraSnapshots(timeout_s=timeout_s, stale_fallback_s=60.0,
                                   id_factory=lambda: queue.pop(0), clock=clock)


class LocalCameraContractTest(unittest.TestCase):
    def test_topics_ids_and_request_shape(self):
        self.assertEqual(LC.LOCAL_CAMERA_LEAF, CONST.TOPIC_LOCAL_CAMERA)
        self.assertEqual(LC.local_camera_command_topic("hometiles/p1"), "hometiles/p1/cmnd/local_camera")
        self.assertEqual(LC.local_camera_status_topic("hometiles/p1"), "hometiles/p1/stat/local_camera")
        prefix = LC.local_camera_image_prefix("b")
        self.assertEqual(prefix, "b/stat/local_camera/image")
        self.assertEqual(LC.local_camera_error_prefix("b"), "b/stat/local_camera/error")
        self.assertEqual(LC.request_id_from_topic(prefix, f"{prefix}/{ID_A}"), ID_A)
        for topic in [f"{prefix}/{ID_A.upper()}", f"{prefix}/abc", f"{prefix}/{ID_A}/x",
                      f"b/stat/local_camera/error/{ID_A}", f"{prefix}{ID_A}", None, b"x"]:
            self.assertIsNone(LC.request_id_from_topic(prefix, topic))
        for _ in range(20):
            generated = LC.new_request_id()
            self.assertTrue(LC.valid_request_id(generated))
            self.assertEqual(len(generated), 16)
        self.assertTrue(LC.valid_request_id("a" * 32))
        self.assertFalse(LC.valid_request_id("a" * 33))
        self.assertFalse(LC.valid_request_id("a" * 15))
        self.assertEqual(LC.build_request(ID_A, 131072),
                         {"v": 1, "id": ID_A, "op": "snapshot", "max_bytes": 131072})
        with self.assertRaises(ValueError):
            LC.build_request("bad", 1)
        self.assertEqual(LC.local_camera_unique_id("mac"), "mac_local_camera")
        self.assertEqual(CONST.LOCAL_CAMERA_MAX_BYTES, 256 * 1024)
        self.assertLess(CONST.LOCAL_CAMERA_REQUEST_TIMEOUT_S, 10)

    def test_jpeg_validation_rejects_truncated_oversize_and_text(self):
        self.assertTrue(LC.valid_jpeg(JPEG, len(JPEG)))
        self.assertTrue(LC.valid_jpeg(bytearray(JPEG), 1000))
        self.assertFalse(LC.valid_jpeg(JPEG, len(JPEG) - 1))
        self.assertFalse(LC.valid_jpeg(JPEG[:-1], 1000))
        self.assertFalse(LC.valid_jpeg(b"\x89PNG" + JPEG[2:], 1000))
        self.assertFalse(LC.valid_jpeg(JPEG.decode("latin-1"), 1000))
        self.assertFalse(LC.valid_jpeg(b"\xff\xd9", 1000))
        self.assertFalse(LC.valid_jpeg(None, 1000))

    def test_status_parsing_is_bounded(self):
        ready = {"v": 1, "state": "ready", "width": 1280, "height": 720, "format": "jpeg",
                 "max_bytes": 131072, "min_interval_ms": 1000, "sensor": "ov02c10"}
        status = LC.parse_status(json.dumps(ready), CONST.LOCAL_CAMERA_MAX_BYTES)
        self.assertEqual(status, {"state": "ready", "width": 1280, "height": 720,
                                  "max_bytes": 131072, "min_interval_s": 1.0, "sensor": "ov02c10",
                                  "paused": False})
        self.assertEqual(LC.parse_status(json.dumps(ready).encode(), 262144)["state"], "ready")
        # The Bridge cap bounds what a panel may send.
        self.assertEqual(LC.parse_status(json.dumps(dict(ready, max_bytes=1_000_000)), 262144)["max_bytes"], 262144)
        disabled = LC.parse_status('{"v":1,"state":"disabled"}', 262144)
        self.assertEqual(disabled["state"], "disabled")
        self.assertEqual(disabled["max_bytes"], 131072)
        errored = LC.parse_status('{"v":1,"state":"error","error":"sensor_unavailable"}', 262144)
        self.assertEqual(errored["error"], "sensor_unavailable")
        self.assertNotIn("error", LC.parse_status('{"v":1,"state":"error","error":"<script>"}', 262144))
        for bad in [dict(ready, v=2), dict(ready, v=True), dict(ready, state="streaming"),
                    dict(ready, width=-1), dict(ready, height=10**9), dict(ready, width=True),
                    dict(ready, width=1.5), dict(ready, max_bytes=0), dict(ready, max_bytes=2**40),
                    dict(ready, min_interval_ms=-5), dict(ready, min_interval_ms=10**7),
                    dict(ready, format="png"), {k: v for k, v in ready.items() if k != "width"}]:
            self.assertIsNone(LC.parse_status(json.dumps(bad), 262144), bad)
        for raw in ["", "not json", "[]", "null", b"\xff\xfe", "{" + " " * 2000 + "}", 5]:
            self.assertIsNone(LC.parse_status(raw, 262144), raw)

    def test_paused_status_is_additive_and_strict(self):
        ready = {"v": 1, "state": "ready", "width": 1280, "height": 720}
        # Firmware without the pause switch never sends the field.
        self.assertIs(LC.parse_status(json.dumps(ready), 262144)["paused"], False)
        self.assertIs(LC.parse_status('{"v":1,"state":"disabled"}', 262144)["paused"], False)
        self.assertIs(LC.parse_status(json.dumps(dict(ready, paused=False)), 262144)["paused"], False)
        paused = LC.parse_status('{"v":1,"state":"disabled","paused":true}', 262144)
        self.assertEqual((paused["state"], paused["paused"]), ("disabled", True))
        self.assertIs(LC.parse_status(b'{"v":1,"state":"disabled","paused":true}', 262144)["paused"], True)
        for bad in ["true", 1, 0, None, [], {}]:
            payload = json.dumps({"v": 1, "state": "disabled", "paused": bad})
            self.assertIsNone(LC.parse_status(payload, 262144), bad)

    def test_pause_commands_and_switch_state(self):
        self.assertEqual(LC.build_pause_request(True), {"v": 1, "action": "pause"})
        self.assertEqual(LC.build_pause_request(False), {"v": 1, "action": "resume"})
        self.assertEqual(json.dumps(LC.build_pause_request(True), separators=(",", ":")),
                         '{"v":1,"action":"pause"}')
        self.assertEqual(json.dumps(LC.build_pause_request(False), separators=(",", ":")),
                         '{"v":1,"action":"resume"}')

        def status(raw):
            return LC.parse_status(raw, 262144)

        ready = status('{"v":1,"state":"ready","width":2,"height":2}')
        paused = status('{"v":1,"state":"disabled","paused":true}')
        errored = status('{"v":1,"state":"error","error":"sensor_unavailable"}')
        disabled = status('{"v":1,"state":"disabled"}')
        for previous in (None, True, False):
            self.assertIsNone(LC.camera_allowed(None, previous))
            self.assertIs(LC.camera_allowed(paused, previous), False)
            self.assertIs(LC.camera_allowed(ready, previous), True)
            # An error or a Web Admin disable says nothing about the pause.
            self.assertIs(LC.camera_allowed(errored, previous), previous)
            self.assertIs(LC.camera_allowed(disabled, previous), previous)

    def test_error_and_connected_parsing(self):
        for code in ["busy", "disabled", "sensor_unavailable", "encoder_busy", "too_large", "rate_limited"]:
            self.assertEqual(LC.parse_error(json.dumps({"v": 1, "error": code})), code)
        for raw in ['{"v":1,"error":"exploded"}', "garbage", b"\xff", "[1]"]:
            self.assertEqual(LC.parse_error(raw), "unknown")
        self.assertTrue(LC.parse_connected(" online "))
        self.assertTrue(LC.parse_connected(b"1"))
        self.assertFalse(LC.parse_connected("offline"))
        self.assertIsNone(LC.parse_connected("maybe"))

    def test_self_loop_matches_only_this_entry_camera(self):
        class Entry:
            def __init__(self, **values):
                self.__dict__.update(values)
        own = Entry(platform="tab5_lvgl", domain="camera", config_entry_id="e1", unique_id="mac_local_camera")
        self.assertTrue(LC.is_local_camera_self_loop(own, "tab5_lvgl", "e1"))
        self.assertFalse(LC.is_local_camera_self_loop(own, "tab5_lvgl", "other"))
        self.assertFalse(LC.is_local_camera_self_loop(None, "tab5_lvgl", "e1"))
        for change in [{"platform": "generic"}, {"domain": "sensor"}, {"unique_id": "mac_view"}]:
            self.assertFalse(LC.is_local_camera_self_loop(Entry(**dict(own.__dict__, **change)), "tab5_lvgl", "e1"))

    def test_warning_rate_limit_is_per_reason_and_bounded(self):
        warnings = LC.RateLimitedWarnings(60, max_keys=2)
        self.assertTrue(warnings.allow("timeout", 0))
        self.assertFalse(warnings.allow("timeout", 59))
        self.assertTrue(warnings.allow("busy", 59))
        self.assertTrue(warnings.allow("timeout", 60))
        warnings.allow("a", 61)
        warnings.allow("b", 62)
        self.assertLessEqual(len(warnings._last), 2)


class LocalCameraLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callers_share_one_request_and_cache_bounds_rate(self):
        clock = Clock()
        state = snapshots(clock)
        published = []

        async def publish(request):
            published.append(request)
            await asyncio.sleep(0)

        first = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        second = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        self.assertEqual([request["id"] for request in published], [ID_A])
        self.assertEqual(state.resolve_image(ID_A, JPEG, clock.now), "accepted")
        self.assertEqual(await first, (JPEG, None))
        self.assertEqual(await second, (JPEG, None))
        clock.now += 1.0
        self.assertEqual(await state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5), (JPEG, None))
        self.assertEqual(len(published), 1)
        clock.now += 1.0
        third = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        self.assertEqual(published[-1]["id"], ID_B)
        state.resolve_image(ID_B, JPEG + b"", clock.now)
        self.assertEqual((await third)[1], None)

    async def test_unsolicited_stale_and_invalid_images_are_dropped(self):
        clock = Clock()
        state = snapshots(clock)
        self.assertEqual(state.resolve_image(ID_A, JPEG, clock.now), "unsolicited")
        published = []

        async def publish(request):
            published.append(request)

        task = asyncio.create_task(state.async_fetch(publish, max_bytes=100, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        self.assertEqual(state.resolve_image(ID_B, JPEG, clock.now), "unsolicited")
        self.assertEqual(state.resolve_image(None, JPEG, clock.now), "unsolicited")
        self.assertEqual(state.pending_id, ID_A)
        self.assertEqual(state.resolve_image(ID_A, b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9", clock.now), "invalid")
        self.assertEqual(await task, (None, "invalid_image"))
        self.assertIsNone(state.pending_id)
        # A late valid frame for the finished id is not accepted into the cache.
        self.assertEqual(state.resolve_image(ID_A, JPEG, clock.now), "unsolicited")
        self.assertIsNone(state.fallback(clock.now))

    async def test_timeout_returns_recent_cache_then_nothing(self):
        clock = Clock()
        state = snapshots(clock, ids=(ID_A, ID_B, "a" * 16), timeout_s=0.05)

        async def publish(request):
            pass

        task = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        state.resolve_image(ID_A, JPEG, clock.now)
        await task
        clock.now += 30
        self.assertEqual(await state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5), (JPEG, "timeout"))
        self.assertIsNone(state.pending_id)
        clock.now += 31
        self.assertEqual(await state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5), (None, "timeout"))

    async def test_failure_backoff_limits_request_rate(self):
        clock = Clock()
        state = snapshots(clock, ids=(ID_A, ID_B))
        published = []

        async def publish(request):
            published.append(request)

        task = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        self.assertTrue(state.resolve_error(ID_A, "busy"))
        self.assertEqual(await task, (None, "busy"))
        self.assertEqual(await state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5), (None, None))
        self.assertEqual(len(published), 1)
        self.assertFalse(state.resolve_error(ID_B, "busy"))

    async def test_offline_fails_pending_and_publish_errors_do_not_leak(self):
        clock = Clock()
        state = snapshots(clock, ids=(ID_A, ID_B))

        async def publish(request):
            pass

        task = asyncio.create_task(state.async_fetch(publish, max_bytes=1000, min_interval_s=1.5))
        await asyncio.sleep(0.01)
        state.fail_all("offline")
        self.assertEqual(await task, (None, "offline"))
        clock.now += 2

        async def broken(request):
            raise RuntimeError("mqtt down")

        self.assertEqual(await state.async_fetch(broken, max_bytes=1000, min_interval_s=1.5), (None, "publish_failed"))
        self.assertIsNone(state.pending_id)

    async def test_abandoned_request_expires_for_the_next_caller(self):
        clock = Clock()
        state = snapshots(clock, ids=(ID_A, ID_B), timeout_s=6.0)
        future, request_id, is_new = state.begin(clock.now, 1.5, 1000)
        self.assertTrue(is_new)
        joined = state.begin(clock.now + 1, 1.5, 1000)
        self.assertIs(joined[0], future)
        self.assertFalse(joined[2])
        clock.now += 7
        _, next_id, is_new = state.begin(clock.now, 1.5, 1000)
        self.assertEqual((request_id, next_id, is_new), (ID_A, ID_B, True))
        with self.assertRaises(LC.LocalCameraError):
            future.result()


if __name__ == "__main__":
    unittest.main()
