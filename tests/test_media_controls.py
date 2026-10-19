"""The Media popup's extra state fields and its control commands."""

from __future__ import annotations

import json
import types
import unittest

from test_control_entity_validation import bridge_methods
from test_view_navigation import load_module

CONTROLS = load_module("media_controls")
CONTROL = load_module("control_helpers")
CLIMATE = load_module("climate_helpers")

ALL = (CONTROLS.FEATURE_SHUFFLE_SET | CONTROLS.FEATURE_REPEAT_SET | CONTROLS.FEATURE_SELECT_SOURCE
       | CONTROLS.FEATURE_SELECT_SOUND_MODE | CONTROLS.FEATURE_GROUPING)


class MediaControlFieldsTest(unittest.TestCase):
  def test_fields_follow_the_feature_bits(self):
    attrs = {"supported_features": 0, "shuffle": True, "repeat": "all", "source_list": ["TV"],
             "sound_mode_list": ["Movie"], "group_members": ["media_player.a"]}
    self.assertEqual(CONTROLS.media_control_fields(attrs), {"supported_features": 0})

  def test_every_supported_field_is_sent(self):
    attrs = {"supported_features": ALL, "shuffle": False, "repeat": "ONE", "source_list": ["TV", "Spotify"],
             "sound_mode": "Movie", "sound_mode_list": ["Stereo", "Movie"],
             "group_members": ["media_player.living", "media_player.kitchen"]}
    fields = CONTROLS.media_control_fields(
      attrs, [{"entity_id": "media_player.kitchen", "name": "Kitchen", "state": "playing"}])
    self.assertEqual(fields["shuffle"], False)
    self.assertEqual(fields["repeat"], "one")
    self.assertEqual(fields["source_list"], ["TV", "Spotify"])
    self.assertEqual(fields["sound_mode"], "Movie")
    self.assertEqual(fields["sound_mode_list"], ["Stereo", "Movie"])
    self.assertEqual(fields["group_members"], ["media_player.living", "media_player.kitchen"])
    self.assertEqual(fields["group_candidates"],
                     [{"entity_id": "media_player.kitchen", "name": "Kitchen", "state": "playing"}])

  def test_lists_are_bounded(self):
    attrs = {"supported_features": ALL, "source_list": [f"Favourite {i} " + "x" * 80 for i in range(60)],
             "sound_mode_list": [str(i) for i in range(40)]}
    fields = CONTROLS.media_control_fields(attrs)
    self.assertEqual(len(fields["source_list"]), CONTROLS.MAX_SOURCES)
    self.assertTrue(all(len(name) <= CONTROLS.MAX_NAME_CHARS for name in fields["source_list"]))
    self.assertEqual(len(fields["sound_mode_list"]), CONTROLS.MAX_SOUND_MODES)

  def test_unknown_values_stay_absent(self):
    fields = CONTROLS.media_control_fields({"supported_features": ALL, "shuffle": None, "repeat": "sometimes"})
    self.assertNotIn("shuffle", fields)
    self.assertNotIn("repeat", fields)


class MediaControlCallTest(unittest.TestCase):
  attrs = {"supported_features": ALL, "source_list": ["TV", "Spotify"], "sound_mode_list": ["Stereo", "Movie"]}

  def call(self, command, payload, attrs=None, candidates=("media_player.kitchen",)):
    return CONTROLS.build_media_control_call(command, payload, attrs or self.attrs, list(candidates))

  def test_shuffle_and_repeat(self):
    self.assertEqual(self.call("shuffle_set", {"shuffle": True}), ("shuffle_set", {"shuffle": True}, ""))
    self.assertEqual(self.call("repeat_set", {"repeat": "one"}), ("repeat_set", {"repeat": "one"}, ""))
    self.assertEqual(self.call("shuffle_set", {"shuffle": "yes"})[2], "invalid_value")
    self.assertEqual(self.call("repeat_set", {"repeat": "twice"})[2], "invalid_value")

  def test_a_missing_feature_refuses(self):
    attrs = {"supported_features": CONTROLS.FEATURE_SELECT_SOURCE}
    self.assertEqual(self.call("shuffle_set", {"shuffle": True}, attrs)[2], "unsupported")
    self.assertEqual(self.call("join", {"group_members": ["media_player.kitchen"]}, attrs)[2], "unsupported")

  def test_source_and_sound_mode_must_be_offered(self):
    self.assertEqual(self.call("select_source", {"source": "TV"}), ("select_source", {"source": "TV"}, ""))
    self.assertEqual(self.call("select_source", {"source": "Radio"})[2], "invalid_value")
    self.assertEqual(self.call("select_sound_mode", {"sound_mode": "Movie"}),
                     ("select_sound_mode", {"sound_mode": "Movie"}, ""))
    self.assertEqual(self.call("select_sound_mode", {"sound_mode": "Night"})[2], "invalid_value")

  def test_join_and_unjoin_only_candidates(self):
    self.assertEqual(self.call("join", {"group_members": ["media_player.kitchen"]}),
                     ("join", {"group_members": ["media_player.kitchen"]}, ""))
    self.assertEqual(self.call("join", {"group_members": ["media_player.neighbour"]})[2], "not_a_candidate")
    self.assertEqual(self.call("unjoin", {"member": "media_player.kitchen"}),
                     ("unjoin", {"entity_id": "media_player.kitchen"}, ""))
    self.assertEqual(self.call("unjoin", {"member": "media_player.neighbour"})[2], "not_a_candidate")


class MediaControlWiringTest(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.calls = []

    async def call(*args, **kwargs):
      self.calls.append(args)

    scope = {
      **vars(CONTROL), **vars(CLIMATE), "json": json,
      "MEDIA_CONTROL_COMMANDS": CONTROLS.CONTROL_COMMANDS,
      "build_media_control_call": CONTROLS.build_media_control_call,
      "_LOGGER": types.SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
      "mqtt": types.SimpleNamespace(async_publish=None), "DOMAIN": "tab5_lvgl",
      "CAMERA_BRIDGE_PROTOCOL_VERSION": 1, "CAMERA_STREAM_TRANSPORT": "", "CAMERA_STREAM_WIDTH": 0,
      "CAMERA_STREAM_HEIGHT": 0, "CAMERA_STREAM_FPS": 0,
    }
    self.methods = bridge_methods(scope)
    player = types.SimpleNamespace(state="playing", attributes={"supported_features": ALL})
    self.bridge = types.SimpleNamespace(
      hass=types.SimpleNamespace(states=types.SimpleNamespace(get=lambda entity_id: player),
                                 services=types.SimpleNamespace(async_call=call)),
      media_players=["media_player.living", "media_player.kitchen"],
    )
    self.bridge._resolve_target_entity = (
      lambda entity_id, candidates: self.methods["_resolve_target_entity"](self.bridge, entity_id, candidates))
    self.bridge._media_group_candidates = lambda entity_id, state: [{"entity_id": "media_player.kitchen"}]

  async def command(self, payload):
    await self.methods["_async_handle_media_command"](
      self.bridge, types.SimpleNamespace(payload=json.dumps(payload), retain=False))

  async def test_join_reaches_home_assistant(self):
    await self.command({"entity_id": "media_player.living", "command": "join",
                        "group_members": ["media_player.kitchen"]})
    self.assertEqual(self.calls, [("media_player", "join",
                                   {"entity_id": "media_player.living", "group_members": ["media_player.kitchen"]})])

  async def test_unjoin_targets_the_member(self):
    await self.command({"entity_id": "media_player.living", "command": "unjoin", "member": "media_player.kitchen"})
    self.assertEqual(self.calls, [("media_player", "unjoin", {"entity_id": "media_player.kitchen"})])

  async def test_a_refused_command_never_reaches_home_assistant(self):
    await self.command({"entity_id": "media_player.living", "command": "join",
                        "group_members": ["media_player.neighbour"]})
    await self.command({"entity_id": "media_player.living", "command": "repeat", "repeat": "twice"})
    self.assertEqual(self.calls, [])

  async def test_shuffle_alias(self):
    await self.command({"entity_id": "media_player.living", "command": "shuffle", "shuffle": True})
    self.assertEqual(self.calls, [("media_player", "shuffle_set", {"entity_id": "media_player.living", "shuffle": True})])


if __name__ == "__main__":
  unittest.main()
