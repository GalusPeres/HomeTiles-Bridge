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
from test_pairing import ATTEMPT, KEY, NUMBER, Panel as PairingPanel, Random as PairingRandom

CC = load_module("command_channel")
LOCAL_CAMERA = load_module("local_camera")
PAIRING = load_module("pairing")

# The pairing key of the pairing v2 vectors (test_pairing.py) and the channel
# keys both implementations derive from it.
PAIRING_KEY = bytes.fromhex(KEY)
PANEL_KEY = "3ce896914535a84f25b6bcbb18bae3e2e0bbdefa5b03712b2fbaf16e51d084de"
BRIDGE_KEY = "5631cdca5a6fbae0a0fdfc738926f75254f40065c9e64322430aba78be18278d"
KEY_ID = "20a8108ed11215c5"
SESSION = "00112233445566778899aabbccddeeff"
# Envelope vectors shared with the firmware (docs-dev/command-encryption.md),
# sealed with the channel keys above on the base topic "hometiles".
ENVELOPE_PANEL_TOPIC = "hometiles/secure/panel"
ENVELOPE_BRIDGE_TOPIC = "hometiles/secure/bridge"
FIRMWARE_COMMAND = (
    '{"v":1,"k":"20a8108ed11215c5","n":"0102030405060708090a0b0c","d":"d6897ddce5bea6e0aa22053a5ef1bd4bfde4f4955a'
    '583f8114a4389b1d8763a81913cd4023b4686a4f505a521561c61f493950b10c113309dfcdf4e97cdb40c2355f680ae55b1a36c7740244'
    '160c7facdae9637913ba13e9598e1f05642507bc34fa264f166e670e20a04aab"}'
)
COMMAND_PLAINTEXT = (
    b"cmd 00112233445566778899aabbccddeeff 42 light\n"
    b'{"entity_id":"light.kitchen","state":"toggle"}'
)
BASE = "hometiles/test"
PANEL_TOPIC = "hometiles/test/secure/panel"
BRIDGE_TOPIC = "hometiles/test/secure/bridge"
PAIR_PANEL_TOPIC = "hometiles/test/pair/panel"
PAIR_BRIDGE_TOPIC = "hometiles/test/pair/bridge"
# Envelope vectors as well: an unpair in each direction.
UNPAIR_PLAINTEXT = b"unpair 0123456789abcdef0123456789abcdef 1 -\n"
UNPAIR_NONCE = bytes.fromhex("000102030405060708090a0b")
UNPAIR_FROM_PANEL = (
    '{"v":1,"k":"20a8108ed11215c5","n":"000102030405060708090a0b","d":"11a6abad551643a47b5deb36c6616860a1b94678af'
    '75e8ac6bfee025bc2f3e4c190eac833e0cb381557d3e5bda1967f7f79b5700e0ab459406d83fef"}'
)
UNPAIR_FROM_BRIDGE = (
    '{"v":1,"k":"20a8108ed11215c5","n":"000102030405060708090a0b","d":"2df48e85bda4d37632843fde4a78d178089f741c47'
    'e3291ce1d451a60cc7473dd4676251129cc80d9219d7433ce992181d00bc712a5ce6e994abb98f"}'
)


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
    return CC.BridgeChannel(PAIRING_KEY, BASE, clock=clock or Clock(), random=Random())


def hello(challenge: str = "ffeeddccbbaa99887766554433221100") -> str:
    return panel_seal(f"hello - 0 {challenge}\n".encode())


def establish(channel, challenge: str = "ffeeddccbbaa99887766554433221100") -> str:
    action, (topic, payload) = channel.handle_panel_message(hello(challenge))
    assert action == "reply" and topic == BRIDGE_TOPIC
    header = panel_open(payload).decode().split("\n")[0].split(" ")
    assert header[0] == "session" and header[3] == challenge
    return header[1]


class PairingKeyTest(unittest.TestCase):
    def test_keys_match_the_pairing_vector(self):
        keys = CC.Keys(PAIRING_KEY)
        self.assertEqual(keys.panel_to_bridge.hex(), PANEL_KEY)
        self.assertEqual(keys.bridge_to_panel.hex(), BRIDGE_KEY)
        self.assertEqual(keys.key_id, KEY_ID)
        self.assertNotIn(PANEL_KEY, repr(keys))
        self.assertNotIn(BRIDGE_KEY, repr(keys))
        for invalid in (b"", PAIRING_KEY[:31], PAIRING_KEY + b"\x00", KEY, None):
            with self.assertRaises(ValueError):
                CC.Keys(invalid)
        self.assertEqual(CC.key_id_for_key(PAIRING_KEY), KEY_ID)
        self.assertIsNone(CC.key_id_for_key(None))

    def test_status_parsing(self):
        self.assertEqual(CC.parse_status(""), {"state": "off", "kid": None})
        self.assertEqual(CC.parse_status(b""), {"state": "off", "kid": None})
        self.assertEqual(CC.parse_status('{"v":1,"state":"active","kid":"20A8108ED11215C5"}'),
                         {"state": "active", "kid": KEY_ID})
        self.assertEqual(CC.parse_status('{"state":"active","kid":"xyz"}'), {"state": "active", "kid": None})
        # Pairing v2 has no "pending" state any more.
        self.assertEqual(CC.parse_status('{"state":"pending","kid":"20a8108ed11215c5"}'),
                         {"state": "unknown", "kid": KEY_ID})
        self.assertEqual(CC.parse_status("not json"), {"state": "unknown", "kid": None})
        self.assertEqual(CC.parse_status('{"state":"hacked"}'), {"state": "unknown", "kid": None})
        self.assertEqual(CC.parse_status("x" * 300), {"state": "off", "kid": None})

    def test_stored_key_prefers_options(self):
        entry = types.SimpleNamespace(data={"command_pairing_key": KEY}, options={})
        self.assertEqual(CC.entry_pairing_key(entry), PAIRING_KEY)
        entry = types.SimpleNamespace(data={"command_pairing_key": KEY}, options={"command_pairing_key": "ab" * 32})
        self.assertEqual(CC.entry_pairing_key(entry), bytes([0xAB]) * 32)
        entry = types.SimpleNamespace(data={}, options={})
        self.assertIsNone(CC.entry_pairing_key(entry))
        for broken in ("broken", KEY.upper(), KEY[:-2], 12):
            entry = types.SimpleNamespace(data={"command_pairing_key": broken}, options=None)
            self.assertIsNone(CC.entry_pairing_key(entry), broken)
        # Home Assistant stores entry data and options as read-only mappings.
        entry = types.SimpleNamespace(data=MappingProxyType({"command_pairing_key": KEY}),
                                      options=MappingProxyType({}))
        self.assertEqual(CC.entry_pairing_key(entry), PAIRING_KEY)
        entry = types.SimpleNamespace(data=MappingProxyType({}), options=MappingProxyType({}))
        self.assertIsNone(CC.entry_pairing_key(entry))

    def test_removed_key_waits_for_the_panel(self):
        removing = types.SimpleNamespace(
            data=MappingProxyType({"base_topic": BASE, "command_pairing_removing": KEY}), options=MappingProxyType({}))
        self.assertIsNone(CC.entry_pairing_key(removing))
        self.assertEqual(CC.entry_removing_key(removing), PAIRING_KEY)
        # A new pairing wins over a removal still in progress.
        paired = types.SimpleNamespace(
            data=MappingProxyType({"command_pairing_key": KEY, "command_pairing_removing": KEY}), options={})
        self.assertIsNone(CC.entry_removing_key(paired))
        self.assertEqual(CC.without_pairing(removing.data), {"base_topic": BASE})
        self.assertEqual(CC.without_pairing(None), {})

    def test_code_of_the_betas_is_dropped(self):
        # v0.7.1b1/b2 stored a typed code; it is neither a key nor kept.
        legacy = types.SimpleNamespace(
            data=MappingProxyType({"base_topic": BASE, "command_pairing_code": "ABCDEFGHJKMNPQRSTVWXYZ012"}),
            options=MappingProxyType({}))
        self.assertTrue(CC.has_legacy_code(legacy))
        self.assertIsNone(CC.entry_pairing_key(legacy))
        self.assertEqual(CC.without_pairing(legacy.data), {"base_topic": BASE})
        self.assertFalse(CC.has_legacy_code(types.SimpleNamespace(data={"base_topic": BASE}, options=None)))
        self.assertEqual(CC.with_pairing_key(legacy.data, PAIRING_KEY),
                         {"base_topic": BASE, "command_pairing_key": KEY})
        with self.assertRaises(ValueError):
            CC.with_pairing_key({}, b"short")

    def test_setup_drops_the_code_before_the_bridge_reads_the_entry(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup = next(node for node in tree.body
                     if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
        segment = ast.get_source_segment(source, setup)
        self.assertLess(segment.index("if has_legacy_code(entry):"), segment.index("bridge = Tab5Bridge(hass, entry)"))
        self.assertLess(segment.index("bridge = Tab5Bridge(hass, entry)"), segment.index("add_update_listener"))


NEEDS_CRYPTOGRAPHY = unittest.skipIf(
    ChaCha20Poly1305 is None, "cryptography (bundled with Home Assistant) is required")


@NEEDS_CRYPTOGRAPHY
class EnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.keys = CC.Keys(PAIRING_KEY)

    def test_firmware_command_envelope_is_byte_identical(self):
        sealed = CC.seal(self.keys.panel_to_bridge, KEY_ID, ENVELOPE_PANEL_TOPIC, COMMAND_PLAINTEXT,
                         bytes(range(1, 13)))
        self.assertEqual(sealed, FIRMWARE_COMMAND)
        status, plaintext = CC.open_envelope(self.keys.panel_to_bridge, KEY_ID, ENVELOPE_PANEL_TOPIC,
                                             FIRMWARE_COMMAND)
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
        reflected = CC.seal(self.keys.bridge_to_panel, KEY_ID, ENVELOPE_PANEL_TOPIC, COMMAND_PLAINTEXT)
        self.assertEqual(CC.open_envelope(key, KEY_ID, ENVELOPE_PANEL_TOPIC, reflected)[0], CC.OPEN_REJECTED)
        tampered = json.loads(FIRMWARE_COMMAND)
        tampered["d"] = ("0" if tampered["d"][0] != "0" else "1") + tampered["d"][1:]
        self.assertEqual(CC.open_envelope(key, KEY_ID, ENVELOPE_PANEL_TOPIC, json.dumps(tampered))[0],
                         CC.OPEN_REJECTED)
        other = dict(json.loads(FIRMWARE_COMMAND), k="0" * 16)
        self.assertEqual(CC.open_envelope(key, KEY_ID, ENVELOPE_PANEL_TOPIC, json.dumps(other))[0],
                         CC.OPEN_OTHER_KEY)
        for malformed in ("", "not json", "[]", '{"v":2}', '{"v":1,"k":"20a8108ed11215c5","n":"00","d":"00"}',
                          b"\xff\xfe", "x" * 10000, None):
            self.assertEqual(CC.open_envelope(key, KEY_ID, ENVELOPE_PANEL_TOPIC, malformed)[0], CC.OPEN_MALFORMED,
                             malformed)

    def test_unpair_envelopes_are_byte_identical(self):
        self.assertEqual(CC.seal(self.keys.panel_to_bridge, KEY_ID, ENVELOPE_PANEL_TOPIC, UNPAIR_PLAINTEXT,
                                 UNPAIR_NONCE), UNPAIR_FROM_PANEL)
        self.assertEqual(CC.seal(self.keys.bridge_to_panel, KEY_ID, ENVELOPE_BRIDGE_TOPIC, UNPAIR_PLAINTEXT,
                                 UNPAIR_NONCE), UNPAIR_FROM_BRIDGE)
        self.assertEqual(CC.build_plaintext("unpair", "0123456789abcdef0123456789abcdef", 1, None),
                         UNPAIR_PLAINTEXT)
        message = CC.parse_plaintext(UNPAIR_PLAINTEXT)
        self.assertEqual((message.kind, message.session, message.seq, message.name, message.body),
                         ("unpair", "0123456789abcdef0123456789abcdef", 1, None, b""))

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
        self.assertEqual(channel.handle_panel_message(json.dumps(dict(json.loads(hello()), k="0" * 16))),
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

    def test_unpair_from_the_panel_counts_like_a_command(self):
        channel = new_channel()
        session = establish(channel)
        unpair = panel_seal(f"unpair {session} 1 -\n".encode())
        self.assertEqual(channel.handle_panel_message(unpair), ("unpair", None))
        # Authenticated, current session only, and never twice.
        self.assertEqual(channel.handle_panel_message(unpair), ("ignore", "replayed"))
        other = panel_seal(f"unpair {'ab' * 16} 2 -\n".encode(), nonce=b"\x08" * 12)
        self.assertEqual(channel.handle_panel_message(other), ("ignore", "stale_session"))
        named = panel_seal(f"unpair {session} 3 light\n".encode(), nonce=b"\x09" * 12)
        self.assertEqual(channel.handle_panel_message(named), ("ignore", "invalid_unpair"))
        # The panel's numbering is shared with its commands.
        command = panel_seal(f"cmd {session} 1 light\n{{}}".encode(), nonce=b"\x0a" * 12)
        self.assertEqual(channel.handle_panel_message(command), ("ignore", "replayed"))

    def test_removing_channel_only_carries_the_unpair(self):
        clock = Clock()
        channel = CC.BridgeChannel(PAIRING_KEY, BASE, clock=clock, random=Random(), removing=True)
        self.assertIsNone(channel.seal_unpair())  # No session yet.
        session = establish(channel)
        topic, payload = channel.seal_unpair()
        self.assertEqual((topic, panel_open(payload)), (BRIDGE_TOPIC, f"unpair {session} 1 -\n".encode()))
        # Sealed commands no longer run; a stale one still gets a rekey.
        command = panel_seal(f"cmd {session} 1 light\n{{}}".encode())
        self.assertEqual(channel.handle_panel_message(command), ("ignore", "removing"))
        stale = panel_seal(f"cmd {'ab' * 16} 2 light\n{{}}".encode(), nonce=b"\x08" * 12)
        self.assertEqual(channel.handle_panel_message(stale)[0], "reply")

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
        "_async_send_start_rekey", "_drop_pairing", "_notify_pairing",
        "_async_handle_pair_message", "pairing_number", "async_answer_pairing", "_async_pairing_tick",
        "_schedule_pairing_tick", "_async_apply_pairing_events", "_show_pairing_card", "_close_pairing_card",
        "_store_pairing",
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
        self.timers = []
        self.notifications = []
        self.updates = []
        self.cards = []
        self.flows = []
        self.aborted = []

        def call_later(hass, delay, action):
            self.timers.append((delay, action))
            return lambda: None

        def notify(hass, message, title=None, notification_id=None):
            self.notifications.append((notification_id, message))

        scope = {
            "mqtt": self.mqtt, "json": json, "monotonic": self.clock,
            "_LOGGER": logging.getLogger("test_command_channel"),
            "command_status_topic": CC.status_topic, "parse_command_status": CC.parse_status,
            "local_camera_command_topic": LOCAL_CAMERA.local_camera_command_topic,
            "async_track_state_change_event": lambda *args: None,
            "async_call_later": call_later, "SECURE_START_REKEY_DELAY_S": 3.0,
            "persistent_notification": types.SimpleNamespace(async_create=notify),
            "without_pairing": CC.without_pairing, "DOMAIN": "tab5_lvgl",
            "with_pairing_key": CC.with_pairing_key, "CommandKeys": CC.Keys, "PAIRING_TICK_S": 1.0,
            "discovery_flow": types.SimpleNamespace(
                async_create_flow=lambda hass, domain, context, data: self.cards.append((domain, context, data))),
            "config_entries": types.SimpleNamespace(SOURCE_INTEGRATION_DISCOVERY="integration_discovery"),
            "DISCOVERY_PAIRING_ENTRY": "pairing_entry_id", "DISCOVERY_PAIRING_ATTEMPT": "pairing_attempt",
            "PAIRING_UNIQUE_ID_PREFIX": "pairing_",
        }
        self.Bridge = bridge_class(scope)
        self.handled = []

    async def fire_timers(self, delay=3.0):
        """Run the timers with this delay (3 s: start rekey, 1 s: pairing tick)."""
        due = [action for when, action in self.timers if when == delay]
        self.timers = [(when, action) for when, action in self.timers if when != delay]
        for action in due:
            await action(None)

    def make(self, paired: bool, removing: bool = False):
        bridge = self.Bridge()
        stored = {"base_topic": BASE}
        if paired:
            stored["command_pairing_key"] = KEY
        if removing:
            stored["command_pairing_removing"] = KEY

        def update(entry, **fields):
            self.updates.append(fields)

        def progress(handler, match_context=None):
            self.assertEqual((handler, match_context), ("tab5_lvgl", {"unique_id": "pairing_entry1"}))
            return [{"flow_id": flow_id} for flow_id in self.flows]

        flow = types.SimpleNamespace(async_progress_by_handler=progress, async_abort=self.aborted.append)
        bridge.hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(
            async_update_entry=update, flow=flow))
        bridge.entry = types.SimpleNamespace(entry_id="entry1", title="Kitchen",
                                             data=MappingProxyType(stored), options=MappingProxyType({}))
        bridge.base_topic = BASE
        bridge.tracked_entities = []
        bridge._command_channel = (
            CC.BridgeChannel(PAIRING_KEY, BASE, clock=self.clock, random=Random(), removing=removing)
            if paired or removing else None)
        bridge._unsub_start_rekey = None
        bridge._pairing = PAIRING.PanelPairing(BASE, clock=self.clock, random=PairingRandom())
        bridge._unsub_pair = None
        bridge._unsub_pairing_tick = None
        bridge._answering_flow_id = None
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
        self.assertIn(PAIR_PANEL_TOPIC, topics)
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(self.timers, [])
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
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn(PANEL_KEY, "\n".join(logs.output))
        topics = set(self.mqtt.subscriptions)
        self.assertFalse([topic for topic in topics if "/cmnd/" in topic], topics)
        self.assertIn(PANEL_TOPIC, topics)
        # The restarted Bridge asks the panel for a new session once Home
        # Assistant has sent the subscription to the broker.
        self.assertEqual(self.mqtt.published, [])
        await self.fire_timers()
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
        self.assertIn("another pairing key", logs.output[0])
        # A forged "off" does not turn the channel off; the user is told instead.
        with self.assertLogs("test_command_channel", "WARNING") as logs:
            await status(types.SimpleNamespace(payload=""))
        self.assertIn("reports encryption off", logs.output[0])
        self.assertIsNotNone(bridge._command_channel)
        self.assertEqual(self.updates, [])
        self.assertEqual([notification_id for notification_id, _ in self.notifications], ["tab5_lvgl_entry1_pairing"])
        self.assertEqual(bridge.command_status, {"state": "off", "kid": None})
        # A matching panel without a session gets a rekey.
        self.clock.now += 5
        await status(types.SimpleNamespace(payload=json.dumps({"v": 1, "state": "active", "kid": KEY_ID})))
        [(topic, payload, _retain)] = self.mqtt.published
        self.assertEqual(panel_open(payload), b"rekey - 0 -\n")


    async def test_unpair_from_the_panel_removes_the_pairing(self):
        bridge = self.make(paired=True)
        await bridge.async_setup()
        await self.fire_timers()
        deliver = self.mqtt.subscriptions[PANEL_TOPIC]
        await deliver(types.SimpleNamespace(payload=hello(), retain=False))
        session = panel_open(self.mqtt.published[-1][1]).decode().split(" ")[1]
        with self.assertLogs("test_command_channel", "WARNING") as logs:
            await deliver(types.SimpleNamespace(payload=panel_seal(f"unpair {session} 1 -\n".encode()), retain=False))
        self.assertIn("turned encryption off", logs.output[0])
        self.assertIsNone(bridge._command_channel)
        # The entry forgets the key and reloads unpaired.
        self.assertEqual(self.updates, [{"data": {"base_topic": BASE}, "options": {}}])
        self.assertEqual(len(self.notifications), 1)

    async def test_removed_pairing_turns_the_panel_off_too(self):
        bridge = self.make(paired=False, removing=True)
        await bridge.async_setup()
        topics = set(self.mqtt.subscriptions)
        # Plain commands work again while the panel is being unpaired.
        for leaf in ("scene", "light", "switch", "value", "media", "climate", "cover", "camera"):
            self.assertIn(f"{BASE}/cmnd/{leaf}", topics)
        self.assertIn(PANEL_TOPIC, topics)
        await self.fire_timers()
        self.assertEqual(panel_open(self.mqtt.published[-1][1]), b"rekey - 0 -\n")
        self.mqtt.published.clear()
        # The panel's hello gets the session and right away the unpair.
        await self.mqtt.subscriptions[PANEL_TOPIC](types.SimpleNamespace(payload=hello(), retain=False))
        [session_message, unpair] = [panel_open(payload) for _topic, payload, _retain in self.mqtt.published]
        session = session_message.decode().split(" ")[1]
        self.assertEqual(unpair, f"unpair {session} 1 -\n".encode())
        # Stream tokens go plain again: the panel is about to turn pairing off.
        self.mqtt.published.clear()
        await bridge._async_publish_camera_status({"status": "stopped"})
        self.assertEqual(self.mqtt.published[0][0], f"{BASE}/stat/camera")
        # Once the panel's status is empty, the Bridge forgets the key.
        status = self.mqtt.subscriptions[f"{BASE}/stat/secure"]
        await status(types.SimpleNamespace(payload='{"v":1,"state":"active","kid":"20a8108ed11215c5"}'))
        self.assertEqual(self.updates, [])
        await status(types.SimpleNamespace(payload=""))
        self.assertEqual(self.updates, [{"data": {"base_topic": BASE}, "options": {}}])
        self.assertEqual(self.notifications, [])


    async def pair_until_the_number(self, bridge):
        await bridge.async_setup()
        deliver = self.mqtt.subscriptions[PAIR_PANEL_TOPIC]
        panel = PairingPanel()
        await deliver(types.SimpleNamespace(payload=panel.start(), retain=False))
        [(topic, commit, retain)] = self.mqtt.published
        self.assertEqual((topic, retain), (PAIR_BRIDGE_TOPIC, False))
        with self.assertLogs("test_command_channel", "INFO") as logs:
            await deliver(types.SimpleNamespace(payload=panel.send_nonce(), retain=False))
        self.assertIn("asks to pair", logs.output[0])
        derived = panel.finish(json.loads(commit), json.loads(self.mqtt.published[-1][1]))
        self.mqtt.published.clear()
        return deliver, panel, derived

    async def test_panel_pairs_by_comparing_the_number(self):
        bridge = self.make(paired=False)
        deliver, panel, derived = await self.pair_until_the_number(bridge)
        # A card under Discovered; it reads the number from the Bridge.
        self.assertEqual(self.cards, [("tab5_lvgl", {"source": "integration_discovery"},
                                       {"pairing_entry_id": "entry1", "pairing_attempt": ATTEMPT})])
        self.assertEqual(bridge.pairing_number(ATTEMPT), NUMBER)
        self.assertEqual(derived["number"], NUMBER.replace(" ", ""))
        self.flows = ["card"]
        self.assertEqual(await bridge.async_answer_pairing(ATTEMPT, True, "card"), "waiting")
        [(topic, confirm, _retain)] = self.mqtt.published
        self.assertEqual((topic, json.loads(confirm)["m"]), (PAIR_BRIDGE_TOPIC, derived["m_bridge"]))
        self.assertEqual(self.aborted, [])
        # The Bridge repeats its confirm while the panel's is missing.
        self.mqtt.published.clear()
        self.clock.now += 2
        await self.fire_timers(1.0)
        self.assertEqual([json.loads(payload)["t"] for _topic, payload, _retain in self.mqtt.published], ["confirm"])
        with self.assertLogs("test_command_channel", "INFO") as logs:
            await deliver(types.SimpleNamespace(payload=panel.confirm(derived["m_panel"]), retain=False))
        self.assertIn(f"paired (key id {KEY_ID})", "\n".join(logs.output))
        self.assertNotIn(KEY, "\n".join(logs.output))
        # The card closes and the entry reloads with the key.
        self.assertEqual(self.aborted, ["card"])
        self.assertEqual(self.updates, [{"data": {"base_topic": BASE, "command_pairing_key": KEY}, "options": {}}])

    async def test_the_answering_card_is_not_aborted_under_its_feet(self):
        bridge = self.make(paired=False)
        deliver, panel, derived = await self.pair_until_the_number(bridge)
        await deliver(types.SimpleNamespace(payload=panel.confirm(derived["m_panel"]), retain=False))
        self.flows = ["card", "stale"]
        self.assertEqual(await bridge.async_answer_pairing(ATTEMPT, True, "card"), "paired")
        self.assertEqual(self.aborted, ["stale"])
        self.assertEqual(len(self.updates), 1)

    async def test_rejected_or_cancelled_pairing_changes_nothing(self):
        bridge = self.make(paired=False)
        deliver, panel, _derived = await self.pair_until_the_number(bridge)
        self.flows = ["card"]
        self.assertEqual(await bridge.async_answer_pairing(ATTEMPT, False, "card"), "rejected")
        self.assertEqual(json.loads(self.mqtt.published[0][1]), {"v": 2, "t": "abort", "id": ATTEMPT, "r": "rejected"})
        self.assertEqual(await bridge.async_answer_pairing(ATTEMPT, True, "card"), "expired")
        self.assertEqual(self.updates, [])
        # Retained pairing messages are never handled.
        self.mqtt.published.clear()
        await deliver(types.SimpleNamespace(payload=PairingPanel(attempt="8899aabbccddeeff").start(), retain=True))
        self.assertEqual(self.mqtt.published, [])

    async def test_a_paired_bridge_refuses_a_new_pairing(self):
        # A removal in progress counts as paired: the panel turns it off first.
        for removing in (False, True):
            bridge = self.make(paired=not removing, removing=removing)
            await bridge.async_setup()
            self.mqtt.published.clear()
            with self.assertLogs("test_command_channel", "WARNING") as logs:
                await self.mqtt.subscriptions[PAIR_PANEL_TOPIC](
                    types.SimpleNamespace(payload=PairingPanel().start(), retain=False))
            self.assertIn("still paired", logs.output[0])
            self.assertEqual([(topic, json.loads(payload)) for topic, payload, _retain in self.mqtt.published],
                             [(PAIR_BRIDGE_TOPIC, {"v": 2, "t": "abort", "id": ATTEMPT, "r": "paired"})])
            self.assertEqual(self.cards, [])


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


class FlowTextsTest(unittest.TestCase):
    def test_pairing_texts_are_translated_everywhere(self):
        package = ROOT
        for path in [package / "strings.json", *sorted((package / "translations").glob("*.json"))]:
            data = json.loads(path.read_text(encoding="utf-8"))
            options = data["options"]
            self.assertIn("security", options["step"]["init"]["menu_options"], path)
            step = options["step"]["security"]
            self.assertEqual(set(step["data"]), {"remove_pairing"}, path)
            self.assertIn("{key_id}", step["description"], path)
            self.assertIn("pairing_nothing_selected", options["error"], path)
            for reason in ("pairing_removed", "pairing_not_paired", "pairing_removing"):
                self.assertTrue(options["abort"][reason].strip(), path)
            for gone in ("pairing_code_empty", "invalid_pairing_code", "pairing_saved"):
                self.assertNotIn(gone, options["error"] | options["abort"], path)
            card = data["config"]["step"]["pairing_confirm"]
            self.assertIn("{name}", card["description"], path)
            self.assertIn("{number}", card["description"], path)
            self.assertEqual(set(card["menu_options"]), {"pairing_accept", "pairing_reject"}, path)
            for reason in ("pairing_done", "pairing_confirmed", "pairing_rejected", "pairing_expired"):
                self.assertTrue(data["config"]["abort"][reason].strip(), path)

    def test_security_step_only_removes(self):
        source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        step = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_security")
        segment = ast.get_source_segment(source, step)
        # A removed key waits until the panel turned pairing off too.
        self.assertIn("updated[CONF_COMMAND_PAIRING_REMOVING] = stored_key.hex()", segment)
        self.assertIn('reason="pairing_not_paired"', segment)
        self.assertIn('reason="pairing_removing"', segment)
        self.assertIn('reason="pairing_removed"', segment)
        # An empty form is not reported as a success.
        self.assertIn('errors["base"] = "pairing_nothing_selected"', segment)
        self.assertNotIn("_LOGGER", segment)
        self.assertNotIn("async_create_entry", segment)


def pairing_flow_class():
    """The pairing card steps of config_flow.py on a small flow, without HA."""
    source = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    flow = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5ConfigFlow")
    names = {"_pairing_bridge", "_pairing_number", "_async_start_pairing_card", "async_step_pairing_confirm",
             "async_step_pairing_accept", "async_step_pairing_reject", "_async_answer_pairing", "async_step_ignore"}
    functions = [node for node in flow.body if getattr(node, "name", None) in names]
    results = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and getattr(node.targets[0], "id", None) == "_PAIRING_RESULTS")
    discovery = next(node for node in flow.body if getattr(node, "name", None) == "async_step_integration_discovery")
    # Its first statement sends pairing data to the card; the panel card is left out.
    head = ast.AsyncFunctionDef(name="async_step_integration_discovery", args=discovery.args,
                                body=[discovery.body[1], ast.Return(ast.Constant("panel card"))],
                                decorator_list=[], returns=None, type_params=[])
    cls = ast.ClassDef(name="Flow", bases=[ast.Name("FlowBase", ast.Load())], keywords=[],
                       body=[*functions, head], decorator_list=[], type_params=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              results, cls], type_ignores=[])

    class FlowBase:
        _pairing_entry_id = None
        _pairing_attempt = None

        def __init__(self, hass):
            self.hass = hass
            self.context = {}
            self.flow_id = "card"

        async def async_set_unique_id(self, unique_id):
            self.context["unique_id"] = unique_id

        def async_abort(self, *, reason):
            return ("abort", reason)

        def async_show_menu(self, *, step_id, menu_options, description_placeholders):
            return ("menu", step_id, list(menu_options), description_placeholders)

        async def async_step_ignore(self, user_input):
            return ("ignored", user_input["unique_id"])

    scope = {"FlowBase": FlowBase, "DOMAIN": "tab5_lvgl", "DISCOVERY_PAIRING_ENTRY": "pairing_entry_id",
             "DISCOVERY_PAIRING_ATTEMPT": "pairing_attempt", "PAIRING_UNIQUE_ID_PREFIX": "pairing_"}
    exec(compile(ast.fix_missing_locations(module), "config_flow.py", "exec"), scope)
    return scope["Flow"]


class FakeBridge:
    def __init__(self, number=NUMBER, outcome="waiting"):
        self.number = number
        self.outcome = outcome
        self.answers = []

    def pairing_number(self, attempt_id):
        return self.number if attempt_id == ATTEMPT else None

    async def async_answer_pairing(self, attempt_id, accept, flow_id=None):
        self.answers.append((attempt_id, accept, flow_id))
        return self.outcome


class PairingCardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Flow = pairing_flow_class()
        self.bridge = FakeBridge()
        entry = types.SimpleNamespace(entry_id="entry1", title="Kitchen")
        self.hass = types.SimpleNamespace(
            data={"tab5_lvgl": {"entries": {"entry1": self.bridge}}},
            config_entries=types.SimpleNamespace(async_get_entry=lambda entry_id: entry if entry_id == "entry1" else None))

    async def open_card(self):
        flow = self.Flow(self.hass)
        result = await flow.async_step_integration_discovery(
            {"pairing_entry_id": "entry1", "pairing_attempt": ATTEMPT})
        return flow, result

    async def test_card_shows_the_number_and_passes_the_answer_on(self):
        flow, result = await self.open_card()
        self.assertEqual(result, ("menu", "pairing_confirm", ["pairing_accept", "pairing_reject"],
                                  {"name": "Kitchen", "number": NUMBER}))
        self.assertEqual(flow.context, {"unique_id": "pairing_entry1",
                                        "title_placeholders": {"name": f"Kitchen ({NUMBER})"}})
        self.assertEqual(await flow.async_step_pairing_accept(), ("abort", "pairing_confirmed"))
        self.assertEqual(self.bridge.answers, [(ATTEMPT, True, "card")])
        for outcome, reason in (("paired", "pairing_done"), ("rejected", "pairing_rejected"),
                                ("expired", "pairing_expired")):
            self.bridge.outcome = outcome
            self.assertEqual(await flow.async_step_pairing_accept(), ("abort", reason))
        self.bridge.outcome = "rejected"
        self.assertEqual(await flow.async_step_pairing_reject(), ("abort", "pairing_rejected"))
        self.assertEqual(self.bridge.answers[-1], (ATTEMPT, False, "card"))

    async def test_ended_attempts_show_no_number(self):
        self.bridge.number = None
        _flow, result = await self.open_card()
        self.assertEqual(result, ("abort", "pairing_expired"))
        self.bridge.number = NUMBER
        flow, _result = await self.open_card()
        self.hass.data["tab5_lvgl"]["entries"].clear()
        self.assertEqual(await flow.async_step_pairing_confirm(), ("abort", "pairing_expired"))
        self.assertEqual(await flow.async_step_pairing_accept(), ("abort", "pairing_expired"))

    async def test_ignoring_the_card_rejects_the_pairing(self):
        flow = self.Flow(self.hass)
        self.assertEqual(await flow.async_step_ignore({"unique_id": "pairing_entry1", "title": "x"}),
                         ("abort", "pairing_rejected"))
        self.assertEqual(self.bridge.answers, [(None, False, None)])
        # Cards of new panels are ignored as usual.
        self.assertEqual(await flow.async_step_ignore({"unique_id": "A1B2C3D4E5F6", "title": "x"}),
                         ("ignored", "A1B2C3D4E5F6"))

    async def test_panel_cards_take_the_usual_way(self):
        flow = self.Flow(self.hass)
        self.assertEqual(await flow.async_step_integration_discovery({"device_id": "A1B2C3D4E5F6"}), "panel card")

    def test_results_cover_every_answer(self):
        scope = {}
        exec(compile(ast.Module(body=[next(
            node for node in ast.parse((ROOT / "config_flow.py").read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_PAIRING_RESULTS")],
            type_ignores=[]), "config_flow.py", "exec"), scope)
        self.assertEqual(set(scope["_PAIRING_RESULTS"]),
                         {PAIRING.ANSWER_PAIRED, PAIRING.ANSWER_WAITING, PAIRING.ANSWER_REJECTED})


if __name__ == "__main__":
    unittest.main()
