"""Direct link wire format (link_protocol.py): shared vectors and limits."""

from __future__ import annotations

import importlib
import json
import struct
import sys
import types
import unittest
from unittest import mock

from test_view_navigation import ROOT

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except BaseException as error:  # A broken system build can panic in its Rust bindings.
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise
    HKDF = None

PACKAGE = "_hometiles_link_testpkg"


def load_link_module(name):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


LINK = load_link_module("link_protocol")

# Shared with the firmware (docs-dev/bridge-link.md, tools/tests/network/
# test-bridge-link-core.mjs): K is the pairing key of the pairing vector.
KEY = bytes.fromhex("b925def556256ead767b0f1d14879e50d6bddbd0bb44dc0d2435e4af011b3e19")
DEVICE_ID = "A1B2C3D4E5F6"
BASE = "hometiles/test"
N_P = bytes([0x11]) * 16
N_B = bytes([0x22]) * 16
SALT = "e2f7cbd13441d013eac632f477cc4089426d29a8b9d80d78bb1c9122521a344b"
PANEL_KEY = "858d17321dd91836bffc192068a51f76cf9307dbc5686201abd6fefdcc63fbc0"
BRIDGE_KEY = "2857758680e582927ad615da7c129064288311e90d6d9555594917fca8137514"
PANEL_READY = "00000011b230093592018101bedfa930245277e0b4"
BRIDGE_READY = "0000001156346d09190796699a0f2ce96c6763226c"
# Bridge frame 1: publish of "42" on hometiles/test/stat/value, retained.
BRIDGE_PUBLISH = ("0000002f0bbee66972e68a3cfe3a03196924acb3502794088cbb1936a4b7b7f09ee7649f85"
                  "bf839a2f0d7001a5f70e8b9883f3")
HELLO = ('{"v":1,"id":"A1B2C3D4E5F6","base":"hometiles/test","mode":"session",'
         '"kid":"20a8108ed11215c5","n":"11111111111111111111111111111111"}')

NEEDS_CRYPTOGRAPHY = unittest.skipIf(
    HKDF is None, "cryptography (bundled with Home Assistant) is required")


class VectorTest(unittest.TestCase):
    def test_salt_binds_device_base_and_both_nonces(self):
        self.assertEqual(LINK.session_salt(DEVICE_ID, BASE, N_P, N_B).hex(), SALT)
        self.assertNotEqual(LINK.session_salt(DEVICE_ID, "hometiles/other", N_P, N_B).hex(), SALT)
        self.assertNotEqual(LINK.session_salt(DEVICE_ID, BASE, N_B, N_P).hex(), SALT)

    @NEEDS_CRYPTOGRAPHY
    def test_session_keys_match_an_independent_hkdf(self):
        panel, bridge = LINK.session_keys(KEY, DEVICE_ID, BASE, N_P, N_B)
        self.assertEqual(panel.hex(), PANEL_KEY)
        self.assertEqual(bridge.hex(), BRIDGE_KEY)
        for info, expected in ((b"HomeTiles link panel-to-bridge v1", PANEL_KEY),
                               (b"HomeTiles link bridge-to-panel v1", BRIDGE_KEY)):
            hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes.fromhex(SALT), info=info)
            self.assertEqual(hkdf.derive(KEY).hex(), expected)

    @NEEDS_CRYPTOGRAPHY
    def test_sealed_frames_match_the_vectors_and_open_in_order(self):
        self.assertEqual(LINK.Sealer(bytes.fromhex(PANEL_KEY)).frame(LINK.TYPE_READY).hex(), PANEL_READY)
        sealer = LINK.Sealer(bytes.fromhex(BRIDGE_KEY))
        self.assertEqual(sealer.frame(LINK.TYPE_READY).hex(), BRIDGE_READY)
        publish = LINK.publish_payload("hometiles/test/stat/value", b"42", True)
        self.assertEqual(sealer.frame(LINK.TYPE_PUBLISH, publish).hex(), BRIDGE_PUBLISH)

        opener = LINK.Opener(bytes.fromhex(BRIDGE_KEY))
        for frame, expected_type in ((BRIDGE_READY, LINK.TYPE_READY), (BRIDGE_PUBLISH, LINK.TYPE_PUBLISH)):
            raw = bytes.fromhex(frame)
            frame_type, payload = opener.open(raw[:4], raw[4:])
            self.assertEqual(frame_type, expected_type)
        self.assertEqual(LINK.parse_publish(payload), ("hometiles/test/stat/value", b"42", True))

    @NEEDS_CRYPTOGRAPHY
    def test_replayed_reordered_or_altered_frames_are_rejected(self):
        raw = bytes.fromhex(BRIDGE_PUBLISH)
        # Counter 1 frame offered as the first frame: wrong nonce.
        with self.assertRaises(LINK.ProtocolError):
            LINK.Opener(bytes.fromhex(BRIDGE_KEY)).open(raw[:4], raw[4:])
        opener = LINK.Opener(bytes.fromhex(BRIDGE_KEY))
        ready = bytes.fromhex(BRIDGE_READY)
        opener.open(ready[:4], ready[4:])
        with self.assertRaises(LINK.ProtocolError):  # The same frame again.
            opener.open(ready[:4], ready[4:])
        altered = bytearray(raw)
        altered[10] ^= 1
        opener = LINK.Opener(bytes.fromhex(BRIDGE_KEY))
        opener.open(ready[:4], ready[4:])
        with self.assertRaises(LINK.ProtocolError):
            opener.open(bytes(altered[:4]), bytes(altered[4:]))
        # The panel's key does not open the Bridge's frames (reflection).
        with self.assertRaises(LINK.ProtocolError):
            LINK.Opener(bytes.fromhex(PANEL_KEY)).open(ready[:4], ready[4:])

    def test_hello_vector_parses(self):
        hello = LINK.parse_hello(HELLO.encode())
        self.assertEqual(hello["id"], DEVICE_ID)
        self.assertEqual(hello["base"], BASE)
        self.assertEqual(hello["mode"], LINK.MODE_SESSION)
        self.assertEqual(hello["kid"], "20a8108ed11215c5")
        self.assertEqual(hello["n"], N_P)
        self.assertEqual(LINK.build_hello(DEVICE_ID, BASE, LINK.MODE_SESSION, "20a8108ed11215c5", N_P).decode(), HELLO)


class HandshakeParsingTest(unittest.TestCase):
    def hello(self, **changes):
        base = {"v": 1, "id": DEVICE_ID, "base": BASE, "mode": "session",
                "kid": "20a8108ed11215c5", "n": "11" * 16}
        base.update(changes)
        return json.dumps({k: v for k, v in base.items() if v is not None}).encode()

    def test_pair_mode_needs_no_key_or_nonce(self):
        hello = LINK.parse_hello(self.hello(mode="pair", kid=None, n=None))
        self.assertEqual(hello, {"id": DEVICE_ID, "base": BASE, "mode": "pair"})

    def test_invalid_hellos_are_refused(self):
        cases = [
            self.hello(v=2),
            self.hello(id="bad id"),
            self.hello(id=""),
            self.hello(base="a/+/b"),
            self.hello(base="x" * 129),
            self.hello(mode="other"),
            self.hello(kid="ABCDEF0123456789"),
            self.hello(kid=None),
            self.hello(n="11" * 15),
            b"[1,2]",
            b"not json",
            b"{" + b" " * 600 + b"}",
        ]
        for payload in cases:
            with self.subTest(payload=payload[:60]):
                with self.assertRaises(LINK.ProtocolError):
                    LINK.parse_hello(payload)
        with self.assertRaises(LINK.ProtocolError) as caught:
            LINK.parse_hello(self.hello(v=2))
        self.assertEqual(str(caught.exception), LINK.REFUSE_VERSION)

    def test_welcome_and_refuse_bodies(self):
        self.assertEqual(LINK.build_welcome(N_B), b'{"v":1,"n":"' + (b"22" * 16) + b'"}')
        self.assertEqual(LINK.build_welcome(None), b'{"v":1}')
        self.assertEqual(LINK.build_refuse("unknown"), b'{"v":1,"r":"unknown"}')


class FrameAndTopicTest(unittest.TestCase):
    def test_frame_length_limits(self):
        self.assertEqual(LINK.parse_frame_length(struct.pack(">I", 1)), 1)
        self.assertEqual(LINK.parse_frame_length(struct.pack(">I", LINK.MAX_FRAME)), LINK.MAX_FRAME)
        for length in (0, LINK.MAX_FRAME + 1, 0xFFFFFFFF):
            with self.assertRaises(LINK.ProtocolError):
                LINK.parse_frame_length(struct.pack(">I", length))
        self.assertEqual(LINK.plain_frame(LINK.TYPE_PING), b"\x00\x00\x00\x01\x20")

    def test_publish_round_trip_and_limits(self):
        payload = LINK.publish_payload("a/b", b"x" * LINK.MAX_PAYLOAD, False)
        self.assertEqual(LINK.parse_publish(payload), ("a/b", b"x" * LINK.MAX_PAYLOAD, False))
        self.assertLessEqual(1 + len(payload) + LINK.TAG_LENGTH + 255, LINK.MAX_FRAME)
        with self.assertRaises(LINK.ProtocolError):
            LINK.publish_payload("a/b", b"x" * (LINK.MAX_PAYLOAD + 1), False)
        for bad in (b"", b"\x00\x00", b"\x02\x00\x01a", b"\x00\x00\x05ab", b"\x00\x00\x03a/#", b"\x00\x00\x02\xff\xfe"):
            with self.subTest(bad=bad):
                with self.assertRaises(LINK.ProtocolError):
                    LINK.parse_publish(bad)

    def test_stream_begin(self):
        payload = LINK.stream_begin_payload("base/stat/local_camera/image", 1234)
        self.assertEqual(LINK.parse_stream_begin(payload), ("base/stat/local_camera/image", 1234, False))
        with self.assertRaises(LINK.ProtocolError):
            LINK.parse_stream_begin(payload[:-1])
        with self.assertRaises(LINK.ProtocolError):
            LINK.stream_begin_payload("a", LINK.MAX_STREAM + 1)

    def test_topics_and_filters(self):
        self.assertTrue(LINK.valid_topic("hometiles/stat/value"))
        for bad in ("", "a/+", "a/#", "a\0b", "x" * 256, None, 3):
            self.assertFalse(LINK.valid_topic(bad))
        self.assertTrue(LINK.valid_filter("tab5_lvgl/config/+/bridge"))
        self.assertTrue(LINK.valid_filter("a/#"))
        for bad in ("a/#/b", "a/b#", "a/x+", ""):
            self.assertFalse(LINK.valid_filter(bad))
        self.assertTrue(LINK.topic_matches("tab5_lvgl/config/+/bridge", "tab5_lvgl/config/ABC/bridge"))
        self.assertFalse(LINK.topic_matches("tab5_lvgl/config/+/bridge", "tab5_lvgl/config/ABC/bridge/apply"))
        self.assertTrue(LINK.topic_matches("a/#", "a/b/c"))
        self.assertTrue(LINK.topic_matches("a/#", "a"))
        self.assertFalse(LINK.topic_matches("a/+", "a"))
        self.assertTrue(LINK.topic_matches("x/y", "x/y"))


if __name__ == "__main__":
    unittest.main()
