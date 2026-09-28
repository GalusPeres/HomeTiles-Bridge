"""Pairing by number (pairing.py): the shared vectors and the Bridge's rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
except BaseException as error:  # A broken system build can panic in its Rust bindings.
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise
    X25519PrivateKey = None

from test_view_navigation import load_module

PAIRING = load_module("pairing")
CC = load_module("command_channel")

# Shared with the firmware (docs-dev/command-encryption.md, pairing v2): both
# implementations must compute exactly these values.
BASE = "hometiles/test"
SK_P = bytes([0xA1]) * 32
SK_B = bytes([0xB2]) * 32
N_P = bytes([0xC3]) * 16
N_B = bytes([0xD4]) * 16
PK_P = "c306fb0ef2bf8b7f93bad98155fa37daec74db0c4cbeda6c6f1dba9d36558252"
PK_B = "db48257e1237976a74ad8cfedca00213408fe89ac6251f1b930245f242b5c31a"
COMMIT = "3262f80a0dcdf8b757e44596ed47c05afdc9a16405a915c710ab33443f8af112"
SHARED = "9502af7a4b678841b839429623a09a23f6cc551836e48a52c0e4faf4b9d3b06e"
TRANSCRIPT = "2d4cd2f1d56b383880cc9e27ec65419c1ee1bf1df99bbe5dd115e65d3c613e9f"
NUMBER = "061 806"
KEY = "b925def556256ead767b0f1d14879e50d6bddbd0bb44dc0d2435e4af011b3e19"
M_PANEL = "a2bbe3db083e9884b39df9d41eac55ed94b652e364c636157423f773bc35516b"
M_BRIDGE = "ce6193e03597bf02204ee8dd3a1f05d38675088c497a6f5ae35718acf78ef456"
PANEL_TO_BRIDGE = "3ce896914535a84f25b6bcbb18bae3e2e0bbdefa5b03712b2fbaf16e51d084de"
BRIDGE_TO_PANEL = "5631cdca5a6fbae0a0fdfc738926f75254f40065c9e64322430aba78be18278d"
ANNOUNCE = "318f1b1153aed588afc39a727b1f7a56659c9104b8f4d2eac4b8ee08eb71563e"
KEY_ID = "20a8108ed11215c5"
# Chain check: the panel's first hello with the derived key.
HELLO_PLAINTEXT = b"hello - 0 00112233445566778899aabbccddeeff\n"
HELLO_NONCE = bytes.fromhex("000102030405060708090a0b")
HELLO_DATA = ("0cadb7a053444eb47a4fe832c2666d6aabeb102ffe25b8aa6dfbeb29b0206815420dad833e0db0c4023b193d8e4881"
              "cacf43b23f7beced790a8ef3")
ATTEMPT = "0011223344556677"

NEEDS_CRYPTOGRAPHY = unittest.skipIf(
    X25519PrivateKey is None, "cryptography (bundled with Home Assistant) is required")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Random:
    """The Bridge's key and nonce from the vectors, then fresh ones."""

    def __init__(self) -> None:
        self.values = [SK_B, N_B]
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        if self.values:
            value = self.values.pop(0)
            assert len(value) == size
            return value
        self.counter += 1
        return bytes([0x40 + self.counter]) * size


class Panel:
    """The panel's side, written from the contract (independent of pairing.py)."""

    def __init__(self, secret: bytes = SK_P, nonce: bytes = N_P, attempt: str = ATTEMPT) -> None:
        self.private = X25519PrivateKey.from_private_bytes(secret)
        self.pk = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.nonce = nonce
        self.id = attempt

    def message(self, kind: str, **fields) -> str:
        return json.dumps({"v": 2, "t": kind, "id": self.id, **fields})

    def start(self) -> str:
        return self.message("start", pk=self.pk.hex())

    def send_nonce(self) -> str:
        return self.message("nonce", n=self.nonce.hex())

    def finish(self, commit: dict, nonce: dict) -> dict:
        """What the panel checks and derives once it has the Bridge's nonce."""
        pk_b, n_b = bytes.fromhex(commit["pk"]), bytes.fromhex(nonce["n"])
        expected = hashlib.sha256(b"HomeTiles pairing commit v2" + pk_b + self.pk + n_b).hexdigest()
        assert commit["c"] == expected, "commitment does not open"
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(pk_b))
        base = BASE.encode()
        transcript = hashlib.sha256(b"HomeTiles pairing v2" + len(base).to_bytes(2, "big") + base
                                    + self.pk + pk_b + self.nonce + n_b).digest()
        value = int.from_bytes(hashlib.sha256(b"HomeTiles pairing number v2" + transcript).digest()[:4], "big")
        prk = hmac.new(transcript, shared, hashlib.sha256).digest()
        key = hmac.new(prk, b"pairing key\x01", hashlib.sha256).digest()
        return {
            "number": f"{value % 1000000:06d}",
            "key": key,
            "m_panel": hmac.new(key, b"confirm panel" + transcript, hashlib.sha256).hexdigest(),
            "m_bridge": hmac.new(key, b"confirm bridge" + transcript, hashlib.sha256).hexdigest(),
        }

    def confirm(self, mac: str) -> str:
        return self.message("confirm", m=mac)


def sent(events):
    return [json.loads(value) for event, value in events if event == "send"]


def kinds(events):
    return [event for event, _value in events]


@NEEDS_CRYPTOGRAPHY
class VectorTest(unittest.TestCase):
    def test_contract_values(self):
        _private, pk_p = PAIRING.x25519_keypair(SK_P)
        private_b, pk_b = PAIRING.x25519_keypair(SK_B)
        self.assertEqual((pk_p.hex(), pk_b.hex()), (PK_P, PK_B))
        self.assertEqual(PAIRING.commitment(pk_b, pk_p, N_B).hex(), COMMIT)
        shared = PAIRING.x25519_shared(private_b, pk_p)
        self.assertEqual(shared.hex(), SHARED)
        transcript = PAIRING.transcript(BASE, pk_p, pk_b, N_P, N_B)
        self.assertEqual(transcript.hex(), TRANSCRIPT)
        self.assertEqual(PAIRING.format_number(PAIRING.pairing_number(transcript)), NUMBER)
        key = PAIRING.pairing_key(shared, transcript)
        self.assertEqual(key.hex(), KEY)
        self.assertEqual(PAIRING.confirm_mac(key, transcript, "panel").hex(), M_PANEL)
        self.assertEqual(PAIRING.confirm_mac(key, transcript, "bridge").hex(), M_BRIDGE)
        keys = CC.Keys(key)
        self.assertEqual((keys.panel_to_bridge.hex(), keys.bridge_to_panel.hex(), keys.announce.hex(), keys.key_id),
                         (PANEL_TO_BRIDGE, BRIDGE_TO_PANEL, ANNOUNCE, KEY_ID))
        hello = CC.seal(keys.panel_to_bridge, KEY_ID, f"{BASE}/secure/panel", HELLO_PLAINTEXT, HELLO_NONCE)
        self.assertEqual(json.loads(hello)["d"], HELLO_DATA)

    def test_rfc7748_vector(self):
        # Section 6.1, the firmware's self-test before each attempt.
        alice, _ = PAIRING.x25519_keypair(bytes.fromhex(
            "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"))
        _, bob = PAIRING.x25519_keypair(bytes.fromhex(
            "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb"))
        self.assertEqual(PAIRING.x25519_shared(alice, bob).hex(),
                         "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")

    def test_number_keeps_six_digits(self):
        self.assertEqual(PAIRING.format_number(61806), "061 806")
        self.assertEqual(PAIRING.format_number(7), "000 007")
        self.assertEqual(PAIRING.format_number(999999), "999 999")


@NEEDS_CRYPTOGRAPHY
class PairingTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pairing = PAIRING.PanelPairing(BASE, clock=self.clock, random=Random())
        self.panel = Panel()

    def handle(self, payload, paired=False):
        return self.pairing.handle(payload, paired=paired)

    def exchange(self):
        """start, commit, nonce, nonce: both sides now show the number."""
        [commit] = sent(self.handle(self.panel.start()))
        events = self.handle(self.panel.send_nonce())
        [nonce] = sent(events)
        self.assertIn(("prompt", (ATTEMPT, NUMBER)), events)
        return self.panel.finish(commit, nonce)

    def test_the_vectors_on_the_wire(self):
        self.assertEqual(self.pairing.panel_topic, "hometiles/test/pair/panel")
        self.assertEqual(self.pairing.bridge_topic, "hometiles/test/pair/bridge")
        [commit] = sent(self.handle(self.panel.start()))
        self.assertEqual(commit, {"v": 2, "t": "commit", "id": ATTEMPT, "pk": PK_B, "c": COMMIT})
        events = self.handle(self.panel.send_nonce())
        self.assertEqual(events, [("send", '{"v":2,"t":"nonce","id":"0011223344556677","n":"' + N_B.hex() + '"}'),
                                  ("prompt", (ATTEMPT, NUMBER))])
        self.assertEqual(self.pairing.number(ATTEMPT), NUMBER)
        self.assertIsNone(self.pairing.number("ffffffffffffffff"))

    def test_user_first_then_panel(self):
        panel = self.exchange()
        self.assertEqual(panel["number"], "061806")
        outcome, events = self.pairing.answer(ATTEMPT, True)
        self.assertEqual(outcome, PAIRING.ANSWER_WAITING)
        self.assertEqual(sent(events), [{"v": 2, "t": "confirm", "id": ATTEMPT, "m": M_BRIDGE}])
        # A wrong confirm changes nothing; the panel's own finishes the pairing.
        self.assertEqual(self.handle(self.panel.confirm("00" * 32)), [])
        self.assertEqual(self.handle(self.panel.confirm(panel["m_panel"])), [("paired", bytes.fromhex(KEY))])
        self.assertIsNone(self.pairing.number(ATTEMPT))

    def test_panel_first_then_user(self):
        panel = self.exchange()
        self.assertEqual(self.handle(self.panel.confirm(panel["m_panel"])), [])
        outcome, events = self.pairing.answer(ATTEMPT, True)
        self.assertEqual(outcome, PAIRING.ANSWER_PAIRED)
        self.assertEqual(kinds(events), ["send", "paired"])
        self.assertEqual(events[-1][1], panel["key"])
        # A second click on the card only reports the result.
        self.assertEqual(self.pairing.answer(ATTEMPT, True), (PAIRING.ANSWER_PAIRED, []))

    def test_lost_confirms_are_repeated(self):
        panel = self.exchange()
        self.pairing.answer(ATTEMPT, True)
        # The Bridge repeats its confirm every two seconds until the panel's arrives.
        self.clock.now += 1
        self.assertEqual(self.pairing.tick(), [])
        self.clock.now += 1
        self.assertEqual(sent(self.pairing.tick()), [{"v": 2, "t": "confirm", "id": ATTEMPT, "m": M_BRIDGE}])
        self.assertEqual(kinds(self.handle(self.panel.confirm(panel["m_panel"]))), ["paired"])
        self.clock.now += 10
        self.assertEqual(self.pairing.tick(), [])
        # The panel missed ours and repeats its own: it gets ours again,
        # until 120 s after the start.
        self.assertEqual(sent(self.handle(self.panel.confirm(panel["m_panel"]))),
                         [{"v": 2, "t": "confirm", "id": ATTEMPT, "m": M_BRIDGE}])
        self.assertEqual(self.handle(self.panel.confirm("11" * 32)), [])
        self.assertTrue(self.pairing.active)
        self.clock.now = 1000.0 + 120
        self.assertEqual(self.handle(self.panel.confirm(panel["m_panel"])), [])
        self.assertFalse(self.pairing.active)

    def test_only_the_first_nonce_counts(self):
        panel = self.exchange()
        other = Panel(nonce=bytes(16))
        self.assertEqual(self.handle(other.send_nonce()), [])
        self.assertEqual(self.pairing.number(ATTEMPT), NUMBER)
        self.assertEqual(self.pairing.answer(ATTEMPT, True)[0], PAIRING.ANSWER_WAITING)
        self.assertEqual(kinds(self.handle(self.panel.confirm(panel["m_panel"]))), ["paired"])

    def test_messages_of_other_attempts_are_ignored(self):
        self.handle(self.panel.start())
        stranger = Panel(attempt="8899aabbccddeeff")
        self.assertEqual(self.handle(stranger.send_nonce()), [])
        self.assertEqual(self.handle(stranger.message("abort")), [])
        self.assertEqual(self.handle(stranger.confirm("00" * 32)), [])
        self.assertEqual(kinds(self.handle(self.panel.send_nonce())), ["send", "prompt"])
        # A confirm before the nonces, or the user's answer then, does nothing.
        fresh = PAIRING.PanelPairing(BASE, clock=self.clock, random=Random())
        fresh.handle(self.panel.start(), paired=False)
        self.assertEqual(fresh.answer(ATTEMPT, True), (PAIRING.ANSWER_EXPIRED, []))
        self.assertEqual(fresh.handle(self.panel.confirm(M_PANEL), paired=False), [])

    def test_repeated_start_is_answered_once(self):
        self.handle(self.panel.start())
        self.assertEqual(self.handle(self.panel.start()), [])

    def test_a_paired_panel_is_not_paired_again(self):
        events = self.handle(self.panel.start(), paired=True)
        self.assertEqual(events, [("send", '{"v":2,"t":"abort","id":"0011223344556677","r":"paired"}'),
                                  ("refused", "paired")])
        self.assertFalse(self.pairing.active)

    def test_one_attempt_at_a_time(self):
        self.handle(self.panel.start())
        other = Panel(attempt="8899aabbccddeeff")
        self.assertEqual(sent(self.handle(other.start())),
                         [{"v": 2, "t": "abort", "id": "8899aabbccddeeff", "r": "busy"}])
        # Refusals go out at most once a second.
        self.assertEqual(self.handle(Panel(attempt="8899aabbccddeef0").start()), [])

    def test_starts_are_limited(self):
        # At most one every 10 s and three in 10 minutes: the number cannot
        # be ground while the user looks at it.
        for index in range(3):
            panel = Panel(attempt=f"{index:016x}")
            self.assertEqual(sent(self.handle(panel.start()))[0]["t"], "commit")
            self.assertEqual(self.handle(panel.message("abort")), [("closed", "cancel")])
            self.clock.now += 5
            if index < 2:
                self.assertEqual(sent(self.handle(Panel(attempt="00000000000000ff").start()))[0]["r"], "rate")
            self.clock.now += 5
        self.assertEqual(sent(self.handle(Panel(attempt="00000000000000fe").start()))[0]["r"], "rate")
        self.clock.now = 1000.0 + 600
        self.assertEqual(sent(self.handle(Panel(attempt="00000000000000fd").start()))[0]["t"], "commit")

    def test_rejection_and_timeout_send_an_abort(self):
        self.exchange()
        outcome, events = self.pairing.answer(ATTEMPT, False)
        self.assertEqual(outcome, PAIRING.ANSWER_REJECTED)
        self.assertEqual(events, [("send", '{"v":2,"t":"abort","id":"0011223344556677","r":"rejected"}'),
                                  ("closed", "rejected")])
        self.assertEqual(self.pairing.answer(ATTEMPT, True), (PAIRING.ANSWER_EXPIRED, []))

        self.clock.now += 10
        panel = Panel(attempt="8899aabbccddeeff")
        self.handle(panel.start())
        self.clock.now += 119
        self.assertEqual(self.pairing.tick(), [])
        self.clock.now += 1
        self.assertEqual(self.pairing.tick(),
                         [("send", '{"v":2,"t":"abort","id":"8899aabbccddeeff","r":"timeout"}'),
                          ("closed", "timeout")])
        self.assertFalse(self.pairing.active)

    def test_ignoring_the_card_rejects_the_current_attempt(self):
        self.exchange()
        outcome, events = self.pairing.answer(None, False)
        self.assertEqual((outcome, kinds(events)), (PAIRING.ANSWER_REJECTED, ["send", "closed"]))

    def test_abort_after_pairing_rolls_nothing_back(self):
        panel = self.exchange()
        self.pairing.answer(ATTEMPT, True)
        self.handle(self.panel.confirm(panel["m_panel"]))
        self.assertEqual(self.handle(self.panel.message("abort")), [])
        self.assertEqual(self.pairing.answer(ATTEMPT, True)[0], PAIRING.ANSWER_PAIRED)
        # Until the entry reloaded with the key, the panel counts as paired.
        self.clock.now += 10
        self.assertEqual(sent(self.handle(Panel(attempt="8899aabbccddeeff").start())),
                         [{"v": 2, "t": "abort", "id": "8899aabbccddeeff", "r": "paired"}])

    def test_low_order_key_fails(self):
        start = self.panel.message("start", pk="00" * 32)
        self.assertEqual(sent(self.handle(start))[0]["t"], "commit")
        events = self.handle(self.panel.send_nonce())
        self.assertEqual(events, [("send", '{"v":2,"t":"abort","id":"0011223344556677"}'), ("closed", "failed")])

    def test_malformed_messages_are_ignored(self):
        pk = PK_P
        for payload in (
            None, "", "not json", "[]", "x" * 600, b"\xff",
            '{"v":1,"t":"start","id":"0011223344556677","pk":"%s"}' % pk,
            '{"v":2.0,"t":"start","id":"0011223344556677","pk":"%s"}' % pk,
            '{"v":true,"t":"start","id":"0011223344556677","pk":"%s"}' % pk,
            '{"v":2,"t":"commit","id":"0011223344556677","pk":"%s"}' % pk,
            '{"v":2,"t":"start","id":"00112233445566","pk":"%s"}' % pk,
            '{"v":2,"t":"start","id":"0011223344556677","pk":"%s"}' % pk.upper(),
            '{"v":2,"t":"start","id":"0011223344556677","pk":"%s"}' % pk[:-2],
            '{"v":2,"t":"start","id":"0011223344556677"}',
        ):
            self.assertEqual(self.handle(payload), [], payload)
        # Unknown fields are ignored; bytes payloads are fine.
        extra = json.dumps({"v": 2, "t": "start", "id": ATTEMPT, "pk": pk, "x": 1}).encode()
        self.assertEqual(sent(self.handle(extra))[0]["t"], "commit")

    def test_repr_hides_the_key(self):
        self.exchange()
        self.assertNotIn(KEY, repr(self.pairing._attempt))


if __name__ == "__main__":
    unittest.main()
