"""Select entities for Tab5 device settings."""

from __future__ import annotations

from datetime import timedelta
import json
from time import monotonic

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .assist import (
    ASSIST_SATELLITES_KEY,
    assist_select_prefix,
    assist_status_topic,
    assist_wake_word_select_unique_id,
    parse_status,
)
from .capabilities import merged_capabilities_data, supports
from .view_navigation import ViewNavigation
from .const import DOMAIN, SLEEP_OPTION_LABELS, SLEEP_OPTIONS, TOPIC_SLEEP_BATTERY, TOPIC_SLEEP_MAINS
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    state_topic,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    entities = [
        Tab5SleepSelect(
            entry,
            base_topic,
            TOPIC_SLEEP_MAINS,
            f"{entry_device_id(entry)}_sleep_mains",
            "sleep_mains",
            "mdi:power-plug",
        ),
        Tab5SleepSelect(
            entry,
            base_topic,
            TOPIC_SLEEP_BATTERY,
            f"{entry_device_id(entry)}_sleep_battery",
            "sleep_battery",
            "mdi:battery",
        ),
    ]
    capabilities = merged_capabilities_data(entry)
    if supports(capabilities, "view_navigation"):
        entities.append(HomeTilesViewSelect(entry, base_topic))
    if supports(capabilities, "audio_input"):
        entities.append(HomeTilesWakeWordSelect(entry, base_topic))
        entities.extend(_assist_selects(hass, entry))
    async_add_entities(entities)


def sleep_option_from_payload(payload) -> str | None:
    """Option key for the panel's sleep label ("5 min", "Nie"), or None."""
    raw = str(payload or "").strip().lower()
    if raw in {"off", "never", "nie", "0"}:
        return "never"
    for option, label in SLEEP_OPTION_LABELS.items():
        if raw in (label.lower(), option):
            return option
    return None


def _assist_selects(hass: HomeAssistant, entry: ConfigEntry) -> list:
    """Assistant and Finished speaking selects for the panel's microphone.

    The pipeline select registers the panel as an Assist device, which is
    where Home Assistant shows its assistant and wake word settings.
    """
    # Lazy import: assist_pipeline is only an after-dependency, and its
    # selects need it loaded.
    if "assist_pipeline" not in hass.config.components:
        return []
    from homeassistant.components.assist_pipeline import (
        AssistPipelineSelect,
        VadSensitivitySelect,
    )
    prefix = assist_select_prefix(entry_device_id(entry))
    device_info = entry_device_info(entry)
    selects = [AssistPipelineSelect(hass, DOMAIN, prefix), VadSensitivitySelect(hass, prefix)]
    for select in selects:
        select._attr_has_entity_name = True
        select._attr_device_info = device_info
    return selects


class Tab5SleepSelect(SelectEntity):
    """Auto-sleep select."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = SLEEP_OPTIONS

    def __init__(
        self,
        entry: ConfigEntry,
        base_topic: str,
        leaf: str,
        unique_id: str,
        translation_key: str,
        icon: str,
    ) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = unique_id
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._topic_cmd = command_topic(base_topic, leaf)
        self._topic_state = state_topic(base_topic, leaf)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            option = sleep_option_from_payload(msg.payload)
            if option is not None:
                self._attr_current_option = option
                self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_select_option(self, option: str) -> None:
        label = SLEEP_OPTION_LABELS.get(option)
        if label is None:
            return
        await mqtt.async_publish(self.hass, self._topic_cmd, label, qos=0, retain=False)
        self._attr_current_option = option
        self.async_write_ha_state()


class HomeTilesViewSelect(SelectEntity):
    """Navigate the existing display device with device-confirmed feedback."""

    _attr_has_entity_name = True
    _attr_translation_key = "view"
    _attr_icon = "mdi:view-dashboard"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_view"
        self._base = base_topic
        self._view = ViewNavigation()
        self._subscriptions = []
        self._last_refresh = -30.0

    @property
    def available(self) -> bool:
        return self._view.available(monotonic()) and mqtt.is_connected(self.hass)

    @property
    def options(self) -> list[str]:
        return list(self._view.targets.values())

    @property
    def current_option(self) -> str | None:
        return self._view.current_label()

    @property
    def extra_state_attributes(self):
        return {"target_id": self._view.current, "display_mode": self._view.mode,
                "view_targets": dict(self._view.targets)}

    async def _request_catalog(self) -> None:
        now = monotonic()
        if now - self._last_refresh < 2:
            return
        self._last_refresh = now
        await mqtt.async_publish(self.hass, command_topic(self._base, "view_refresh"),
                                 "refresh", qos=0, retain=False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def handle_state(msg: mqtt.ReceiveMessage) -> None:
            if len(msg.payload) > 1024:
                return
            try:
                payload = json.loads(msg.payload)
            except (TypeError, ValueError):
                return
            previous = (self._view.session, self._view.revision)
            if self._view.state(payload, monotonic(), retained=msg.retain):
                if previous != (self._view.session, self._view.revision):
                    await self._request_catalog()
                self.async_write_ha_state()

        async def handle_catalog(msg: mqtt.ReceiveMessage) -> None:
            if len(msg.payload) > 32768:
                return
            try:
                payload = json.loads(msg.payload)
            except (TypeError, ValueError):
                return
            if self._view.catalog(payload):
                self.async_write_ha_state()

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip().lower()
            if raw not in {"on", "off", "1", "0", "true", "false"}:
                return
            self._view.online = raw in {"on", "1", "true"}
            if not self._view.online:
                self._view.received_at = None
            else:
                await self._request_catalog()
            self.async_write_ha_state()

        async def refresh(_now) -> None:
            if (self._view.online and not self._view.targets
                    and monotonic() - self._last_refresh >= 60):
                await self._request_catalog()
            self.async_write_ha_state()

        for destination, handler in (
            (state_topic(self._base, "view"), handle_state),
            (f"{self._base}/view/catalog/+", handle_catalog),
            (state_topic(self._base, "connected"), handle_connected),
        ):
            self._subscriptions.append(await mqtt.async_subscribe(
                self.hass, destination, handler, qos=0))
        self._subscriptions.append(async_track_time_interval(
            self.hass, refresh, timedelta(seconds=10)))
        await self._request_catalog()

    async def async_will_remove_from_hass(self) -> None:
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    async def async_select_option(self, option: str) -> None:
        try:
            if not mqtt.is_connected(self.hass):
                raise ValueError("view_unavailable")
            payload = self._view.command(option, monotonic())
        except ValueError as err:
            raise HomeAssistantError(translation_domain="tab5_lvgl",
                                     translation_key=str(err)) from err
        await mqtt.async_publish(self.hass, command_topic(self._base, "view"),
                                 json.dumps(payload), qos=0, retain=False)


WAKE_WORD_OFF = "off"


class HomeTilesWakeWordSelect(SelectEntity):
    """Wake word the panel listens for, from the words the panel reports.

    Home Assistant's own wake word picker only appears in some frontend
    views, so the choice is also a plain device setting. It sends the same
    command as the satellite's configuration (sealed when paired).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "wake_word"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:account-voice"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry_id = entry.entry_id
        self._attr_device_info = entry_device_info(entry)
        self._attr_unique_id = assist_wake_word_select_unique_id(entry_device_id(entry))
        self._base = base_topic
        self._words: list[str] = []
        self._active: list[str] = []
        self._status_seen = False
        self._unsubscribe = None

    @property
    def available(self) -> bool:
        return self._status_seen

    @property
    def options(self) -> list[str]:
        return [*self._words, WAKE_WORD_OFF]

    @property
    def current_option(self) -> str | None:
        if not self._status_seen:
            return None
        return self._active[0] if self._active else WAKE_WORD_OFF

    def apply_status(self, payload) -> bool:
        """Mirror the panel's retained Assist status; False when ignored."""
        if not payload:
            # The panel withdrew its microphone.
            self._status_seen = False
            return True
        status = parse_status(payload)
        if status is None:
            return False
        if status["wake_words"] is not None:
            self._words = status["wake_words"]
        if status["active_wake_words"] is not None:
            self._active = [word for word in status["active_wake_words"] if word in self._words]
        self._status_seen = True
        return True

    async def async_select_option(self, option: str) -> None:
        if option != WAKE_WORD_OFF and option not in self._words:
            raise HomeAssistantError(f"Unknown wake word: {option}")
        satellite = (self.hass.data.get(DOMAIN, {})
                     .get(ASSIST_SATELLITES_KEY, {}).get(self._entry_id))
        if satellite is None:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_unavailable")
        words = [] if option == WAKE_WORD_OFF else [option]
        await satellite.async_set_active_wake_words(words)
        # The panel's retained status confirms it; until then show the request.
        self._active = words
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if self.apply_status(msg.payload):
                self.async_write_ha_state()

        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, assist_status_topic(self._base), handle_status, qos=0)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
