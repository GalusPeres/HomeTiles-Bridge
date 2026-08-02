"""Number entities for Tab5 device settings."""

from __future__ import annotations

from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import TOPIC_DISPLAY_BRIGHTNESS
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_info,
    entry_device_id,
    state_topic,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    async_add_entities([Tab5BrightnessNumber(entry, base_topic)])


class Tab5BrightnessNumber(NumberEntity):
    """Display brightness control."""

    _attr_has_entity_name = True
    _attr_name = "Display Helligkeit"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 121
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_display_brightness"
        self._topic_cmd = command_topic(base_topic, TOPIC_DISPLAY_BRIGHTNESS)
        self._topic_state = state_topic(base_topic, TOPIC_DISPLAY_BRIGHTNESS)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                return
            if value < 121:
                value = 121
            if value > 255:
                value = 255
            self._attr_native_value = value
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        value_int = int(round(value))
        if value_int < 121:
            value_int = 121
        if value_int > 255:
            value_int = 255
        await mqtt.async_publish(self.hass, self._topic_cmd, str(value_int), qos=0, retain=False)
        self._attr_native_value = value_int
        self.async_write_ha_state()
