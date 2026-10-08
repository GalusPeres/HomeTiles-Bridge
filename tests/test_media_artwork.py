"""Media tiles drop a cover only after the player stayed without artwork."""

from __future__ import annotations

import asyncio
import json
import types
import unittest

from test_announcement_guard import extract
from test_view_navigation import load_module

ARTWORK = load_module("media_artwork")
PLAYER = "media_player.sonos"
COVER = "https://cdn.test/song.jpg"
METHODS = {
  "_async_build_state_payload", "_gate_media_artwork", "_async_publish_entity_state",
  "_owns_state_publish", "_ha_topic_for_entity",
}


def _state(value, **attributes):
  return types.SimpleNamespace(state=value, attributes=attributes, name="Sonos")


class ArtworkClearGateTest(unittest.TestCase):
  def test_artwork_keeps_payload_unchanged(self) -> None:
    gate = ARTWORK.ArtworkClearGate()
    for key in ARTWORK.ARTWORK_URL_KEYS:
      payload = {"state": "playing", key: COVER}
      self.assertIsNone(gate.apply(PLAYER, payload, 0.0))
      self.assertEqual(payload, {"state": "playing", key: COVER})

  def test_missing_artwork_clears_only_after_the_delay(self) -> None:
    gate = ARTWORK.ArtworkClearGate()
    payload = {"state": "playing", "source": "TV"}
    self.assertEqual(gate.apply(PLAYER, payload, 10.0), 3.0)
    self.assertNotIn("entity_picture", payload)
    payload = {"state": "playing", "source": "TV"}
    self.assertAlmostEqual(gate.apply(PLAYER, payload, 12.5), 0.5)
    self.assertNotIn("entity_picture", payload)
    payload = {"state": "playing", "source": "TV"}
    self.assertIsNone(gate.apply(PLAYER, payload, 13.0))
    self.assertEqual(payload["entity_picture"], "")

  def test_artwork_between_songs_restarts_the_delay(self) -> None:
    gate = ARTWORK.ArtworkClearGate()
    gate.apply(PLAYER, {"state": "playing"}, 0.0)
    self.assertIsNone(gate.apply(PLAYER, {"state": "playing", "entity_picture": COVER}, 2.0))
    payload = {"state": "playing"}
    self.assertEqual(gate.apply(PLAYER, payload, 4.0), 3.0)
    self.assertNotIn("entity_picture", payload)

  def test_blank_urls_are_no_artwork(self) -> None:
    payload = {"state": "playing", "entity_picture": " ", "media_image_url": ""}
    self.assertFalse(ARTWORK.has_artwork(payload))

  def test_buffering_unavailable_and_unknown_keep_the_cover(self) -> None:
    gate = ARTWORK.ArtworkClearGate()
    gate.apply(PLAYER, {"state": "playing"}, 0.0)
    for index, state in enumerate(("buffering", "unavailable", "unknown")):
      payload = {"state": state}
      self.assertIsNone(gate.apply(PLAYER, payload, 10.0 + index))
      self.assertNotIn("entity_picture", payload)
    # The delay starts again once the player reports a real state.
    self.assertEqual(gate.apply(PLAYER, {"state": "playing"}, 20.0), 3.0)

  def test_players_are_independent(self) -> None:
    gate = ARTWORK.ArtworkClearGate()
    gate.apply(PLAYER, {"state": "playing"}, 0.0)
    self.assertEqual(gate.apply("media_player.kitchen", {"state": "idle"}, 2.0), 3.0)
    payload = {"state": "playing"}
    self.assertIsNone(gate.apply(PLAYER, payload, 3.0))
    self.assertEqual(payload["entity_picture"], "")


class MediaArtworkWiringTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.clock = 100.0
    self.publishes: list[tuple[str, dict, bool]] = []
    self.timers: list[list] = []
    self.states = {PLAYER: _state("playing", entity_picture=COVER, media_title="Song")}

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.publishes.append((topic, json.loads(payload), retain))

    def call_later(hass, delay, action):
      timer = [delay, action, True]
      self.timers.append(timer)

      def cancel() -> None:
        timer[2] = False
      return cancel

    def extract_payload(state, hass=None):
      payload = {"state": state.state}
      payload.update({key: value for key, value in state.attributes.items() if value is not None})
      return payload

    scope = {
      "ArtworkClearGate": ARTWORK.ArtworkClearGate,
      "image_key_field": ARTWORK.image_key_field,
      "DATA_IMAGES": "images",
      "async_call_later": call_later,
      "callback": lambda function: function,
      "monotonic": lambda: self.clock,
      "json": json,
      "mqtt": types.SimpleNamespace(async_publish=publish),
      "_extract_media_player_payload": extract_payload,
      "_is_weather_entity": lambda entity_id: False,
      "DOMAIN": "tab5_lvgl",
      "_LOGGER": types.SimpleNamespace(debug=lambda *args: None),
    }
    extract(METHODS, scope)
    bridge_class = type("Bridge", (), {name: scope[name] for name in METHODS})
    self.hass = types.SimpleNamespace(
      data={"tab5_lvgl": {}},
      states=types.SimpleNamespace(get=lambda entity_id: self.states.get(entity_id)),
      async_create_task=lambda coro: asyncio.ensure_future(coro),
    )
    bridge = bridge_class()
    bridge.hass = self.hass
    bridge.entry = types.SimpleNamespace(entry_id="entry1")
    bridge.ha_prefix = "ha"
    bridge.numbers = bridge.selects = bridge.datetimes = []
    bridge._media_publish_generation = {}
    bridge._media_artwork_gate = ARTWORK.ArtworkClearGate()
    bridge._media_artwork_timers = {}

    async def attach_cover(entity_id, payload):
      if payload.get("entity_picture"):
        payload["entity_picture_data"] = "art"
    bridge._async_attach_media_cover_data = attach_cover
    self.bridge = bridge

  async def change(self, state) -> None:
    self.states[PLAYER] = state
    self.publishes.clear()
    await self.bridge._async_publish_entity_state(PLAYER, state)

  def payloads(self) -> dict[str, dict]:
    return {topic.rsplit("/", 1)[1]: payload for topic, payload, _ in self.publishes}

  def pending(self) -> list[list]:
    return [timer for timer in self.timers if timer[2]]

  async def fire(self, timer) -> None:
    self.publishes.clear()
    timer[2] = False
    timer[1](None)
    await asyncio.sleep(0)

  async def test_song_change_without_artwork_in_between_keeps_the_cover(self) -> None:
    await self.change(_state("playing", entity_picture=COVER, media_title="Song"))
    self.assertEqual(self.payloads()["state"]["entity_picture_data"], "art")
    await self.change(_state("playing", media_title="Next song"))
    self.assertNotIn("entity_picture", self.payloads()["state_fast"])
    self.assertNotIn("entity_picture", self.payloads()["state"])
    self.assertEqual(len(self.pending()), 1)
    self.clock += 1.5
    await self.change(_state("playing", entity_picture="https://cdn.test/next.jpg", media_title="Next song"))
    self.assertEqual(self.payloads()["state"]["entity_picture"], "https://cdn.test/next.jpg")
    self.assertEqual(self.pending(), [], "New artwork must cancel the pending clear")

  async def test_source_without_artwork_clears_the_cover_once(self) -> None:
    await self.change(_state("playing", source="TV"))
    self.assertNotIn("entity_picture", self.payloads()["state"])
    # A further update without artwork must not start a second timer.
    self.clock += 1.0
    await self.change(_state("playing", source="TV", volume_level=0.4))
    timers = self.pending()
    self.assertEqual(len(timers), 1)
    self.assertAlmostEqual(timers[0][0], 3.05)
    self.clock += 2.1
    await self.fire(timers[0])
    payloads = self.payloads()
    self.assertEqual(payloads["state_fast"]["entity_picture"], "")
    self.assertEqual(payloads["state"]["entity_picture"], "")
    self.assertNotIn("entity_picture_data", payloads["state"])
    self.assertEqual(self.pending(), [])
    self.assertEqual(self.bridge._media_artwork_timers, {})

  async def test_buffering_never_clears_the_cover(self) -> None:
    await self.change(_state("buffering", media_title="Next song"))
    self.clock += 10.0
    await self.change(_state("buffering", media_title="Next song"))
    self.assertNotIn("entity_picture", self.payloads()["state"])
    self.assertEqual(self.pending(), [])

  async def test_timer_rechecks_the_current_state(self) -> None:
    await self.change(_state("playing", source="TV"))
    timer = self.pending()[0]
    # The artwork returned without a state event reaching this entry.
    self.states[PLAYER] = _state("playing", entity_picture=COVER)
    self.clock += 3.1
    await self.fire(timer)
    self.assertEqual(self.payloads()["state"]["entity_picture"], COVER)


if __name__ == "__main__":
  unittest.main()
