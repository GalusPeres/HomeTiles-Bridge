"""Source-level regressions for Bridge wiring that does not require HA imports."""

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

    def test_device_removal_uses_stale_identifier_policy(self) -> None:
        handler = _find_function(self.tree, "async_remove_config_entry_device")

        self.assertIn("entry_device_id", _called_names(handler))
        self.assertIn("is_stale_device_entry", _called_names(handler))


if __name__ == "__main__":
    unittest.main()
