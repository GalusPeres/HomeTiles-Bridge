from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional

MAX_MEDIA_PAGES = 128
MAX_ITEMS_PER_PAGE = 32
MAX_CATALOG_PAYLOAD_BYTES = 32768

MAX_TITLE_BYTES = 255
MAX_MEDIA_CLASS_BYTES = 64
MAX_MEDIA_CONTENT_TYPE_BYTES = 255
MAX_MEDIA_CONTENT_ID_BYTES = 1024
MAX_THUMBNAIL_BYTES = 1024


def _truncate_string(value: Any, max_bytes: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    out = bytearray()
    for byte in text.encode("utf-8"):
        if len(out) >= max_bytes:
            break
        out.append(byte)

    # Avoid cutting UTF-8 continuation bytes in the middle
    if out and (out[-1] & 0xC0) == 0x80:
        while out and (out[-1] & 0xC0) == 0x80:
            out.pop()

    try:
        return out.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_thumbnail(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return _truncate_string(value, MAX_THUMBNAIL_BYTES)


def normalize_browse_item(item: Any) -> Optional[Dict[str, Any]]:
    """Normalize a single HA browse_media child."""
    if not isinstance(item, dict):
        return None

    title = _truncate_string(item.get("title"), MAX_TITLE_BYTES) or ""
    media_class = _truncate_string(item.get("media_class"), MAX_MEDIA_CLASS_BYTES) or ""
    media_content_type = _truncate_string(
        item.get("media_content_type"), MAX_MEDIA_CONTENT_TYPE_BYTES
    ) or ""
    media_content_id = _truncate_string(
        item.get("media_content_id"), MAX_MEDIA_CONTENT_ID_BYTES
    ) or ""

    normalized = {
        "title": title,
        "media_class": media_class,
        "media_content_type": media_content_type,
        "media_content_id": media_content_id,
        "can_play": _coerce_bool(item.get("can_play"), False),
        "can_expand": _coerce_bool(item.get("can_expand"), False),
        "can_search": _coerce_bool(item.get("can_search"), False),
        "thumbnail": _normalize_thumbnail(item.get("thumbnail")),
    }

    # Accept an item if it has something usable for the device
    if not normalized["title"] and not (
        normalized["media_content_id"] or normalized["media_content_type"]
    ):
        return None

    return normalized


def normalize_browse_result(result: Any) -> Optional[Dict[str, Any]]:
    """Normalize a Home Assistant browse_media result to a flat, bounded list."""
    if result is None:
        return None

    if not isinstance(result, dict):
        return None

    children = result.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list):
        return None

    items: List[Dict[str, Any]] = []
    for child in children:
        normalized = normalize_browse_item(child)
        if normalized is not None:
            items.append(normalized)

    # Keep device traffic bounded.
    if len(items) > MAX_ITEMS_PER_PAGE:
        items = items[:MAX_ITEMS_PER_PAGE]

    title = _truncate_string(result.get("title"), MAX_TITLE_BYTES) or ""
    media_class = _truncate_string(result.get("media_class"), MAX_MEDIA_CLASS_BYTES) or ""
    media_content_type = _truncate_string(
        result.get("media_content_type"), MAX_MEDIA_CONTENT_TYPE_BYTES
    ) or ""
    media_content_id = _truncate_string(
        result.get("media_content_id"), MAX_MEDIA_CONTENT_ID_BYTES
    ) or ""

    return {
        "title": title,
        "media_class": media_class,
        "media_content_type": media_content_type,
        "media_content_id": media_content_id,
        "can_play": _coerce_bool(result.get("can_play"), False),
        "can_expand": _coerce_bool(result.get("can_expand"), False),
        "can_search": _coerce_bool(result.get("can_search"), False),
        "thumbnail": _normalize_thumbnail(result.get("thumbnail")),
        "items": items,
    }


def paginate_items(items: Iterable[Dict[str, Any]], max_items_per_page: int = MAX_ITEMS_PER_PAGE):
    items_list = list(items)
    if not items_list:
        return 1, [[]]

    page_size = max(1, int(max_items_per_page))
    page_count = max(1, int(math.ceil(len(items_list) / page_size)))

    pages: List[List[Dict[str, Any]]] = []
    for page_index in range(page_count):
        start = page_index * page_size
        end = start + page_size
        pages.append(items_list[start:end])  # ← Direkt die List, kein Dict!

    return page_count, pages

def serialize_catalog_page(payload: Dict[str, Any], max_payload_bytes: int = MAX_CATALOG_PAYLOAD_BYTES) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > max_payload_bytes:
        raise ValueError("catalog page exceeds max payload bytes")
    return text


class MediaBrowserState:
    """Tracks a single paginated browse session.

    Semantics:
      - old sessions rejected
      - old revisions rejected
      - old request ids rejected
      - partial pages keep separate until complete
    """

    def __init__(self) -> None:
        self.session: str = ""
        self.revision: int = 0
        self.request_id: int = 0
        self.entity_id: str = ""
        self.media_content_id: str = ""
        self.media_content_type: str = ""
        self.page_count: int = 1
        self.pages: Dict[int, List[Dict[str, Any]]] = {}
        self.complete: bool = False

    def begin(
        self,
        *,
        session: str,
        revision: int,
        request_id: int,
        entity_id: str,
        media_content_id: str = "",
        media_content_type: str = "",
        page_count: int = 1,
    ) -> None:
        self.session = str(session)
        self.revision = int(revision)
        self.request_id = int(request_id)
        self.entity_id = str(entity_id)
        self.media_content_id = str(media_content_id or "")
        self.media_content_type = str(media_content_type or "")
        self.page_count = max(1, int(page_count))
        self.pages = {}
        self.complete = False

    def accept_page(
        self,
        *,
        session: str,
        revision: int,
        request_id: int,
        page: int,
        items: List[Dict[str, Any]],
    ) -> bool:
        if str(session) != self.session:
            return False
        if int(revision) != self.revision:
            return False
        if int(request_id) != self.request_id:
            return False

        page_index = int(page)
        if page_index < 0 or page_index >= self.page_count:
            return False
        if not isinstance(items, list):
            return False

        self.pages[page_index] = list(items)[:MAX_ITEMS_PER_PAGE]
        self.complete = set(self.pages.keys()) == set(range(self.page_count))
        return True

    def to_payload(self) -> Dict[str, Any]:
        composite: List[Dict[str, Any]] = []
        for page_index in range(self.page_count):
            composite.extend(self.pages.get(page_index, []))

        return {
            "version": 1,
            "session": self.session,
            "revision": self.revision,
            "request_id": self.request_id,
            "entity_id": self.entity_id,
            "media_content_id": self.media_content_id,
            "media_content_type": self.media_content_type,
            "page_count": self.page_count,
            "items": composite,
        }


class MediaBrowser:
    """Reusable protocol helper, not tied to MQTT or HA service calls."""

    def __init__(self) -> None:
        self._state_by_session: Dict[str, MediaBrowserState] = {}

    def state_for(self, session: str) -> MediaBrowserState:
        state = self._state_by_session.get(session)
        if state is None:
            state = MediaBrowserState()
            self._state_by_session[session] = state
        return state

    def build_page_payload(
        self,
        result: Any,
        *,
        session: str,
        revision: int,
        request_id: int,
        entity_id: str,
        media_content_id: str = "",
        media_content_type: str = "",
        page: int = 0,
        max_items_per_page: int = MAX_ITEMS_PER_PAGE,
    ) -> Dict[str, Any]:
        normalized = normalize_browse_result(result)
        _LOGGER.info(
            "Tab5 normalized browse result: %s",
            normalized,
        )
        if normalized is None:
            raise ValueError("invalid browse media result")

        items = normalized.get("items", [])
        page_count, pages = paginate_items(items, max_items_per_page=max_items_per_page)
        if page < 0 or page >= page_count:
            raise ValueError("page index out of range")

        selected = pages[page]["items"]
        payload = {
            "version": 1,
            "session": str(session),
            "revision": int(revision),
            "request_id": int(request_id),
            "page": int(page),
            "pages": int(page_count),
            "entity_id": str(entity_id),
            "parent": {
                "media_content_id": str(media_content_id or ""),
                "media_content_type": str(media_content_type or ""),
            },
            "items": selected,
        }

        text = serialize_catalog_page(payload, max_payload_bytes=MAX_CATALOG_PAYLOAD_BYTES)
        return json.loads(text)