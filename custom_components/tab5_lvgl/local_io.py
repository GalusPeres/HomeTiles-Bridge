"""Validation and topic helpers for firmware-announced local I/O channels."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_LOCAL_IO
from .device_helpers import command_topic, state_topic

_LOGGER = logging.getLogger(__name__)

LOCAL_IO_RELAY = "relay"
LOCAL_IO_TEMPERATURE = "temperature"
LOCAL_IO_TYPES = {LOCAL_IO_RELAY, LOCAL_IO_TEMPERATURE}

MAX_LOCAL_IO_CHANNELS = 64
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TYPE_ALIASES = {
    "relay": LOCAL_IO_RELAY,
    "switch": LOCAL_IO_RELAY,
    "temperature": LOCAL_IO_TEMPERATURE,
    "temperature_sensor": LOCAL_IO_TEMPERATURE,
    "temp": LOCAL_IO_TEMPERATURE,
}
_TEMPERATURE_UNITS = {
    "c": "°C",
    "°c": "°C",
    "celsius": "°C",
    "f": "°F",
    "°f": "°F",
    "fahrenheit": "°F",
    "k": "K",
    "kelvin": "K",
}


def normalise_local_io(raw: Any) -> list[dict[str, Any]]:
    """Return a stable, safe representation of a device's local I/O list.

    Validation is deliberately atomic: a malformed non-empty announcement is
    rejected instead of being mistaken for the intentional empty list that
    removes all channels.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("invalid_local_io")
    if len(raw) > MAX_LOCAL_IO_CHANNELS:
        raise ValueError("too_many_local_io_channels")

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_local_io_item_{index}")

        channel_type = _TYPE_ALIASES.get(str(item.get("type") or "").strip().lower())
        channel_id = str(item.get("id") or "").strip()
        if channel_type not in LOCAL_IO_TYPES or not _CHANNEL_ID_RE.fullmatch(channel_id):
            raise ValueError(f"invalid_local_io_item_{index}")
        if channel_id in seen_ids:
            raise ValueError(f"duplicate_local_io_id_{channel_id}")
        seen_ids.add(channel_id)

        name = str(item.get("name") or "").strip()
        descriptor: dict[str, Any] = {
            "id": channel_id,
            "type": channel_type,
            "name": name[:100] if name else _default_channel_name(channel_id, channel_type),
        }
        if channel_type == LOCAL_IO_TEMPERATURE:
            raw_unit = str(item.get("unit") or "°C").strip().lower()
            descriptor["unit"] = _TEMPERATURE_UNITS.get(raw_unit, "°C")
            descriptor["precision"] = _normalise_precision(item.get("precision"))
        result.append(descriptor)

    return result


def entry_local_io(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return validated local I/O descriptors stored for a config entry."""
    data = dict(entry.data or {})
    if entry.options:
        data.update(entry.options)
    try:
        return normalise_local_io(data.get(CONF_LOCAL_IO))
    except ValueError:
        _LOGGER.warning("HomeTiles config entry %s has invalid local_io data", entry.entry_id)
        return []


def local_io_unique_id(device_id: str, descriptor: dict[str, Any]) -> str:
    """Build the stable Home Assistant unique ID for a local I/O entity."""
    return f"{device_id}_local_io_{descriptor['type']}_{descriptor['id']}"


def local_io_state_topic(base_topic: str, channel_id: str) -> str:
    """Return the retained state topic used by a local hardware channel."""
    return state_topic(base_topic, f"io/{channel_id}")


def local_io_command_topic(base_topic: str, channel_id: str) -> str:
    """Return the non-retained command topic used by a local relay."""
    return command_topic(base_topic, f"io/{channel_id}")


def parse_on_off_payload(payload: Any) -> bool | None:
    """Parse plain or JSON MQTT relay/availability state."""
    value = payload
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                return None
            if not isinstance(decoded, dict):
                return None
            value = decoded.get("state", decoded.get("value"))
        else:
            value = stripped
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value) if value in (0, 1) else None
    lowered = str(value or "").strip().lower()
    if lowered in {"on", "1", "true", "yes", "online", "connected"}:
        return True
    if lowered in {"off", "0", "false", "no", "offline", "disconnected"}:
        return False
    return None


def parse_temperature_payload(payload: Any) -> float | None:
    """Parse a finite temperature from a plain or JSON MQTT payload."""
    value = payload
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                return None
            if not isinstance(decoded, dict):
                return None
            value = decoded.get("value", decoded.get("temperature", decoded.get("state")))
        else:
            value = stripped.replace(",", ".")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalise_precision(value: Any) -> int:
    try:
        precision = int(value)
    except (TypeError, ValueError):
        precision = 1
    return max(0, min(3, precision))


def _default_channel_name(channel_id: str, channel_type: str) -> str:
    label = channel_id.replace("_", " ").replace("-", " ").strip().title()
    if label.lower().startswith(("relay", "temperature", "temp")):
        return label
    prefix = "Relay" if channel_type == LOCAL_IO_RELAY else "Temperature"
    return f"{prefix} {label}".strip()
