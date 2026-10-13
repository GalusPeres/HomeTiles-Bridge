"""Switch entities for Tab5 device settings."""

from __future__ import annotations

import json

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .assist import (
    ASSIST_SATELLITES_KEY,
    assist_microphone_switch_unique_id,
    assist_status_topic,
    parse_status as parse_assist_status,
)
from .capabilities import merged_capabilities_data, supports
from .command_channel import entry_pairing_key
from .const import DOMAIN, LOCAL_CAMERA_MAX_BYTES, TOPIC_DISPLAY_ROTATE, TOPIC_DISPLAY_SLEEP
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    state_topic,
)
from .local_camera import (
    async_publish_local_camera_command,
    build_pause_request,
    camera_allowed,
    local_camera_status_topic,
    local_camera_unique_id,
    parse_connected,
    parse_status,
)
from .local_io import (
    LOCAL_IO_RELAY,
    entry_local_io,
    local_io_announced_entity_id,
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
    # Same gate as the camera entity (camera.py): the pause switch exists
    # exactly while the panel announces its own camera.
    if supports(merged_capabilities_data(entry), "local_camera"):
        entities.append(HomeTilesLocalCameraSwitch(entry, base_topic))
    # Same gate as the Assist satellite: the panel announced its microphone.
    if supports(merged_capabilities_data(entry), "audio_input"):
        entities.append(HomeTilesMicrophoneSwitch(entry, base_topic))
    async_add_entities(entities)


class HomeTilesMicrophoneSwitch(SwitchEntity):
    """The panel's microphone privacy switch.

    Off means the panel refuses every Assist session and runs no wake word.
    The retained Assist status confirms every change. Turning it on from Home
    Assistant needs the encrypted pairing; the panel's own Settings > Audio
    page can always switch it.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "microphone"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry_id = entry.entry_id
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = assist_microphone_switch_unique_id(entry_device_id(entry))
        self._base_topic = base_topic
        self._unsubscribe = None
        self._attr_is_on = None
        # Firmware without the switch never reports "mic_enabled".
        self._attr_available = False

    @property
    def icon(self) -> str:
        return "mdi:microphone" if self._attr_is_on is not False else "mdi:microphone-off"

    def apply_status(self, payload) -> bool:
        """Mirror the retained Assist status; False when ignored."""
        if not payload:
            self._attr_available = False
            return True
        status = parse_assist_status(payload)
        if status is None:
            return False
        if status["mic_enabled"] is None:
            self._attr_available = False
        else:
            self._attr_available = True
            self._attr_is_on = status["mic_enabled"]
        return True

    async def _async_set(self, enabled: bool) -> None:
        satellite = (self.hass.data.get(DOMAIN, {})
                     .get(ASSIST_SATELLITES_KEY, {}).get(self._entry_id))
        if satellite is None:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_unavailable")
        await satellite.async_set_microphone(enabled)
        # The panel's retained status confirms it; until then show the request.
        self._attr_is_on = enabled
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if self.apply_status(msg.payload):
                self.async_write_ha_state()

        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, assist_status_topic(self._base_topic), handle_status, qos=0)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await super().async_will_remove_from_hass()


class HomeTilesLocalCameraSwitch(SwitchEntity):
    """Pause or resume the camera built into the panel.

    On means the panel may capture. Off means the user paused the camera; the
    camera entity then stays registered and available but serves no images.
    The retained ``{base}/stat/local_camera`` status confirms every change.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "local_camera"
    _attr_icon = "mdi:camera"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = local_camera_unique_id(entry_device_id(entry))
        self._entry_id = entry.entry_id
        self._paired = entry_pairing_key(entry) is not None
        self._base_topic = base_topic
        self._topic_status = local_camera_status_topic(base_topic)
        self._topic_available = state_topic(base_topic, "connected")
        self._subscriptions = []
        self._status: dict | None = None
        self._panel_online: bool | None = None
        self._attr_is_on = None
        self._attr_available = True

    def _refresh_available(self) -> None:
        # A camera disabled in the panel Web Admin (not paused) cannot be
        # resumed from here; the capability withdrawal then removes the entity.
        withdrawn = (
            self._status is not None
            and self._status["state"] == "disabled"
            and not self._status["paused"]
        )
        self._attr_available = self._panel_online is not False and not withdrawn

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                # A cleared retained status: the pause state is unknown.
                self._status = None
            else:
                status = parse_status(msg.payload, LOCAL_CAMERA_MAX_BYTES)
                if status is None:
                    # The camera entity already logs invalid payloads.
                    return
                self._status = status
            self._attr_is_on = camera_allowed(self._status, self._attr_is_on)
            self._refresh_available()
            self.async_write_ha_state()

        async def _handle_available(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            self._refresh_available()
            self.async_write_ha_state()

        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, self._topic_status, _handle_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, self._topic_available, _handle_available, qos=0))

    async def async_will_remove_from_hass(self) -> None:
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    async def _async_publish_pause(self, paused: bool) -> None:
        # A command, never retained state: the panel keeps the pause itself.
        data = getattr(self.hass, "data", None)
        domain_data = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        bridge = domain_data.get("entries", {}).get(self._entry_id)
        await async_publish_local_camera_command(
            self.hass, bridge, self._paired, self._base_topic,
            json.dumps(build_pause_request(paused), separators=(",", ":")),
            mqtt.async_publish)
        # Optimistic; the next retained status confirms or corrects it.
        self._attr_is_on = not paused
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_publish_pause(False)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_publish_pause(True)


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
        if announced_entity_id := local_io_announced_entity_id(descriptor):
            # Home Assistant treats an entity_id set before platform addition as
            # the integration's suggested object ID. The registry still owns the
            # final ID and adds a suffix if another entity already uses it.
            self.entity_id = announced_entity_id
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
