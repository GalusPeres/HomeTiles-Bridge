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

# A camera: a little sound, then a pause, as a live source delivers it.
TRICKLE_FFMPEG = textwrap.dedent("""
    import sys, time
    sys.stderr.write("  Duration: N/A, start: 0.000000, bitrate: N/A\\n")
    sys.stderr.write("Output #0, s16le, to 'pipe:1':\\n")
    sys.stderr.flush()
    sys.stdout.buffer.write(bytes(1001))
    sys.stdout.buffer.flush()
    time.sleep(3)
""")

FAILING_FFMPEG = textwrap.dedent("""
    import sys
    sys.stderr.write("http://ha/missing.mp3?authSig=x: Server returned 404 Not Found\\n")
    sys.exit(1)
""")


def fake_command(script, *args):
    def command(binary, url, rate, start_ms=0, live=False):
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

    def test_camera_sound_command(self):
        rtsp = "rtsp://user:pw@192.168.1.9:554/stream1"
        command = AUDIO.ffmpeg_audio_command("ffmpeg", rtsp, 22050, live=True)
        # A camera's RTSP source like the picture reads it, sound only.
        self.assertEqual(command[command.index("-rtsp_transport") + 1], "tcp")
        self.assertIn("rtsp", command[command.index("-protocol_whitelist") + 1].split(","))
        self.assertNotIn("file", command[command.index("-protocol_whitelist") + 1].split(","))
        self.assertEqual(command[command.index("-flush_packets") + 1], "1")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        # A clip from a service call never reads RTSP.
        clip = AUDIO.ffmpeg_audio_command("ffmpeg", rtsp, 22050)
        self.assertNotIn("rtsp", clip[clip.index("-protocol_whitelist") + 1].split(","))
        probe = AUDIO.ffmpeg_probe_command("ffmpeg", rtsp)
        self.assertEqual(probe[-5:], ["-t", "0", "-f", "null", "-"])

    def test_audio_track_detection(self):
        with_sound = (
            "Input #0, rtsp, from 'rtsp://cam/stream1':\n"
            "  Duration: N/A, start: 0.000000, bitrate: N/A\n"
            "  Stream #0:0: Video: h264 (High), yuv420p, 2560x1440, 15 fps\n"
            "  Stream #0:1: Audio: aac (LC), 16000 Hz, mono, fltp\n")
        without = with_sound.rsplit("  Stream #0:1", 1)[0]
        self.assertTrue(AUDIO.has_audio_stream(with_sound))
        self.assertFalse(AUDIO.has_audio_stream(without))
        self.assertFalse(AUDIO.has_audio_stream("    comment : Stream #0:1: Audio: x"))
        self.assertTrue(AUDIO.has_audio_stream("  Stream #0:1[0x101](und): Audio: pcm_alaw, 8000 Hz"))

    def test_camera_probe(self):
        log = (
            "Input #0, rtsp, from 'rtsp://cam/stream1':\n"
            "  Duration: N/A, start: 0.000000, bitrate: N/A\n"
            "  Stream #0:0: Video: h264 (High), yuv420p(progressive), 2560x1440, 15 fps, 15 tbr, 90k tbn\n"
            "  Stream #0:1: Audio: aac (LC), 16000 Hz, mono, fltp\n")
        self.assertEqual(AUDIO.parse_camera_probe(log), AUDIO.CameraProbe(True, 15.0, 64))
        g711 = AUDIO.parse_camera_probe(
            "  Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 tbr, 90k tbn\n"
            "  Stream #0:1: Audio: pcm_alaw, 8000 Hz, mono, s16, 64 kb/s\n")
        self.assertEqual(g711, AUDIO.CameraProbe(True, 25.0, 0))
        self.assertEqual(AUDIO.parse_camera_probe("  Stream #0:0: Video: mjpeg, 10 fps\n"),
                         AUDIO.CameraProbe(False, 10.0, 0))

    def test_sound_lead(self):
        lead = AUDIO.camera_sound_lead_ms
        # 4 decode threads hold a picture 3 source frames, plus scaling and
        # encoding; the sound one codec packet.
        self.assertEqual(lead(AUDIO.CameraProbe(True, 15.0, 64), 4), 151)
        self.assertEqual(lead(AUDIO.CameraProbe(True, 30.0, 21), 4), 94)
        self.assertEqual(lead(AUDIO.CameraProbe(True, None, 0), 4), 135)
        self.assertEqual(lead(AUDIO.CameraProbe(True, 1.0, 0), 4), 400)
        self.assertEqual(lead(AUDIO.CameraProbe(True, 60.0, 64), 1), 0)

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

    async def test_finish_ends_a_live_clip_with_its_end_message(self):
        # The camera closed: the panel plays out what it holds and reports the
        # clip done, not a connection lost mid-clip.
        session = self.manager.create_session(DEVICE, "rtsp://cam/stream1", 22050, live=True)
        with mock.patch.object(AUDIO, "ffmpeg_audio_command",
                               fake_command(FAKE_FFMPEG, 10_000_000)):
            panel = await self.connect(session.token)
            await panel.hello()
            kind, sequence, payload = await panel.message()
            self.assertEqual(kind, AUDIO.AUDIO_MESSAGE_FORMAT)
            await panel.ack(sequence, len(payload))
            self.assertIs(self.manager.current(DEVICE), session)
            self.manager.finish_device(DEVICE)
            self.assertIsNone(self.manager.current(DEVICE))
            self.assertEqual(await panel.play(), "end")

    async def test_live_sound_goes_out_without_waiting_for_a_full_block(self):
        session = self.manager.create_session(DEVICE, "rtsp://cam/stream1", 22050, live=True)
        with mock.patch.object(AUDIO, "ffmpeg_audio_command", fake_command(TRICKLE_FFMPEG)):
            panel = await self.connect(session.token)
            await panel.hello()
            kind, sequence, payload = await panel.message()
            await panel.ack(sequence, len(payload))
            self.assertEqual(AUDIO.AUDIO_STREAM_FORMAT_STRUCT.unpack(payload)[4], 0)
            # 1001 bytes arrived, FFmpeg then waits: 1000 go out at once (b11
            # waited for 4 KB, 93 ms of sound), the odd byte waits for its pair.
            kind, sequence, payload = await panel.message(timeout=1.5)
            self.assertEqual((kind, len(payload)), (AUDIO.AUDIO_MESSAGE_AUDIO, 1000))
            self.manager.stop_device(DEVICE)

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
