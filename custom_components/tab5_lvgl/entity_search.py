"""Entity search for the panel's Web Admin picker and the tiles' own entities.

A paired panel asks over the sealed command channel ("entities") for the
entities of one tile field matching a search, and reports the entities its
tiles use beyond the released lists ("tiles"). With a Web Admin password on
the panel (the claim travels sealed, like Lock and Alarm) the search covers
every Home Assistant entity of the field's domains and the reported entities
are served like released ones; otherwise only the released entities are
found and reports are ignored. Nothing here performs I/O.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .control_helpers import SWITCH_DOMAINS, entity_domain
from .editable_helpers import DATETIME_DOMAINS, NUMBER_DOMAINS, SELECT_DOMAINS

# Picker list (/api/entity_options key on the panel) -> searchable domains.
LIST_DOMAINS: Dict[str, Tuple[str, ...]] = {
  "sensors": ("sensor",),
  "binary_sensors": ("binary_sensor",),
  "numbers": NUMBER_DOMAINS,
  "selects": SELECT_DOMAINS,
  "datetimes": DATETIME_DOMAINS,
  "weathers": ("weather",),
  "switches": ("light",) + SWITCH_DOMAINS,
  "media": ("media_player",),
  "climates": ("climate",),
  "covers": ("cover",),
  "cameras": ("camera",),
  "locks": ("lock",),
  "alarm_panels": ("alarm_control_panel",),
  "fans": ("fan",),
}

MAX_QUERY = 64
MAX_RESULTS = 60
MAX_PART_BYTES = 1900  # below command_channel.MAX_BODY with the envelope fields
MAX_PARTS = 6
MAX_TILE_ENTITIES = 200


def parse_search(body: Any) -> Optional[Dict[str, Any]]:
  """{"id", "q", "list", "web_auth"} or None for an invalid request."""
  try:
    data = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
  except (TypeError, ValueError):
    return None
  if not isinstance(data, dict):
    return None
  request_id = data.get("id")
  if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 <= request_id <= 0xFFFFFFFF:
    return None
  key = data.get("list")
  if key not in LIST_DOMAINS:
    return None
  query = data.get("q", "")
  if not isinstance(query, str):
    return None
  return {"id": request_id, "list": key, "q": query.strip()[:MAX_QUERY],
          "web_auth": data.get("web_auth") is True}


def matches(entry: Mapping[str, Any], words: List[str]) -> bool:
  text = " ".join(str(entry.get(field) or "") for field in ("t", "v", "a", "d")).casefold()
  return all(word in text for word in words)


def search(entries: Iterable[Mapping[str, Any]], query: str, limit: int = MAX_RESULTS) -> Tuple[List[Dict[str, Any]], bool]:
  """Matching entries sorted by name, at most `limit`; True when more matched."""
  words = [word for word in query.casefold().split() if word]
  found = [dict(entry) for entry in entries if matches(entry, words)]
  found.sort(key=lambda entry: (str(entry.get("t") or "").casefold(), str(entry.get("v"))))
  return found[:limit], len(found) > limit


def compact(entry: Mapping[str, Any]) -> Dict[str, str]:
  """Only the fields with a value: v id, t name, i icon, a area, d device."""
  return {field: str(entry[field]) for field in ("v", "t", "i", "a", "d") if entry.get(field)}


def pack_answer(request_id: int, results: List[Mapping[str, Any]], full: bool, more: bool) -> List[str]:
  """The answer in sealed-size parts: {"id","p","n","full","more","r"}."""
  parts: List[List[Dict[str, str]]] = [[]]
  base = len(json.dumps({"id": request_id, "p": MAX_PARTS, "n": MAX_PARTS, "full": full, "more": True, "r": []},
                        separators=(",", ":")))
  size = base
  for entry in results:
    item = compact(entry)
    item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
    if parts[-1] and size + item_size > MAX_PART_BYTES:
      if len(parts) == MAX_PARTS:
        more = True
        break
      parts.append([])
      size = base
    if item_size + base > MAX_PART_BYTES:
      continue  # one absurdly long name never blocks the rest
    parts[-1].append(item)
    size += item_size
  return [json.dumps({"id": request_id, "p": index, "n": len(parts), "full": full, "more": more, "r": items},
                     ensure_ascii=False, separators=(",", ":"))
          for index, items in enumerate(parts)]


def parse_tiles(body: Any) -> Optional[Dict[str, List[str]]]:
  """{"lists": {list: [entity ids]}} with each list's domains; None if invalid."""
  try:
    data = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
  except (TypeError, ValueError):
    return None
  lists = data.get("lists") if isinstance(data, dict) else None
  if not isinstance(lists, dict):
    return None
  result: Dict[str, List[str]] = {}
  total = 0
  for key, items in lists.items():
    if key not in LIST_DOMAINS or not isinstance(items, list):
      return None
    clean: List[str] = []
    for item in items:
      if not isinstance(item, str) or entity_domain(item) not in LIST_DOMAINS[key]:
        return None
      if item not in clean:
        clean.append(item)
    total += len(clean)
    if total > MAX_TILE_ENTITIES:
      return None
    if clean:
      result[key] = clean
  return result
