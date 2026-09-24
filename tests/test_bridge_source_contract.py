"""Source-level regressions for Bridge wiring that does not require HA imports."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest


BRIDGE_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tab5_lvgl"
    / "__init__.py"
)


def _bridge_tree() -> ast.Module:
    return ast.parse(BRIDGE_SOURCE.read_text(encoding="utf-8"))


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


def _logger_calls(function: ast.AST) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "_LOGGER":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if isinstance(node.args[0].value, str):
            calls.append((node.func.attr, node.args[0].value))
    return calls


class BridgeSourceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = _bridge_tree()

    def test_climate_and_cover_handlers_use_validated_helpers(self) -> None:
        climate_handler = _find_function(self.tree, "_async_handle_climate_command")
        climate_state = _find_function(self.tree, "_build_state_payload")
        cover_handler = _find_function(self.tree, "_async_handle_cover_command")

        self.assertIn("build_climate_service_call", _called_names(climate_handler))
        self.assertIn("build_climate_state_payload", _called_names(climate_state))
        self.assertIn("cover_command_supported", _called_names(cover_handler))

    def test_successful_energy_response_is_debug_but_failures_stay_visible(self) -> None:
        handler = _find_function(self.tree, "_async_handle_energy_request")
        calls = _logger_calls(handler)

        success_calls = [
            level for level, message in calls if message.startswith("Tab5 energy response:")
        ]
        self.assertEqual(success_calls, ["debug"])
        self.assertIn(
            ("exception", "Tab5 failed to load energy manager"),
            calls,
        )
        self.assertIn(
            ("exception", "Tab5 energy: failed to fetch statistics"),
            calls,
        )
        self.assertTrue(
            any(
                level == "warning" and message.startswith("Tab5 energy request ignored")
                for level, message in calls
            )
        )

    def test_media_cover_status_is_debug_and_failures_are_rate_limited(self) -> None:
        payload_builder = _find_function(self.tree, "_async_build_state_payload")
        cover_handler = _find_function(self.tree, "_async_attach_media_cover_data")
        warning_limiter = _find_function(self.tree, "_log_media_cover_warning")

        payload_logs = _logger_calls(payload_builder)
        cover_logs = _logger_calls(cover_handler)
        self.assertIn(
            ("debug", "Tab5 media state payload for %s: %s chars, cover_data=%s"),
            payload_logs,
        )
        self.assertFalse(any(level == "warning" for level, _ in payload_logs))

        expected_debug_messages = {
            "Tab5 media cover has no artwork URL for %s",
            "Tab5 media cover fetch started for %s",
            "Tab5 media cover cache hit for %s: %s bytes",
            "Tab5 media cover converted for %s: %s %s bytes -> %s %s bytes",
            "Tab5 media cover attached for %s: %s bytes, base64=%s chars",
        }
        debug_messages = {
            message for level, message in cover_logs if level == "debug"
        }
        self.assertTrue(expected_debug_messages.issubset(debug_messages))
        self.assertFalse(any(level == "warning" for level, _ in cover_logs))
        self.assertIn("_log_media_cover_warning", _called_names(cover_handler))
        self.assertIn("monotonic", _called_names(warning_limiter))

        source = BRIDGE_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("url[:120]", source)
        self.assertIn("MEDIA_COVER_WARNING_INTERVAL_SECONDS = 15 * 60", source)
        self.assertIn("type(err).__name__", source)

    def test_device_removal_uses_stale_identifier_policy(self) -> None:
        handler = _find_function(self.tree, "async_remove_config_entry_device")

        self.assertIn("entry_device_id", _called_names(handler))
        self.assertIn("is_stale_device_entry", _called_names(handler))

    def test_runtime_sensor_feedback_is_filtered_and_upgrade_is_repaired(self) -> None:
        setup = _find_function(self.tree, "async_setup_entry")
        feedback = _find_function(self.tree, "_async_process_bridge_config")

        self.assertIn(
            "_cleanup_persisted_runtime_sensor_entities", _called_names(setup)
        )
        self.assertIn("_runtime_managed_sensor_entity_ids", _called_names(feedback))
        self.assertIn("filter_runtime_sensor_entities", _called_names(feedback))
        self.assertIn("clean_stored_sensor_selections", _called_names(feedback))
        self.assertIn("should_import_feedback_selection", _called_names(feedback))

        source = ast.get_source_segment(
            BRIDGE_SOURCE.read_text(encoding="utf-8"), feedback
        )
        self.assertIsNotNone(source)
        import_filter = source.rfind("filter_runtime_sensor_entities(")
        source_import = source.rfind("SOURCE_IMPORT")
        self.assertGreater(import_filter, 0)
        self.assertGreater(source_import, import_filter)

    def test_runtime_sensor_list_is_still_published_to_panels(self) -> None:
        publisher = _find_function(self.tree, "async_publish_config_to_device")
        source = ast.get_source_segment(
            BRIDGE_SOURCE.read_text(encoding="utf-8"), publisher
        )

        self.assertIsNotNone(source)
        self.assertIn('"sensors": self.sensors', source)

    def test_local_camera_platform_contract(self) -> None:
        package = BRIDGE_SOURCE.parent
        platforms = next(
            node.value for node in self.tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PLATFORMS" for target in node.targets)
        )
        self.assertIn("camera", ast.literal_eval(platforms))
        self.assertTrue((package / "camera.py").is_file())

        camera_tree = ast.parse((package / "camera.py").read_text(encoding="utf-8"))
        subscriptions = [
            node for node in ast.walk(camera_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_subscribe"
        ]
        binary = [call for call in subscriptions
                  if any(keyword.arg == "encoding" for keyword in call.keywords)]
        self.assertEqual(len(binary), 1)
        [encoding] = [keyword.value for keyword in binary[0].keywords if keyword.arg == "encoding"]
        self.assertIsInstance(encoding, ast.Constant)
        self.assertIsNone(encoding.value)
        # Snapshot requests are never retained on the broker.
        publishes = [node for node in ast.walk(camera_tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "async_publish"]
        self.assertEqual(len(publishes), 1)
        retain = {keyword.arg: keyword.value for keyword in publishes[0].keywords}["retain"]
        self.assertIs(retain.value, False)

        for module in ("camera.py", "local_camera.py"):
            tree = ast.parse((package / module).read_text(encoding="utf-8"))
            for _level, message in _logger_calls(tree):
                self.assertTrue(message.isascii(), message)
                self.assertTrue(message.startswith("HomeTiles local camera"), message)

    def test_existing_camera_stream_contract_gains_only_the_self_loop_guard(self) -> None:
        handler = _find_function(self.tree, "_async_handle_camera_command")
        source = ast.get_source_segment(BRIDGE_SOURCE.read_text(encoding="utf-8"), handler)
        self.assertIsNotNone(source)
        self.assertIn('f"{self.base_topic}/stat/camera"', source)
        self.assertIn('"camera_self_loop"', source)
        self.assertIn('"unknown_camera"', source)
        self.assertNotIn("local_camera/", source)
        self.assertLess(source.index("_is_local_camera_self_loop"),
                        source.index("async_create_session"))

    def test_local_camera_name_is_translated_everywhere(self) -> None:
        package = BRIDGE_SOURCE.parent
        files = [package / "strings.json", *sorted((package / "translations").glob("*.json"))]
        self.assertGreaterEqual(len(files), 3)
        names = {}
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            names[path.name] = data["entity"]["camera"]["local_camera"]["name"]
        self.assertEqual(names["strings.json"], names["en.json"])
        self.assertEqual(names["de.json"], "Kamera")
        self.assertTrue(all(isinstance(name, str) and name.strip() for name in names.values()))


if __name__ == "__main__":
    unittest.main()
