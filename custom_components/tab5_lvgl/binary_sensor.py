"""Binary sensor entities for HomeTiles panel diagnostics."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .command_channel import entry_pairing_key, key_id_for_key
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    async_add_entities([HomeTilesMqttConnectionSensor(entry, base_topic), HomeTilesEncryptionSensor(entry)])


class HomeTilesMqttConnectionSensor(BinarySensorEntity):
    """Shows whether the panel is currently connected to MQTT."""

    _attr_has_entity_name = True
    _attr_name = "MQTT Verbindung"
    _attr_icon = "mdi:lan-connect"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_mqtt_connected"
        self._topic_state = state_topic(base_topic, "connected")
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip().lower()
            if raw in {"1", "on", "online", "true", "yes"}:
                self._attr_is_on = True
            elif raw in {"0", "off", "offline", "false", "no"}:
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


class HomeTilesEncryptionSensor(BinarySensorEntity):
    """On while the Bridge runs only encrypted commands from the panel.

    Every change of the pairing reloads the entry, so the state is fixed for
    the lifetime of the entity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "encryption"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_encryption"
        pairing_key = entry_pairing_key(entry)
        self._attr_is_on = pairing_key is not None
        self._attr_icon = "mdi:shield-lock" if pairing_key else "mdi:shield-off-outline"
        # The key id is public (the panel publishes it); it tells pairings apart.
        self._attr_extra_state_attributes = {"key_id": key_id_for_key(pairing_key)} if pairing_key else {}

    @property
    def device_info(self):
        return self._device_info
