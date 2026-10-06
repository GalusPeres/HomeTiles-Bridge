"""Source-level regressions for dedicated binary-sensor Bridge wiring."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = ROOT / "custom_components" / "tab5_lvgl" / "__init__.py"
CONFIG_FLOW_SOURCE = ROOT / "custom_components" / "tab5_lvgl" / "config_flow.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name} was not found")


def _source(path: Path, node: ast.AST) -> str:
    result = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
    assert result is not None
    return result


class BinarySensorConfigContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge_tree = _tree(BRIDGE_SOURCE)
        cls.flow_tree = _tree(CONFIG_FLOW_SOURCE)

    def test_config_flow_has_dedicated_domain_selector_and_version(self) -> None:
        source = CONFIG_FLOW_SOURCE.read_text(encoding="utf-8")
        self.assertIn("VERSION = 2", source)
        self.assertIn("CONF_BINARY_SENSORS", source)
        self.assertIn('domain=["binary_sensor"]', source)
        self.assertIn('domain=["sensor"]', source)

        converter = _source(
            CONFIG_FLOW_SOURCE,
            _find_function(self.flow_tree, "_convert_entity_data"),
        )
        merger = _source(
            CONFIG_FLOW_SOURCE,
            _find_function(self.flow_tree, "_merge_all_entities"),
        )
        self.assertIn("split_binary_sensor_entities", converter)
        self.assertIn("updated[CONF_BINARY_SENSORS]", converter)
        self.assertIn("CONF_BINARY_SENSORS", merger)

    def test_entry_migration_uses_pure_backward_compatibility_helper(self) -> None:
        migration = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "async_migrate_entry"),
        )
        self.assertIn("migrate_binary_sensor_config", migration)
        self.assertIn('"version": 2', migration)
        self.assertIn("CONF_BINARY_SENSORS", migration)

    def test_runtime_tracks_and_publishes_binary_sensor_config(self) -> None:
        collector = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_collect_all_entries_entities"),
        )
        refresh = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_refresh_runtime_entity_lists"),
        )
        publisher = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "async_publish_config_to_device"),
        )

        self.assertIn("split_binary_sensor_entities", collector)
        self.assertIn('"binary_sensors": source["binary_sensors"]', refresh)
        self.assertIn("+ self.binary_sensors", refresh)
        self.assertIn("CONF_BINARY_SENSORS: self.binary_sensors", publisher)
        self.assertIn('"binary_sensor_meta": self._build_binary_sensor_meta()', publisher)

    def test_state_and_meta_use_the_structured_payload_helper(self) -> None:
        state_builder = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_build_state_payload"),
        )
        meta_builder = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_build_binary_sensor_meta"),
        )

        self.assertIn('entity_id.startswith("binary_sensor.")', state_builder)
        self.assertIn("build_binary_sensor_state_payload", state_builder)
        self.assertIn("icon=_extract_mdi_icon(state, self.hass)", state_builder)
        self.assertIn("build_binary_sensor_meta_entry", meta_builder)

        bridge_source = BRIDGE_SOURCE.read_text(encoding="utf-8")
        runtime_fields = bridge_source.split(
            "_CONFIG_META_RUNTIME_FIELDS = frozenset(", 1
        )[1].split(")", 1)[0]
        self.assertIn('"available"', runtime_fields)
        self.assertIn('"last_changed"', runtime_fields)

    def test_absent_binary_state_is_published_retained_on_snapshot_and_removal(self) -> None:
        snapshot = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "async_publish_snapshot"),
        )
        absent_publisher = _source(
            BRIDGE_SOURCE,
            _find_function(
                self.bridge_tree, "_async_publish_binary_sensor_absent_state"
            ),
        )
        state_event = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_handle_state_event"),
        )

        self.assertIn("entity_id in self.binary_sensors", snapshot)
        self.assertIn("_async_publish_binary_sensor_absent_state", snapshot)
        self.assertIn("build_binary_sensor_state_payload(None, {}, None)", absent_publisher)
        self.assertIn("retain=True", absent_publisher)
        self.assertIn("new_state is None", state_event)
        self.assertIn("entity_id in self.binary_sensors", state_event)
        self.assertIn("_async_publish_binary_sensor_absent_state", state_event)

    def test_feedback_config_accepts_only_binary_sensor_domain(self) -> None:
        parser = _source(
            BRIDGE_SOURCE,
            _find_function(self.bridge_tree, "_payload_to_entry_data"),
        )
        self.assertIn("CONF_BINARY_SENSORS", parser)
        self.assertIn('entity_id.startswith("binary_sensor.")', parser)
        self.assertIn('ValueError("invalid_binary_sensors")', parser)
        self.assertIn("split_binary_sensor_entities", parser)

    def test_all_entity_form_translations_include_binary_sensors(self) -> None:
        component = ROOT / "custom_components" / "tab5_lvgl"
        for relative in ("strings.json", "translations/en.json", "translations/de.json"):
            with self.subTest(relative=relative):
                data = json.loads((component / relative).read_text(encoding="utf-8"))
                fields = data["options"]["step"]["entities"]["data"]
                self.assertTrue(fields["binary_sensors"])


if __name__ == "__main__":
    unittest.main()
