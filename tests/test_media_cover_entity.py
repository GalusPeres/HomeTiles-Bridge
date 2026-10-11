"""Player covers behind Home Assistant's own proxy come from the player itself.

Bridge issue #10: with Home Assistant on https and a certificate from a private
CA, the Bridge's request to HA's own address failed
(ClientConnectorCertificateError), so a HomePod's cover never reached the
panel. Both ways to the panel (the link's pictures and the MQTT state payload)
now read such a cover from the entity, like HA's media_player_proxy does.
"""

from __future__ import annotations

import asyncio
import base64
import types
import unittest

from test_announcement_guard import extract
from test_link_protocol import load_link_module

ARTWORK = load_link_module("media_artwork")
IMAGES = load_link_module("images")
CONTROL = load_link_module("control_helpers")

PLAYER = "media_player.homepod"
PROXY = "https://hass.home/api/media_player_proxy/media_player.homepod?token=abc&cache=f00"
CDN = "https://i.scdn.co/image/song.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
CDN_JPEG = b"\xff\xd8\xff\xe0" + b"c" * 64


class CertificateError(Exception):
    """Stands in for aiohttp's ClientConnectorCertificateError."""


class Player:
    def __init__(self, picture=(JPEG, "image/jpeg"), error=None):
        self.picture = picture
        self.error = error
        self.calls = 0

    async def async_get_media_image(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.picture


class Players:
    def __init__(self, entities):
        self.entities = entities

    def get_entity(self, entity_id):
        return self.entities.get(entity_id)


class Response:
    def __init__(self, data):
        self.status = 200
        self.headers = {"content-type": "image/jpeg"}
        self.content = types.SimpleNamespace(iter_chunked=self._chunks)
        self._data = data

    async def _chunks(self, size):
        yield self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Web:
    """The Bridge's HTTP session: HA's own https address fails its certificate."""

    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if ARTWORK.is_ha_media_proxy(url):
            raise CertificateError("private CA")
        return Response(CDN_JPEG)


class Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda message, *args: self.lines.append((level, message % args))


def scope(web, log):
    async def executor(function, *args):
        return function(*args)

    return {
        "asyncio": asyncio,
        "base64": base64,
        "monotonic": lambda: 0.0,
        "_LOGGER": log,
        "async_get_clientsession": lambda hass: web,
        "is_ha_media_proxy": ARTWORK.is_ha_media_proxy,
        "artwork_url": ARTWORK.artwork_url,
        "image_key": ARTWORK.image_key,
        "entity_domain": CONTROL.entity_domain,
        "ImageSource": IMAGES.Source,
        "ImageService": lambda broker, **parts: parts,
        "_resize_media_cover": lambda data: (data, "image/jpeg"),
        "MEDIA_COVER_ENTITY_TIMEOUT_S": 10,
        "MEDIA_COVER_FETCH_MAX_BYTES": 1_500_000,
        "MEDIA_COVER_MAX_BYTES": 14000,
        "MEDIA_COVER_CACHE_MAX": 24,
        "MEDIA_COVER_WARNING_INTERVAL_SECONDS": 15 * 60,
        "MEDIA_COVER_WARNING_MAX_KEYS": 64,
        "executor": executor,
    }


def hass_with(players, states=None):
    async def add_executor_job(function, *args):
        return function(*args)

    return types.SimpleNamespace(
        data={"media_player": Players(players)},
        states=types.SimpleNamespace(get=lambda entity_id: (states or {}).get(entity_id)),
        async_add_executor_job=add_executor_job,
    )


class ProxyAddressTest(unittest.TestCase):
    def test_only_home_assistants_player_proxy_counts(self):
        self.assertTrue(ARTWORK.is_ha_media_proxy(PROXY))
        self.assertTrue(ARTWORK.is_ha_media_proxy("http://192.168.1.2:8123/api/media_player_proxy/media_player.tv?token=x"))
        for url in (CDN, "https://hass.home/local/cover.jpg", "https://cdn.test/api/media_player_proxy",
                    "https://cdn.test/x/api/media_player_proxy/a", "", "http://[broken"):
            self.assertFalse(ARTWORK.is_ha_media_proxy(url), url)


class MqttCoverTest(unittest.IsolatedAsyncioTestCase):
    """The state payload of panels that still use MQTT."""

    def bridge(self, players):
        self.web, self.log = Web(), Log()
        names = {"_media_player_entity", "_async_media_player_image", "_is_png_payload",
                 "_async_attach_media_cover_data", "_async_download_media_cover", "_log_media_cover_warning"}
        found = extract(names, scope(self.web, self.log))
        bridge = type("Bridge", (), {name: found[name] for name in names if name.startswith(("_async_attach", "_async_download", "_log"))})()
        bridge.hass = hass_with(players)
        bridge._media_cover_cache = {}
        bridge._media_cover_warning_last = {}
        return bridge

    async def test_a_proxy_cover_comes_from_the_player_without_a_request(self):
        player = Player()
        bridge = self.bridge({PLAYER: player})
        payload = {"state": "playing", "entity_picture": PROXY}
        await bridge._async_attach_media_cover_data(PLAYER, payload)
        self.assertEqual(base64.b64decode(payload["entity_picture_data"]), JPEG)
        self.assertEqual(payload["entity_picture_mime"], "image/jpeg")
        self.assertEqual(player.calls, 1)
        self.assertEqual(self.web.urls, [], "no request to Home Assistant's own https address")
        self.assertFalse([line for line in self.log.lines if line[0] == "warning"])

    async def test_a_player_out_of_reach_still_downloads_like_before(self):
        bridge = self.bridge({})
        payload = {"state": "playing", "entity_picture": PROXY}
        await bridge._async_attach_media_cover_data(PLAYER, payload)
        self.assertNotIn("entity_picture_data", payload)
        self.assertEqual(self.web.urls, [PROXY])
        self.assertIn(("warning", f"Tab5 media cover fetch failed for {PLAYER}: CertificateError"), self.log.lines)

    async def test_an_upstream_cover_is_still_downloaded(self):
        player = Player()
        bridge = self.bridge({PLAYER: player})
        payload = {"state": "playing", "media_image_url": CDN, "entity_picture": PROXY}
        await bridge._async_attach_media_cover_data(PLAYER, payload)
        self.assertEqual(base64.b64decode(payload["entity_picture_data"]), CDN_JPEG)
        self.assertEqual(self.web.urls, [CDN])
        self.assertEqual(player.calls, 0)

    async def test_a_failing_player_leaves_the_payload_without_cover(self):
        bridge = self.bridge({PLAYER: Player(error=TimeoutError())})
        payload = {"state": "playing", "entity_picture": PROXY}
        await bridge._async_attach_media_cover_data(PLAYER, payload)
        self.assertNotIn("entity_picture_data", payload)
        self.assertEqual(self.web.urls, [])
        self.assertIn(("warning", f"Tab5 media cover skipped for {PLAYER}: empty response"), self.log.lines)

    async def test_a_player_without_picture_sends_no_cover(self):
        bridge = self.bridge({PLAYER: Player(picture=(None, None))})
        payload = {"state": "playing", "entity_picture": PROXY}
        await bridge._async_attach_media_cover_data(PLAYER, payload)
        self.assertNotIn("entity_picture_data", payload)
        self.assertEqual(self.web.urls, [])


class LinkPictureTest(unittest.IsolatedAsyncioTestCase):
    """The pictures of linked panels (images.py)."""

    def service(self, players, attributes):
        self.web, self.log = Web(), Log()
        state = types.SimpleNamespace(state="playing", attributes=attributes)
        found = extract({"_create_image_service", "_media_player_entity", "_async_media_player_image"},
                        dict(scope(self.web, self.log),
                             _extract_media_player_payload=lambda state, hass=None: dict(state.attributes)))
        hass = hass_with(players, {PLAYER: state})
        return found["_create_image_service"](hass, types.SimpleNamespace(broker=None))

    async def test_a_proxy_cover_comes_from_the_player(self):
        player = Player()
        parts = self.service({PLAYER: player}, {"entity_picture": PROXY})
        source = parts["source"](PLAYER)
        self.assertEqual(source.key, ARTWORK.image_key(PROXY), "the picture keeps its name")
        self.assertEqual(await source.load(), JPEG)
        self.assertEqual(self.web.urls, [])

    async def test_without_the_player_and_for_upstream_covers_the_address_is_used(self):
        parts = self.service({}, {"entity_picture": PROXY})
        self.assertIsNone(await parts["source"](PLAYER).load())
        self.assertEqual(self.web.urls, [PROXY])
        parts = self.service({PLAYER: Player()}, {"media_image_url": CDN, "entity_picture": PROXY})
        self.assertEqual(await parts["source"](PLAYER).load(), CDN_JPEG)
        self.assertEqual(self.web.urls, [CDN])

    async def test_an_oversized_player_picture_is_dropped(self):
        parts = self.service({PLAYER: Player(picture=(b"x" * 1_500_001, "image/jpeg"))}, {"entity_picture": PROXY})
        self.assertIsNone(await parts["source"](PLAYER).load())


if __name__ == "__main__":
    unittest.main()
