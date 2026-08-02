"""Light entities for Tab5 device settings."""

from __future__ import annotations

from typing import Optional

from homeassistant.components import mqtt
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import TOPIC_DISPLAY_BRIGHTNESS, TOPIC_SCREENSAVER_BRIGHTNESS
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    state_topic,
)

MIN_BRIGHTNESS_RAW = 121
MAX_BRIGHTNESS_RAW = 255
MIN_SCREENSAVER_PERCENT = 1
MAX_SCREENSAVER_PERCENT = 100


def _raw_to_ha(raw: int) -> int:
    raw = max(MIN_BRIGHTNESS_RAW, min(MAX_BRIGHTNESS_RAW, raw))
    return round((raw - MIN_BRIGHTNESS_RAW) * 255 / (MAX_BRIGHTNESS_RAW - MIN_BRIGHTNESS_RAW))


def _ha_to_raw(value: int) -> int:
    value = max(0, min(255, value))
    return round(MIN_BRIGHTNESS_RAW + (value * (MAX_BRIGHTNESS_RAW - MIN_BRIGHTNESS_RAW) / 255))


def _percent_to_ha(percent: int) -> int:
    """Map the firmware's 1..100 percent range to HA's 1..255 range."""
    percent = max(MIN_SCREENSAVER_PERCENT, min(MAX_SCREENSAVER_PERCENT, percent))
    return round(
        1
        + (percent - MIN_SCREENSAVER_PERCENT)
        * 254
        / (MAX_SCREENSAVER_PERCENT - MIN_SCREENSAVER_PERCENT)
    )


def _ha_to_percent(value: int) -> int:
    """Map HA's 1..255 brightness range to firmware percent."""
    value = max(1, min(255, value))
    return round(
        MIN_SCREENSAVER_PERCENT
        + (value - 1)
        * (MAX_SCREENSAVER_PERCENT - MIN_SCREENSAVER_PERCENT)
        / 254
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    async_add_entities(
        [
            Tab5DisplayLight(entry, base_topic),
            Tab5ScreensaverBrightnessLight(entry, base_topic),
        ]
    )


class Tab5DisplayLight(LightEntity):
    """Display brightness exposed as a light entity."""

    _attr_has_entity_name = True
    _attr_name = "Display Helligkeit"
    _attr_icon = "mdi:brightness-6"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_display_brightness_light"
        self._topic_cmd = command_topic(base_topic, TOPIC_DISPLAY_BRIGHTNESS)
        self._topic_state = state_topic(base_topic, TOPIC_DISPLAY_BRIGHTNESS)
        self._unsub_state = None
        self._last_nonzero: Optional[int] = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw_text = msg.payload.strip()
            try:
                raw = int(float(raw_text))
            except (TypeError, ValueError):
                return
            raw = max(MIN_BRIGHTNESS_RAW, min(MAX_BRIGHTNESS_RAW, raw))
            brightness = _raw_to_ha(raw)
            self._attr_brightness = brightness
            self._attr_is_on = brightness > 0
            if brightness > 0:
                self._last_nonzero = brightness
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
        brightness = kwargs.get("brightness")
        if brightness is None:
            brightness = self._last_nonzero if self._last_nonzero is not None else 255
        raw = _ha_to_raw(int(brightness))
        await mqtt.async_publish(self.hass, self._topic_cmd, str(raw), qos=0, retain=False)
        self._attr_brightness = _raw_to_ha(raw)
        self._attr_is_on = self._attr_brightness > 0
        if self._attr_brightness > 0:
            self._last_nonzero = self._attr_brightness
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        raw = _ha_to_raw(0)
        await mqtt.async_publish(self.hass, self._topic_cmd, str(raw), qos=0, retain=False)
        self._attr_brightness = 0
        self._attr_is_on = False
        self.async_write_ha_state()


class Tab5ScreensaverBrightnessLight(LightEntity):
    """Screensaver brightness exposed as a percentage-backed light entity."""

    _attr_has_entity_name = True
    _attr_name = "Screensaver Helligkeit"
    _attr_icon = "mdi:brightness-4"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = (
            f"{entry_device_id(entry)}_screensaver_brightness_light"
        )
        self._topic_cmd = command_topic(base_topic, TOPIC_SCREENSAVER_BRIGHTNESS)
        self._topic_state = state_topic(base_topic, TOPIC_SCREENSAVER_BRIGHTNESS)
        self._unsub_state = None
        self._last_nonzero: Optional[int] = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw_text = msg.payload.strip()
            try:
                percent = int(float(raw_text))
            except (TypeError, ValueError):
                return
            percent = max(
                MIN_SCREENSAVER_PERCENT,
                min(MAX_SCREENSAVER_PERCENT, percent),
            )
            brightness = _percent_to_ha(percent)
            self._attr_brightness = brightness
            self._attr_is_on = percent > MIN_SCREENSAVER_PERCENT
            if self._attr_is_on:
                self._last_nonzero = brightness
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
        brightness = kwargs.get("brightness")
        if brightness is None:
            brightness = self._last_nonzero if self._last_nonzero is not None else 255
        percent = _ha_to_percent(int(brightness))
        await mqtt.async_publish(
            self.hass,
            self._topic_cmd,
            str(percent),
            qos=0,
            retain=False,
        )
        self._attr_brightness = _percent_to_ha(percent)
        self._attr_is_on = percent > MIN_SCREENSAVER_PERCENT
        if self._attr_is_on:
            self._last_nonzero = self._attr_brightness
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        percent = MIN_SCREENSAVER_PERCENT
        await mqtt.async_publish(
            self.hass,
            self._topic_cmd,
            str(percent),
            qos=0,
            retain=False,
        )
        self._attr_brightness = _percent_to_ha(percent)
        self._attr_is_on = False
        self.async_write_ha_state()
