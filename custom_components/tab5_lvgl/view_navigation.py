"""Bounded navigation catalog and command state, independent of Home Assistant."""

from __future__ import annotations

import re
from typing import Any

MAX_PAGES = 128
MAX_TARGETS_PER_PAGE = 65
# A camera tile's popup straight in full screen (#65, doorbell automations).
# The panel lists these under "full_targets" (older Bridges ignore that key)
# and reports the full screen as "full": true next to the tile's target.
MAX_FULL_TARGETS_PER_PAGE = 64
_TARGET = re.compile(r"(?:home|(?:folder|tile):[1-9][0-9]{0,4})\Z")
_FULL_TARGET = re.compile(r"full:([1-9][0-9]{0,4})\Z")
_SESSION = re.compile(r"[0-9a-f]{32}\Z")


class ViewNavigation:
    """Assemble one session/revision and report only device-confirmed state."""

    def __init__(self) -> None:
        self.session = ""
        self.revision = 0
        self.pages: dict[int, dict[str, str]] = {}
        self.page_count = 0
        self.targets: dict[str, str] = {}
        self.current: str | None = None
        self.mode = "unknown"
        self.ready = False
        self.sequence = 0
        self.uptime = 0
        self.received_at: float | None = None
        self.online = False

    def state(self, payload: Any, now: float, *, retained: bool = False) -> bool:
        if not isinstance(payload, dict):
            return False
        session, revision = payload.get("session"), payload.get("revision")
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            return False
        if type(revision) is not int or not 0 < revision < 2**32:
            return False
        for key in ("sequence", "uptime"):
            if type(payload.get(key)) is not int or not 0 <= payload[key] < 2**32:
                return False
        current = payload.get("current")
        if current is not None and (not isinstance(current, str) or not _TARGET.fullmatch(current)):
            return False
        if payload.get("mode") not in {"folder", "popup", "pin", "settings", "sleep", "screensaver"}:
            return False
        full = payload.get("full", False)
        if type(full) is not bool:
            return False
        if full and current is not None and current.startswith("tile:"):
            current = "full:" + current[5:]
        if (session, revision) != (self.session, self.revision):
            self.pages.clear()
            self.targets.clear()
            self.page_count = 0
            if session != self.session:
                self.sequence = 0
            self.session, self.revision = session, revision
        self.current, self.mode = current, payload["mode"]
        self.ready = payload.get("ready") is True
        self.sequence = max(self.sequence, payload["sequence"])
        self.uptime = payload["uptime"]
        # A retained snapshot does not prove that the panel is currently alive.
        self.received_at = None if retained else now
        return True

    def catalog(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if (payload.get("session"), payload.get("revision")) != (self.session, self.revision):
            return False
        page, count, raw = payload.get("page"), payload.get("pages"), payload.get("targets")
        if type(page) is not int or type(count) is not int or not 0 <= page < count <= MAX_PAGES:
            return False
        if self.page_count and self.page_count != count:
            return False
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TARGETS_PER_PAGE:
            return False
        options: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                return False
            target, label = item.get("id"), item.get("label")
            if not isinstance(target, str) or not _TARGET.fullmatch(target):
                return False
            if not isinstance(label, str) or not 1 <= len(label) <= 255 or target in options:
                return False
            options[target] = label
        raw_full = payload.get("full_targets", [])
        if not isinstance(raw_full, list) or len(raw_full) > MAX_FULL_TARGETS_PER_PAGE:
            return False
        for item in raw_full:
            if not isinstance(item, dict):
                return False
            target, label = item.get("id"), item.get("label")
            match = _FULL_TARGET.fullmatch(target) if isinstance(target, str) else None
            # Only for a tile of the same page.
            if match is None or f"tile:{match.group(1)}" not in options:
                return False
            if not isinstance(label, str) or not 1 <= len(label) <= 255 or target in options:
                return False
            options[target] = label
        pages = dict(self.pages)
        pages[page] = options
        combined: dict[str, str] = {}
        for index in sorted(pages):
            for target, label in pages[index].items():
                if target in combined or label in combined.values():
                    return False
                combined[target] = label
        self.pages, self.page_count = pages, count
        if len(pages) == count and "home" in combined:
            self.targets = combined
        return True

    def current_label(self) -> str | None:
        """The shown view's option; a full screen not listed shows its tile."""
        label = self.targets.get(self.current)
        if label is None and self.current is not None and self.current.startswith("full:"):
            label = self.targets.get("tile:" + self.current[5:])
        return label

    def available(self, now: float) -> bool:
        return (self.online and self.ready and bool(self.targets)
                and self.received_at is not None and 0 <= now - self.received_at < 20)

    def command(self, option: str, now: float) -> dict[str, Any]:
        if not self.available(now):
            raise ValueError("view_unavailable")
        target = next((key for key, label in self.targets.items() if label == option), None)
        if target is None:
            raise ValueError("invalid_view")
        self.sequence += 1
        if self.sequence >= 2**32:
            raise ValueError("view_unavailable")
        elapsed = int((now - self.received_at) * 1000)
        return {"session": self.session, "revision": self.revision,
                "sequence": self.sequence, "target": target,
                "deadline": (self.uptime + elapsed + 5000) % 2**32}
