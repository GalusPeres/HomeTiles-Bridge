"""Explicit telemetry capabilities with a conservative legacy fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CAPABILITIES = "capabilities"


def normalise_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("invalid_capabilities")
    result = {}
    for key in ("battery_soc", "legacy_external_temperature", "local_camera",
                "local_camera_stream", "view_navigation"):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError("invalid_capabilities")
            result[key] = value[key]
    return result


def merged_capabilities_data(entry) -> dict[str, Any]:
    # Capabilities are device-announced; stale options must never override them.
    data = dict(entry.data or {})
    data.update({key: value for key, value in (entry.options or {}).items()
                 if key not in (CAPABILITIES, "local_io")})
    return data


def supports(data: Mapping[str, Any], capability: str) -> bool:
    explicit = data.get(CAPABILITIES, {})
    if capability in explicit:
        return explicit[capability] is True
    if capability in ("view_navigation", "local_camera", "local_camera_stream"):
        # Never inferred: only firmware that announces it can answer requests.
        return False
    # No fixed external channel exists once firmware announces local I/O,
    # including an empty list. Preserve the original topic for legacy panels.
    if capability == "legacy_external_temperature":
        model = str(data.get("model") or "").strip().lower()
        return "local_io" not in data and model in {"", "tab5", "m5stack tab5", "m5stack_tab5"}
    if capability == "battery_soc":
        model = str(data.get("model") or "").strip().lower()
        # Older Tab5 firmware provided real PMIC telemetry. Keep it when no
        # capability announcement exists; current firmware explicitly says false.
        return model in {"", "tab5", "m5stack tab5", "m5stack_tab5"}
    return False


def stale_internal_sensor(unique_id: str, data: Mapping[str, Any]) -> bool:
    return ((unique_id.endswith("_battery_soc") and not supports(data, "battery_soc"))
            or (unique_id.endswith("_external_temperature")
                and not supports(data, "legacy_external_temperature")))


def stale_local_camera(unique_id: str, data: Mapping[str, Any]) -> bool:
    return unique_id.endswith("_local_camera") and not supports(data, "local_camera")
