"""Tap-to-talk button for a HomeTiles panel's microphone.

Pressing the button starts one Assist run on the panel's Assist satellite
(assist_satellite.py), so a HomeTiles Scene tile can start voice control.
Only panels that announce the ``audio_input`` capability get the entity.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .assist import ASSIST_SATELLITES_KEY, assist_button_unique_id
from .capabilities import merged_capabilities_data, supports
from .const import DOMAIN
from .device_helpers import entry_device_id, entry_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    if supports(merged_capabilities_data(entry), "audio_input"):
        async_add_entities([HomeTilesAssistButton(entry)])


class HomeTilesAssistButton(ButtonEntity):
    """Starts listening on the panel, like a voice satellite's action button."""

    _attr_has_entity_name = True
    _attr_translation_key = "assist"
    _attr_icon = "mdi:microphone"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = assist_button_unique_id(entry_device_id(entry))

    async def async_press(self) -> None:
        data = getattr(self.hass, "data", None)
        domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        satellite = domain.get(ASSIST_SATELLITES_KEY, {}).get(self._entry_id)
        if satellite is None:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_unavailable")
        await satellite.async_start_tap_to_talk()
