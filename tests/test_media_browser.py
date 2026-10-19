"""The Media popup's library: the browse result as the panel gets it."""

from __future__ import annotations

import json
import types
import unittest

from test_view_navigation import load_module

BROWSER = load_module("media_browser")


def child(index, **extra):
  return {"title": f"Item {index}", "media_class": "track", "media_content_type": "music",
          "media_content_id": f"spotify://track/{index}", "can_play": True, "can_expand": False, **extra}


class NormalizeTest(unittest.TestCase):
  def test_children_become_items(self):
    title, items, truncated = BROWSER.normalize_result({"title": "Spotify", "children": [
      child(1, thumbnail="https://i.scdn.co/image/1"), {"bad": "entry"}, child(2)]})
    self.assertEqual(title, "Spotify")
    self.assertEqual([item["title"] for item in items], ["Item 1", "Item 2"])
    self.assertEqual(items[0]["thumbnail"], "https://i.scdn.co/image/1")
    self.assertNotIn("thumbnail", items[1])
    self.assertFalse(truncated)

  def test_an_object_with_as_dict_is_read(self):
    result = types.SimpleNamespace(as_dict=lambda: {"title": "Root", "children": [child(1)]})
    self.assertEqual(BROWSER.normalize_result(result)[0], "Root")

  def test_not_a_result(self):
    self.assertIsNone(BROWSER.normalize_result(None))
    self.assertIsNone(BROWSER.normalize_result({"children": {"bad": 1}}))

  def test_long_values_are_cut_at_a_character(self):
    _, items, _ = BROWSER.normalize_result({"children": [child(1, title="ä" * 400)]})
    self.assertLessEqual(len(items[0]["title"].encode("utf-8")), BROWSER.MAX_TITLE_BYTES)
    self.assertEqual(set(items[0]["title"]), {"ä"})

  def test_at_most_max_items(self):
    _, items, truncated = BROWSER.normalize_result({"children": [child(i) for i in range(400)]})
    self.assertEqual(len(items), BROWSER.MAX_ITEMS)
    self.assertTrue(truncated)


class CatalogPagesTest(unittest.TestCase):
  def test_pages_stay_below_the_size_limit(self):
    _, items, _ = BROWSER.normalize_result({"children": [child(i, thumbnail="https://x/" + "t" * 300)
                                                          for i in range(200)]})
    pages = BROWSER.catalog_pages({"version": 1, "session": "s", "request_id": 3}, items)
    self.assertGreater(len(pages), 1)
    decoded = [json.loads(page) for page in pages]
    self.assertTrue(all(len(page.encode("utf-8")) <= BROWSER.MAX_PAGE_BYTES for page in pages))
    self.assertEqual([page["page"] for page in decoded], list(range(len(pages))))
    self.assertTrue(all(page["pages"] == len(pages) for page in decoded))
    self.assertEqual(sum(len(page["items"]) for page in decoded), 200)
    self.assertEqual(decoded[0]["session"], "s")

  def test_an_empty_level_is_one_page(self):
    pages = BROWSER.catalog_pages({"version": 1}, [])
    self.assertEqual(json.loads(pages[0])["items"], [])
    self.assertEqual(json.loads(pages[0])["pages"], 1)


if __name__ == "__main__":
  unittest.main()
