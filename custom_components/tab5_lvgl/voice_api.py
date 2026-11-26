"""The "HomeTiles room control" LLM API (rules in voice_scope.py).

An LLM conversation agent (Ollama, OpenAI, Anthropic, ...) that has this API
ticked under "Control Home Assistant" reaches only the entities the speaking
panel's Bridge options release under "Voice access". Home Assistant tells the
API which device speaks (LLMContext.device_id); that device's panel entry
holds the lists. Every tool call checks the lists again before it calls a
fixed Home Assistant service, so a model that ignores the prompt still
cannot reach anything else.

Timers use Home Assistant's own timer intents on the speaking device, like
the Assist API, without the "command when the timer ends": that command
would run through the default agent and its exposure list.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent, llm
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .device_helpers import entry_device_name, is_bridge_entry
from . import voice_scope as scope_rules
from .voice_scope import EntityFacts, VoiceRefusal

# Tool parameters use the schema library Home Assistant's llm helper uses:
# probatio since 2026, voluptuous before.
vol = getattr(llm, "probatio", None) or getattr(llm, "vol", None)
if vol is None:  # pragma: no cover
  import voluptuous as vol  # type: ignore[no-redef]

try:
  from homeassistant.components.intent.timers import async_device_supports_timers
except ImportError:  # pragma: no cover - the intent integration is always there
  async_device_supports_timers = None

_LOGGER = logging.getLogger(__name__)

API_ID = "tab5_lvgl_room"
API_NAME = "HomeTiles room control"
TOOL_PREFIX = f"{DOMAIN}__"
DATA_VOICE_API = "_voice_api_unsub"

_ToolResult = getattr(llm, "ToolResult", None)
_ToolAnnotations = getattr(llm, "ToolAnnotations", None)

TIMER_INTENTS = (
  "HassStartTimer", "HassCancelTimer", "HassCancelAllTimers", "HassIncreaseTimer",
  "HassDecreaseTimer", "HassPauseTimer", "HassUnpauseTimer", "HassTimerStatus",
)
# HassStartTimer's slot that runs a sentence through the default agent when
# the timer ends.
TIMER_COMMAND_SLOT = "conversation_command"


def _result(data: Dict[str, Any], error: bool = False) -> Any:
  if _ToolResult is not None:
    return _ToolResult(data=data, error=error)
  return data


def _annotate(tool: Any, *, read_only: bool) -> None:
  tool.integration = DOMAIN
  if _ToolAnnotations is not None:
    tool.annotations = (_ToolAnnotations(read_only=True, open_world=False) if read_only
                        else _ToolAnnotations(open_world=False))


@callback
def panel_entry_for_device(hass: HomeAssistant, device_id: Optional[str]) -> Any:
  """The panel config entry of the speaking device, or None."""
  if not device_id:
    return None
  device = dr.async_get(hass).async_get(device_id)
  if device is None:
    return None
  for entry_id in device.config_entries:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None and entry.domain == DOMAIN and not is_bridge_entry(entry):
      return entry
  return None


@callback
def _entity_facts(hass: HomeAssistant, wanted: scope_rules.VoiceScope) -> List[EntityFacts]:
  """Registry facts of every entity with a state; entities without a
  registry entry only count when named one by one."""
  ent_reg = er.async_get(hass)
  dev_reg = dr.async_get(hass)
  area_reg = ar.async_get(hass)
  named = wanted.control.entities | wanted.read.entities
  facts: List[EntityFacts] = []
  for state in hass.states.async_all():
    entry = ent_reg.async_get(state.entity_id)
    if entry is None:
      if state.entity_id in named:
        facts.append(EntityFacts(entity_id=state.entity_id, name=state.name))
      continue
    area_id = entry.area_id
    if area_id is None and entry.device_id and (device := dev_reg.async_get(entry.device_id)):
      area_id = device.area_id
    area = area_reg.async_get_area(area_id) if area_id else None
    facts.append(EntityFacts(
      entity_id=state.entity_id,
      name=state.name,
      area_id=area_id,
      area_name=area.name if area else "",
      label_ids=tuple(entry.labels or ()),
      aliases=tuple(alias for alias in (entry.aliases or ()) if isinstance(alias, str)),
      hidden=entry.hidden_by is not None,
      auxiliary=getattr(entry, "entity_category", None) is not None,
    ))
  return facts


@callback
def scoped_facts_for_entry(hass: HomeAssistant, entry: Any) -> Tuple[List[EntityFacts], Dict[str, scope_rules.ScopedEntity]]:
  """Every entity's registry facts and the panel's scoped entities."""
  if entry is None:
    return [], {}
  scope = scope_rules.scope_from_config(entry.data)
  if scope.empty:
    return [], {}
  facts = _entity_facts(hass, scope)
  return facts, scope_rules.scoped_entities(scope, facts)


@callback
def scoped_for_entry(hass: HomeAssistant, entry: Any) -> Dict[str, scope_rules.ScopedEntity]:
  return scoped_facts_for_entry(hass, entry)[1]


@callback
def _device_area_name(hass: HomeAssistant, device_id: Optional[str]) -> str:
  if not device_id or not (device := dr.async_get(hass).async_get(device_id)) or not device.area_id:
    return ""
  area = ar.async_get(hass).async_get_area(device.area_id)
  return area.name if area else ""


class _ScopedTool(llm.Tool):
  """A tool that resolves its entity in the speaking panel's lists at call
  time and runs the service voice_scope plans for it."""

  read_only = False
  planner: Callable[..., Any] | None = None
  arguments: Dict[str, str] = {}

  def __init__(self) -> None:
    _annotate(self, read_only=self.read_only)

  async def async_call(self, hass: HomeAssistant, tool_input: Any, llm_context: Any) -> Any:
    args = dict(tool_input.tool_args or {})
    entry = panel_entry_for_device(hass, llm_context.device_id)
    try:
      facts, entities = scoped_facts_for_entry(hass, entry)
      item = scope_rules.resolve_entity(args.get("entity") or args.get("spoken_name"), entities,
                                        area=args.get("area"))
      if not self.read_only:
        # Reading a near match does no harm; switching one does.
        scope_rules.check_spoken_name(args.get("spoken_name"), item, facts, entities)
      state = hass.states.get(item.entity_id)
      value = state.state if state is not None else None
      attributes = dict(state.attributes) if state is not None else {}
      if self.read_only:
        return _result(scope_rules.state_summary(item, value, attributes))
      domain, service, data = self.plan(item, value, attributes, args)
    except VoiceRefusal as err:
      _LOGGER.info("HomeTiles voice refused %s for %s: %s", self.name,
                   entry_device_name(entry) if entry else "no panel", err)
      return _result({"success": False, "error": str(err)}, error=True)
    await hass.services.async_call(domain, service, data, blocking=True, context=llm_context.context)
    return _result({"success": True, "entity_id": item.entity_id, "name": item.name,
                    "action": f"{domain}.{service}"})

  def plan(self, item: Any, value: Optional[str], attributes: Dict[str, Any], args: Dict[str, Any]) -> Any:
    raise NotImplementedError


_ENTITY = "The entity_id from the device list (or the device's name)."
_SPOKEN = "The device's name exactly in the user's words, for example 'worktable light'."
_AREA = "Optional area name, only when a name fits devices in several areas."


def _schema(extra: Dict[Any, Any] | None = None) -> Any:
  fields: Dict[Any, Any] = {
    vol.Required("entity", description=_ENTITY): str,
    vol.Required("spoken_name", description=_SPOKEN): str,
    vol.Optional("area", description=_AREA): str,
  }
  fields.update(extra or {})
  return vol.Schema(fields)


class TurnOnTool(_ScopedTool):
  name = f"{TOOL_PREFIX}turn_on"
  description = ("Turn a device on. Opens covers and valves; activates scenes and scripts; presses buttons.")
  parameters = _schema()

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_turn(item, True, value, attributes)


class TurnOffTool(_ScopedTool):
  name = f"{TOOL_PREFIX}turn_off"
  description = "Turn a device off. Closes covers and valves."
  parameters = _schema()

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_turn(item, False, value, attributes)


class SetLightTool(_ScopedTool):
  name = f"{TOOL_PREFIX}set_light"
  description = "Turn a light on with a brightness, a color name or a color temperature."
  parameters = _schema({
    vol.Optional("brightness", description="Brightness in percent, 1 to 100."): vol.Coerce(float),
    vol.Optional("color", description="Color name: " + ", ".join(scope_rules.COLOR_NAMES) + "."):
      vol.In(list(scope_rules.COLOR_NAMES)),
    vol.Optional("kelvin", description="Color temperature in Kelvin, for example 2700 warm or 5000 cool."):
      vol.Coerce(float),
  })

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_light(item, value, attributes, brightness=args.get("brightness"),
                                  color=args.get("color"), kelvin=args.get("kelvin"))


class SetPositionTool(_ScopedTool):
  name = f"{TOOL_PREFIX}set_cover"
  description = "Open, close or stop a cover, blind, garage door or valve, or move it to a position."
  parameters = _schema({
    vol.Optional("command"): vol.In(list(scope_rules.COVER_COMMANDS)),
    vol.Optional("position", description="Position in percent, 0 closed to 100 open."): vol.Coerce(float),
  })

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_position(item, value, attributes, command=args.get("command"),
                                     position=args.get("position"))


class SetClimateTool(_ScopedTool):
  name = f"{TOOL_PREFIX}set_climate"
  description = "Set a thermostat's target temperature and/or its HVAC mode."
  parameters = _schema({
    vol.Optional("temperature", description="Target temperature in the home's unit."): vol.Coerce(float),
    vol.Optional("hvac_mode", description="One of the thermostat's own modes, such as heat, cool, auto or off."): str,
  })

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_climate(item, value, attributes, temperature=args.get("temperature"),
                                    hvac_mode=args.get("hvac_mode"))


class MediaTool(_ScopedTool):
  name = f"{TOOL_PREFIX}media"
  description = "Play, pause, stop, skip on a media player, or set its volume."
  parameters = _schema({
    vol.Optional("command"): vol.In(list(scope_rules.MEDIA_COMMANDS)),
    vol.Optional("volume", description="Volume in percent, 0 to 100."): vol.Coerce(float),
  })

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_media(item, value, attributes, command=args.get("command"), volume=args.get("volume"))


class FanSpeedTool(_ScopedTool):
  name = f"{TOOL_PREFIX}set_fan_speed"
  description = "Set a fan's speed in percent."
  parameters = _schema({vol.Required("percentage", description="Speed in percent, 0 to 100."): vol.Coerce(float)})

  def plan(self, item, value, attributes, args):
    return scope_rules.plan_fan(item, value, attributes, percentage=args.get("percentage"))


class GetStateTool(_ScopedTool):
  name = f"{TOOL_PREFIX}get_state"
  description = "Get the current state and details of one device from the list."
  parameters = _schema()
  read_only = True


CONTROL_TOOLS = (TurnOnTool, TurnOffTool, SetLightTool, SetPositionTool, SetClimateTool, MediaTool, FanSpeedTool)


class _TimerTool(llm.IntentTool):
  """Home Assistant's timer intent on the speaking device; HassStartTimer
  without the command it would run at the end."""

  def __init__(self, handler: Any) -> None:
    super().__init__(f"{TOOL_PREFIX}{handler.intent_type}", handler)
    _annotate(self, read_only=handler.intent_type == "HassTimerStatus")
    schema = getattr(self.parameters, "schema", None)
    if isinstance(schema, dict):
      kept = {key: value for key, value in schema.items() if str(key) != TIMER_COMMAND_SLOT}
      if len(kept) != len(schema):
        self.parameters = vol.Schema(kept)

  async def async_call(self, hass: HomeAssistant, tool_input: Any, llm_context: Any) -> Any:
    if isinstance(tool_input.tool_args, dict):
      tool_input.tool_args.pop(TIMER_COMMAND_SLOT, None)
    return await super().async_call(hass, tool_input, llm_context)


@callback
def _supports_timers(hass: HomeAssistant, device_id: str) -> bool:
  if async_device_supports_timers is None:
    return False
  return async_device_supports_timers(hass, device_id)


class HomeTilesRoomAPI(llm.API):
  """One API for every panel: the speaking device picks the lists."""

  def __init__(self, hass: HomeAssistant) -> None:
    super().__init__(hass=hass, id=API_ID, name=API_NAME)

  async def async_get_api_instance(self, llm_context: Any) -> Any:
    hass = self.hass
    entry = panel_entry_for_device(hass, llm_context.device_id)
    now = dt_util.now()
    if entry is None:
      prompt = ("This request does not come from a HomeTiles panel, so no Home Assistant devices may be "
                "used. Say so if asked to control or read one.")
      tools: List[Any] = []
    else:
      entities = scoped_for_entry(hass, entry)
      states = {entity_id: (state.state if (state := hass.states.get(entity_id)) else None)
                for entity_id in entities}
      prompt = scope_rules.build_prompt(entry_device_name(entry), entities, states,
                                        _device_area_name(hass, llm_context.device_id))
      tools = [GetStateTool()] if entities else []
      if any(item.can_control for item in entities.values()):
        tools += [tool() for tool in CONTROL_TOOLS]
      if llm_context.device_id and _supports_timers(hass, llm_context.device_id):
        tools += [_TimerTool(handler) for handler in intent.async_get(hass)
                  if handler.intent_type in TIMER_INTENTS]
    prompt += f"\nCurrent time is {now.strftime('%H:%M')}, today is {now.strftime('%A, %Y-%m-%d')}."
    return llm.APIInstance(api=self, api_prompt=prompt, llm_context=llm_context, tools=tools)


@callback
def async_register_voice_api(hass: HomeAssistant, domain_data: Dict[str, Any]) -> None:
  """Register the API once per Home Assistant run."""
  if domain_data.get(DATA_VOICE_API) is not None:
    return
  try:
    domain_data[DATA_VOICE_API] = llm.async_register_api(hass, HomeTilesRoomAPI(hass))
  except Exception as err:  # noqa: BLE001 - a voice extra must never stop the Bridge
    _LOGGER.warning("HomeTiles room control for voice assistants is unavailable: %s", err)
