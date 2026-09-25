"""Source-level checks for textual sensor-state history wiring."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


BRIDGE_SOURCE = (
  Path(__file__).resolve().parents[1]
  / "custom_components"
  / "tab5_lvgl"
  / "__init__.py"
)


def _bridge_source() -> str:
  return BRIDGE_SOURCE.read_text(encoding="utf-8")


def _bridge_tree() -> ast.Module:
  return ast.parse(_bridge_source())


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
      return node
  raise AssertionError(f"Bridge function {name} was not found")


def _called_names(function: ast.AST) -> set[str]:
  names: set[str] = set()
  for node in ast.walk(function):
    if not isinstance(node, ast.Call):
      continue
    if isinstance(node.func, ast.Name):
      names.add(node.func.id)
    elif isinstance(node.func, ast.Attribute):
      names.add(node.func.attr)
  return names


class StateHistoryWiringTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls) -> None:
    cls.source = _bridge_source()
    cls.tree = _bridge_tree()

  def test_state_kind_dispatches_before_legacy_numeric_history(self) -> None:
    handler = _find_function(self.tree, "_async_handle_history_request")
    calls = _called_names(handler)
    source = ast.get_source_segment(self.source, handler)

    self.assertIsNotNone(source)
    self.assertIn("_async_handle_state_history_request", calls)
    self.assertIn("_async_handle_binary_history_request", calls)
    self.assertLess(
      source.index("STATE_HISTORY_KIND"),
      source.index('parsed.get("period_minutes")'),
    )
    self.assertLess(
      source.index("BINARY_HISTORY_KIND"),
      source.index('parsed.get("period_minutes")'),
    )
    self.assertIn('"values": values', source)

  def test_kindless_text_sensor_request_keeps_exact_legacy_numeric_envelope(self) -> None:
    handler = _find_function(self.tree, "_async_handle_history_request")
    source = ast.get_source_segment(self.source, handler)
    calls = _called_names(handler)

    self.assertIsNotNone(source)
    self.assertIn(
      'history_kind = str(parsed.get("kind") or "").strip().lower()',
      source,
    )
    self.assertIn("history_kind == STATE_HISTORY_KIND", source)
    self.assertIn("history_kind == BINARY_HISTORY_KIND", source)
    self.assertNotIn("sensor_state_kind", calls)
    # The legacy numeric path answers only configured entities, and the
    # membership check runs before any Home Assistant or Recorder read.
    guard = source.index(
      "if entity_id not in self.sensors and entity_id not in self.binary_sensors:"
    )
    self.assertLess(guard, source.index("self.hass.states.get(entity_id)"))
    self.assertLess(guard, source.index("get_instance("))
    self.assertIn("current_numeric = _coerce_float(state.state) if state else None", source)
    self.assertIn('response["current"] = state.state', source)

    response_assignment = next(
      node
      for node in ast.walk(handler)
      if isinstance(node, ast.AnnAssign)
      and isinstance(node.target, ast.Name)
      and node.target.id == "response"
      and isinstance(node.value, ast.Dict)
    )
    response_keys = [
      key.value
      for key in response_assignment.value.keys
      if isinstance(key, ast.Constant) and isinstance(key.value, str)
    ]
    self.assertEqual(
      response_keys,
      ["entity_id", "hours", "period_minutes", "stat", "values"],
    )
    self.assertNotIn("kind", response_keys)
    self.assertNotIn("version", response_keys)

  def test_state_handler_is_validated_bounded_and_nonretained(self) -> None:
    handler = _find_function(self.tree, "_async_handle_state_history_request")
    calls = _called_names(handler)
    source = ast.get_source_segment(self.source, handler)

    self.assertIsNotNone(source)
    self.assertIn("parse_state_history_request", calls)
    self.assertIn("build_state_history_response", calls)
    self.assertIn("build_state_history_error", calls)
    self.assertIn("fetch_bounded_state_history", calls)
    self.assertIn("async_add_executor_job", calls)
    self.assertIn('entity_id.startswith("sensor.")', source)
    self.assertIn("entity_id not in self.sensors", source)
    self.assertIn('"entity_not_configured"', source)
    self.assertIn("history_complete=history_complete", source)
    self.assertIn("STATE_HISTORY_MAX_RESPONSE_BYTES", source)
    self.assertIn("ensure_ascii=False", source)
    self.assertIn("qos=0", source)
    self.assertIn("retain=False", source)
    self.assertNotIn("get_significant_states", source)
    self.assertNotIn("minimal_response", source)

  def test_sensor_metadata_and_live_payload_use_state_helpers(self) -> None:
    meta_builder = _find_function(self.tree, "_build_sensor_meta")
    state_builder = _find_function(self.tree, "_build_state_payload")
    meta_source = ast.get_source_segment(self.source, meta_builder)
    state_source = ast.get_source_segment(self.source, state_builder)

    self.assertIsNotNone(meta_source)
    self.assertIsNotNone(state_source)
    self.assertIn('entry["state_kind"] = sensor_state_kind(state)', meta_source)
    self.assertIn('entity_id.startswith("sensor.")', state_source)
    self.assertIn("return sensor_live_state_payload(state)", state_source)
    self.assertLess(
      state_source.index("return sensor_live_state_payload(state)"),
      state_source.index('return state.state.replace(",", ".")'),
    )

  def test_state_kind_is_additive_and_absent_state_keeps_legacy_meta_shape(self) -> None:
    meta_builder = _find_function(self.tree, "_build_sensor_meta")
    meta_source = ast.get_source_segment(self.source, meta_builder)

    self.assertIsNotNone(meta_source)
    for legacy_field in ("entity_id", "unit", "name", "value", "icon"):
      self.assertIn(f'"{legacy_field}"', meta_source)
    self.assertIn('entry["state_kind"] = sensor_state_kind(state)', meta_source)
    self.assertNotIn("if sensor_state_kind", meta_source)

    entity_loop = next(
      node
      for node in meta_builder.body
      if isinstance(node, ast.For)
      and isinstance(node.target, ast.Name)
      and node.target.id == "entity_id"
    )
    state_guard = next(node for node in entity_loop.body if isinstance(node, ast.If))
    append_statement = next(
      node
      for node in entity_loop.body
      if isinstance(node, ast.Expr)
      and isinstance(node.value, ast.Call)
      and isinstance(node.value.func, ast.Attribute)
      and node.value.func.attr == "append"
    )
    self.assertNotIn(append_statement, list(ast.walk(state_guard)))
    self.assertLess(state_guard.lineno, append_statement.lineno)


if __name__ == "__main__":
  unittest.main()
