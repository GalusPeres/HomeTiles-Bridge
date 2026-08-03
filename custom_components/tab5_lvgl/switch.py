"""Switch entities for Tab5 device settings."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import TOPIC_DISPLAY_ROTATE, TOPIC_DISPLAY_SLEEP
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    state_topic,
)
from .local_io import (
    LOCAL_IO_RELAY,
    entry_local_io,
    local_io_command_topic,
    local_io_state_topic,
    local_io_unique_id,
    parse_on_off_payload,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    entities = [
        Tab5RotateSwitch(entry, base_topic),
        Tab5DisplaySleepSwitch(entry, base_topic),
    ]
    entities.extend(
        HomeTilesLocalRelay(entry, base_topic, descriptor)
        for descriptor in entry_local_io(entry)
        if descriptor["type"] == LOCAL_IO_RELAY
    )
    async_add_entities(entities)


class HomeTilesLocalRelay(SwitchEntity):
    """Relay physically attached to a HomeTiles panel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:electric-switch"
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, base_topic: str, descriptor: dict
    ) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = local_io_unique_id(entry_device_id(entry), descriptor)
        self._attr_name = descriptor["name"]
        self._topic_cmd = local_io_command_topic(base_topic, descriptor["id"])
        self._topic_state = local_io_state_topic(base_topic, descriptor["id"])
        self._topic_available = state_topic(base_topic, "connected")
        self._unsub_state = None
        self._unsub_available = None
        self._panel_available: bool | None = None
        self._state_available = False
        self._attr_available = False

    def _refresh_available(self) -> None:
        self._attr_available = (
            self._panel_available is not False and self._state_available
        )

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            state = parse_on_off_payload(msg.payload)
            if state is None:
                if msg.payload.strip().lower() in {"unknown", "unavailable"}:
                    self._state_available = False
                    self._refresh_available()
                    self.async_write_ha_state()
                return
            self._attr_is_on = state
            self._state_available = True
            self._refresh_available()
            self.async_write_ha_state()

        async def _handle_available(msg: mqtt.ReceiveMessage) -> None:
            available = parse_on_off_payload(msg.payload)
            if available is None:
                return
            self._panel_available = available
            self._refresh_available()
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )
        self._unsub_available = await mqtt.async_subscribe(
            self.hass, self._topic_available, _handle_available
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_available:
            self._unsub_available()
            self._unsub_available = None
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "ON", qos=0, retain=False)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "OFF", qos=0, retain=False)
        self._attr_is_on = False
        self.async_write_ha_state()


class Tab5RotateSwitch(SwitchEntity):
    """Switch to rotate the display 180 degrees."""

    _attr_has_entity_name = True
    _attr_name = "Display Rotation"
    _attr_icon = "mdi:phone-rotate-portrait"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_display_rotate"
        self._topic_cmd = command_topic(base_topic, TOPIC_DISPLAY_ROTATE)
        self._topic_state = state_topic(base_topic, TOPIC_DISPLAY_ROTATE)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip().lower()
            if raw in {"on", "1", "true", "yes"}:
                self._attr_is_on = True
            elif raw in {"off", "0", "false", "no"}:
                self._attr_is_on = False
            else:
                return
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "ON", qos=0, retain=False)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "OFF", qos=0, retain=False)
        self._attr_is_on = False
        self.async_write_ha_state()


class Tab5DisplaySleepSwitch(SwitchEntity):
    """Switch to sleep/wake the display immediately."""

    _attr_has_entity_name = True
    _attr_name = "Display Sleep"
    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_display_sleep"
        self._topic_cmd = command_topic(base_topic, TOPIC_DISPLAY_SLEEP)
        self._topic_state = state_topic(base_topic, TOPIC_DISPLAY_SLEEP)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip().lower()
            if raw in {"on", "1", "true", "yes"}:
                self._attr_is_on = True
            elif raw in {"off", "0", "false", "no"}:
                self._attr_is_on = False
            else:
                return
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "ON", qos=0, retain=False)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "OFF", qos=0, retain=False)
        self._attr_is_on = False
        self.async_write_ha_state()
