"""Exercise the live icon cache after delayed entity registration and edits."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"


def helpers():
    names = {"_prime_icon_cache", "_async_publish_icon_update", "_extract_mdi_icon",
             "_normalize_mdi_icon_value", "_fallback_icon_from_state", "_build_entity_meta"}
    nodes = [n for n in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    async def load_icons(hass, entities):
        return None
    # Icon translations are covered by test_entity_icons.py.
    no_translations = SimpleNamespace(async_load_entity_icons=load_icons,
                                      cached_entity_icon=lambda *args, **kwargs: None,
                                      cached_component_icon=lambda *args, **kwargs: None)
    scope = {"json": json, "entity_icons": no_translations,
             "er": SimpleNamespace(async_get=lambda hass: hass.registry),
             "_is_weather_entity": lambda entity: entity.startswith("weather.")}
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(SOURCE), "exec"), scope)
    return scope


class IconRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_icons_are_not_erased_by_a_stale_startup_cache(self):
        scope = helpers()
        states, entries, published = {}, {}, []
        async def publish(hass, topic, payload, **kwargs):
            published.append(json.loads(payload))
        scope["mqtt"] = SimpleNamespace(async_publish=publish)
        hass = SimpleNamespace(states=SimpleNamespace(get=states.get),
                               registry=SimpleNamespace(async_get=entries.get))
        runtime = SimpleNamespace(hass=hass, tracked_entities=["select.view", "time.end"],
                                  _icon_cache={}, icons_topic="device/bridge/icons")
        runtime._prime_icon_cache = lambda: scope["_prime_icon_cache"](runtime)
        runtime._prime_icon_cache()
        self.assertEqual(runtime._icon_cache, {"select.view": "", "time.end": ""})
        for entity, icon in [("select.view", "mdi:view-dashboard"), ("time.end", "mdi:clock-end")]:
            states[entity] = SimpleNamespace(entity_id=entity, state="17:00:00", name=entity,
                                            attributes={"icon": icon})
        config = scope["_build_entity_meta"](runtime, runtime.tracked_entities)
        self.assertEqual(config[1]["icon"], "mdi:clock-end")
        await scope["_async_publish_icon_update"](runtime)
        self.assertEqual(published[-1], {item["entity_id"]: item["icon"] for item in config})
        entries["time.end"] = SimpleNamespace(icon="mdi:clock-start")
        await scope["_async_publish_icon_update"](runtime)
        self.assertEqual(published[-1]["time.end"], "mdi:clock-start")
        entries["time.end"].icon = None
        await scope["_async_publish_icon_update"](runtime)
        self.assertEqual(published[-1]["time.end"], "mdi:clock-end")
        states["time.end"].attributes.clear()
        await scope["_async_publish_icon_update"](runtime)
        self.assertEqual(published[-1]["time.end"], "")


if __name__ == "__main__":
    unittest.main()
