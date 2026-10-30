"""Timer volume: how loud the panel rings finished voice timers.

Its own level, apart from the media volume and mute, so a timer still rings
after the music was turned down; 0 rings silently (the alert card and the
spoken sentence stay). Home Assistant keeps the value (restore state) and the
satellite sends it to the panel with the timer sound on every connect.
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import EntityCategory

from .assist import ASSIST_SATELLITES_KEY
from .const import DOMAIN
from .device_helpers import entry_device_id, entry_device_info
from . import voice_timers


class HomeTilesTimerVolumeNumber(RestoreNumber):
    """Volume of the finished-timer chime, 0-100 %."""

    _attr_has_entity_name = True
    _attr_translation_key = "timer_volume"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bell-cog"
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = voice_timers.timer_volume_number_unique_id(entry_device_id(entry))
        self._attr_native_value = voice_timers.DEFAULT_VOLUME

    def _store(self) -> None:
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            voice_timers.TIMER_VOLUMES_KEY, {})[self._entry_id] = int(self._attr_native_value)

    async def _async_send(self) -> None:
        satellite = (self.hass.data.get(DOMAIN, {})
                     .get(ASSIST_SATELLITES_KEY, {}).get(self._entry_id))
        if satellite is not None:
            # A panel that is offline gets it on its next connect.
            await satellite.async_send_timer_sound()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = voice_timers.clamp_volume(value)
        self._store()
        self.async_write_ha_state()
        await self._async_send()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = voice_timers.clamp_volume(last.native_value)
        self._store()
        await self._async_send()

    async def async_will_remove_from_hass(self) -> None:
        self.hass.data.get(DOMAIN, {}).get(voice_timers.TIMER_VOLUMES_KEY, {}).pop(self._entry_id, None)
        await super().async_will_remove_from_hass()
