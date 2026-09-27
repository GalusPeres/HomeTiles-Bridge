import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "tab5_lvgl" / "media_browser.py"


def load_media_browser_module():
    spec = importlib.util.spec_from_file_location(
        "media_browser_test_module",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEDIA_BROWSER = load_media_browser_module()

MediaBrowserState = MEDIA_BROWSER.MediaBrowserState
normalize_browse_result = MEDIA_BROWSER.normalize_browse_result
paginate_items = MEDIA_BROWSER.paginate_items
serialize_catalog_page = MEDIA_BROWSER.serialize_catalog_page

class TestBrowseNormalization(unittest.TestCase):
    def test_normalize_basic_root_result(self):
        result = {
            "title": "Sonos",
            "media_class": "directory",
            "media_content_type": "root",
            "media_content_id": "",
            "can_play": False,
            "can_expand": True,
            "can_search": False,
            "children": [
                {
                    "title": "Favorites",
                    "media_class": "directory",
                    "media_content_type": "favorites",
                    "media_content_id": "",
                    "can_play": False,
                    "can_expand": True,
                    "can_search": False,
                    "thumbnail": "/api/brands/integration/sonos/logo.png",
                },
                {
                    "title": "AI generated images",
                    "media_class": "app",
                    "media_content_type": "app",
                    "media_content_id": "media-source://ai_task",
                    "can_play": False,
                    "can_expand": True,
                    "can_search": False,
                    "thumbnail": None,
                },
            ],
        }

        normalized = normalize_browse_result(result)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["title"], "Sonos")
        self.assertEqual(len(normalized["items"]), 2)
        self.assertEqual(normalized["items"][0]["title"], "Favorites")
        self.assertEqual(
            normalized["items"][1]["media_content_id"],
            "media-source://ai_task",
        )

    def test_normalize_invalid_children_rejected(self):
        result = {"title": "Root", "children": {"bad": "value"}}
        self.assertIsNone(normalize_browse_result(result))

    def test_normalize_skips_invalid_item(self):
        result = {
            "title": "Root",
            "children": [
                {"bad": "entry"},
                {
                    "title": "Good",
                    "media_content_type": "album",
                    "media_content_id": "spotify://album/123",
                    "can_play": True,
                    "can_expand": False,
                    "can_search": False,
                },
            ],
        }
        normalized = normalize_browse_result(result)
        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["title"], "Good")

    def test_normalize_truncates_long_values(self):
        result = {
            "title": "X" * 10000,
            "media_class": "directory",
            "media_content_type": "root",
            "media_content_id": "",
            "children": [
                {
                    "title": "Y" * 5000,
                    "media_class": "album",
                    "media_content_type": "album",
                    "media_content_id": "spotify://album/" + ("Z" * 5000),
                    "can_play": True,
                    "can_expand": False,
                    "can_search": False,
                }
            ],
        }
        normalized = normalize_browse_result(result)
        self.assertLess(len(normalized["title"].encode("utf-8")), 10000)
        self.assertLess(len(normalized["items"][0]["media_content_id"].encode("utf-8")), 10000)


class TestPagination(unittest.TestCase):
    def test_paginate_items(self):
        items = [{"title": f"Item {i}"} for i in range(70)]
        page_count, pages = paginate_items(items, max_items_per_page=32)
        self.assertEqual(page_count, 3)
        self.assertEqual(len(pages), 3)
        self.assertEqual(len(pages[0]["items"]), 32)
        self.assertEqual(len(pages[1]["items"]), 32)
        self.assertEqual(len(pages[2]["items"]), 6)

    def test_serialize_catalog_page(self):
        payload = {
            "version": 1,
            "session": "abc",
            "revision": 1,
            "request_id": 2,
            "page": 0,
            "pages": 1,
            "entity_id": "media_player.kuche",
            "parent": {"media_content_id": "", "media_content_type": ""},
            "items": [{"title": "Favorites", "media_content_type": "favorites"}],
        }
        text = serialize_catalog_page(payload)
        self.assertIn('"session":"abc"', text)
        self.assertIn('"entity_id":"media_player.kuche"', text)


class TestMediaBrowserState(unittest.TestCase):
    def test_session_accepts_two_pages(self):
        state = MediaBrowserState()
        state.begin(
            session="sess-1",
            revision=3,
            request_id=10,
            entity_id="media_player.kuche",
            page_count=2,
        )

        self.assertTrue(
            state.accept_page(
                session="sess-1",
                revision=3,
                request_id=10,
                page=0,
                items=[{"title": "A"}],
            )
        )
        self.assertFalse(state.complete)

        self.assertTrue(
            state.accept_page(
                session="sess-1",
                revision=3,
                request_id=10,
                page=1,
                items=[{"title": "B"}],
            )
        )
        self.assertTrue(state.complete)

    def test_old_session_is_rejected(self):
        state = MediaBrowserState()
        state.begin(
            session="sess-1",
            revision=3,
            request_id=10,
            entity_id="media_player.kuche",
            page_count=1,
        )
        self.assertFalse(
            state.accept_page(
                session="sess-2",
                revision=3,
                request_id=10,
                page=0,
                items=[{"title": "bad"}],
            )
        )

    def test_old_revision_is_rejected(self):
        state = MediaBrowserState()
        state.begin(
            session="sess-1",
            revision=3,
            request_id=10,
            entity_id="media_player.kuche",
            page_count=1,
        )
        self.assertFalse(
            state.accept_page(
                session="sess-1",
                revision=4,
                request_id=10,
                page=0,
                items=[{"title": "bad"}],
            )
        )

    def test_page_out_of_range_is_rejected(self):
        state = MediaBrowserState()
        state.begin(
            session="sess-1",
            revision=3,
            request_id=10,
            entity_id="media_player.kuche",
            page_count=1,
        )
        self.assertFalse(
            state.accept_page(
                session="sess-1",
                revision=3,
                request_id=10,
                page=2,
                items=[{"title": "bad"}],
            )
        )


if __name__ == "__main__":
    unittest.main()