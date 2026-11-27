"""The configuration's sensor values never overtake a newer live state.

The configuration carries every sensor's value, and a panel replaces its
stored values with them. A temperature tile kept a superseded value for days:
the Bridge read the values, waited for the Energy dashboard's sources, and
only then published, so a state change in that wait reached the panel first
and the older configuration value overwrote it. The run below is the real
async_publish_config_to_device() with Home Assistant changing the
temperature while the Bridge waits; the panel model applies the messages in
the order they leave the Bridge.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl/__init__.py"
TEMPERATURE = "sensor.og_wohnbereich_temperatur"
STATE_TOPIC = "hometiles/sensor/og_wohnbereich_temperatur/state"
CONFIG_TOPIC = "hometiles/test/bridge/apply"


def bridge_scope():
    """The publisher and the sensor meta builder of Tab5Bridge, with the
    module-level signature helpers they use."""
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    methods = [node for node in bridge.body
               if getattr(node, "name", None) in {"async_publish_config_to_device", "_build_sensor_meta"}]
    helpers = [node for node in tree.body
               if getattr(node, "name", None) in {"_config_signature", "_config_meta_key"}
               or (isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "_CONFIG_META_RUNTIME_FIELDS"
                                                        for target in node.targets))]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *helpers, *methods], type_ignores=[])
    publisher = ast.get_source_segment(source, methods[0]) + ast.get_source_segment(source, methods[1])
    return module, [node.name for node in methods], sorted(set(re.findall(r"\bCONF_\w+", publisher)))


class ConfigValueOrderTests(unittest.IsolatedAsyncioTestCase):
    def make(self, change_while_waiting: Optional[str]):
        module, names, conf_names = bridge_scope()
        self.wire: List[tuple] = []
        states: Dict[str, Any] = {}

        def set_state(value: str) -> None:
            states[TEMPERATURE] = SimpleNamespace(entity_id=TEMPERATURE, state=value, name="OG Wohnbereich",
                                                  attributes={"unit_of_measurement": "°C"})

        set_state("20.0")

        async def publish(hass, topic, payload, qos=0, retain=False):
            self.wire.append((topic, payload))

        async def load_icons(hass, ids):
            await asyncio.sleep(0)

        scope: Dict[str, Any] = {
            "json": json, "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "State": object,
            "mqtt": SimpleNamespace(async_publish=publish),
            "entity_icons": SimpleNamespace(async_load_entity_icons=load_icons),
            "posix_rule": lambda now: "CET-1CEST,M3.5.0,M10.5.0/3", "dt_util": SimpleNamespace(now=lambda: None),
            "_LOGGER": SimpleNamespace(debug=lambda *args: None),
            "_extract_mdi_icon": lambda state, hass: "mdi:thermometer",
            "_extract_media_player_mdi_icon": lambda state, hass: "mdi:speaker",
            "sensor_state_kind": lambda state: "numeric",
        }
        scope.update({name: name.lower() for name in conf_names})
        exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), scope)
        Bridge = type("Bridge", (), {name: scope[name] for name in names})
        bridge = Bridge()

        async def energy_meta(categories=None):
            # Home Assistant changes the temperature while the Bridge waits for
            # the Energy dashboard; its state handler publishes it at once
            # (_handle_state_event -> _async_publish_entity_state).
            await asyncio.sleep(0)
            if change_while_waiting is not None:
                set_state(change_while_waiting)
                self.wire.append((STATE_TOPIC, change_while_waiting))
            await asyncio.sleep(0)
            return [{"id": "solar_total", "category": "solar", "name": "PV", "unit": "kWh", "is_total": True}]

        async def nothing():
            pass

        bridge.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        bridge.config_topic = CONFIG_TOPIC
        bridge.device_id = "mac"
        bridge.base_topic = "hometiles/test"
        bridge.ha_prefix = "hometiles"
        bridge.sensors = [TEMPERATURE]
        bridge.tracked_entities = [TEMPERATURE]
        bridge._configured_sensors = [TEMPERATURE]
        for name in ("binary_sensors", "weathers", "lights", "switches", "media_players", "climates", "numbers",
                     "selects", "datetimes", "covers", "cameras", "images", "locks", "alarm_panels", "fans",
                     "vacuums", "calendars"):
            setattr(bridge, name, [])
        bridge.scene_map = {}
        bridge._config_publish_lock = asyncio.Lock()
        bridge._config_meta_cache = {}
        bridge._last_config_signature = None
        bridge._refresh_runtime_entity_lists = lambda: None
        bridge._build_binary_sensor_meta = lambda: []
        bridge._build_weather_meta = lambda: []
        bridge._build_entity_meta = lambda entities: []
        bridge._build_scene_meta = lambda: []
        bridge._energy_categories = lambda: {"solar"}
        bridge._build_energy_meta = energy_meta
        bridge._async_publish_icon_update = nothing
        return bridge

    def panel_value(self) -> Optional[str]:
        """The panel's stored value after every message, in wire order: a
        state sets it, a configuration replaces it with its own value
        (HaBridgeConfig::applyJson -> rebuildEntityIndexes)."""
        value = None
        for topic, payload in self.wire:
            if topic == STATE_TOPIC:
                value = payload
            elif topic == CONFIG_TOPIC:
                for item in json.loads(payload)["sensor_meta"]:
                    if item["entity_id"] == TEMPERATURE and "value" in item:
                        value = item["value"]
        return value

    async def test_a_change_while_the_bridge_waits_stays_on_the_panel(self):
        bridge = self.make(change_while_waiting="19.5")
        await bridge.async_publish_config_to_device(force=True)
        self.assertEqual([topic for topic, _ in self.wire], [STATE_TOPIC, CONFIG_TOPIC])
        self.assertEqual(self.panel_value(), "19.5", "the configuration must not bring back the older 20.0")
        config = json.loads(self.wire[-1][1])
        self.assertEqual(config["energy"][0]["id"], "solar_total", "the Energy sources still travel along")

    async def test_without_a_change_the_configuration_carries_the_current_value(self):
        bridge = self.make(change_while_waiting=None)
        await bridge.async_publish_config_to_device(force=True)
        self.assertEqual(self.panel_value(), "20.0")

    def test_no_wait_between_reading_the_values_and_publishing(self):
        """Every await of the publisher before its configuration is read, except
        the publish itself: the values leave as they were read."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
        publisher = next(node for node in bridge.body if getattr(node, "name", None) == "async_publish_config_to_device")
        built = next(node for node in ast.walk(publisher)
                     if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "config_data")
        published = next(node for node in ast.walk(publisher)
                         if isinstance(node, ast.Await) and "mqtt.async_publish" in ast.unparse(node.value.func))
        between = [node for node in ast.walk(publisher)
                   if isinstance(node, ast.Await) and built.lineno < node.lineno < published.lineno]
        self.assertEqual([ast.unparse(node) for node in between], [])


if __name__ == "__main__":
    unittest.main()
