"""Entity search for the panel's Web Admin picker and the panel's entity
declaration.

A paired panel asks over the sealed command channel ("entities") for the
entities of one tile field matching a search. It also declares every entity
its tiles use ("tiles"), like an ESPHome device names the Home Assistant
states it needs: on every connection and after every tile change, always the
complete list, built from its tiles alone. A declaration therefore never
depends on what the Bridge serves, and a new one replaces the previous one.
With a Web Admin password on the panel (the claim travels sealed, like Lock
and Alarm) the search covers every Home Assistant entity of the field's
domains and the declared entities are served like released ones; otherwise
only the released entities are found and declarations serve nothing.
Nothing here performs I/O.
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
MAX_RESULTS = 60  # one page; the picker asks for the next one while scrolling
MAX_OFFSET = 5000
MAX_PART_BYTES = 1900  # below command_channel.MAX_BODY with the envelope fields
MAX_PARTS = 6
MAX_PANEL_ENTITIES = 300
MAX_DECLARATION_PARTS = 8


def parse_search(body: Any) -> Optional[Dict[str, Any]]:
  """{"id", "q", "list", "o", "web_auth"} or None for an invalid request."""
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
  offset = data.get("o", 0)
  if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
    return None
  return {"id": request_id, "list": key, "q": query.strip()[:MAX_QUERY], "o": offset,
          "web_auth": data.get("web_auth") is True}


def matches(entry: Mapping[str, Any], words: List[str]) -> bool:
  text = " ".join(str(entry.get(field) or "") for field in ("t", "v", "a", "d")).casefold()
  return all(word in text for word in words)


def search(entries: Iterable[Mapping[str, Any]], query: str, limit: int = MAX_RESULTS,
           offset: int = 0) -> Tuple[List[Dict[str, Any]], bool]:
  """Matching entries sorted by name, at most `limit` from `offset` on; True
  when more follow."""
  words = [word for word in query.casefold().split() if word]
  found = [dict(entry) for entry in entries if matches(entry, words)]
  found.sort(key=lambda entry: (str(entry.get("t") or "").casefold(), str(entry.get("v"))))
  return found[offset:offset + limit], len(found) > offset + limit


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


def clean_lists(lists: Any) -> Optional[Dict[str, List[str]]]:
  """{list: [entity ids]}, sorted and without duplicates; None unless a dict.
  An unknown list or an entity outside its list's domains is left out, so
  one stray tile value never voids the whole declaration."""
  if not isinstance(lists, dict):
    return None
  result: Dict[str, List[str]] = {}
  for key, items in lists.items():
    if key not in LIST_DOMAINS or not isinstance(items, list):
      continue
    valid = {item for item in items if isinstance(item, str) and entity_domain(item) in LIST_DOMAINS[key]}
    if valid:
      result[key] = sorted(valid)
  return result


def parse_declaration(body: Any) -> Optional[Dict[str, Any]]:
  """One part of a declaration, {"v": version, "p": part, "n": parts,
  "lists": {list: [entity ids]}, "web_auth": bool}; None if invalid."""
  try:
    data = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
  except (TypeError, ValueError):
    return None
  if not isinstance(data, dict):
    return None
  version, part, parts = data.get("v"), data.get("p"), data.get("n")
  for value, low, high in ((version, 0, 0xFFFFFFFF), (part, 0, MAX_DECLARATION_PARTS - 1),
                           (parts, 1, MAX_DECLARATION_PARTS)):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
      return None
  lists = clean_lists(data.get("lists"))
  if part >= parts or lists is None:
    return None
  return {"v": version, "p": part, "n": parts, "lists": lists, "web_auth": data.get("web_auth") is True}


class DeclarationParts:
  """Joins the parts of one declaration version; another version starts over."""

  def __init__(self) -> None:
    self._version: Optional[int] = None
    self._count = 0
    self._parts: Dict[int, Mapping[str, Any]] = {}

  def add(self, part: Mapping[str, Any]) -> Optional[Tuple[Dict[str, List[str]], bool]]:
    """(lists, web_auth) once every part of the version arrived; None while
    parts are missing or for more than MAX_PANEL_ENTITIES entities. web_auth
    holds only when every part claims it."""
    if part["v"] != self._version or part["n"] != self._count:
      self._version, self._count, self._parts = part["v"], part["n"], {}
    self._parts[part["p"]] = part
    if len(self._parts) < self._count:
      return None
    parts = [self._parts[index] for index in range(self._count)]
    self._version, self._count, self._parts = None, 0, {}
    joined: Dict[str, List[str]] = {}
    for item in parts:
      for key, ids in item["lists"].items():
        joined.setdefault(key, []).extend(ids)
    lists = {key: sorted(set(ids)) for key, ids in joined.items()}
    if sum(len(ids) for ids in lists.values()) > MAX_PANEL_ENTITIES:
      return None
    return lists, all(item["web_auth"] for item in parts)
