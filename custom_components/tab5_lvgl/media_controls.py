"""Media player controls for the panel's Media popup.

The extra state fields the popup needs (feature bits, shuffle, repeat, the
source and sound mode lists, the group) and the checks of its commands. Pure
functions, no Home Assistant imports, so the tests load them directly.

Every command is checked against the player's own feature bits and lists:
shuffle_set needs SHUFFLE_SET, a source must be one of source_list, a sound
mode one of sound_mode_list, and join/unjoin accept only the player's group
candidates (players of the same integration with GROUPING that the Bridge
serves to this panel).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# MediaPlayerEntityFeature (homeassistant.components.media_player.const).
FEATURE_PLAY_MEDIA = 512
FEATURE_SELECT_SOURCE = 2048
FEATURE_SHUFFLE_SET = 32768
FEATURE_SELECT_SOUND_MODE = 65536
FEATURE_BROWSE_MEDIA = 131072
FEATURE_REPEAT_SET = 262144
FEATURE_GROUPING = 524288

# Bounds for the panel: a long favourites list as sources must not blow up
# the state message.
MAX_SOURCES = 24
MAX_SOUND_MODES = 12
MAX_GROUP = 8
MAX_NAME_CHARS = 48

REPEAT_MODES = ("off", "all", "one")

# The panel's command names (after MEDIA_COMMAND_ALIASES) handled here.
CONTROL_COMMANDS = ("shuffle_set", "repeat_set", "select_source", "select_sound_mode", "join", "unjoin")


def _features(attrs: Dict[str, Any]) -> int:
  try:
    return int(attrs.get("supported_features") or 0)
  except (TypeError, ValueError):
    return 0


def _short(value: Any) -> str:
  text = str(value).strip()
  return text[:MAX_NAME_CHARS]


def _names(values: Any, limit: int) -> List[str]:
  if not isinstance(values, (list, tuple)):
    return []
  out: List[str] = []
  for value in values:
    if value is None:
      continue
    text = _short(value)
    if text and text not in out:
      out.append(text)
    if len(out) >= limit:
      break
  return out


def media_control_fields(attrs: Dict[str, Any], candidates: Iterable[Dict[str, str]] = ()) -> Dict[str, Any]:
  """The popup's extra fields of one player's state; absent stays absent."""
  features = _features(attrs)
  fields: Dict[str, Any] = {"supported_features": features}
  if features & FEATURE_SHUFFLE_SET and isinstance(attrs.get("shuffle"), bool):
    fields["shuffle"] = attrs["shuffle"]
  if features & FEATURE_REPEAT_SET:
    repeat = str(attrs.get("repeat") or "").lower()
    if repeat in REPEAT_MODES:
      fields["repeat"] = repeat
  if features & FEATURE_SELECT_SOURCE:
    sources = _names(attrs.get("source_list"), MAX_SOURCES)
    if sources:
      fields["source_list"] = sources
  if features & FEATURE_SELECT_SOUND_MODE:
    modes = _names(attrs.get("sound_mode_list"), MAX_SOUND_MODES)
    if modes:
      fields["sound_mode_list"] = modes
    if attrs.get("sound_mode") is not None:
      fields["sound_mode"] = _short(attrs["sound_mode"])
  if features & FEATURE_GROUPING:
    members = _names(attrs.get("group_members"), MAX_GROUP + 1)
    if members:
      fields["group_members"] = members
    group = [
      {"entity_id": str(c["entity_id"]), "name": _short(c.get("name") or c["entity_id"]), "state": str(c.get("state") or "")}
      for c in list(candidates)[:MAX_GROUP]
      if isinstance(c, dict) and c.get("entity_id")
    ]
    if group:
      fields["group_candidates"] = group
  return fields


def build_media_control_call(
  command: str,
  payload: Dict[str, Any],
  attrs: Dict[str, Any],
  candidates: Iterable[str],
) -> Tuple[Optional[str], Dict[str, Any], str]:
  """(service, data without entity_id, error) of one control command.

  The service is None with an error code when the player cannot do it or the
  value is not one it offers. data["entity_id"] is set only for unjoin (the
  member that leaves).
  """
  features = _features(attrs)
  if command == "shuffle_set":
    if not features & FEATURE_SHUFFLE_SET:
      return None, {}, "unsupported"
    value = payload.get("shuffle")
    if not isinstance(value, bool):
      return None, {}, "invalid_value"
    return "shuffle_set", {"shuffle": value}, ""
  if command == "repeat_set":
    if not features & FEATURE_REPEAT_SET:
      return None, {}, "unsupported"
    value = str(payload.get("repeat") or "").lower()
    if value not in REPEAT_MODES:
      return None, {}, "invalid_value"
    return "repeat_set", {"repeat": value}, ""
  if command == "select_source":
    value = str(payload.get("source") or "").strip()
    if not value:
      return None, {}, "invalid_value"
    offered = attrs.get("source_list")
    # A player that lists its sources only takes one of them.
    if isinstance(offered, (list, tuple)) and value not in [str(item) for item in offered]:
      return None, {}, "invalid_value"
    return "select_source", {"source": value}, ""
  if command == "select_sound_mode":
    if not features & FEATURE_SELECT_SOUND_MODE:
      return None, {}, "unsupported"
    value = str(payload.get("sound_mode") or "").strip()
    offered = attrs.get("sound_mode_list")
    if not value or not isinstance(offered, (list, tuple)) or value not in [str(item) for item in offered]:
      return None, {}, "invalid_value"
    return "select_sound_mode", {"sound_mode": value}, ""
  if command in ("join", "unjoin"):
    if not features & FEATURE_GROUPING:
      return None, {}, "unsupported"
    allowed = set(candidates)
    if command == "join":
      members = payload.get("group_members")
      if not isinstance(members, list) or not members:
        return None, {}, "invalid_value"
      members = [str(member).strip() for member in members]
      if any(member not in allowed for member in members):
        return None, {}, "not_a_candidate"
      return "join", {"group_members": members}, ""
    member = str(payload.get("member") or "").strip()
    if member not in allowed:
      return None, {}, "not_a_candidate"
    return "unjoin", {"entity_id": member}, ""
  return None, {}, "unsupported"
