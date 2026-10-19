"""The Media popup's library: Home Assistant's media browser for the panel.

Based on memooox3's media browse work (HomeTiles PR #87, Bridge PR #11). The
panel asks for one level with `browse_media` (empty id and type = the root);
the Bridge answers with the level's children on `stat/media/catalog`, split
into pages by size so every page fits the panel's 32 KB receive buffer (one
fixed topic: the direct link takes no wildcard subscription from a panel),
or with an error on `stat/media`. Pure helpers, no Home Assistant imports.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# The panel keeps at most this many rows of one level.
MAX_ITEMS = 256
# One catalog page stays below this many bytes of JSON (the panel's large
# buffer is 32 KB; the topic and the link frame need room too).
MAX_PAGE_BYTES = 12 * 1024
MAX_TITLE_BYTES = 160
MAX_CLASS_BYTES = 32
MAX_TYPE_BYTES = 160
MAX_ID_BYTES = 1024
MAX_THUMBNAIL_BYTES = 1024


def _cut(value: Any, max_bytes: int) -> str:
  """The text, at most max_bytes of UTF-8, never cut inside a character."""
  if value is None:
    return ""
  text = str(value).strip()
  data = text.encode("utf-8")
  if len(data) <= max_bytes:
    return text
  return data[:max_bytes].decode("utf-8", errors="ignore")


def browse_arguments(content_id: Any, content_type: Any) -> Tuple[Optional[str], Optional[str]]:
  """(media_content_type, media_content_id) for async_browse_media, like Home
  Assistant's own media browser: the root without either; a folder with its
  id and type exactly as listed, an empty id too (Sonos lists "Favorites" and
  "Music Library" with the id "" and their type; None would be its root
  again, so the panel showed the same level once more)."""
  content_id = "" if content_id is None else str(content_id)
  content_type = "" if content_type is None else str(content_type)
  if not content_id and not content_type:
    return None, None
  return content_type, content_id


def _exact(value: Any, max_bytes: int) -> Optional[str]:
  """An id or type exactly as Home Assistant listed it (a cut or stripped one
  would not open again); None when it is too long for the panel."""
  text = "" if value is None else str(value)
  return text if len(text.encode("utf-8")) <= max_bytes else None


def normalize_item(item: Any) -> Optional[Dict[str, Any]]:
  """One child of a BrowseMedia result as the panel needs it."""
  if not isinstance(item, dict):
    return None
  title = _cut(item.get("title"), MAX_TITLE_BYTES)
  content_id = _exact(item.get("media_content_id"), MAX_ID_BYTES)
  content_type = _exact(item.get("media_content_type"), MAX_TYPE_BYTES)
  if content_id is None or content_type is None:
    return None
  if not title and not content_id and not content_type:
    return None
  out: Dict[str, Any] = {
    "title": title,
    "media_class": _cut(item.get("media_class"), MAX_CLASS_BYTES),
    "media_content_id": content_id,
    "media_content_type": content_type,
    "can_play": bool(item.get("can_play")),
    "can_expand": bool(item.get("can_expand")),
  }
  thumbnail = _cut(item.get("thumbnail"), MAX_THUMBNAIL_BYTES)
  if thumbnail:
    out["thumbnail"] = thumbnail
  return out


def normalize_result(result: Any) -> Optional[Tuple[str, List[Dict[str, Any]], bool]]:
  """(title, items, truncated) of a BrowseMedia result (dict form), None if
  it is not one."""
  if hasattr(result, "as_dict"):
    result = result.as_dict()
  if not isinstance(result, dict):
    return None
  children = result.get("children") or []
  if not isinstance(children, list):
    return None
  items = [item for item in (normalize_item(child) for child in children) if item is not None]
  truncated = len(items) > MAX_ITEMS
  return _cut(result.get("title"), MAX_TITLE_BYTES), items[:MAX_ITEMS], truncated


def catalog_pages(header: Dict[str, Any], items: List[Dict[str, Any]], max_bytes: int = MAX_PAGE_BYTES) -> List[str]:
  """The level as JSON pages: header fields plus "page", "pages" and "items";
  each page as many items as fit in max_bytes (at least one)."""
  def encode(page: int, pages: int, chunk: List[Dict[str, Any]]) -> str:
    return json.dumps({**header, "page": page, "pages": pages, "items": chunk},
                      ensure_ascii=False, separators=(",", ":"))

  chunks: List[List[Dict[str, Any]]] = []
  current: List[Dict[str, Any]] = []
  for item in items:
    if current and len(encode(0, 99, current + [item]).encode("utf-8")) > max_bytes:
      chunks.append(current)
      current = []
    current.append(item)
  chunks.append(current)
  return [encode(index, len(chunks), chunk) for index, chunk in enumerate(chunks)]

