"""Pictures for linked panels (images.py, docs-dev/images.md)."""

from __future__ import annotations

import asyncio
import types
import unittest
from io import BytesIO

from test_link_protocol import load_link_module

IMAGES = load_link_module("images")
ARTWORK = load_link_module("media_artwork")
BROKER = load_link_module("link_broker")
LINK = load_link_module("link_protocol")

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

PREFIX = "ha/statestream"
TOPIC = f"{PREFIX}/media_player/tv/image/480x480"


def jpeg_source(width=640, height=360, color=(200, 40, 40)):
    output = BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="JPEG", quality=90)
    return output.getvalue()


class Session:
    def __init__(self, device_id="PANEL1", rx_max=524288):
        self.device_id = device_id
        self.base = f"hometiles_{device_id.lower()}"
        self.ha_prefix = PREFIX
        self.mode = LINK.MODE_SESSION
        self.subscriptions = set()
        self.rx_max = rx_max
        self.received = []

    def send_publish(self, topic, payload, retain):
        self.received.append((topic, payload, retain))


class TopicsAndKeysTest(unittest.TestCase):
    def test_picture_topics(self):
        self.assertEqual(IMAGES.parse_image_topic(TOPIC), ("media_player.tv", 480, 480))
        self.assertEqual(IMAGES.parse_image_topic(f"{PREFIX}/media_player/tv/image/1280x800"),
                         ("media_player.tv", 1280, 800))
        for bad in (f"{PREFIX}/media_player/tv/image/8x8", f"{PREFIX}/media_player/tv/image/2000x480",
                    f"{PREFIX}/light/desk/image/480x480", f"{PREFIX}/media_player/TV/image/480x480",
                    f"{PREFIX}/media_player/tv/image/480", f"{PREFIX}/media_player/tv/state",
                    f"{PREFIX}/media_player/tv/image/0480x480"):
            self.assertIsNone(IMAGES.parse_image_topic(bad), bad)

    def test_key_ignores_the_proxy_token_and_prefers_the_player_picture(self):
        proxy = "http://ha:8123/api/media_player_proxy/media_player.tv?token=abc&cache=f00"
        self.assertEqual(ARTWORK.image_key(proxy),
                         ARTWORK.image_key("http://ha:8123/api/media_player_proxy/media_player.tv?token=xyz&cache=f00"))
        self.assertNotEqual(ARTWORK.image_key(proxy),
                            ARTWORK.image_key("http://ha:8123/api/media_player_proxy/media_player.tv?token=abc&cache=f01"))
        self.assertRegex(ARTWORK.image_key(proxy), r"\A[0-9a-f]{16}\Z")
        payload = {"entity_picture": proxy, "media_image_url": "https://cdn.example/a.jpg"}
        self.assertEqual(ARTWORK.artwork_url(payload), "https://cdn.example/a.jpg")
        self.assertEqual(ARTWORK.artwork_url({"entity_picture": "/relative"}), "")

    def test_state_image_key_follows_the_artwork_gate(self):
        url = "https://cdn.example/a.jpg"
        self.assertEqual(ARTWORK.image_key_field({"media_image_url": url}), ARTWORK.image_key(url))
        self.assertEqual(ARTWORK.image_key_field({"entity_picture": ""}), "")
        self.assertIsNone(ARTWORK.image_key_field({"state": "playing"}))

    def test_payload_header(self):
        self.assertEqual(IMAGES.build_payload("0123456789abcdef", 480, 480, b"\xff\xd8"),
                         b"HTIMG1 0123456789abcdef 480x480\n\xff\xd8")


@unittest.skipIf(Image is None, "Pillow is not installed")
class RenderTest(unittest.TestCase):
    def test_render_fills_the_exact_size_within_the_budget(self):
        jpeg = IMAGES.render_jpeg(jpeg_source(), 480, 480, 65535)
        self.assertIsNotNone(jpeg)
        self.assertLessEqual(len(jpeg), 65535)
        with Image.open(BytesIO(jpeg)) as picture:
            self.assertEqual(picture.size, (480, 480))
            self.assertEqual(picture.format, "JPEG")
            self.assertFalse(picture.info.get("progressive"))
        self.assertIsNone(IMAGES.render_jpeg(b"not a picture", 480, 480, 65535))


@unittest.skipIf(Image is None, "Pillow is not installed")
class ImageServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = BROKER.LinkBroker()
        self.url = "https://cdn.example/song-1.jpg"
        self.fetched = []
        self.allowed = {"PANEL1": {"media_player.tv"}, "PANEL2": {"media_player.tv"}}

        async def fetch(url):
            self.fetched.append(url)
            return jpeg_source()

        async def render(data, width, height, budget):
            self.budgets.append(budget)
            return IMAGES.render_jpeg(data, width, height, budget)

        self.budgets = []
        self.service = IMAGES.ImageService(
            self.broker,
            artwork=lambda entity_id: self.url if entity_id == "media_player.tv" else "",
            fetch=fetch,
            render=render,
            allowed=lambda session, entity_id: entity_id in self.allowed.get(session.device_id, set()),
        )

    async def asyncTearDown(self):
        self.service.stop()

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    def attach(self, session):
        self.broker.attach(session)
        return session

    async def test_a_subscription_gets_the_picture_once_per_artwork(self):
        panel = self.attach(Session())
        self.broker.panel_subscribe(panel, TOPIC)
        await self.settle()
        topic, payload, _retain = panel.received[-1]
        self.assertEqual(topic, TOPIC)
        key = ARTWORK.image_key(self.url)
        self.assertTrue(payload.startswith(f"HTIMG1 {key} 480x480\n".encode()))
        self.assertEqual(self.broker.retained(TOPIC), payload)
        with Image.open(BytesIO(payload.split(b"\n", 1)[1])) as picture:
            self.assertEqual(picture.size, (480, 480))

        # A state change without new artwork renders nothing.
        self.service.artwork_changed("media_player.tv")
        await self.settle()
        self.assertEqual(len(panel.received), 1)
        self.assertEqual(self.fetched, [self.url])

        # New artwork: a new picture with its own key.
        self.url = "https://cdn.example/song-2.jpg"
        self.service.artwork_changed("media_player.tv")
        await self.settle()
        self.assertEqual(len(panel.received), 2)
        self.assertTrue(panel.received[-1][1].startswith(f"HTIMG1 {ARTWORK.image_key(self.url)} ".encode()))

    async def test_a_second_panel_gets_the_retained_picture_without_a_render(self):
        first = self.attach(Session("PANEL1"))
        self.broker.panel_subscribe(first, TOPIC)
        await self.settle()
        second = self.attach(Session("PANEL2"))
        self.broker.panel_subscribe(second, TOPIC)
        await self.settle()
        self.assertEqual(second.received, [(TOPIC, first.received[-1][1], True)])
        self.assertEqual(len(self.fetched), 1)

    async def test_players_the_panel_is_not_served_get_nothing(self):
        stranger = self.attach(Session("OTHER"))
        self.broker.panel_subscribe(stranger, TOPIC)
        await self.settle()
        self.assertEqual(stranger.received, [])
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.service.wanted(), {})

    async def test_leaving_stops_the_renders_and_budget_follows_the_smallest_room(self):
        big = self.attach(Session("PANEL1", rx_max=524288))
        small = self.attach(Session("PANEL2", rx_max=0))
        self.broker.panel_subscribe(big, TOPIC)
        self.broker.panel_subscribe(small, TOPIC)
        await self.settle()
        header = len(IMAGES.build_payload("0" * 16, 480, 480, b""))
        self.assertIn(LINK.MAX_PAYLOAD - header, self.budgets)
        self.broker.panel_unsubscribe(big, TOPIC)
        self.broker.detach(small)
        self.assertEqual(self.service.wanted(), {})
        self.url = "https://cdn.example/song-3.jpg"
        self.service.artwork_changed("media_player.tv")
        await self.settle()
        self.assertNotIn(self.url, self.fetched)


if __name__ == "__main__":
    unittest.main()


class WiringTest(unittest.TestCase):
    def test_the_integration_wires_the_service(self):
        from test_view_navigation import ROOT
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"images = _create_image_service\(hass, runtime\)\s*domain_data\[DATA_IMAGES\] = images")
        self.assertRegex(source, r"async def _async_stop_link\(_event: Event\) -> None:\s*images\.stop\(\)")
        self.assertRegex(source, r"self\._gate_media_artwork\(entity_id, payload\)\s*# Linked panels show the picture with this key \(images\.py\)\.\s*key = image_key_field\(payload\)\s*if key is not None:\s*payload\[\"image_key\"\] = key")
        self.assertIn("images.artwork_changed(entity_id)", source)
        self.assertRegex(source, r"return entity_id in getattr\(bridge, \"media_players\", \[\]\)")
