"""Clips decoded by the Bridge for a panel's speaker (audio_stream.py).

b501 let the panel download and decode every clip itself. While a song
played, the download filled the TCP window and 64 KB waited in the panel's
internal RAM for the whole song (free internal RAM 168 -> 80 KB on a V2); the
UI and the link slowed down. The Bridge now decodes with FFmpeg and sends PCM
over the camera's acknowledged transport: never more than two 4 KB blocks are
unacknowledged, whatever the clip.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import struct
import sys
import textwrap
import unittest
from unittest import mock

from test_camera_stream_lag import FakeHass
from test_local_camera_stream import ROOT, load_camera_stream_module, load_package_module

AUDIO = load_package_module("audio_stream")
DEVICE = "panel-mac"
URL = "http://192.168.1.2:8123/local/song.mp3?authSig=secret"

# Stands in for FFmpeg: names the input's length on stderr like FFmpeg does,
# then writes an odd number of PCM bytes (the stray last byte is no sample).
FAKE_FFMPEG = textwrap.dedent("""
    import sys
    size = int(sys.argv[1])
    sys.stderr.write("Input #0, mp3, from 'http://ha/song.mp3?authSig=x':\\n")
    sys.stderr.write("  Metadata:\\n    comment         : Duration: 99:00:00\\n")
    sys.stderr.write("  Duration: 00:02:42.12, start: 0.025057, bitrate: 192 kb/s\\n")
    sys.stderr.write("Stream mapping:\\nOutput #0, s16le, to 'pipe:1':\\n")
    sys.stderr.flush()
    out = sys.stdout.buffer
    for i in range(size):
        out.write(bytes([i % 251]))
    out.flush()
""")

FAILING_FFMPEG = textwrap.dedent("""
    import sys
    sys.stderr.write("http://ha/missing.mp3?authSig=x: Server returned 404 Not Found\\n")
    sys.exit(1)
""")


def fake_command(script, *args):
    def command(binary, url, rate, start_ms=0):
        return [sys.executable, "-c", script, *map(str, args)]
    return command


class ContractTest(unittest.TestCase):
    def test_wire_matches_the_camera_transport(self):
        tree = ast.parse((ROOT / "camera_stream.py").read_text(encoding="utf-8"))
        constants = {node.target.id: node.value for node in tree.body
                     if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
        fmt = lambda name: constants[name].args[0].value  # noqa: E731
        self.assertEqual(AUDIO.AUDIO_STREAM_HELLO_STRUCT.format, fmt("CAMERA_STREAM_HELLO_STRUCT"))
        self.assertEqual(AUDIO.AUDIO_STREAM_FRAME_STRUCT.format, fmt("CAMERA_STREAM_FRAME_STRUCT"))
        self.assertEqual(AUDIO.AUDIO_STREAM_ACK_STRUCT.format, fmt("CAMERA_STREAM_ACK_STRUCT"))
        self.assertEqual(AUDIO.AUDIO_STREAM_FRAME_MAGIC, constants["CAMERA_STREAM_FRAME_MAGIC"].value)
        self.assertEqual(AUDIO.AUDIO_STREAM_ACK_MAGIC, constants["CAMERA_STREAM_ACK_MAGIC"].value)
        self.assertEqual(AUDIO.AUDIO_STREAM_FORMAT_STRUCT.size, 16)
        # The panel's internal network memory holds at most this much.
        self.assertEqual(AUDIO.AUDIO_STREAM_CHUNK_BYTES * AUDIO.AUDIO_STREAM_WINDOW, 8 * 1024)

    def test_ffmpeg_command(self):
        command = AUDIO.ffmpeg_audio_command("ffmpeg", URL, 44100)
        self.assertNotIn("-ss", command)
        self.assertEqual(command[command.index("-i") + 1], URL)
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "44100")
        self.assertEqual(command[command.index("-f") + 1], "s16le")
        self.assertEqual(command[-1], "pipe:1")
        # Only network inputs: a playlist cannot point FFmpeg at local files.
        self.assertEqual(command[command.index("-protocol_whitelist") + 1],
                         "http,https,tcp,tls,crypto")
        seek = AUDIO.ffmpeg_audio_command("ffmpeg", URL, 22050, 90500)
        self.assertEqual(seek[seek.index("-ss") + 1], "90.500")
        self.assertLess(seek.index("-ss"), seek.index("-i"))

    def test_duration_line(self):
        parse = AUDIO.parse_duration_ms
        self.assertEqual(parse("  Duration: 00:02:42.12, start: 0.025057, bitrate: 192 kb/s"),
                         162120)
        self.assertEqual(parse("  Duration: 01:00:00.00, start: 0"), 3600000)
        self.assertEqual(parse("  Duration: N/A, start: 0.000000, bitrate: 128 kb/s"), 0)
        self.assertIsNone(parse("    comment         : Duration: 99:00:00"))
        self.assertIsNone(parse("Output #0, s16le, to 'pipe:1':"))

    def test_logged_lines_hide_signed_tokens(self):
        line = "http://ha:8123/api/tts_proxy/a.mp3?authSig=eyJhbGci: Server returned 404"
        self.assertNotIn("eyJhbGci", AUDIO.without_query(line))
        self.assertIn("Server returned 404", AUDIO.without_query(line))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.manager = AUDIO.AudioStreamManager(FakeHass(), ffmpeg_binary=lambda: "ffmpeg")

    def test_a_token_is_used_once(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        self.assertIs(self.manager.take_session(session.token), session)
        self.assertIsNone(self.manager.take_session(session.token))

    def test_a_new_clip_ends_the_previous_one(self):
        first = self.manager.create_session(DEVICE, URL, 44100)
        second = self.manager.create_session(DEVICE, URL, 22050, 5000)
        self.assertTrue(first.stop_event.is_set())
        self.assertIsNone(self.manager.take_session(first.token))
        self.assertEqual((second.rate, second.start_ms), (22050, 5000))

    def test_stop_and_expiry_revoke_the_token(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        self.manager.stop_device(DEVICE)
        self.assertTrue(session.stop_event.is_set())
        self.assertIsNone(self.manager.take_session(session.token))
        expired = self.manager.create_session(DEVICE, URL, 44100)
        expired.expires_at = 0
        self.assertIsNone(self.manager.take_session(expired.token))

    def test_only_the_two_rates(self):
        with self.assertRaises(ValueError):
            self.manager.create_session(DEVICE, URL, 48000)

    def test_pause_marks_the_running_clip(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        self.manager.set_paused(DEVICE, True)
        self.assertTrue(session.paused)
        self.manager.set_paused("other", True)


class FakePanel:
    """The panel's side (speaker.cpp): acknowledges a message once it read it."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.format = None
        self.pcm = bytearray()
        self.most_unacknowledged = 0

    async def hello(self):
        magic, status, chunk = AUDIO.AUDIO_STREAM_HELLO_STRUCT.unpack(
            await self.reader.readexactly(8))
        assert magic == b"HTC1"
        return status, chunk

    async def message(self, timeout=5.0):
        header = await asyncio.wait_for(self.reader.readexactly(16), timeout)
        magic, kind, sequence, length = AUDIO.AUDIO_STREAM_FRAME_STRUCT.unpack(header)
        assert magic == b"HTF1"
        payload = await self.reader.readexactly(length) if length else b""
        return kind, sequence, payload

    async def ack(self, sequence, length):
        self.writer.write(struct.pack(">4sII", b"HTA1", sequence, length))
        await self.writer.drain()

    async def play(self, stall_after=None, stall_s=0.0):
        """Read everything; hold acknowledgements back to count the window."""
        unacked = []
        while True:
            try:
                kind, sequence, payload = await self.message(timeout=0.3 if unacked else 5.0)
            except TimeoutError:
                # The Bridge waits: everything in flight is unacknowledged.
                self.most_unacknowledged = max(self.most_unacknowledged, len(unacked))
                if stall_after is not None and len(self.pcm) >= stall_after:
                    await asyncio.sleep(stall_s)
                    stall_after = None
                for item in unacked:
                    await self.ack(*item)
                unacked.clear()
                continue
            except asyncio.IncompleteReadError:
                return "closed"
            if kind == AUDIO.AUDIO_MESSAGE_END:
                for item in unacked:
                    await self.ack(*item)
                return "end"
            if kind == AUDIO.AUDIO_MESSAGE_FORMAT:
                self.format = AUDIO.AUDIO_STREAM_FORMAT_STRUCT.unpack(payload)
            else:
                assert kind == AUDIO.AUDIO_MESSAGE_AUDIO and len(payload) % 2 == 0
                self.pcm += payload
            unacked.append((sequence, len(payload)))


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = AUDIO.AudioStreamManager(FakeHass(), ffmpeg_binary=lambda: "ffmpeg")

        async def handle(reader, writer):
            line = (await reader.readline()).decode().strip()
            try:
                await self.manager.async_handle(line, reader, writer, "test")
            finally:
                writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.panels = []

    async def asyncTearDown(self):
        for panel in self.panels:
            panel.writer.close()
            with contextlib.suppress(ConnectionError):
                await panel.writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, token):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"HTAUD/1 {token}\n".encode())
        await writer.drain()
        panel = FakePanel(reader, writer)
        self.panels.append(panel)
        return panel

    async def test_the_whole_clip_with_two_blocks_in_flight(self):
        session = self.manager.create_session(DEVICE, URL, 22050, 1500)
        with mock.patch.object(AUDIO, "ffmpeg_audio_command", fake_command(FAKE_FFMPEG, 50001)):
            panel = await self.connect(session.token)
            self.assertEqual(await panel.hello(), (0, 4096))
            self.assertEqual(await panel.play(), "end")
        # Rate, mono, 16 bit, stream position and the clip's length.
        self.assertEqual(panel.format, (22050, 1, 16, 1500, 162120))
        self.assertEqual(bytes(panel.pcm), bytes(i % 251 for i in range(50000)))
        self.assertEqual(panel.most_unacknowledged, AUDIO.AUDIO_STREAM_WINDOW)

    async def test_a_paused_panel_keeps_its_clip(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        with mock.patch.multiple(AUDIO, ffmpeg_audio_command=fake_command(FAKE_FFMPEG, 60000),
                                 AUDIO_ACK_POLL_SECONDS=0.05, AUDIO_ACK_TIMEOUT_SECONDS=0.2):
            panel = await self.connect(session.token)
            await panel.hello()
            self.manager.set_paused(DEVICE, True)
            self.assertEqual(await panel.play(stall_after=16384, stall_s=0.6), "end")
        self.assertEqual(len(panel.pcm), 60000)

    async def test_a_silent_playing_panel_is_gone(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        with mock.patch.multiple(AUDIO, ffmpeg_audio_command=fake_command(FAKE_FFMPEG, 60000),
                                 AUDIO_ACK_POLL_SECONDS=0.05, AUDIO_ACK_TIMEOUT_SECONDS=0.2):
            panel = await self.connect(session.token)
            await panel.hello()
            with self.assertLogs(AUDIO._LOGGER, "WARNING") as logs:
                self.assertEqual(await panel.play(stall_after=16384, stall_s=0.6), "closed")
        self.assertIn("acknowledged nothing", logs.output[0])

    async def test_a_clip_ffmpeg_cannot_read(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        with mock.patch.object(AUDIO, "ffmpeg_audio_command", fake_command(FAILING_FFMPEG)):
            panel = await self.connect(session.token)
            await panel.hello()
            with self.assertLogs(AUDIO._LOGGER, "WARNING") as logs:
                self.assertEqual(await panel.play(), "closed")
        self.assertIsNone(panel.format)
        self.assertIn("could not read the clip", logs.output[0])
        self.assertNotIn("authSig=x", logs.output[0])

    async def test_stop_ends_a_running_clip(self):
        session = self.manager.create_session(DEVICE, URL, 44100)
        with mock.patch.object(AUDIO, "ffmpeg_audio_command",
                               fake_command(FAKE_FFMPEG, 10_000_000)):
            panel = await self.connect(session.token)
            await panel.hello()
            await panel.message()
            self.manager.stop_device(DEVICE)
            self.assertEqual(await panel.play(), "closed")

    async def test_an_unknown_token_is_refused(self):
        panel = await self.connect("nope")
        with self.assertLogs(AUDIO._LOGGER, "WARNING"):
            status, _chunk = await panel.hello()
        self.assertEqual(status, AUDIO.AUDIO_HELLO_UNKNOWN_TOKEN)


class CameraListenerTest(unittest.IsolatedAsyncioTestCase):
    """The camera listener hands HTAUD/1 connections to the speaker streams."""

    async def test_dispatch(self):
        module = load_camera_stream_module()
        manager = module.CameraStreamManager(FakeHass())
        accepted = asyncio.get_running_loop().create_future()

        async def accept(reader, writer):
            accepted.set_result(None)
            await manager.tcp_connection.async_handle(reader, writer)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"HTAUD/1 unknown-token\n")
        await writer.drain()
        hello = await asyncio.wait_for(reader.readexactly(8), 5)
        self.assertEqual(struct.unpack(">4sHH", hello)[:2], (b"HTC1", 1))
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
