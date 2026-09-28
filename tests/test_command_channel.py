"""Encrypted panel commands (command_channel.py) and their Bridge wiring."""

from __future__ import annotations

import ast
import json
import logging
import types
from types import MappingProxyType
import unittest

try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
except BaseException as error:  # A broken system build can panic in its Rust bindings.
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise
    ChaCha20Poly1305 = None

from test_view_navigation import ROOT, load_module

CC = load_module("command_channel")
LOCAL_CAMERA = load_module("local_camera")

# Shared with the firmware test tools/tests/network/test-command-channel-core.mjs:
# both implementations must derive and seal exactly these values.
CODE = "ABCDE-FGHJK-MNPQR-STVWX-YZ012"
PANEL_KEY = "5e41f54d08a53a373dc49a8cb476f5ff8b311c4ec725bb221e545ae1047afc09"
BRIDGE_KEY = "5e0380b97a923649ffb7578e7ef4c7f852fa194a4586a7bd140a48bcfbd51c02"
KEY_ID = "8982fb24a78d94e1"
SESSION = "00112233445566778899aabbccddeeff"
FIRMWARE_COMMAND = (
    '{"v":1,"k":"8982fb24a78d94e1","n":"0102030405060708090a0b0c","d":"7832dd1bd249db728b0a50bc7f872a71128e0ceb'
    '9914732336b5052b125d2e15b17d8e61b37b8088f840f1bf08fff2509452d86153ead2e27de853dc2e18ceb8216139e4dd9f2ca29e'
    'cc3c75b9e87b874ff98daef024584a856613fa5f1d52fbb43bb25a9350ab84ebf6299d"}'
)
COMMAND_PLAINTEXT = (
    b"cmd 00112233445566778899aabbccddeeff 42 light\n"
    b'{"entity_id":"light.kitchen","state":"toggle"}'
)
BASE = "hometiles"
PANEL_TOPIC = "hometiles/secure/panel"
BRIDGE_TOPIC = "hometiles/secure/bridge"


def panel_seal(plaintext: bytes, topic: str = PANEL_TOPIC, nonce: bytes = b"\x07" * 12) -> str:
    """What the panel publishes (independent of the module under test)."""
    data = ChaCha20Poly1305(bytes.fromhex(PANEL_KEY)).encrypt(nonce, plaintext, topic.encode())
    return json.dumps({"v": 1, "k": KEY_ID, "n": nonce.hex(), "d": data.hex()})


def panel_open(envelope: str, topic: str = BRIDGE_TOPIC) -> bytes:
    """What the panel decrypts from the Bridge."""
    parsed = json.loads(envelope)
    assert parsed["k"] == KEY_ID
    return ChaCha20Poly1305(bytes.fromhex(BRIDGE_KEY)).decrypt(
        bytes.fromhex(parsed["n"]), bytes.fromhex(parsed["d"]), topic.encode())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Random:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        return bytes([self.counter % 256]) * size


def new_channel(clock=None):
    return CC.BridgeChannel(CODE, BASE, clock=clock or Clock(), random=Random())


def hello(challenge: str = "ffeeddccbbaa99887766554433221100") -> str:
    return panel_seal(f"hello - 0 {challenge}\n".encode())


def establish(channel, challenge: str = "ffeeddccbbaa99887766554433221100") -> str:
    action, (topic, payload) = channel.handle_panel_message(hello(challenge))
    assert action == "reply" and topic == BRIDGE_TOPIC
    header = panel_open(payload).decode().split("\n")[0].split(" ")
    assert header[0] == "session" and header[3] == challenge
    return header[1]


class PairingCodeTest(unittest.TestCase):
    def test_keys_match_the_firmware_vector(self):
        keys = CC.Keys(CODE)
        self.assertEqual(keys.panel_to_bridge.hex(), PANEL_KEY)
        self.assertEqual(keys.bridge_to_panel.hex(), BRIDGE_KEY)
        self.assertEqual(keys.key_id, KEY_ID)
        self.assertNotIn(PANEL_KEY, repr(keys))
        self.assertNotIn(BRIDGE_KEY, repr(keys))

    def test_code_input_is_tolerant_like_the_firmware(self):
        canonical = "ABCDEFGHJKMNPQRSTVWXYZ012"
        self.assertEqual(CC.normalize_code(CODE), canonical)
        self.assertEqual(CC.normalize_code(" abcde fghjk mnpqr stvwx yz012 "), canonical)
        # O reads as 0, I and L as 1.
        self.assertEqual(CC.normalize_code("ABCDE-FGHJK-MNPQR-STVWX-YZOI2"), canonical)
        self.assertEqual(CC.normalize_code("abcde-fghjk-mnpqr-stvwx-yzol2"), canonical)
        for invalid in ("ABCDE-FGHJK-MNPQR-STVWX-YZ01", "ABCDE-FGHJK-MNPQR-STVWX-YZ0123",
                        "ABCDE-FGHJK-MNPQR-STVWX-YZ01U", "", None, 12345):
            self.assertIsNone(CC.normalize_code(invalid), invalid)
        self.assertEqual(CC.format_code(canonical), CODE)
        self.assertEqual(CC.key_id_for_code(CODE.lower()), KEY_ID)
        self.assertIsNone(CC.key_id_for_code("nope"))

    def test_entered_code_is_checked_against_the_panel_status(self):
        pending = {"state": "pending", "kid": KEY_ID}
        self.assertEqual(CC.check_pairing_code(CODE.lower(), pending), ("ABCDEFGHJKMNPQRSTVWXYZ012", None))
        self.assertEqual(CC.check_pairing_code(CODE, {"state": "active", "kid": KEY_ID})[1], None)
        self.assertEqual(CC.check_pairing_code("ABCDE", pending), (None, "invalid_pairing_code"))
        # Old firmware, pairing off on the panel, or no retained status yet.
        for status in (None, {"state": "off", "kid": None}, {"state": "unknown", "kid": None}):
            self.assertEqual(CC.check_pairing_code(CODE, status), (None, "pairing_not_started"))
        self.assertEqual(CC.check_pairing_code(CODE, {"state": "pending", "kid": "0" * 16}),
                         (None, "pairing_code_mismatch"))

    def test_status_parsing(self):
        self.assertEqual(CC.parse_status(""), {"state": "off", "kid": None})
        self.assertEqual(CC.parse_status(b""), {"state": "off", "kid": None})
        self.assertEqual(CC.parse_status('{"v":1,"state":"pending","kid":"8982FB24A78D94E1"}'),
                         {"state": "pending", "kid": KEY_ID})
        self.assertEqual(CC.parse_status('{"state":"active","kid":"xyz"}'), {"state": "active", "kid": None})
        self.assertEqual(CC.parse_status("not json"), {"state": "unknown", "kid": None})
        self.assertEqual(CC.parse_status('{"state":"hacked"}'), {"state": "unknown", "kid": None})
        self.assertEqual(CC.parse_status("x" * 300), {"state": "off", "kid": None})

    def test_stored_code_prefers_options(self):
        entry = types.SimpleNamespace(data={"command_pairing_code": CODE}, options={})
        self.assertEqual(CC.entry_pairing_code(entry), "ABCDEFGHJKMNPQRSTVWXYZ012")
        entry = types.SimpleNamespace(data={}, options={})
        self.assertIsNone(CC.entry_pairing_code(entry))
        entry = types.SimpleNamespace(data={"command_pairing_code": "broken"}, options=None)
        self.assertIsNone(CC.entry_pairing_code(entry))
        # Home Assistant stores entry data and options as read-only mappings.
        entry = types.SimpleNamespace(data=MappingProxyType({"command_pairing_code": CODE}),
                                      options=MappingProxyType({}))
        self.assertEqual(CC.entry_pairing_code(entry), "ABCDEFGHJKMNPQRSTVWXYZ012")
        entry = types.SimpleNamespace(data=MappingProxyType({}), options=MappingProxyType({}))
        self.assertIsNone(CC.entry_pairing_code(entry))


NEEDS_CRYPTOGRAPHY = unittest.skipIf(
    ChaCha20Poly1305 is None, "cryptography (bundled with Home Assistant) is required")


@NEEDS_CRYPTOGRAPHY
class EnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.keys = CC.Keys(CODE)

    def test_firmware_command_envelope_is_byte_identical(self):
        sealed = CC.seal(self.keys.panel_to_bridge, KEY_ID, PANEL_TOPIC, COMMAND_PLAINTEXT,
                         bytes(range(1, 13)))
        self.assertEqual(sealed, FIRMWARE_COMMAND)
        status, plaintext = CC.open_envelope(self.keys.panel_to_bridge, KEY_ID, PANEL_TOPIC, FIRMWARE_COMMAND)
        self.assertEqual((status, plaintext), (CC.OPEN_OK, COMMAND_PLAINTEXT))
        message = CC.parse_plaintext(plaintext)
        self.assertEqual((message.kind, message.session, message.seq, message.name),
                         ("cmd", SESSION, 42, "light"))
        self.assertEqual(json.loads(message.body), {"entity_id": "light.kitchen", "state": "toggle"})

    def test_topic_key_and_tampering_are_rejected(self):
        key = self.keys.panel_to_bridge
        self.assertEqual(CC.open_envelope(key, KEY_ID, "other/secure/panel", FIRMWARE_COMMAND)[0],
                         CC.OPEN_REJECTED)
        # A Bridge message reflected back to the Bridge uses the other key.
        reflected = CC.seal(self.keys.bridge_to_panel, KEY_ID, PANEL_TOPIC, COMMAND_PLAINTEXT)
        self.assertEqual(CC.open_envelope(key, KEY_ID, PANEL_TOPIC, reflected)[0], CC.OPEN_REJECTED)
        tampered = json.loads(FIRMWARE_COMMAND)
        tampered["d"] = ("0" if tampered["d"][0] != "0" else "1") + tampered["d"][1:]
        self.assertEqual(CC.open_envelope(key, KEY_ID, PANEL_TOPIC, json.dumps(tampered))[0], CC.OPEN_REJECTED)
        other = dict(json.loads(FIRMWARE_COMMAND), k="0" * 16)
        self.assertEqual(CC.open_envelope(key, KEY_ID, PANEL_TOPIC, json.dumps(other))[0], CC.OPEN_OTHER_KEY)
        for malformed in ("", "not json", "[]", '{"v":2}', '{"v":1,"k":"8982fb24a78d94e1","n":"00","d":"00"}',
                          b"\xff\xfe", "x" * 10000, None):
            self.assertEqual(CC.open_envelope(key, KEY_ID, PANEL_TOPIC, malformed)[0], CC.OPEN_MALFORMED, malformed)

    def test_plaintext_header_is_strict(self):
        self.assertEqual(CC.build_plaintext("hello", None, 0, "ffeeddccbbaa99887766554433221100"),
                         b"hello - 0 ffeeddccbbaa99887766554433221100\n")
        for invalid in (b"cmd - 1 light", b"cmd - 1 light extra\n", b"cmd - 01 light\n", b"cmd - 1 Light\n",
                        b"cmd - 4294967296 light\n", b"cmd ABC 1 light\n", b"nope - 1 light\n",
                        b"cmd - 1 light\n" + b"x" * 2049, b"\xff - 1 light\n"):
            self.assertIsNone(CC.parse_plaintext(invalid), invalid)
        self.assertEqual(CC.parse_plaintext(b"cmd - 4294967295 light\n").seq, 4294967295)
        with self.assertRaises(ValueError):
            CC.build_plaintext("cmd", SESSION, 1, "Light")
        with self.assertRaises(ValueError):
            CC.build_plaintext("cmd", SESSION, 1, "light", b"x" * 2049)

    def test_replay_window_matches_the_firmware(self):
        window = CC.ReplayWindow()
        self.assertFalse(window.accept(0))
        self.assertTrue(window.accept(1))
        self.assertFalse(window.accept(1))
        self.assertTrue(window.accept(5))
        self.assertTrue(window.accept(3))  # Reordered between publish lanes.
        self.assertFalse(window.accept(3))
        self.assertTrue(window.accept(100))
        self.assertFalse(window.accept(36))  # 64 behind the highest.
        self.assertTrue(window.accept(37))
        self.assertTrue(window.accept(1000))
        self.assertFalse(window.accept(100))


@NEEDS_CRYPTOGRAPHY
class BridgeChannelTest(unittest.TestCase):
    def test_hello_creates_a_session_bound_to_the_challenge(self):
        channel = new_channel()
        session = establish(channel)
        self.assertEqual(channel.session, session)
        self.assertRegex(session, r"^[0-9a-f]{32}$")

    def test_commands_run_once_and_only_in_the_current_session(self):
        clock = Clock()
        channel = new_channel(clock)
        session = establish(channel)
        body = b'{"entity_id":"light.kitchen","state":"toggle"}'
        first = panel_seal(f"cmd {session} 1 light\n".encode() + body)
        self.assertEqual(channel.handle_panel_message(first), ("command", ("light", body)))
        self.assertEqual(channel.handle_panel_message(first), ("ignore", "replayed"))
        second = panel_seal(f"cmd {session} 2 value\n".encode() + b"{}", nonce=b"\x08" * 12)
        self.assertEqual(channel.handle_panel_message(second)[0], "command")
        # Unknown command names and panel-side types are never executed.
        self.assertEqual(channel.handle_panel_message(panel_seal(f"cmd {session} 3 restart\n".encode())),
                         ("ignore", "unknown_command"))
        self.assertEqual(channel.handle_panel_message(panel_seal(f"data {session} 4 camera\n".encode())),
                         ("ignore", "unexpected_type"))
        # A command from an older session asks the panel for a new one.
        stale = panel_seal(f"cmd {'ab' * 16} 1 light\n".encode() + body)
        action, (topic, payload) = channel.handle_panel_message(stale)
        self.assertEqual((action, topic), ("reply", BRIDGE_TOPIC))
        self.assertEqual(panel_open(payload), b"rekey - 0 -\n")
        # ...at most every five seconds.
        self.assertEqual(channel.handle_panel_message(stale), ("ignore", "stale_session"))
        clock.now += 5
        self.assertEqual(channel.handle_panel_message(stale)[0], "reply")

    def test_new_session_resets_numbering_and_limits_hello_rate(self):
        clock = Clock()
        channel = new_channel(clock)
        first = establish(channel)
        old = panel_seal(f"cmd {first} 1 light\n{{}}".encode())
        self.assertEqual(channel.handle_panel_message(hello("11" * 16)), ("ignore", "session_rate_limited"))
        clock.now += 2
        second = establish(channel, "22" * 16)
        self.assertNotEqual(first, second)
        self.assertEqual(channel.handle_panel_message(old)[0], "reply")  # Old session: rekey.
        fresh = panel_seal(f"cmd {second} 1 light\n{{}}".encode())
        self.assertEqual(channel.handle_panel_message(fresh)[0], "command")

    def test_invalid_messages_are_ignored(self):
        channel = new_channel()
        self.assertEqual(channel.handle_panel_message(panel_seal(f"hello {SESSION} 0 {'ff' * 16}\n".encode())),
                         ("ignore", "invalid_hello"))
        self.assertEqual(channel.handle_panel_message(panel_seal(b"hello - 0 light\n")), ("ignore", "invalid_hello"))
        self.assertEqual(channel.handle_panel_message(FIRMWARE_COMMAND.replace(KEY_ID, "0" * 16)),
                         ("ignore", "other_key"))
        self.assertEqual(channel.handle_panel_message(panel_seal(b"hello - 0 x\n", topic="other/secure/panel")),
                         ("ignore", "rejected"))
        self.assertIsNone(channel.session)

    def test_data_for_the_panel_is_sealed_and_numbered(self):
        channel = new_channel()
        self.assertIsNone(channel.seal_data("camera", b"{}"))  # No session yet.
        session = establish(channel)
        topic, payload = channel.seal_data("camera", b'{"status":"ready","url":"tcp://h:1/t0k3n"}')
        self.assertEqual(topic, BRIDGE_TOPIC)
        self.assertNotIn("t0k3n", payload)
        self.assertEqual(panel_open(payload),
                         f"data {session} 1 camera\n".encode() + b'{"status":"ready","url":"tcp://h:1/t0k3n"}')
        self.assertTrue(panel_open(channel.seal_data("local_camera", b"{}")[1]).startswith(
            f"data {session} 2 local_camera\n".encode()))
        self.assertIsNone(channel.seal_data("light", b"{}"))
        self.assertIsNone(channel.seal_data("camera", b"x" * 2049))
        # The panel cannot open Bridge data on another panel's topic.
        with self.assertRaises(Exception):
            panel_open(payload, topic="other/secure/bridge")

    def test_rekey_is_forced_at_startup_and_rate_limited_afterwards(self):
        clock = Clock()
        channel = new_channel(clock)
        topic, payload = channel.rekey(force=True)
        self.assertEqual((topic, panel_open(payload)), (BRIDGE_TOPIC, b"rekey - 0 -\n"))
        self.assertIsNone(channel.rekey())
        clock.now += 5
        self.assertIsNotNone(channel.rekey())


def bridge_class(scope):
    """Tab5Bridge methods from __init__.py on a small class, without HA."""
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    names = {
        "async_setup", "_async_setup_plain_commands", "_async_setup_secure_commands", "_command_handlers",
        "_secure_log_due", "_async_handle_secure_panel_message", "_async_handle_secure_status",
        "_async_publish_sealed_data", "_async_publish_camera_status", "async_publish_local_camera_command",
    }
    functions = [node for node in bridge.body if getattr(node, "name", None) in names]
    opened = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_OpenedCommand")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              opened, *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
    return type("Bridge", (), {name: scope[name] for name in names})


class FakeMqtt:
    def __init__(self):
        self.subscriptions = {}
        self.published = []

    async def async_subscribe(self, hass, topic, handler, qos=0, encoding="utf-8"):
        self.subscriptions[topic] = handler
        return lambda: self.subscriptions.pop(topic, None)

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


@NEEDS_CRYPTOGRAPHY
class BridgeWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        self.clock = Clock()
        scope = {
            "mqtt": self.mqtt, "json": json, "monotonic": self.clock,
            "_LOGGER": logging.getLogger("test_command_channel"),
            "command_status_topic": CC.status_topic, "parse_command_status": CC.parse_status,
            "local_camera_command_topic": LOCAL_CAMERA.local_camera_command_topic,
            "async_track_state_change_event": lambda *args: None,
        }
        self.Bridge = bridge_class(scope)
        self.handled = []

    def make(self, paired: bool):
        bridge = self.Bridge()
        bridge.hass = types.SimpleNamespace(data={})
        bridge.base_topic = BASE
        bridge.tracked_entities = []
        bridge._command_channel = CC.BridgeChannel(CODE, BASE, clock=self.clock, random=Random()) if paired else None
        bridge.command_status = {"state": "unknown", "kid": None}
        bridge._secure_log_at = {}
        bridge._refresh_runtime_entity_lists = lambda: None

        async def noop(*_args):
            return None
        bridge._async_setup_requests = noop
        for leaf in ("scene", "light", "switch", "value", "media", "climate", "cover", "camera"):
            async def handler(msg, leaf=leaf):
                self.handled.append((leaf, msg.topic, msg.payload, msg.retain))
            setattr(bridge, f"_async_handle_{leaf}_command", handler)
        bridge._async_handle_connected = noop
        bridge._async_handle_ip = noop
        return bridge

    async def test_unpaired_bridge_keeps_the_plain_protocol(self):
        bridge = self.make(paired=False)
        await bridge.async_setup()
        topics = set(self.mqtt.subscriptions)
        for leaf in ("scene", "light", "switch", "value", "media", "climate", "cover", "camera"):
            self.assertIn(f"{BASE}/cmnd/{leaf}", topics)
        self.assertNotIn(PANEL_TOPIC, topics)
        self.assertIn(f"{BASE}/stat/secure", topics)
        self.assertEqual(self.mqtt.published, [])
        await bridge._async_publish_camera_status({"status": "ready", "url": "tcp://h:1/t0k3n"})
        await bridge.async_publish_local_camera_command('{"op":"snapshot"}')
        self.assertEqual(self.mqtt.published, [
            (f"{BASE}/stat/camera", json.dumps({"status": "ready", "url": "tcp://h:1/t0k3n"}), False),
            (f"{BASE}/cmnd/local_camera", '{"op":"snapshot"}', False),
        ])

    async def test_paired_bridge_accepts_only_sealed_commands(self):
        bridge = self.make(paired=True)
        with self.assertLogs("test_command_channel", "INFO") as logs:
            await bridge.async_setup()
        self.assertNotIn(CODE, "\n".join(logs.output))
        self.assertNotIn(PANEL_KEY, "\n".join(logs.output))
        topics = set(self.mqtt.subscriptions)
        self.assertFalse([topic for topic in topics if "/cmnd/" in topic], topics)
        self.assertIn(PANEL_TOPIC, topics)
        # The restarted Bridge asks the panel for a new session right away.
        [(topic, payload, retain)] = self.mqtt.published
        self.assertEqual((topic, panel_open(payload), retain), (BRIDGE_TOPIC, b"rekey - 0 -\n", False))
        self.mqtt.published.clear()

        deliver = self.mqtt.subscriptions[PANEL_TOPIC]
        await deliver(types.SimpleNamespace(payload=hello(), retain=False))
        [(topic, payload, retain)] = self.mqtt.published
        session = panel_open(payload).decode().split(" ")[1]
        await deliver(types.SimpleNamespace(
            payload=panel_seal(f"cmd {session} 1 light\n".encode() + b'{"entity_id":"light.kitchen"}'),
            retain=False))
        self.assertEqual(self.handled, [("light", f"{BASE}/cmnd/light", '{"entity_id":"light.kitchen"}', False)])
        # Retained or replayed copies never run again.
        replay = panel_seal(f"cmd {session} 1 light\n".encode() + b'{"entity_id":"light.kitchen"}')
        await deliver(types.SimpleNamespace(payload=replay, retain=False))
        await deliver(types.SimpleNamespace(payload=panel_seal(f"cmd {session} 2 light\n{{}}".encode()), retain=True))
        self.assertEqual(len(self.handled), 1)

        # Camera replies and built-in camera requests carry stream tokens:
        # sealed only, never on the plain topics.
        self.mqtt.published.clear()
        await bridge._async_publish_camera_status({"status": "ready", "url": "tcp://h:1/t0k3n"})
        await bridge.async_publish_local_camera_command('{"op":"stream","token":"s3cr3t"}')
        self.assertEqual([topic for topic, _payload, _retain in self.mqtt.published], [BRIDGE_TOPIC, BRIDGE_TOPIC])
        self.assertTrue(all(retain is False for _topic, _payload, retain in self.mqtt.published))
        self.assertNotIn("t0k3n", self.mqtt.published[0][1])
        self.assertEqual(panel_open(self.mqtt.published[0][1]).split(b"\n")[0], f"data {session} 1 camera".encode())
        self.assertEqual(panel_open(self.mqtt.published[1][1]),
                         f"data {session} 2 local_camera\n".encode() + b'{"op":"stream","token":"s3cr3t"}')

    async def test_paired_bridge_without_session_drops_tokens_and_rekeys(self):
        bridge = self.make(paired=True)
        self.clock.now += 10
        await bridge._async_publish_camera_status({"status": "ready", "url": "tcp://h:1/t0k3n"})
        [(topic, payload, _retain)] = self.mqtt.published
        self.assertEqual((topic, panel_open(payload)), (BRIDGE_TOPIC, b"rekey - 0 -\n"))

    async def test_status_mismatch_is_reported_without_trusting_it(self):
        bridge = self.make(paired=True)
        await bridge.async_setup()
        self.mqtt.published.clear()
        status = self.mqtt.subscriptions[f"{BASE}/stat/secure"]
        with self.assertLogs("test_command_channel", "WARNING") as logs:
            await status(types.SimpleNamespace(payload='{"v":1,"state":"active","kid":"0000000000000000"}'))
        self.assertIn("another pairing code", logs.output[0])
        # A forged "off" does not turn the channel off.
        await status(types.SimpleNamespace(payload=""))
        self.assertIsNotNone(bridge._command_channel)
        self.assertEqual(bridge.command_status, {"state": "off", "kid": None})
        # A matching pending panel gets a rekey so it can finish pairing.
        self.clock.now += 5
        await status(types.SimpleNamespace(payload=json.dumps({"v": 1, "state": "pending", "kid": KEY_ID})))
        [(topic, payload, _retain)] = self.mqtt.published
        self.assertEqual(panel_open(payload), b"rekey - 0 -\n")


class LocalCameraPublisherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.published = []

        async def publish(hass, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload, retain))
        self.publish = publish

    async def test_unpaired_panel_without_bridge_gets_plain_requests(self):
        sent = await LOCAL_CAMERA.async_publish_local_camera_command(None, None, False, BASE, "{}", self.publish)
        self.assertTrue(sent)
        self.assertEqual(self.published, [(f"{BASE}/cmnd/local_camera", "{}", False)])

    async def test_paired_panel_never_gets_plain_requests(self):
        sent = await LOCAL_CAMERA.async_publish_local_camera_command(None, None, True, BASE, "{}", self.publish)
        self.assertFalse(sent)
        self.assertEqual(self.published, [])

    async def test_running_bridge_decides(self):
        calls = []

        async def sealed(payload):
            calls.append(payload)
        bridge = types.SimpleNamespace(async_publish_local_camera_command=sealed)
        await LOCAL_CAMERA.async_publish_local_camera_command(None, bridge, True, BASE, '{"a":1}', self.publish)
        self.assertEqual((calls, self.published), (['{"a":1}'], []))


class OptionsFlowContractTest(unittest.TestCase):
    def test_security_step_is_translated_everywhere(self):
        package = ROOT
        for path in [package / "strings.json", *sorted((package / "translations").glob("*.json"))]:
            data = json.loads(path.read_text(encoding="utf-8"))
            options = data["options"]
            self.assertIn("security", options["step"]["init"]["menu_options"], path)
            step = options["step"]["security"]
            self.assertEqual(set(step["data"]), {"pairing_code", "remove_pairing"}, path)
            self.assertIn("{key_id}", step["description"], path)
            for error in ("invalid_pairing_code", "pairing_not_started", "pairing_code_mismatch",
                          "pairing_code_empty"):
                self.assertIn(error, options["error"], path)
            # The result names the stored key id, so it is clear which panel entry holds the code.
            self.assertIn("{key_id}", options["abort"]["pairing_saved"], path)
            self.assertTrue(options["abort"]["pairing_removed"].strip(), path)

    def test_security_step_validates_before_storing(self):
        source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        step = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_security")
        segment = ast.get_source_segment(source, step)
        self.assertIn('"security"', source)
        self.assertLess(segment.index("check_pairing_code("), segment.index("updated[CONF_COMMAND_PAIRING] = code"))
        self.assertIn("updated.pop(CONF_COMMAND_PAIRING, None)", segment)
        self.assertNotIn("_LOGGER", segment)
        # An empty form is not reported as a success.
        self.assertIn('errors["base"] = "pairing_code_empty"', segment)
        self.assertIn('reason="pairing_saved"', segment)
        self.assertIn('reason="pairing_removed"', segment)
        self.assertNotIn("async_create_entry", segment)


if __name__ == "__main__":
    unittest.main()
