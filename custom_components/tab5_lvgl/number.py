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

MIN_BRIGHTNESS_PERCENT = 1
MAX_BRIGHTNESS_PERCENT = 100
LEGACY_MIN_BRIGHTNESS_RAW = 121
LEGACY_MAX_BRIGHTNESS_RAW = 255


def _legacy_raw_to_percent(raw: int) -> int:
    raw = max(LEGACY_MIN_BRIGHTNESS_RAW, min(LEGACY_MAX_BRIGHTNESS_RAW, raw))
    return round(
        MIN_BRIGHTNESS_PERCENT
        + (raw - LEGACY_MIN_BRIGHTNESS_RAW)
        * (MAX_BRIGHTNESS_PERCENT - MIN_BRIGHTNESS_PERCENT)
        / (LEGACY_MAX_BRIGHTNESS_RAW - LEGACY_MIN_BRIGHTNESS_RAW)
    )


def _percent_to_legacy_raw(percent: int) -> int:
    percent = max(MIN_BRIGHTNESS_PERCENT, min(MAX_BRIGHTNESS_PERCENT, percent))
    return round(
        LEGACY_MIN_BRIGHTNESS_RAW
        + (percent - MIN_BRIGHTNESS_PERCENT)
        * (LEGACY_MAX_BRIGHTNESS_RAW - LEGACY_MIN_BRIGHTNESS_RAW)
        / (MAX_BRIGHTNESS_PERCENT - MIN_BRIGHTNESS_PERCENT)
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
    _attr_native_min_value = MIN_BRIGHTNESS_PERCENT
    _attr_native_max_value = MAX_BRIGHTNESS_PERCENT
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
        self._legacy_raw_topics = False

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
            self._legacy_raw_topics = value > MAX_BRIGHTNESS_PERCENT
            if self._legacy_raw_topics:
                value = _legacy_raw_to_percent(value)
            else:
                value = max(
                    MIN_BRIGHTNESS_PERCENT,
                    min(MAX_BRIGHTNESS_PERCENT, value),
                )
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
        value_int = max(
            MIN_BRIGHTNESS_PERCENT,
            min(MAX_BRIGHTNESS_PERCENT, value_int),
        )
        command = (
            _percent_to_legacy_raw(value_int)
            if self._legacy_raw_topics
            else value_int
        )
        await mqtt.async_publish(
            self.hass, self._topic_cmd, str(command), qos=0, retain=False
        )
        self._attr_native_value = value_int
        self.async_write_ha_state()
