"""Sensor entities for Tab5 runtime telemetry."""

from __future__ import annotations

import math

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HA_PREFIX, CONF_LOCAL_IO, DEFAULT_PREFIX, TOPIC_SENSOR_SOC
from .device_helpers import (
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    normalise_topic,
    sensor_topic,
    state_topic,
)
from .local_io import (
    LOCAL_IO_TEMPERATURE,
    entry_local_io,
    local_io_announced_entity_id,
    local_io_state_topic,
    local_io_unique_id,
    parse_on_off_payload,
    parse_temperature_payload,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    merged = dict(entry.data or {})
    if entry.options:
        merged.update(entry.options)
    ha_prefix = normalise_topic(merged.get(CONF_HA_PREFIX), DEFAULT_PREFIX)
    entities = [Tab5BatterySensor(entry, base_topic)]
    if CONF_LOCAL_IO not in merged:
        # Legacy firmware exposed one fixed external sensor on the HA prefix.
        # New firmware announces every channel through local_io, including an
        # explicit empty list, so creating both would leave a duplicate entity.
        entities.append(Tab5ExternalTemperatureSensor(entry, ha_prefix))
    entities.extend(
        HomeTilesLocalTemperatureSensor(entry, base_topic, descriptor)
        for descriptor in entry_local_io(entry)
        if descriptor["type"] == LOCAL_IO_TEMPERATURE
    )
    async_add_entities(entities)


class HomeTilesLocalTemperatureSensor(SensorEntity):
    """Temperature channel physically attached to a HomeTiles panel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, base_topic: str, descriptor: dict
    ) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = local_io_unique_id(entry_device_id(entry), descriptor)
        if announced_entity_id := local_io_announced_entity_id(descriptor):
            # See HomeTilesLocalRelay: this is a registry suggestion, not a
            # forced runtime ID, so Home Assistant still resolves collisions.
            self.entity_id = announced_entity_id
        self._attr_name = descriptor["name"]
        self._attr_native_unit_of_measurement = descriptor["unit"]
        self._precision = descriptor["precision"]
        self._attr_suggested_display_precision = self._precision
        self._topic_state = local_io_state_topic(base_topic, descriptor["id"])
        self._topic_available = state_topic(base_topic, "connected")
        self._unsub_state = None
        self._unsub_available = None
        self._panel_available: bool | None = None
        self._sensor_available = False
        self._attr_available = False

    def _refresh_available(self) -> None:
        self._attr_available = (
            self._panel_available is not False and self._sensor_available
        )

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            value = parse_temperature_payload(msg.payload)
            if value is None:
                if msg.payload.strip().lower() in {
                    "unknown", "unavailable", "nan", "inf", "-inf"
                }:
                    self._sensor_available = False
                    self._refresh_available()
                    self._attr_native_value = None
                    self.async_write_ha_state()
                return
            self._attr_native_value = round(value, self._precision)
            self._sensor_available = True
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


class Tab5BatterySensor(SensorEntity):
    """Battery state-of-charge in percent."""

    _attr_has_entity_name = True
    _attr_name = "Batterie SoC"
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_battery_soc"
        self._topic_state = sensor_topic(base_topic, TOPIC_SENSOR_SOC)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            if raw.endswith("%"):
                raw = raw[:-1].strip()
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                return
            value_int = int(round(value))
            if value_int < 0:
                value_int = 0
            if value_int > 100:
                value_int = 100
            self._attr_native_value = value_int
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()


class Tab5ExternalTemperatureSensor(SensorEntity):
    """External DS18x20 temperature from Tab5."""

    _attr_has_entity_name = True
    _attr_name = "Externe Temperatur"
    _attr_icon = "mdi:thermometer"
    _attr_native_unit_of_measurement = "C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, ha_prefix: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_external_temperature"
        self._topic_state = f"{ha_prefix}/sensor/tab5_external_temperature/state"
        self._unsub_state = None
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            lowered = raw.lower()
            if lowered in {"unknown", "unavailable", "nan", "inf", "-inf"}:
                self._attr_available = False
                self._attr_native_value = None
                self.async_write_ha_state()
                return
            raw = raw.replace(",", ".")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                self._attr_available = False
                self._attr_native_value = None
            else:
                self._attr_available = True
                self._attr_native_value = round(value, 1)
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()
