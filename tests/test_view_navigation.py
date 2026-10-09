"""Executable navigation state, MQTT lifecycle and HA select regressions."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VIEW = load_module("view_navigation")
SESSION = "a" * 32


def state(**changes):
    return dict({"session": SESSION, "revision": 1, "ready": True,
                 "sequence": 0, "uptime": 10000, "current": "home",
                 "mode": "folder"}, **changes)


def catalog(**changes):
    return dict({"session": SESSION, "revision": 1, "page": 0, "pages": 1,
                 "targets": [{"id": "home", "label": "Home"},
                             {"id": "folder:1", "label": "Home / Room [f:1]"},
                             {"id": "tile:12", "label": "Home / Room / Door [t:12]"}]}, **changes)


class ViewNavigationTest(unittest.TestCase):
    def setUp(self):
        self.view = VIEW.ViewNavigation()
        self.view.online = True
        self.assertTrue(self.view.state(state(), 10))
        self.assertTrue(self.view.catalog(catalog()))

    def test_automation_camera_then_home_requires_device_feedback(self):
        command = self.view.command("Home / Room / Door [t:12]", 11)
        self.assertEqual(command["target"], "tile:12")
        self.assertEqual(command["deadline"], 16000)
        self.assertEqual(self.view.current, "home")
        self.view.state(state(current="tile:12", mode="popup", sequence=1), 11)
        self.assertEqual(self.view.current, "tile:12")
        self.assertEqual(self.view.command("Home", 12)["sequence"], 2)
        self.assertEqual(self.view.current, "tile:12")
        self.view.state(state(sequence=2), 12)
        self.assertEqual(self.view.current, "home")

    def test_camera_full_screen_option_for_doorbell_automations(self):
        # b27 (#65): a camera tile also opens straight in full screen; the
        # panel lists it under "full_targets" (older Bridges ignore the key).
        full = {"id": "full:12", "label": "Home / Room / Door (Full screen) [t:12]"}
        self.assertTrue(self.view.catalog(catalog(full_targets=[full])))
        self.assertEqual(self.view.targets["full:12"], full["label"])
        command = self.view.command(full["label"], 11)
        self.assertEqual(command["target"], "full:12")
        # The panel reports its full screen next to the tile's target.
        self.assertTrue(self.view.state(state(current="tile:12", mode="popup", full=True,
                                              sequence=1), 11))
        self.assertEqual(self.view.current, "full:12")
        self.assertEqual(self.view.current_label(), full["label"])
        self.assertTrue(self.view.state(state(current="tile:12", mode="popup", sequence=1), 11))
        self.assertEqual(self.view.current_label(), "Home / Room / Door [t:12]")
        # A full screen without its option listed shows the tile's option.
        self.view.catalog(catalog())
        self.view.state(state(current="tile:12", mode="popup", full=True, sequence=1), 11)
        self.assertEqual(self.view.current_label(), "Home / Room / Door [t:12]")
        self.assertFalse(self.view.state(state(full="yes"), 11))

    def test_full_targets_are_checked(self):
        bad = [
            [{"id": "full:13", "label": "x"}],                         # no such tile here
            [{"id": "tile:12", "label": "x"}],                         # not a full target
            [{"id": "full:12", "label": "Home / Room / Door [t:12]"}], # duplicate label
            [{"id": "full:12", "label": ""}],
            "full:12",
            [{"id": f"full:{i}", "label": str(i)} for i in range(1, 66)],
        ]
        for full_targets in bad:
            view = VIEW.ViewNavigation()
            view.state(state(), 10)
            self.assertFalse(view.catalog(catalog(full_targets=full_targets)), full_targets)
        # Older firmware sends no "full_targets".
        view = VIEW.ViewNavigation()
        view.state(state(), 10)
        self.assertTrue(view.catalog(catalog()))
        self.assertNotIn("full:12", view.targets)

    def test_manual_navigation_close_pin_sleep_and_screensaver_do_not_send(self):
        for current, mode in [("folder:1", "folder"), ("tile:12", "popup"),
                              ("folder:1", "folder"), (None, "pin"),
                              (None, "sleep"), (None, "screensaver")]:
            self.assertTrue(self.view.state(state(current=current, mode=mode), 10))
            self.assertEqual(self.view.current, current)
            self.assertEqual(self.view.sequence, 0)

    def test_rename_move_delete_and_duplicate_names_use_stable_ids(self):
        self.view.state(state(revision=2), 10)
        self.assertFalse(self.view.available(10))
        self.view.catalog(catalog(revision=2, targets=[
            {"id": "home", "label": "Home"},
            {"id": "tile:12", "label": "Home / Upstairs / Door [t:12]"},
            {"id": "tile:13", "label": "Home / Upstairs / Door [t:13]"}]))
        self.assertEqual(self.view.command("Home / Upstairs / Door [t:12]", 10)["target"], "tile:12")
        with self.assertRaises(ValueError):
            self.view.command("Home / Room / Door [t:12]", 10)
        self.view.state(state(revision=3), 10)
        self.view.catalog(catalog(revision=3, targets=[{"id": "home", "label": "Home"}]))
        self.assertNotIn("tile:12", self.view.targets)

    def test_reboot_reconnect_retained_offline_and_expired_state(self):
        self.view.state(state(session="b" * 32), 11, retained=True)
        self.assertFalse(self.view.catalog(catalog()))
        self.view.catalog(catalog(session="b" * 32))
        self.assertFalse(self.view.available(11))
        self.view.state(state(session="b" * 32), 12)
        self.assertTrue(self.view.available(12))
        self.assertFalse(self.view.available(33))
        self.view.online = False
        with self.assertRaises(ValueError):
            self.view.command("Home", 12)

    def test_out_of_order_pages_are_atomic_and_bounded(self):
        self.view.state(state(revision=2), 10)
        self.assertTrue(self.view.catalog(catalog(revision=2, page=1, pages=2,
            targets=[{"id": "folder:3", "label": "Room [f:3]"}])))
        self.assertFalse(self.view.available(10))
        self.assertTrue(self.view.catalog(catalog(revision=2, page=0, pages=2)))
        self.assertTrue(self.view.available(10))
        for invalid in [catalog(pages=129), catalog(targets=[]), catalog(page=True),
                        catalog(targets=[{"id": "bad", "label": "Home"}]),
                        catalog(targets=[{"id": "home", "label": "x" * 256}])]:
            self.assertFalse(self.view.catalog(invalid))

    def test_bad_state_and_millis_wrap(self):
        for invalid in [state(session="bad"), state(sequence=True), state(uptime=-1),
                        state(revision=0), state(mode="bad"), state(current="tile:-1")]:
            self.assertFalse(self.view.state(invalid, 10))
        self.view.state(state(uptime=2**32 - 100), 10)
        self.assertEqual(self.view.command("Home", 10)["deadline"], 4900)


class SelectLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_select_callbacks_publish_only_explicit_commands(self):
        subscriptions = {}
        removed = []
        now = [10.0]
        async def subscribe(hass, topic, callback, **kwargs):
            subscriptions[topic] = callback
            return lambda: removed.append(topic)
        class Entity:
            async def async_added_to_hass(self): pass
            async def async_will_remove_from_hass(self): pass
            def async_write_ha_state(self): pass
        class HAError(Exception):
            def __init__(self, **kwargs): self.key = kwargs["translation_key"]
        mqtt = types.SimpleNamespace(async_subscribe=subscribe,
            async_publish=AsyncMock(), is_connected=lambda hass: True)
        scope = {"SelectEntity": Entity, "HomeAssistantError": HAError,
                 "mqtt": mqtt, "ViewNavigation": VIEW.ViewNavigation,
                 "monotonic": lambda: now[0], "json": json,
                 "entry_device_info": lambda entry: {"identifiers": {("tab5_lvgl", entry.device_id)}},
                 "entry_device_id": lambda entry: entry.device_id,
                 "command_topic": lambda base, leaf: f"{base}/cmnd/{leaf}",
                 "state_topic": lambda base, leaf: f"{base}/stat/{leaf}",
                 "timedelta": lambda **kwargs: kwargs,
                 "async_track_time_interval": lambda *args: lambda: removed.append("timer")}
        tree = ast.parse((ROOT / "select.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HomeTilesViewSelect")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "select.py", "exec"), scope)
        entity = scope["HomeTilesViewSelect"](types.SimpleNamespace(device_id="panel-a"), "panel/a")
        entity.hass = object()
        await entity.async_added_to_hass()
        self.assertEqual(entity._attr_device_info["identifiers"], {("tab5_lvgl", "panel-a")})
        message = lambda payload, retain=False: types.SimpleNamespace(payload=payload, retain=retain)
        await subscriptions["panel/a/stat/connected"](message("ON", True))
        await subscriptions["panel/a/stat/view"](message(json.dumps(state())))
        await subscriptions["panel/a/view/catalog/+"](message(json.dumps(catalog()), True))
        self.assertTrue(entity.available)
        mqtt.async_publish.reset_mock()
        await entity.async_select_option("Home / Room / Door [t:12]")
        self.assertEqual(entity.current_option, "Home")
        args, kwargs = mqtt.async_publish.call_args
        self.assertEqual(args[1], "panel/a/cmnd/view")
        self.assertEqual(json.loads(args[2])["target"], "tile:12")
        self.assertEqual(kwargs, {"qos": 0, "retain": False})
        mqtt.async_publish.reset_mock()
        await subscriptions["panel/a/stat/view"](message(json.dumps(state(current="tile:12", mode="popup"))))
        await subscriptions["panel/a/stat/view"](message(json.dumps(state(current="folder:1"))))
        mqtt.async_publish.assert_not_called()
        await subscriptions["panel/a/stat/connected"](message("OFF"))
        self.assertFalse(entity.available)
        with self.assertRaises(HAError):
            await entity.async_select_option("Home")
        await entity.async_will_remove_from_hass()
        self.assertEqual(len(removed), 4)


if __name__ == "__main__":
    unittest.main()
