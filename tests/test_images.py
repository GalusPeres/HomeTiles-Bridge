"""Pictures for linked panels (images.py, docs-dev/images.md)."""

from __future__ import annotations

import asyncio
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
        self.assertEqual(IMAGES.parse_image_topic(f"{PREFIX}/image/frame/image/1280x800"),
                         ("image.frame", 1280, 800))
        self.assertEqual(IMAGES.parse_image_topic(f"{PREFIX}/camera/door/image/800x480"),
                         ("camera.door", 800, 480))
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
        self.assertRegex(IMAGES.content_key(b"picture"), r"\A[0-9a-f]{16}\Z")

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
        self.loads = []
        self.camera_color = (10, 20, 30)
        self.image_state = "2026-10-09T10:00:00"
        self.allowed = {"PANEL1": {"media_player.tv", "image.frame", "camera.door"},
                        "PANEL2": {"media_player.tv"}}
        self.tracked = []
        self.budgets = []

        def source(entity_id):
            if entity_id == "media_player.tv":
                url = self.url

                async def load_cover():
                    self.loads.append(url)
                    return jpeg_source()
                return IMAGES.Source(ARTWORK.image_key(url), load_cover)
            if entity_id == "image.frame":
                state = self.image_state

                async def load_image():
                    self.loads.append(state)
                    return jpeg_source(color=(0, 90, 200))
                return IMAGES.Source(IMAGES.content_key(state.encode()), load_image)
            if entity_id == "camera.door":
                async def load_still():
                    self.loads.append("camera")
                    return jpeg_source(color=self.camera_color)
                return IMAGES.Source(None, load_still, 0.05)
            return None

        async def render(data, width, height, budget):
            self.budgets.append(budget)
            return IMAGES.render_jpeg(data, width, height, budget)

        def track(entities, changed):
            entry = [sorted(entities), changed, True]
            self.tracked.append(entry)

            def untrack():
                entry[2] = False
            return untrack

        self.service = IMAGES.ImageService(
            self.broker, source=source, render=render, track=track,
            allowed=lambda session, entity_id: entity_id in self.allowed.get(session.device_id, set()),
        )

    async def asyncTearDown(self):
        self.service.stop()

    async def settle(self, rounds=20):
        for _ in range(rounds):
            await asyncio.sleep(0)

    def attach(self, session):
        self.broker.attach(session)
        return session

    def change(self, entity_id):
        for entities, changed, active in self.tracked:
            if active and entity_id in entities:
                changed(entity_id)

    def active_tracks(self):
        return [entities for entities, _changed, active in self.tracked if active]

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
        self.assertEqual(self.active_tracks(), [["media_player.tv"]])

        # A state change without new artwork renders nothing.
        self.change("media_player.tv")
        await self.settle()
        self.assertEqual(len(panel.received), 1)
        self.assertEqual(self.loads, [self.url])

        # New artwork: a new picture with its own key.
        self.url = "https://cdn.example/song-2.jpg"
        self.change("media_player.tv")
        await self.settle()
        self.assertEqual(len(panel.received), 2)
        self.assertTrue(panel.received[-1][1].startswith(f"HTIMG1 {ARTWORK.image_key(self.url)} ".encode()))

    async def test_an_image_entity_sends_a_new_picture_with_each_state(self):
        topic = f"{PREFIX}/image/frame/image/1280x800"
        panel = self.attach(Session())
        self.broker.panel_subscribe(panel, topic)
        await self.settle()
        self.assertEqual(len(panel.received), 1)
        with Image.open(BytesIO(panel.received[0][1].split(b"\n", 1)[1])) as picture:
            self.assertEqual(picture.size, (1280, 800))
        self.change("image.frame")
        await self.settle()
        self.assertEqual(len(panel.received), 1, "same state, same picture")
        self.image_state = "2026-10-09T10:05:00"
        self.change("image.frame")
        await self.settle()
        self.assertEqual(len(panel.received), 2)
        self.assertEqual(self.loads, ["2026-10-09T10:00:00", "2026-10-09T10:05:00"])

    async def test_a_camera_is_reloaded_while_shown_and_sent_only_when_it_changed(self):
        topic = f"{PREFIX}/camera/door/image/480x480"
        panel = self.attach(Session())
        self.broker.panel_subscribe(panel, topic)
        await self.settle()
        self.assertEqual(len(panel.received), 1)
        await asyncio.sleep(0.12)
        await self.settle()
        self.assertGreaterEqual(self.loads.count("camera"), 2, "reloaded on its interval")
        self.assertEqual(len(panel.received), 1, "an unchanged still image is not sent again")
        self.camera_color = (200, 200, 30)
        await asyncio.sleep(0.08)
        await self.settle()
        self.assertEqual(len(panel.received), 2)
        self.broker.panel_unsubscribe(panel, topic)
        loads = self.loads.count("camera")
        await asyncio.sleep(0.12)
        await self.settle()
        self.assertEqual(self.loads.count("camera"), loads, "no reloads once nobody shows it")
        self.assertEqual(self.active_tracks(), [])

    async def test_a_second_panel_gets_the_retained_picture_without_a_render(self):
        first = self.attach(Session("PANEL1"))
        self.broker.panel_subscribe(first, TOPIC)
        await self.settle()
        second = self.attach(Session("PANEL2"))
        self.broker.panel_subscribe(second, TOPIC)
        await self.settle()
        self.assertEqual(second.received, [(TOPIC, first.received[-1][1], True)])
        self.assertEqual(len(self.loads), 1)

    async def test_entities_the_panel_is_not_served_get_nothing(self):
        stranger = self.attach(Session("PANEL2"))
        self.broker.panel_subscribe(stranger, f"{PREFIX}/image/frame/image/480x480")
        self.broker.panel_subscribe(stranger, f"{PREFIX}/camera/door/image/480x480")
        other = self.attach(Session("OTHER"))
        self.broker.panel_subscribe(other, TOPIC)
        await self.settle()
        self.assertEqual(stranger.received, [])
        self.assertEqual(other.received, [])
        self.assertEqual(self.loads, [])
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
        self.service.entity_changed("media_player.tv")
        await self.settle()
        self.assertNotIn(self.url, self.loads)


    async def test_a_subscription_before_the_declaration_gets_the_picture_after_it(self):
        # The panel subscribes a newly chosen picture at once and declares the
        # entity 1.5 s later (the screensaver's image.diashow_collection).
        topic = f"{PREFIX}/image/frame/image/1280x800"
        self.allowed["PANEL1"].discard("image.frame")
        panel = self.attach(Session())
        with self.assertLogs(IMAGES._LOGGER, level="INFO") as logs:
            self.broker.panel_subscribe(panel, topic)
            await self.settle()
            self.assertEqual(panel.received, [])
            self.service.recheck()
            await self.settle()
            self.assertEqual(panel.received, [], "still not served: nothing")
            self.allowed["PANEL1"].add("image.frame")
            self.service.recheck()
            await self.settle()
        self.assertEqual(len(panel.received), 1)
        self.assertEqual(self.service.wanted(), {topic: {panel}})
        self.assertEqual(self.active_tracks(), [["image.frame"]])
        self.assertEqual(len(logs.output), 2, "one line per outcome")
        self.assertIn(f"{topic} waits: the entity is not served", logs.output[0])
        self.assertIn(f"{topic} sent (", logs.output[1])

    async def test_a_picture_no_longer_served_stops(self):
        topic = f"{PREFIX}/camera/door/image/480x480"
        panel = self.attach(Session())
        self.broker.panel_subscribe(panel, topic)
        await self.settle()
        self.assertEqual(len(panel.received), 1)
        self.allowed["PANEL1"].discard("camera.door")
        self.service.recheck()
        self.assertEqual(self.service.wanted(), {})
        loads = self.loads.count("camera")
        await asyncio.sleep(0.12)
        await self.settle()
        self.assertEqual(self.loads.count("camera"), loads, "no reloads once the camera is no longer served")
        self.assertEqual(self.active_tracks(), [])
        # Served again: the kept subscription gets the camera back.
        self.allowed["PANEL1"].add("camera.door")
        self.service.recheck()
        await self.settle()
        self.assertEqual(self.service.wanted(), {topic: {panel}})
        self.assertGreater(self.loads.count("camera"), loads)
        # Unsubscribed while refused: nothing is kept.
        self.allowed["PANEL1"].discard("camera.door")
        self.service.recheck()
        self.broker.panel_unsubscribe(panel, topic)
        self.allowed["PANEL1"].add("camera.door")
        self.service.recheck()
        self.assertEqual(self.service.wanted(), {})

    async def test_large_sources_stay_out_of_the_source_cache(self):
        big = b"x" * (IMAGES.SOURCE_CACHE_MAX_BYTES + 1)

        async def load_big():
            return big
        self.assertIs(await self.service._load(IMAGES.Source("b" * 16, load_big)), big)
        self.assertNotIn("b" * 16, self.service._sources)

        half = b"y" * (IMAGES.SOURCE_CACHE_MAX_BYTES // 2 + 1)

        async def load_half():
            return half
        await self.service._load(IMAGES.Source("1" * 16, load_half))
        await self.service._load(IMAGES.Source("2" * 16, load_half))
        self.assertEqual(list(self.service._sources), ["2" * 16], "the total stays within the bound")


class WiringTest(unittest.TestCase):
    def test_the_integration_wires_the_service(self):
        from test_view_navigation import ROOT
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"images = _create_image_service\(hass, runtime\)\s*domain_data\[DATA_IMAGES\] = images")
        self.assertRegex(source, r"async def _async_stop_link\(_event: Event\) -> None:\s*images\.stop\(\)")
        self.assertRegex(source, r"self\._gate_media_artwork\(entity_id, payload\)\s*# Linked panels show the picture with this key \(images\.py\)\.\s*key = image_key_field\(payload\)\s*if key is not None:\s*payload\[\"image_key\"\] = key")
        self.assertNotIn("artwork_changed", source)
        self.assertIn('"camera": ("images", "cameras"),', source)
        self.assertIn("return ImageSource(None, lambda: camera_still(entity_id), CAMERA_REFRESH_S)", source)
        self.assertIn("return async_track_state_change_event(hass, list(entities), _changed)", source)
        # An image entity's picture comes from the entity, up to a full-size
        # photo; only covers keep the small download limit.
        self.assertRegex(source, r'component = hass\.data\.get\("image"\)\s*entity = component\.get_entity\(entity_id\) if hasattr\(component, "get_entity"\) else None')
        self.assertRegex(source, r"async with asyncio\.timeout\(IMAGE_PICTURE_TIMEOUT_S\):\s*data = await entity\.async_image\(\)")
        self.assertIn("data = await fetch(url, IMAGE_PICTURE_MAX_BYTES)", source)
        self.assertIn("lambda: image_picture(entity_id, url))", source)
        self.assertIn("async def fetch(url: str, limit: int = MEDIA_COVER_FETCH_MAX_BYTES)", source)
        # Every change of the served lists rechecks the kept subscriptions,
        # once the entry is set up.
        self.assertRegex(source, r'if images is not None and domain_data\.get\("entries", \{\}\)\.get\(self\.entry\.entry_id\) is self:\s*images\.recheck\(\)')

    def test_images_are_a_released_and_searchable_list(self):
        search = load_link_module("entity_search")
        self.assertEqual(search.LIST_DOMAINS["images"], ("image", "camera"))
        self.assertEqual(search.LIST_ATTRS["images"], ("images", "cameras"))
        served = search.served_lists({"images": ["image.frame"], "cameras": ["camera.door"]},
                                     {"images": [], "cameras": []},
                                     {"own": True, "lists": {"images": ["image.frame", "camera.door", "image.secret"]}})
        self.assertEqual(served["images"], ["image.frame", "camera.door"])


if __name__ == "__main__":
    unittest.main()
