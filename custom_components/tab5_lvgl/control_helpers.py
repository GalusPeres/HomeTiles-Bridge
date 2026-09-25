"""Allowed HA actions for the existing Switch and Scene tile contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


SWITCH_DOMAINS = ("switch", "input_boolean", "automation", "fan", "humidifier", "remote", "siren")
ACTION_DOMAINS = ("scene", "script", "button", "input_button")
# Home Assistant FanEntityFeature and SirenEntityFeature on/off flags.
SWITCH_FEATURES = {"fan": 16 | 32, "siren": 1 | 2}
ACTION_SERVICES = {"scene": "turn_on", "script": "turn_on", "button": "press", "input_button": "press"}
_ENTITY_ID = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")


def entity_domain(entity_id: Any) -> str:
    """Return a validated domain, never a service supplied by MQTT."""
    if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
        return ""
    return entity_id.split(".", 1)[0]


# Control lists a panel may announce, with the domains the Bridge options allow.
PANEL_LIST_DOMAINS = {
    "lights": ("light",),
    "switches": SWITCH_DOMAINS,
    "media_players": ("media_player",),
    "climates": ("climate",),
    "cameras": ("camera",),
}


def panel_entity_list(raw: Any, key: str) -> list[str]:
    """Validate a list announced by a panel; one foreign item rejects it."""
    raw = raw or []
    if not isinstance(raw, list):
        raise ValueError(f"invalid_{key}")
    items = [str(item).strip() for item in raw if str(item).strip()]
    if any(entity_domain(item) not in PANEL_LIST_DOMAINS[key] for item in items):
        raise ValueError(f"invalid_{key}")
    return items


def resolve_control_entity(entity_id: str | None, candidates: Iterable[str]) -> str | None:
    """Keep legacy single-target commands, but require a configured entity."""
    configured = list(dict.fromkeys(candidates))
    if entity_id:
        return entity_id if entity_id in configured and entity_domain(entity_id) else None
    return configured[0] if len(configured) == 1 and entity_domain(configured[0]) else None


def switch_supported(entity_id: str, attributes: Mapping[str, Any]) -> bool:
    """A two-way tile requires both directions, including for explicit commands."""
    domain = entity_domain(entity_id)
    if domain not in SWITCH_DOMAINS:
        return False
    required = SWITCH_FEATURES.get(domain)
    if required is None:
        return True
    features = attributes.get("supported_features")
    return (isinstance(features, int) and not isinstance(features, bool)
            and features >= 0 and features & required == required)


def build_switch_state_payload(entity_id: str, state: str | None, attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Keep unknown distinct from off and disable unsupported/unavailable controls."""
    supported = switch_supported(entity_id, attributes)
    return {"state": state, "available": state in ("on", "off") and supported,
            "supported": supported}


def build_switch_service_call(entity_id: str | None, command: str | None,
                              candidates: Iterable[str], state: Any) -> tuple[str, str, dict] | None:
    entity_id = resolve_control_entity(entity_id, candidates)
    if not entity_id or state is None or getattr(state, "state", None) not in ("on", "off"):
        return None
    if not switch_supported(entity_id, getattr(state, "attributes", {}) or {}):
        return None
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(command)
    if not service:
        return None
    return entity_domain(entity_id), service, {"entity_id": entity_id}


def resolve_action_entity(payload: str, scene_map: Mapping[str, str]) -> str | None:
    """Aliases and direct IDs share the same configured action allow-list."""
    entity_id = scene_map.get(payload.lower(), payload)
    if entity_id not in scene_map.values() or entity_domain(entity_id) not in ACTION_DOMAINS:
        return None
    return entity_id


def build_action_service_call(payload: str, scene_map: Mapping[str, str], state: Any) -> tuple[str, str, dict] | None:
    entity_id = resolve_action_entity(payload, scene_map)
    # Unpressed buttons and scenes normally have an unknown timestamp. They
    # remain callable; only a missing or explicitly unavailable entity blocks.
    if not entity_id or state is None or getattr(state, "state", None) == "unavailable":
        return None
    domain = entity_domain(entity_id)
    return domain, ACTION_SERVICES[domain], {"entity_id": entity_id}


def build_action_map(selected: Iterable[str], manual: Mapping[str, str], previous: Mapping[str, str]) -> dict[str, str]:
    """Preserve stored aliases when selection order or colliding domains change."""
    selected = list(dict.fromkeys(entity for entity in selected if entity_domain(entity) in ACTION_DOMAINS))
    result = {alias: entity for alias, entity in manual.items() if entity_domain(entity) in ACTION_DOMAINS}
    for alias, entity in previous.items():
        if entity in selected and alias not in result:
            result[alias] = entity
    for entity in selected:
        if entity in result.values():
            continue
        base = entity.split(".", 1)[1]
        alias, index = base, 2
        while alias in result:
            alias, index = f"{base}{index}", index + 1
        result[alias] = entity
    return result
