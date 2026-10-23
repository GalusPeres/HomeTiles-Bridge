"""Explicit telemetry capabilities with a conservative legacy fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CAPABILITIES = "capabilities"


def normalise_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("invalid_capabilities")
    result = {}
    for key in ("audio_input", "audio_output", "battery_soc", "display_rotation", "display_theme",
                "legacy_external_temperature", "local_camera", "local_camera_stream", "view_navigation"):
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
    if capability in ("view_navigation", "local_camera", "local_camera_stream", "audio_output",
                      "audio_input", "display_theme", "display_rotation"):
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


def stale_speaker(unique_id: str, data: Mapping[str, Any]) -> bool:
    return unique_id.endswith("_speaker") and not supports(data, "audio_output")


def stale_display_controls(unique_id: str, domain: str, data: Mapping[str, Any]) -> bool:
    """The theme and rotation selects without their capability, and the old
    180 degree rotation switch once the panel offers the rotation select."""
    if domain == "select" and unique_id.endswith("_display_theme"):
        return not supports(data, "display_theme")
    if domain == "select" and unique_id.endswith("_display_rotation"):
        return not supports(data, "display_rotation")
    if domain == "switch" and unique_id.endswith("_display_rotate"):
        return supports(data, "display_rotation")
    return False


def stale_assist(unique_id: str, domain: str, data: Mapping[str, Any]) -> bool:
    """The microphone's satellite, button, switches or selects without audio_input."""
    if supports(data, "audio_input"):
        return False
    return ((domain == "assist_satellite" and unique_id.endswith("_assist_satellite"))
            or (domain == "button" and unique_id.endswith("_assist"))
            or (domain == "switch" and unique_id.endswith(
                ("_assist_microphone", "_assist_wake_listen")))
            or (domain == "select" and unique_id.endswith(
                ("_assist-pipeline", "_assist-vad_sensitivity", "_assist_wake_word"))))
