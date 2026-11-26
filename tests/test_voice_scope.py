"""voice_scope.py and voice_api.py: what a panel's voice assistant may use.

The "HomeTiles room control" LLM API gives each panel its own lists
("Voice can control", "Voice can read") instead of Home Assistant's one
exposure list for every voice device. The tests cover the list rules, name
matching, every planned service call and its refusals, and the API wiring
with a stubbed Home Assistant: a call outside the speaking panel's lists
never reaches a service, a request without a panel gets no tools, and the
start-timer tool cannot carry a command for the default agent.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from test_link_protocol import load_link_module

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl"
SCOPE = load_link_module("voice_scope")
Facts = SCOPE.EntityFacts


def scoped(name="Lamp", entity_id="light.lamp", area="Kids room", control=True, aliases=()):
    return SCOPE.ScopedEntity(entity_id=entity_id, name=name, area=area, aliases=aliases, can_control=control)


class ScopeRuleTests(unittest.TestCase):
    def test_config_lists(self):
        scope = SCOPE.scope_from_config({
            "voice_control_areas": ["kids", "kids", " "], "voice_control_labels": "kids_voice",
            "voice_control_entities": ["light.hall", "not an id"], "voice_read_entities": ["sensor.outside"],
        })
        self.assertEqual(scope.control.areas, {"kids"})
        self.assertEqual(scope.control.labels, {"kids_voice"})
        self.assertEqual(scope.control.entities, {"light.hall"}, "Only valid entity ids")
        self.assertEqual(scope.read.entities, {"sensor.outside"})
        self.assertTrue(SCOPE.scope_from_config({}).empty, "Nothing selected allows nothing")
        self.assertTrue(SCOPE.scope_from_config(None).empty)

    def test_areas_labels_and_entities(self):
        scope = SCOPE.scope_from_config({
            "voice_control_areas": ["kids"], "voice_control_labels": ["kids_voice"],
            "voice_control_entities": ["light.hall"], "voice_read_areas": ["garden"],
            "voice_read_entities": ["lock.front", "sensor.outside"],
        })
        facts = [
            Facts("light.kids", "Kids light", area_id="kids", area_name="Kids room"),
            Facts("light.porch", "Porch", area_id="outside", label_ids=("kids_voice",)),
            Facts("light.hall", "Hall light", area_id="hall"),
            Facts("light.kitchen", "Kitchen light", area_id="kitchen"),
            Facts("switch.pump", "Pump", area_id="garden"),
            Facts("sensor.kids_temp", "Kids temperature", area_id="kids"),
            Facts("lock.front", "Front door", area_id="hall"),
            Facts("lock.kids_window", "Window lock", area_id="kids"),
            Facts("sensor.outside", "Outside"),
            Facts("light.kids_hidden", "Hidden", area_id="kids", hidden=True),
            Facts("light.hall_hidden", "Hidden hall", area_id="hall", hidden=True),
            Facts("button.kids_factory_reset", "Factory reset", area_id="kids", auxiliary=True),
            Facts("light.status_led", "Status LED", area_id="kids", auxiliary=True),
        ]
        result = SCOPE.scoped_entities(scope, facts)
        control = {entity_id for entity_id, item in result.items() if item.can_control}
        read = set(result) - control
        self.assertEqual(control, {"light.kids", "light.porch", "light.hall"})
        self.assertEqual(read, {"switch.pump", "sensor.kids_temp", "lock.front", "lock.kids_window", "sensor.outside"},
                         "Read lists, sensors in a control area, and locks are only read")
        self.assertNotIn("light.kitchen", result, "Outside every list")
        self.assertNotIn("light.kids_hidden", result, "Hidden entities only when named one by one")
        self.assertNotIn("button.kids_factory_reset", result, "Config and diagnostic entities neither")
        self.assertNotIn("light.status_led", result)


class ResolveTests(unittest.TestCase):
    ENTITIES = {
        "light.kids": scoped("Ceiling", "light.kids", "Kids room"),
        "light.hall": scoped("Ceiling", "light.hall", "Hall"),
        "light.desk": scoped("Desk lamp", "light.desk", "Kids room", aliases=("Reading light",)),
        "sensor.outside": scoped("Outside temperature", "sensor.outside", "", control=False),
    }

    def resolve(self, query, area=None):
        return SCOPE.resolve_entity(query, self.ENTITIES, area=area).entity_id

    def test_ids_names_aliases_and_area(self):
        self.assertEqual(self.resolve("light.desk"), "light.desk")
        self.assertEqual(self.resolve("desk lamp"), "light.desk")
        self.assertEqual(self.resolve("Reading Light!"), "light.desk")
        self.assertEqual(self.resolve("hall ceiling"), "light.hall", "Area before the name")
        self.assertEqual(self.resolve("Ceiling", area="Kids room"), "light.kids")
        self.assertEqual(self.resolve("outside temperature"), "sensor.outside")

    def test_refusals(self):
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "more than one"):
            self.resolve("ceiling")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "not something"):
            self.resolve("kitchen light")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "not something"):
            self.resolve("light.kitchen")
        for empty in (None, "", "  ", 5):
            with self.assertRaises(SCOPE.VoiceRefusal):
                self.resolve(empty)


class SpokenNameTests(unittest.TestCase):
    """The model must not swap a named device for a similar allowed one."""

    FACTS = [
        Facts("light.desk_lights", "Desk Lights", area_id="office", area_name="Office"),
        Facts("light.worktable_left", "Worktable Light Left", area_id="office", area_name="Office"),
        Facts("light.office_ceiling", "Ceiling Light", area_id="office", area_name="Office"),
        Facts("light.kitchen_ceiling", "Ceiling Light", area_id="kitchen", area_name="Kitchen"),
        Facts("light.desk_lamp", "Desk lamp", area_id="den", area_name="Den"),
        Facts("switch.worktable_saw", "Worktable", area_id="garage", area_name="Garage"),
    ]

    def setUp(self):
        scope = SCOPE.scope_from_config({"voice_control_entities": ["light.desk_lights", "light.office_ceiling"]})
        self.entities = SCOPE.scoped_entities(scope, self.FACTS)

    def check(self, spoken, entity_id):
        SCOPE.check_spoken_name(spoken, self.entities[entity_id], self.FACTS, self.entities)

    def test_swapped_devices_are_refused(self):
        for spoken, chosen in (("worktable light", "light.desk_lights"), ("the worktable lights", "light.desk_lights"),
                               ("kitchen ceiling light", "light.office_ceiling")):
            with self.subTest(spoken=spoken), self.assertRaisesRegex(SCOPE.VoiceRefusal, "do not use another"):
                self.check(spoken, chosen)

    def test_fitting_names_pass(self):
        for spoken, chosen in (("desk lights", "light.desk_lights"), ("Desk light", "light.desk_lights"),
                               ("ceiling light", "light.office_ceiling"), ("office ceiling light", "light.office_ceiling"),
                               ("the light", "light.desk_lights"), (None, "light.desk_lights"), ("", "light.desk_lights")):
            with self.subTest(spoken=spoken):
                self.check(spoken, chosen)


class PlanTests(unittest.TestCase):
    def test_turn(self):
        plan = SCOPE.plan_turn
        self.assertEqual(plan(scoped(), True, "off", {}), ("light", "turn_on", {"entity_id": "light.lamp"}))
        self.assertEqual(plan(scoped(), False, "on", {})[1], "turn_off")
        self.assertEqual(plan(scoped("Movie", "scene.movie"), True, "unknown", {})[:2], ("scene", "turn_on"))
        self.assertEqual(plan(scoped("Bell", "button.bell"), True, "unknown", {})[:2], ("button", "press"))
        self.assertEqual(plan(scoped("Blind", "cover.blind"), True, "closed", {"supported_features": 3})[:2],
                         ("cover", "open_cover"))
        self.assertEqual(plan(scoped("Fan", "fan.f"), True, "off", {"supported_features": 48})[:2], ("fan", "turn_on"))
        cases = [
            (scoped(control=False), True, "off", {}, "only read"),
            (scoped(), True, "unavailable", {}, "unavailable"),
            (scoped(), True, None, {}, "unavailable"),
            (scoped("Movie", "scene.movie"), False, "unknown", {}, "only be activated"),
            (scoped("Blind", "cover.blind"), False, "open", {"supported_features": 1}, "cannot be closed"),
            (scoped("Fan", "fan.f"), True, "off", {"supported_features": 0}, "cannot be turned on"),
            (scoped("TV", "media_player.tv"), False, "on", {"supported_features": 128}, "cannot be turned off"),
            (scoped("Temp", "sensor.t"), True, "20", {}, "cannot be turned"),
        ]
        for item, on, state, attributes, message in cases:
            with self.subTest(item=item.entity_id, on=on), self.assertRaisesRegex(SCOPE.VoiceRefusal, message):
                plan(item, on, state, attributes)

    def test_locks_are_never_controlled(self):
        scope = SCOPE.scope_from_config({"voice_control_entities": ["lock.front", "alarm_control_panel.home"]})
        result = SCOPE.scoped_entities(scope, [Facts("lock.front", "Front"), Facts("alarm_control_panel.home", "Alarm")])
        self.assertFalse(any(item.can_control for item in result.values()))
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "only read"):
            SCOPE.plan_turn(result["lock.front"], False, "locked", {})

    def test_light(self):
        color = {"supported_color_modes": ["hs", "color_temp"], "min_color_temp_kelvin": 2200,
                 "max_color_temp_kelvin": 6000}
        plan = SCOPE.plan_light
        self.assertEqual(plan(scoped(), "on", color, brightness="40"),
                         ("light", "turn_on", {"entity_id": "light.lamp", "brightness_pct": 40}))
        self.assertEqual(plan(scoped(), "on", color, brightness=0)[2]["brightness_pct"], 1, "Clamped")
        self.assertEqual(plan(scoped(), "on", color, brightness=500)[2]["brightness_pct"], 100)
        self.assertEqual(plan(scoped(), "on", color, color="Blue")[2]["color_name"], "blue")
        self.assertEqual(plan(scoped(), "on", color, color="warm white")[2], {"entity_id": "light.lamp",
                                                                              "color_temp_kelvin": 2700})
        self.assertEqual(plan(scoped(), "on", color, kelvin=9000)[2]["color_temp_kelvin"], 6000)
        onoff = {"supported_color_modes": ["onoff"]}
        for kwargs, message in (({"brightness": 50}, "cannot be dimmed"), ({"color": "red"}, "cannot change color"),
                                ({"kelvin": 3000}, "no color temperature"), ({"color": "chartreuse"}, "not a color"),
                                ({"brightness": True}, "must be a number"), ({"brightness": "nan"}, "must be a number")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(SCOPE.VoiceRefusal, message):
                plan(scoped(), "on", onoff, **kwargs)
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "not a light"):
            plan(scoped("Pump", "switch.pump"), "on", {}, brightness=10)

    def test_cover(self):
        item = scoped("Blind", "cover.blind")
        plan = SCOPE.plan_position
        self.assertEqual(plan(item, "open", {"supported_features": 15}, position=130),
                         ("cover", "set_cover_position", {"entity_id": "cover.blind", "position": 100}))
        self.assertEqual(plan(item, "open", {"supported_features": 15}, command="stop")[1], "stop_cover")
        self.assertEqual(plan(scoped("Water", "valve.water"), "open", {"supported_features": 2}, command="close")[1],
                         "close_valve")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "cannot move to a position"):
            plan(item, "open", {"supported_features": 3}, position=50)
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "cannot stop"):
            plan(item, "open", {"supported_features": 3}, command="stop")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "Say open"):
            plan(item, "open", {"supported_features": 15}, command="tilt")

    def test_climate(self):
        item = scoped("Heat", "climate.kids")
        attributes = {"supported_features": 1, "min_temp": 7, "max_temp": 30, "target_temp_step": 0.5,
                      "hvac_modes": ["off", "heat", "auto"]}
        plan = SCOPE.plan_climate
        self.assertEqual(plan(item, "heat", attributes, temperature=21.3),
                         ("climate", "set_temperature", {"entity_id": "climate.kids", "temperature": 21.5}))
        self.assertEqual(plan(item, "heat", attributes, temperature=99)[2]["temperature"], 30, "Clamped")
        self.assertEqual(plan(item, "heat", attributes, hvac_mode="Auto"),
                         ("climate", "set_hvac_mode", {"entity_id": "climate.kids", "hvac_mode": "auto"}))
        self.assertEqual(plan(item, "off", attributes, temperature=20, hvac_mode="heat")[2],
                         {"entity_id": "climate.kids", "temperature": 20.0, "hvac_mode": "heat"})
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "supports these modes"):
            plan(item, "heat", attributes, hvac_mode="cool")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "no single target"):
            plan(item, "heat", dict(attributes, supported_features=2), temperature=20)
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "Say a temperature"):
            plan(item, "heat", attributes)

    def test_media_and_fan(self):
        tv = scoped("TV", "media_player.tv")
        self.assertEqual(SCOPE.plan_media(tv, "playing", {"supported_features": 4}, volume=35),
                         ("media_player", "volume_set", {"entity_id": "media_player.tv", "volume_level": 0.35}))
        self.assertEqual(SCOPE.plan_media(tv, "playing", {"supported_features": 1}, command="pause")[1], "media_pause")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "cannot next"):
            SCOPE.plan_media(tv, "playing", {"supported_features": 1}, command="next")
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "no volume"):
            SCOPE.plan_media(tv, "playing", {"supported_features": 1}, volume=10)
        fan = scoped("Fan", "fan.kids")
        self.assertEqual(SCOPE.plan_fan(fan, "on", {"supported_features": 1}, percentage="33.4")[2]["percentage"], 33)
        with self.assertRaisesRegex(SCOPE.VoiceRefusal, "no speed"):
            SCOPE.plan_fan(fan, "on", {"supported_features": 48}, percentage=50)

    def test_state_and_prompt(self):
        summary = SCOPE.state_summary(scoped(), "on", {"brightness": 128, "friendly_name": "x", "entity_picture": "/a"})
        self.assertEqual(summary, {"name": "Lamp", "entity_id": "light.lamp", "area": "Kids room", "state": "on",
                                   "can_control": True, "attributes": {"brightness_pct": 50}})
        entities = {"light.lamp": scoped(), "sensor.out": scoped("Outside", "sensor.out", "", control=False)}
        prompt = SCOPE.build_prompt("Kids panel", entities, {"light.lamp": "on"}, "Kids room")
        self.assertIn("'Kids panel' in the area 'Kids room'", prompt)
        self.assertIn("- Lamp; light.lamp; Kids room; control; on", prompt)
        self.assertIn("- Outside; sensor.out; -; read; unavailable", prompt)
        self.assertIn("No devices", SCOPE.build_prompt("P", {}, {}))
        many = {f"light.l{i}": scoped(f"L{i}", f"light.l{i}") for i in range(SCOPE.MAX_PROMPT_ENTITIES + 5)}
        self.assertIn("(5 more devices", SCOPE.build_prompt("P", many, {}))


# ---------------------------------------------------------------------------
#  voice_api.py with a stubbed Home Assistant
# ---------------------------------------------------------------------------

class Marker(str):
    def __new__(cls, key, description=None, default=None):
        marker = super().__new__(cls, key)
        marker.description = description
        return marker


class FakeSchema:
    def __init__(self, schema):
        self.schema = dict(schema)


def fake_vol():
    module = types.ModuleType("probatio")
    module.Schema = FakeSchema
    module.Required = Marker
    module.Optional = Marker
    module.Coerce = lambda kind: kind
    module.In = lambda values: tuple(values)
    return module


class FakeTool:
    name = ""
    description = None
    parameters = FakeSchema({})
    integration = None


class FakeIntentTool(FakeTool):
    def __init__(self, name, handler):
        self.name = name
        self.intent_type = handler.intent_type
        self.parameters = FakeSchema(handler.slot_schema)

    async def async_call(self, hass, tool_input, llm_context):
        hass.intents.append((self.intent_type, dict(tool_input.tool_args), llm_context.device_id))
        return {"intent": self.intent_type}


class FakeAPI:
    def __init__(self, *, hass, id, name, requires_admin=False):
        self.hass, self.id, self.name = hass, id, name


class FakeInstance:
    def __init__(self, api, api_prompt, llm_context, tools, custom_serializer=None):
        self.api, self.api_prompt, self.llm_context, self.tools = api, api_prompt, llm_context, tools


class FakeResult:
    def __init__(self, data, error=False):
        self.data, self.error = data, error


class FakeAnnotations:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_voice_api(handlers):
    package_name = "_hometiles_voice_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    llm = types.ModuleType("homeassistant.helpers.llm")
    llm.Tool, llm.IntentTool, llm.API, llm.APIInstance = FakeTool, FakeIntentTool, FakeAPI, FakeInstance
    llm.ToolResult, llm.ToolAnnotations = FakeResult, FakeAnnotations
    llm.probatio = fake_vol()
    llm.registered = []
    llm.async_register_api = lambda hass, api: llm.registered.append(api) or (lambda: None)
    intent = types.ModuleType("homeassistant.helpers.intent")
    intent.async_get = lambda hass: handlers
    registries = {}
    for name in ("area_registry", "device_registry", "entity_registry"):
        module = types.ModuleType(f"homeassistant.helpers.{name}")
        module.async_get = (lambda key: lambda hass: hass.registries[key])(name)
        registries[name] = module
    registries["device_registry"].DeviceInfo = dict
    helpers = types.ModuleType("homeassistant.helpers")
    for name, module in registries.items():
        setattr(helpers, name, module)
    helpers.intent, helpers.llm = intent, llm
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    import datetime
    dt.now = lambda: datetime.datetime(2026, 11, 26, 7, 5)
    util.dt = dt
    timers = types.ModuleType("homeassistant.components.intent.timers")
    timers.async_device_supports_timers = lambda hass, device_id: device_id in hass.timer_devices
    stubs = {
        package_name: package,
        "homeassistant": types.ModuleType("homeassistant"), "homeassistant.core": core,
        "homeassistant.config_entries": config_entries, "homeassistant.helpers": helpers,
        "homeassistant.helpers.llm": llm, "homeassistant.helpers.intent": intent,
        "homeassistant.util": util, "homeassistant.util.dt": dt,
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.intent": types.ModuleType("homeassistant.components.intent"),
        "homeassistant.components.intent.timers": timers,
        **{f"homeassistant.helpers.{name}": module for name, module in registries.items()},
    }
    with mock.patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.voice_api"), llm


class State:
    def __init__(self, entity_id, state, name, **attributes):
        self.entity_id, self.state, self.name, self.attributes = entity_id, state, name, attributes


class RegEntry(types.SimpleNamespace):
    pass


class Registry:
    def __init__(self, items):
        self.items = items

    def async_get(self, key):
        return self.items.get(key)

    def async_get_area(self, key):
        return self.items.get(key)


class Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, data))


class FakeHass:
    def __init__(self, entries):
        self.services = Services()
        self.intents = []
        self.timer_devices = {"dev_kids"}
        self._states = {
            "light.kids": State("light.kids", "off", "Kids light", supported_color_modes=["brightness"]),
            "light.kitchen": State("light.kitchen", "off", "Kitchen light", supported_color_modes=["brightness"]),
            "sensor.outside": State("sensor.outside", "4.5", "Outside temperature", unit_of_measurement="°C"),
        }
        self.states = types.SimpleNamespace(async_all=lambda: list(self._states.values()),
                                            get=lambda entity_id: self._states.get(entity_id))
        self.registries = {
            "entity_registry": Registry({
                "light.kids": RegEntry(area_id="kids", device_id=None, labels=set(), aliases=set(), hidden_by=None),
                "light.kitchen": RegEntry(area_id="kitchen", device_id=None, labels=set(), aliases=set(),
                                          hidden_by=None),
                "sensor.outside": RegEntry(area_id=None, device_id=None, labels=set(), aliases=set(), hidden_by=None),
            }),
            "device_registry": Registry({
                "dev_kids": RegEntry(config_entries={"e_kids"}, area_id="kids"),
                "dev_other": RegEntry(config_entries={"e_other"}, area_id=None),
            }),
            "area_registry": Registry({"kids": RegEntry(name="Kids room"), "kitchen": RegEntry(name="Kitchen")}),
        }
        self.config_entries = types.SimpleNamespace(async_get_entry=lambda entry_id: entries.get(entry_id))


def panel_entry(entry_id, **data):
    return types.SimpleNamespace(entry_id=entry_id, domain="tab5_lvgl", data=data, options={})


def context(device_id):
    return types.SimpleNamespace(device_id=device_id, context=None, platform="ollama", assistant="conversation",
                                 language="en")


def call(tool, hass, device_id, **args):
    tool_input = types.SimpleNamespace(tool_name=tool.name, tool_args=args)
    return asyncio.run(tool.async_call(hass, tool_input, context(device_id)))


class ApiWiringTests(unittest.TestCase):
    def setUp(self):
        handlers = [types.SimpleNamespace(intent_type="HassStartTimer",
                                          slot_schema={"minutes": int, "name": str, "conversation_command": str}),
                    types.SimpleNamespace(intent_type="HassTurnOn", slot_schema={"name": str})]
        self.api_module, self.llm = load_voice_api(handlers)
        self.hass = FakeHass({
            "e_kids": panel_entry("e_kids", device_name="Kids panel", voice_control_areas=["kids"],
                                  voice_read_entities=["sensor.outside"]),
            "e_other": panel_entry("e_other", device_name="Empty panel"),
        })
        self.api = self.api_module.HomeTilesRoomAPI(self.hass)

    def instance(self, device_id):
        return asyncio.run(self.api.async_get_api_instance(context(device_id)))

    def tool(self, instance, suffix):
        return next(tool for tool in instance.tools if tool.name == f"tab5_lvgl__{suffix}")

    def test_registers_once(self):
        data = {}
        self.api_module.async_register_voice_api(self.hass, data)
        self.api_module.async_register_voice_api(self.hass, data)
        self.assertEqual([api.id for api in self.llm.registered], ["tab5_lvgl_room"])
        self.assertEqual(self.llm.registered[0].name, "HomeTiles room control")

    def test_prompt_and_tools_follow_the_speaking_panel(self):
        kids = self.instance("dev_kids")
        self.assertIn("- Kids light; light.kids; Kids room; control; off", kids.api_prompt)
        self.assertIn("- Outside temperature; sensor.outside; -; read; 4.5", kids.api_prompt)
        self.assertNotIn("kitchen", kids.api_prompt)
        names = {tool.name for tool in kids.tools}
        self.assertIn("tab5_lvgl__turn_on", names)
        self.assertIn("tab5_lvgl__HassStartTimer", names, "Timers on a timer device")
        self.assertNotIn("tab5_lvgl__HassTurnOn", names, "Only timer intents, never HA's entity intents")
        self.assertTrue(all(tool.name.startswith("tab5_lvgl__") and tool.integration == "tab5_lvgl"
                            for tool in kids.tools))
        self.assertIn("No devices", self.instance("dev_other").api_prompt)
        self.assertEqual(self.instance("dev_other").tools, [], "Empty lists, no timers on that device: no tools")
        for device in (None, "dev_unknown"):
            nothing = self.instance(device)
            self.assertEqual(nothing.tools, [])
            self.assertIn("does not come from a HomeTiles panel", nothing.api_prompt)

    def test_calls_are_checked_against_the_lists(self):
        turn_on = self.tool(self.instance("dev_kids"), "turn_on")
        result = call(turn_on, self.hass, "dev_kids", entity="light.kids", spoken_name="kids light")
        self.assertTrue(result.data["success"])
        refused = call(turn_on, self.hass, "dev_kids", entity="light.kids", spoken_name="kitchen light")
        self.assertTrue(refused.error, "A swapped device is refused")
        self.assertIn("do not use another", refused.data["error"])
        self.assertEqual(self.hass.services.calls, [("light", "turn_on", {"entity_id": "light.kids"})])
        for entity in ("light.kitchen", "Kitchen light", "sensor.outside"):
            result = call(turn_on, self.hass, "dev_kids", entity=entity)
            self.assertTrue(result.error, entity)
            self.assertFalse(result.data["success"])
        self.assertEqual(len(self.hass.services.calls), 1, "Refused calls reach no service")
        # The same tool spoken through another panel gets that panel's (empty) lists.
        self.assertTrue(call(turn_on, self.hass, "dev_other", entity="light.kids").error)
        self.assertTrue(call(turn_on, self.hass, None, entity="light.kids").error)
        self.assertEqual(len(self.hass.services.calls), 1)

    def test_lists_are_read_again_on_every_call(self):
        turn_on = self.tool(self.instance("dev_kids"), "turn_on")
        self.hass.config_entries.async_get_entry("e_kids").data.pop("voice_control_areas")
        self.assertTrue(call(turn_on, self.hass, "dev_kids", entity="light.kids").error)
        self.assertEqual(self.hass.services.calls, [])

    def test_get_state_reads_without_control(self):
        get_state = self.tool(self.instance("dev_kids"), "get_state")
        result = call(get_state, self.hass, "dev_kids", entity="outside temperature")
        self.assertEqual(result.data["state"], "4.5")
        self.assertFalse(result.data["can_control"])
        self.assertTrue(call(get_state, self.hass, "dev_kids", entity="light.kitchen").error)
        self.assertEqual(self.hass.services.calls, [])

    def test_start_timer_has_no_end_command(self):
        timer = self.tool(self.instance("dev_kids"), "HassStartTimer")
        self.assertNotIn("conversation_command", timer.parameters.schema)
        call(timer, self.hass, "dev_kids", minutes=10, conversation_command="unlock the front door")
        self.assertEqual(self.hass.intents, [("HassStartTimer", {"minutes": 10}, "dev_kids")])


class ConfigAndTextsTests(unittest.TestCase):
    def test_options_step_and_texts(self):
        flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn('"energy", "voice", "security"', flow)
        self.assertIn("async def async_step_voice", flow)
        english = json.loads((ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
        step = english["options"]["step"]["voice"]
        self.assertEqual(sorted(step["data"]), sorted(SCOPE.VOICE_KEYS))
        self.assertIn("HomeTiles room control", step["description"])
        strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
        self.assertEqual(strings["options"]["step"]["voice"], step)

    def test_registered_at_setup(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(source, re.compile(r"async def async_setup\(.*?async_register_voice_api\(hass, domain_data\)",
                                            re.S))


if __name__ == "__main__":
    unittest.main()
