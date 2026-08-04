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

_LOCAL_IO_DOMAINS = {
    LOCAL_IO_RELAY: "switch",
    LOCAL_IO_TEMPERATURE: "sensor",
}

MAX_LOCAL_IO_CHANNELS = 64
MAX_LOCAL_IO_LEGACY_ENTITY_IDS = 8
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENTITY_ID_RE = re.compile(r"^(sensor|switch)\.([a-z0-9][a-z0-9_]{0,254})$")
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
    seen_entity_ids: set[str] = set()
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
        raw_entity_id = item.get("entity_id")
        if raw_entity_id is not None:
            entity_id = str(raw_entity_id).strip()
            match = _ENTITY_ID_RE.fullmatch(entity_id)
            if (
                match is None
                or len(entity_id) > 255
                or match.group(1) != local_io_domain(channel_type)
            ):
                raise ValueError(f"invalid_local_io_entity_id_{index}")
            if entity_id in seen_entity_ids:
                raise ValueError(f"duplicate_local_io_entity_id_{entity_id}")
            seen_entity_ids.add(entity_id)
            descriptor["entity_id"] = entity_id
        raw_legacy_entity_ids = item.get("legacy_entity_ids")
        if raw_legacy_entity_ids is not None:
            if not isinstance(raw_legacy_entity_ids, list):
                raise ValueError(f"invalid_local_io_legacy_entity_ids_{index}")
            if len(raw_legacy_entity_ids) > MAX_LOCAL_IO_LEGACY_ENTITY_IDS:
                raise ValueError(f"too_many_local_io_legacy_entity_ids_{index}")
            legacy_entity_ids: list[str] = []
            channel_legacy_ids: set[str] = set()
            for legacy_index, raw_legacy_entity_id in enumerate(raw_legacy_entity_ids):
                if not isinstance(raw_legacy_entity_id, str):
                    raise ValueError(
                        f"invalid_local_io_legacy_entity_id_{index}_{legacy_index}"
                    )
                legacy_entity_id = raw_legacy_entity_id.strip()
                match = _ENTITY_ID_RE.fullmatch(legacy_entity_id)
                if (
                    match is None
                    or len(legacy_entity_id) > 255
                    or match.group(1) != local_io_domain(channel_type)
                ):
                    raise ValueError(
                        f"invalid_local_io_legacy_entity_id_{index}_{legacy_index}"
                    )
                if legacy_entity_id in channel_legacy_ids:
                    continue
                channel_legacy_ids.add(legacy_entity_id)
                legacy_entity_ids.append(legacy_entity_id)
            if legacy_entity_ids:
                descriptor["legacy_entity_ids"] = legacy_entity_ids
        if channel_type == LOCAL_IO_TEMPERATURE:
            raw_unit = str(item.get("unit") or "°C").strip().lower()
            descriptor["unit"] = _TEMPERATURE_UNITS.get(raw_unit, "°C")
            descriptor["precision"] = _normalise_precision(item.get("precision"))
        result.append(descriptor)

    # A legacy ID can identify only one channel and must not collide with any
    # current announced ID. Silently accepting either case could migrate the
    # wrong Home Assistant registry entity.
    current_entity_ids = {
        descriptor["entity_id"]
        for descriptor in result
        if descriptor.get("entity_id")
    }
    legacy_entity_owners: dict[str, str] = {}
    for descriptor in result:
        current_entity_id = descriptor.get("entity_id")
        normalised_legacy_ids: list[str] = []
        for legacy_entity_id in descriptor.get("legacy_entity_ids", []):
            if legacy_entity_id == current_entity_id:
                continue
            if legacy_entity_id in current_entity_ids:
                raise ValueError(
                    f"conflicting_local_io_legacy_entity_id_{legacy_entity_id}"
                )
            previous_owner = legacy_entity_owners.get(legacy_entity_id)
            if previous_owner is not None and previous_owner != descriptor["id"]:
                raise ValueError(
                    f"duplicate_local_io_legacy_entity_id_{legacy_entity_id}"
                )
            legacy_entity_owners[legacy_entity_id] = descriptor["id"]
            normalised_legacy_ids.append(legacy_entity_id)
        if normalised_legacy_ids:
            descriptor["legacy_entity_ids"] = normalised_legacy_ids
        else:
            descriptor.pop("legacy_entity_ids", None)

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


def local_io_domain(channel_type: str) -> str:
    """Return the Home Assistant platform domain for a local channel type."""
    try:
        return _LOCAL_IO_DOMAINS[channel_type]
    except KeyError as err:
        raise ValueError(f"invalid_local_io_type_{channel_type}") from err


def local_io_announced_entity_id(descriptor: dict[str, Any]) -> str | None:
    """Return the validated full entity ID announced by firmware, if present."""
    entity_id = descriptor.get("entity_id")
    return entity_id if isinstance(entity_id, str) and entity_id else None


def local_io_legacy_collision_index(
    entity_id: str,
    legacy_entity_id: str,
) -> int | None:
    """Return a canonical Home Assistant collision suffix index, if present."""
    prefix = f"{legacy_entity_id}_"
    if not entity_id.startswith(prefix):
        return None
    suffix = entity_id[len(prefix):]
    if not suffix.isdigit():
        return None
    collision_index = int(suffix)
    if collision_index < 2 or suffix != str(collision_index):
        return None
    return collision_index


def local_io_migration_target_entity_id(
    current_entity_id: str,
    announced_entity_id: str,
    legacy_entity_ids: list[str],
) -> str:
    """Preserve Home Assistant's numeric suffix while replacing a legacy base."""
    for legacy_entity_id in legacy_entity_ids:
        if current_entity_id == legacy_entity_id:
            return announced_entity_id
        collision_index = local_io_legacy_collision_index(
            current_entity_id, legacy_entity_id
        )
        if collision_index is not None:
            return f"{announced_entity_id}_{collision_index}"
    return announced_entity_id


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
