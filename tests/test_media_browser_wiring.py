import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.tab5_lvgl import Tab5Bridge
from custom_components.tab5_lvgl.media_browser import MediaBrowser


class DummyConfigEntry:
    def __init__(self):
        self.entry_id = "entry-1"
        self.data = {
            "device_id": "dev-1",
            "base_topic": "home/hometiles",
            "ha_prefix": "homeassistant",
            "sensors": [],
            "binary_sensors": [],
            "weathers": [],
            "lights": [],
            "switches": [],
            "media_players": ["media_player.kuche"],
            "climates": [],
            "covers": [],
            "cameras": [],
            "scene_map": {},
        }
        self.options = {}
        self.title = "Test Panel"


@pytest.mark.asyncio
async def test_browse_media_dispatches_to_browse_handler(monkeypatch):
    hass = MagicMock()
    hass.data = {}
    hass.config_entries = MagicMock()
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    bridge = Tab5Bridge.__new__(Tab5Bridge)
    bridge.hass = hass
    bridge.base_topic = "home/hometiles"
    bridge.media_players = ["media_player.kuche"]
    bridge._media_browser = MediaBrowser()
    bridge._async_handle_media_browse_command = AsyncMock()

    msg = types.SimpleNamespace(
        payload='{"entity_id":"media_player.kuche","command":"browse_media","session":"abc","request_id":1,"revision":1,"media_content_id":"","media_content_type":""}'
    )

    await bridge._async_handle_media_command(msg)

    bridge._async_handle_media_browse_command.assert_awaited_once()
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_play_media_still_calls_media_player_service(monkeypatch):
    hass = MagicMock()
    hass.data = {}
    hass.config_entries = MagicMock()
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    bridge = Tab5Bridge.__new__(Tab5Bridge)
    bridge.hass = hass
    bridge.base_topic = "home/hometiles"
    bridge.media_players = ["media_player.kuche"]
    bridge._media_browser = MediaBrowser()

    msg = types.SimpleNamespace(
        payload='{"entity_id":"media_player.kuche","command":"play_media","media_content_id":"spotify://track/123","media_content_type":"music"}'
    )

    await bridge._async_handle_media_command(msg)

    hass.services.async_call.assert_awaited_once()
    call_args = hass.services.async_call.call_args
    assert call_args.args[0] == "media_player"
    assert call_args.args[1] == "play_media"
    assert call_args.args[2]["entity_id"] == "media_player.kuche"
    assert call_args.args[2]["media_content_id"] == "spotify://track/123"
    assert call_args.args[2]["media_content_type"] == "music"