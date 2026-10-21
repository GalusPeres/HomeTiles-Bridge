"""A camera's sound with its picture (Bridge v0.9.4b11, firmware b504).

A panel that can play Bridge streams says so in its camera open ("audio").
When the camera's stream has an audio track, the Bridge starts it on the
panel's speaker as a live clip; switching between popup and full screen keeps
it running, closing the camera ends it gently. Cameras without sound, and
panels that do not ask, stay as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import types
import unittest

from test_local_camera_platform import BASE, bridge_methods
from test_local_camera_stream import load_package_module
from test_view_navigation import load_module

AUDIO = load_package_module("audio_stream")
SPEAKER = load_module("speaker")
LOCAL_CAMERA = load_module("local_camera")
SOURCE = "rtsp://192.168.1.9:554/stream1"
ENTITY = "camera.front_door"


class CameraSoundTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.published = []
        self.audio_commands = []
        self.probes = []
        self.has_audio = True
        self.audio_streams = AUDIO.AudioStreamManager(None, ffmpeg_binary=lambda: "ffmpeg")

        async def publish(hass, topic, payload, qos=0, retain=False):
            self.published.append((topic, json.loads(payload)))

        async def has_audio(binary, source):
            self.probes.append(source)
            return self.has_audio

        async def source_ip(hass, target_ip=None):
            return "192.168.1.2"

        registry = types.SimpleNamespace(async_get=lambda entity_id: None)
        scope = {
            "mqtt": types.SimpleNamespace(async_publish=publish),
            "er": types.SimpleNamespace(async_get=lambda hass: registry),
            "json": json, "DOMAIN": "tab5_lvgl", "CAMERA_BRIDGE_PROTOCOL_VERSION": 1,
            "CAMERA_STREAM_TRANSPORT": "tcp-ack-v1", "CAMERA_STREAM_WIDTH": 752,
            "CAMERA_STREAM_HEIGHT": 424, "CAMERA_STREAM_FPS": 30,
            "CAMERA_STREAM_FRAMING": "ack-jpeg-v1",
            "_try_parse_json": lambda raw: json.loads(raw),
            "_LOGGER": logging.getLogger(__name__),
            "is_local_camera_self_loop": LOCAL_CAMERA.is_local_camera_self_loop,
            "ha_network": types.SimpleNamespace(async_get_source_ip=source_ip),
            "async_has_audio": has_audio,
            "monotonic": lambda: 1000.0,
            "CAMERA_SOUND_PROBE_CACHE_SECONDS": 600.0,
            "AUDIO_RATE_SPEECH": SPEAKER.AUDIO_RATE_SPEECH,
            "AUDIO_STREAM_TRANSPORT": SPEAKER.AUDIO_STREAM_TRANSPORT,
            "build_play_command": SPEAKER.build_play_command,
            "new_request_id": SPEAKER.new_request_id,
        }
        names = {"_is_local_camera_self_loop", "_async_handle_camera_command",
                 "_async_publish_camera_status", "_camera_sound_session", "_start_camera_sound",
                 "_async_start_camera_sound", "_finish_camera_sound"}
        methods = bridge_methods(names, scope)
        manager = types.SimpleNamespace(
            async_create_session=self.create_session, async_stop_device=self.stop_device,
            stream_url=lambda host, token: f"tcp://{host}:8124/{token}", tcp_port=8124,
            audio_streams=self.audio_streams)
        self.tasks = []

        def background_task(coro, name):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        bridge = types.SimpleNamespace(
            hass=types.SimpleNamespace(
                data={"tab5_lvgl": {"camera_stream_manager": manager}},
                async_create_background_task=background_task),
            entry=types.SimpleNamespace(entry_id="e1"), base_topic=BASE, device_id="mac",
            cameras=[ENTITY], _device_ip=None, _command_channel=None,
            _camera_sound_entity=None, _camera_sound_task=None, _camera_sound_probes={},
            _resolve_target_entity=lambda requested, allowed: requested if requested in allowed else None)

        async def create_audio_stream(url, rate, start_ms=0, live=False):
            session = self.audio_streams.create_session("mac", url, rate, start_ms, live)
            return f"tcp://192.168.1.2:8124/{session.token}"

        async def publish_audio_command(text):
            self.audio_commands.append(json.loads(text))

        bridge.async_create_audio_stream = create_audio_stream
        bridge.async_publish_audio_command = publish_audio_command
        for name in names:
            setattr(bridge, name, (lambda method: lambda *args: method(bridge, *args))(methods[name]))
        self.bridge = bridge

    async def create_session(self, device_id, entity_id, *args):
        view = args[3] or "popup"
        return types.SimpleNamespace(
            token="video-token", source=SOURCE, width=752, height=424, fps=30, view=view,
            rotate=0, fit="cover", window=2, chunk_bytes=32768, jpeg_quality=None)

    async def stop_device(self, device_id):
        pass

    async def command(self, **fields):
        await self.bridge._async_handle_camera_command(
            types.SimpleNamespace(payload=json.dumps({"entity_id": ENTITY, **fields})))
        for task in list(self.tasks):
            await task

    async def open(self, view=None, audio=True):
        fields = {"command": "open", "transport": "tcp-ack-v1"}
        if view:
            fields["view"] = view
        if audio:
            fields["audio"] = "tcp-ack-v1"
        await self.command(**fields)

    async def test_sound_starts_with_the_picture_and_survives_full_screen(self):
        await self.open()
        self.assertEqual(self.published[-1][1]["status"], "ready")
        [play] = self.audio_commands
        self.assertEqual((play["op"], play["live"]), ("play", True))
        self.assertTrue(play["url"].startswith("tcp://192.168.1.2:8124/"))
        session = self.audio_streams.current("mac")
        self.assertEqual((session.url, session.rate, session.live), (SOURCE, 22050, True))
        # Popup -> full screen sends another open: the sound keeps running.
        await self.open(view="full")
        self.assertEqual(len(self.audio_commands), 1)
        self.assertIs(self.audio_streams.current("mac"), session)
        # Closing ends it; reopening asks FFmpeg only once per source.
        await self.command(command="close")
        self.assertIsNone(self.audio_streams.current("mac"))
        await self.open()
        self.assertEqual(len(self.audio_commands), 2)
        self.assertEqual(self.probes, [SOURCE])

    async def test_a_camera_without_sound_stays_silent(self):
        self.has_audio = False
        await self.open()
        self.assertEqual(self.audio_commands, [])
        self.assertIsNone(self.audio_streams.current("mac"))

    async def test_older_firmware_gets_no_sound(self):
        await self.open(audio=False)
        self.assertEqual(self.audio_commands, [])
        self.assertEqual(self.probes, [])

    async def test_a_song_is_not_stopped_by_closing_a_silent_camera(self):
        self.audio_streams.create_session("mac", "http://ha/song.mp3", 44100)
        self.has_audio = False
        await self.open()
        await self.command(command="close")
        self.assertEqual(self.audio_streams.current("mac").url, "http://ha/song.mp3")


if __name__ == "__main__":
    unittest.main()
