"""Protocol helpers for forwarding Home Assistant climate entities."""

from __future__ import annotations

import math
from typing import Any, Mapping


CLIMATE_TARGET_TEMPERATURE = 1
CLIMATE_TARGET_TEMPERATURE_RANGE = 2
CLIMATE_TARGET_HUMIDITY = 4
CLIMATE_FAN_MODE = 8
CLIMATE_PRESET_MODE = 16
CLIMATE_SWING_MODE = 32
CLIMATE_TURN_OFF = 128
CLIMATE_TURN_ON = 256
CLIMATE_SWING_HORIZONTAL_MODE = 512

CLIMATE_COMMANDS = frozenset(
  {
    "set_temperature",
    "set_humidity",
    "set_hvac_mode",
    "set_fan_mode",
    "set_preset_mode",
    "set_swing_mode",
    "set_swing_horizontal_mode",
    "turn_on",
    "turn_off",
    "toggle",
  }
)


def build_climate_state_payload(
  state_value: Any,
  attributes: Mapping[str, Any],
  temperature_unit: Any = None,
) -> dict[str, Any]:
  """Build the stable climate state contract consumed by HomeTiles."""
  state = str(state_value or "unknown").strip().lower() or "unknown"
  payload: dict[str, Any] = {
    "state": state,
    "hvac_mode": state,
    "available": state != "unavailable",
  }
  if "supported_features" in attributes:
    payload["supported_features"] = _coerce_non_negative_int(
      attributes.get("supported_features"), 0
    )

  for key in (
    "hvac_action",
    "current_temperature",
    "current_humidity",
    "temperature",
    "target_humidity",
    "humidity",
    "target_temp_low",
    "target_temp_high",
    "min_temp",
    "max_temp",
    "min_humidity",
    "max_humidity",
    "target_temp_step",
    "target_humidity_step",
    "precision",
    "hvac_modes",
    "fan_mode",
    "fan_modes",
    "preset_mode",
    "preset_modes",
    "swing_mode",
    "swing_modes",
    "swing_horizontal_mode",
    "swing_horizontal_modes",
  ):
    value = attributes.get(key)
    if value is not None:
      payload[key] = value

  unit = attributes.get("temperature_unit") or attributes.get(
    "unit_of_measurement"
  )
  if not unit:
    unit = temperature_unit
  if unit:
    payload["temperature_unit"] = str(unit)
  return payload


def build_climate_service_call(
  command_payload: Mapping[str, Any],
  attributes: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
  """Validate a HomeTiles climate command and return its HA service call."""
  command = str(
    command_payload.get("command") or command_payload.get("service") or ""
  ).strip().lower()
  feature_mask_present = "supported_features" in attributes
  features = _coerce_non_negative_int(attributes.get("supported_features"), 0)

  hvac_mode = _normalise_option(command_payload.get("hvac_mode"))
  temperature = command_payload.get("temperature")
  target_low = command_payload.get("target_temp_low")
  target_high = command_payload.get("target_temp_high")

  if command and command not in CLIMATE_COMMANDS:
    raise ValueError("unsupported climate command")

  if not command:
    inferred_commands: list[str] = []
    if temperature is not None or target_low is not None or target_high is not None:
      inferred_commands.append("set_temperature")
    elif hvac_mode:
      inferred_commands.append("set_hvac_mode")
    if command_payload.get("humidity") is not None:
      inferred_commands.append("set_humidity")
    for service, value_key in (
      ("set_fan_mode", "fan_mode"),
      ("set_preset_mode", "preset_mode"),
      ("set_swing_mode", "swing_mode"),
      ("set_swing_horizontal_mode", "swing_horizontal_mode"),
    ):
      if command_payload.get(value_key) is not None:
        inferred_commands.append(service)
    if len(inferred_commands) != 1:
      raise ValueError("no unambiguous climate action was provided")
    command = inferred_commands[0]

  if command == "set_hvac_mode":
    if not hvac_mode:
      raise ValueError("hvac_mode is required")
    _require_option(hvac_mode, attributes.get("hvac_modes"), "hvac_mode")
    return "set_hvac_mode", {"hvac_mode": hvac_mode}

  if command in {"turn_on", "turn_off", "toggle"}:
    if feature_mask_present:
      if command == "toggle":
        _require_any_feature(
          features, (CLIMATE_TURN_OFF, CLIMATE_TURN_ON), command
        )
      else:
        required = CLIMATE_TURN_ON if command == "turn_on" else CLIMATE_TURN_OFF
        _require_feature(features, required, command)
    return command, {}

  if command == "set_humidity":
    _require_feature_if_present(
      feature_mask_present, features, CLIMATE_TARGET_HUMIDITY, "set_humidity"
    )
    humidity = _finite_integer(command_payload.get("humidity"), "humidity")
    _require_range(
      humidity,
      attributes.get("min_humidity"),
      attributes.get("max_humidity"),
      "humidity",
    )
    return "set_humidity", {"humidity": humidity}

  option_commands = (
    ("set_fan_mode", "fan_mode", "fan_modes", CLIMATE_FAN_MODE),
    ("set_preset_mode", "preset_mode", "preset_modes", CLIMATE_PRESET_MODE),
    ("set_swing_mode", "swing_mode", "swing_modes", CLIMATE_SWING_MODE),
    (
      "set_swing_horizontal_mode",
      "swing_horizontal_mode",
      "swing_horizontal_modes",
      CLIMATE_SWING_HORIZONTAL_MODE,
    ),
  )
  for service, value_key, options_key, feature in option_commands:
    if command != service:
      continue
    _require_feature_if_present(feature_mask_present, features, feature, service)
    value = _normalise_option(command_payload.get(value_key))
    if not value:
      raise ValueError(f"{value_key} is required")
    _require_option(value, attributes.get(options_key), value_key)
    return service, {value_key: value}

  if command == "set_temperature":
    has_temperature = temperature is not None
    has_range = target_low is not None or target_high is not None
    if has_temperature and has_range:
      raise ValueError("temperature and target range are mutually exclusive")

    service_data: dict[str, Any]
    if has_temperature:
      _require_feature_if_present(
        feature_mask_present,
        features,
        CLIMATE_TARGET_TEMPERATURE,
        "set_temperature",
      )
      value = _finite_number(temperature, "temperature")
      _require_range(
        value, attributes.get("min_temp"), attributes.get("max_temp"), "temperature"
      )
      service_data = {"temperature": value}
    elif has_range:
      _require_feature_if_present(
        feature_mask_present,
        features,
        CLIMATE_TARGET_TEMPERATURE_RANGE,
        "set_temperature range",
      )
      if target_low is None or target_high is None:
        raise ValueError("target_temp_low and target_temp_high are required together")
      low = _finite_number(target_low, "target_temp_low")
      high = _finite_number(target_high, "target_temp_high")
      _require_range(
        low, attributes.get("min_temp"), attributes.get("max_temp"), "target_temp_low"
      )
      _require_range(
        high,
        attributes.get("min_temp"),
        attributes.get("max_temp"),
        "target_temp_high",
      )
      if low > high:
        raise ValueError("target_temp_low must not exceed target_temp_high")
      service_data = {
        "target_temp_low": low,
        "target_temp_high": high,
      }
    else:
      raise ValueError("set_temperature requires a temperature or target range")

    if hvac_mode:
      _require_option(hvac_mode, attributes.get("hvac_modes"), "hvac_mode")
      service_data["hvac_mode"] = hvac_mode
    return "set_temperature", service_data

  raise ValueError("no supported climate action was provided")


def _finite_integer(value: Any, field: str) -> int:
  number = _finite_number(value, field)
  if not number.is_integer():
    raise ValueError(f"{field} must be an integer")
  return int(number)


def _coerce_non_negative_int(value: Any, default: int) -> int:
  if isinstance(value, bool):
    return default
  try:
    result = int(value)
  except (OverflowError, TypeError, ValueError):
    result = default
  return max(0, result)


def _normalise_option(value: Any) -> str:
  return str(value or "").strip()


def _finite_number(value: Any, field: str) -> float:
  if value is None or isinstance(value, bool):
    raise ValueError(f"{field} must be a finite number")
  try:
    number = float(str(value).strip().replace(",", "."))
  except (TypeError, ValueError) as err:
    raise ValueError(f"{field} must be a finite number") from err
  if not math.isfinite(number):
    raise ValueError(f"{field} must be a finite number")
  return number


def _require_range(value: float, minimum: Any, maximum: Any, field: str) -> None:
  if minimum is not None and value < _finite_number(minimum, f"minimum {field}"):
    raise ValueError(f"{field} is below the supported minimum")
  if maximum is not None and value > _finite_number(maximum, f"maximum {field}"):
    raise ValueError(f"{field} exceeds the supported maximum")


def _require_option(value: str, options: Any, field: str) -> None:
  if not isinstance(options, (list, tuple, set)) or not options:
    raise ValueError(f"{field} has no advertised options")
  allowed = {str(option).strip() for option in options}
  if value not in allowed:
    raise ValueError(f"{field} is not supported by the entity")


def _require_feature(features: int, required: int, action: str) -> None:
  if features & required != required:
    raise ValueError(f"{action} is not supported by the entity")


def _require_any_feature(
  features: int, alternatives: tuple[int, ...], action: str
) -> None:
  if not any(features & required == required for required in alternatives):
    raise ValueError(f"{action} is not supported by the entity")


def _require_feature_if_present(
  present: bool, features: int, required: int, action: str
) -> None:
  if present:
    _require_feature(features, required, action)
