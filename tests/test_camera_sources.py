"""The Bridge's own cameras: one entity per thing, the panels' declarations, webcams,
their pictures at fixed times and the portals' archives."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl"

_package = types.ModuleType("hometiles_sources_pkg")
_package.__path__ = [str(ROOT)]
sys.modules.setdefault("hometiles_sources_pkg", _package)
SOURCES = importlib.import_module("hometiles_sources_pkg.camera_sources")
WEBCAM = importlib.import_module("hometiles_sources_pkg.webcam_source")
STORE = importlib.import_module("hometiles_sources_pkg.picture_store")
ARCHIVES = importlib.import_module("hometiles_sources_pkg.webcam_archives")


def jpeg(color=(10, 20, 30), size=(640, 480)):
  output = BytesIO()
  Image.new("RGB", size, color).save(output, format="JPEG")
  return output.getvalue()


async def direct(fn, *args):
  return fn(*args)


class Network:
  def __init__(self, answers=None):
    self.answers = dict(answers or {})
    self.requests = []

  async def fetch(self, url, limit):
    self.requests.append(url)
    return self.answers.get(url)


class Clock:
  def __init__(self, now=999600.0):
    self.now = now

  def __call__(self):
    return self.now


class AddressTests(unittest.TestCase):
  def test_only_https_to_a_public_name(self):
    for good in ("https://www.bergfex.at/x/webcams/c62/", "https://images.bergfex.at/webcams/?id=62&format=4",
                 "https://8.8.8.8/x.jpg"):
      self.assertTrue(WEBCAM.url_allowed(good), good)
    for bad in ("http://www.bergfex.at/", "ftp://a.b/c", "https://localhost/x", "https://192.168.1.2/x.jpg",
                "https://10.0.0.1/", "https://[::1]/", "https://homeassistant/x", "https://nas.local/x",
                "https://router.lan/x", "https://user:pw@a.example/", "https://a.example/" + "x" * 500, "", None, 5):
      self.assertFalse(WEBCAM.url_allowed(bad), bad)

  def test_a_page_names_its_picture(self):
    # As Bergfex, Foto-Webcam and Panomax write it, seen in their raw pages.
    bergfex = b'<head><meta property="og:image" content="https://images.bergfex.at/webcams/?id=62&amp;format=4" /></head>'
    self.assertEqual(WEBCAM.page_picture(bergfex, "https://www.bergfex.at/x/"),
                     "https://images.bergfex.at/webcams/?id=62&format=4", "Entities are undone")
    foto = b"<meta property='og:image' content='https://www.foto-webcam.eu/webcam/zugspitze/current/1200.jpg' />"
    self.assertEqual(WEBCAM.page_picture(foto, "https://www.foto-webcam.eu/webcam/zugspitze/"),
                     "https://www.foto-webcam.eu/webcam/zugspitze/current/1200.jpg")
    reversed_order = b'<meta content="/a.jpg" name="twitter:image">'
    self.assertEqual(WEBCAM.page_picture(reversed_order, "https://x.example/p/"), "https://x.example/a.jpg")
    self.assertIsNone(WEBCAM.page_picture(b"<html><meta name='title' content='x'></html>", "https://x.example/"))


class WebcamTests(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self._dir = tempfile.TemporaryDirectory()
    self.addCleanup(self._dir.cleanup)
    self.directory = Path(self._dir.name) / "cam"
    self.clock = Clock()

  def webcam(self, network, url="https://cam.example/page"):
    return WEBCAM.WebcamSource(url, fetch=network.fetch, run_blocking=direct, directory=self.directory, clock=self.clock)

  async def test_a_direct_picture_is_taken_as_it_is(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    self.assertTrue(await cam.async_refresh())
    self.assertEqual(cam.times(), [999600])
    self.assertTrue((await cam.async_frame_at(999600)).startswith(b"\xff\xd8"))
    self.assertFalse(cam.failed)

  async def test_a_page_is_read_once_then_only_its_picture(self):
    network = Network({"https://cam.example/page": b'<meta property="og:image" content="https://img.example/c.jpg">',
                       "https://img.example/c.jpg": jpeg()})
    cam = self.webcam(network)
    await cam.async_refresh()
    self.clock.now += 600
    network.answers["https://img.example/c.jpg"] = jpeg((200, 0, 0))
    await cam.async_refresh()
    self.assertEqual(network.requests.count("https://cam.example/page"), 1)
    self.assertEqual(len(cam.times()), 2)

  async def test_an_unchanged_picture_is_no_new_frame(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    told = []
    cam.add_listener(lambda: told.append(True))
    await cam.async_refresh()
    self.clock.now += 600
    await cam.async_refresh()
    self.assertEqual(len(cam.times()), 1)
    self.assertEqual(len(told), 1)

  async def test_a_page_without_a_picture_says_so_and_stays_quiet_about_a_dead_network(self):
    network = Network({"https://cam.example/page": b"<html>no picture</html>"})
    cam = self.webcam(network)
    self.assertFalse(await cam.async_refresh())
    self.assertTrue(cam.failed)
    down = self.webcam(Network())
    self.assertFalse(await down.async_refresh())
    self.assertFalse(down.failed, "Not reached says nothing about the address")

  async def test_a_site_that_refuses_is_told_apart_and_recovers(self):
    calls = []
    answer = {"data": None}

    async def fetch(url, limit):
      calls.append(url)
      if answer["data"] is None:
        raise WEBCAM.SiteRefused(url)
      return answer["data"]

    cam = self.webcam(Network())
    cam._fetch = fetch
    told = []
    cam.add_listener(lambda: told.append(cam.refused))
    self.assertFalse(await cam.async_refresh())
    self.assertTrue(cam.refused)
    self.assertFalse(cam.failed, "Refused says nothing about a picture on the page")
    self.assertEqual(told, [True])
    answer["data"] = jpeg()
    self.assertTrue(await cam.async_refresh())
    self.assertFalse(cam.refused)
    self.assertTrue(cam.times())

  async def test_a_page_may_not_point_into_the_home_network(self):
    network = Network({"https://cam.example/page": b'<meta property="og:image" content="https://192.168.1.2/c.jpg">',
                       "https://192.168.1.2/c.jpg": jpeg()})
    cam = self.webcam(network)
    self.assertFalse(await cam.async_refresh())
    self.assertTrue(cam.failed)
    self.assertNotIn("https://192.168.1.2/c.jpg", network.requests)

  async def test_a_day_stays_and_the_frames_survive_a_restart(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    first = cam.times()[0]
    self.clock.now += 3600
    network.answers["https://cam.example/x.jpg"] = jpeg((200, 0, 0))
    await cam.async_refresh()
    again = self.webcam(network, "https://cam.example/x.jpg")
    await again._store.async_load()
    self.assertEqual(len(again.times()), 2)
    self.assertTrue((await again.async_frame_at(first)).startswith(b"\xff\xd8"), "Read from the disk")
    self.clock.now += WEBCAM.KEEP_S + 1
    network.answers["https://cam.example/x.jpg"] = jpeg((0, 200, 0))
    await cam.async_refresh()
    self.assertEqual(len(cam.times()), 1, "Older than a day is gone, the newest stays")
    self.assertEqual(len(list(self.directory.glob("*.jpg"))), 1)

  async def test_a_large_picture_is_stored_smaller(self):
    network = Network({"https://cam.example/x.jpg": jpeg(size=(4000, 3000))})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    with Image.open(BytesIO(await cam.async_picture())) as image:
      self.assertEqual(max(image.size), WEBCAM.STORED_MAX_SIDE)

  async def test_a_picture_has_the_time_its_site_gives_it_on_the_grid(self):
    network = Network({"https://cam.example/x.jpg": WEBCAM.Answer(jpeg(), modified=999600 - 1800 + 22)})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    self.assertEqual(cam.times(), [999600 - 1800], "Last-Modified, on the 10-minute grid")
    self.clock.now += 240
    network.answers["https://cam.example/x.jpg"] = jpeg((200, 0, 0))
    await cam.async_refresh()
    self.assertEqual(cam.times(), [999600 - 1800, 999600], "Without a time: when it came, on the grid")

  async def test_a_server_time_in_the_future_is_not_believed(self):
    network = Network({"https://cam.example/x.jpg": WEBCAM.Answer(jpeg(), modified=999600 + 86400)})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    self.assertEqual(cam.times(), [999600])

  async def test_two_pictures_in_one_slot_are_one_frame(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    self.clock.now += 120
    network.answers["https://cam.example/x.jpg"] = jpeg((200, 0, 0))
    await cam.async_refresh()
    self.assertEqual(cam.times(), [999600])
    self.assertEqual(len(list(self.directory.glob("*.jpg"))), 1)

  async def test_the_same_picture_after_a_restart_is_not_kept_twice(self):
    # User 2026-11-07: 9:52 and 9:54 showed the same picture after the Bridge's update.
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    self.clock.now += 700
    again = self.webcam(network, "https://cam.example/x.jpg")
    await again.async_refresh()
    self.assertEqual(again.times(), [999600])

  async def test_older_betas_files_become_one_picture_per_slot(self):
    self.directory.mkdir(parents=True)
    for when, color in ((999600 + 120, (1, 2, 3)), (999600 + 240, (4, 5, 6)), (999600 + 700, (7, 8, 9))):
      (self.directory / f"{when}_{when:016x}.jpg").write_bytes(jpeg(color))
    cam = self.webcam(Network(), "https://cam.example/x.jpg")
    await cam._store.async_load()
    self.assertEqual(cam.times(), [999600, 1000200])
    self.assertEqual(sorted(path.name.split("_")[0] for path in self.directory.glob("*.jpg")), ["1000200", "999600"])
    with Image.open(BytesIO(await cam.async_frame_at(999600))) as image:
      self.assertEqual(image.getpixel((5, 5))[0] // 2, 2, "The later of the two in a slot stays")

  async def test_a_portal_with_an_archive_fills_the_last_day(self):
    WEBCAM.ARCHIVE_PAUSE_S = 0
    self.clock.now = 1793995800.0  # 2026-11-06 20:10 UTC, 21:10 in Innsbruck
    page = "https://www.foto-webcam.eu/webcam/innsbruck/"
    current = "https://www.foto-webcam.eu/webcam/innsbruck/current/1200.jpg"
    network = Network({page: b"<meta property='og:image' content='" + current.encode() + b"'>",
                       current: WEBCAM.Answer(jpeg(), modified=int(self.clock.now) + 22)})
    archive = ARCHIVES.archive_for(page)
    for index, (when, url) in enumerate(archive.addresses(self.clock.now - STORE.KEEP_S, self.clock.now)):
      if index % 7 != 3:  # gaps, as at night
        network.answers[url] = jpeg((index % 250, 0, 0))
    cam = self.webcam(network, page)
    told = []
    cam.add_listener(lambda: told.append(len(cam.times())))
    await cam.async_refresh()
    self.assertEqual(cam.times(), [1793995800])
    loaded = await cam.async_fill_from_archive()
    self.assertGreater(loaded, 100)
    self.assertLessEqual(len(cam.times()), STORE.MAX_FRAMES)
    self.assertEqual(cam.times()[-1], 1793995800, "The current picture keeps its slot")
    self.assertTrue(all(when % 600 == 0 for when in cam.times()))
    self.assertGreaterEqual(cam.times()[0], self.clock.now - STORE.KEEP_S)
    self.assertNotIn("https://www.foto-webcam.eu/webcam/innsbruck/2026/11/06/2110_lm.jpg",
                     network.requests, "The current picture's slot is not asked again")
    self.assertEqual(network.requests[2], "https://www.foto-webcam.eu/webcam/innsbruck/2026/11/06/2100_lm.jpg",
                     "Newest first")
    self.assertGreater(len(told), 5, "The time bar grows while the day comes in")
    again = await cam.async_fill_from_archive()
    self.assertEqual(again, 0)

  async def test_any_other_webcam_has_no_archive(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    await cam.async_refresh()
    self.assertEqual(await cam.async_fill_from_archive(), 0)
    self.assertEqual(network.requests, ["https://cam.example/x.jpg"])

  async def test_it_runs_until_it_is_stopped_and_can_remove_its_files(self):
    network = Network({"https://cam.example/x.jpg": jpeg()})
    cam = self.webcam(network, "https://cam.example/x.jpg")
    cam.start()
    self.assertTrue((await cam.async_picture()).startswith(b"\xff\xd8"))
    await cam.async_stop()
    await cam.async_remove_files()
    self.assertFalse(self.directory.exists())


class ArchiveTests(unittest.TestCase):
  def test_the_portals_are_known_by_their_page_or_picture(self):
    for address in ("https://www.foto-webcam.eu/webcam/innsbruck/", "https://www.foto-webcam.eu/webcam/innsbruck/current/1200.jpg"):
      self.assertEqual(ARCHIVES.archive_for(address).portal, "foto-webcam", address)
    snow = ARCHIVES.archive_for("https://www.snow-online.com/webcam/grosser-arber-mountain-east-.htm",
                                "https://cams.snow-online.de/images/ecu/content/c_webcam/grosser-arber-mountain-east-_n79474-23867-0_l.jpg")
    self.assertEqual((snow.portal, snow.step_s), ("snow-online", 3600))
    for other in ("https://www.snow-online.com/webcam/x.htm", "https://images.bergfex.at/webcams/?id=550&format=4",
                  "https://cam.example/_n123-x.jpg", "https://www.foto-webcam.eu/", None):
      self.assertIsNone(ARCHIVES.archive_for(other), other)

  def test_addresses_in_central_european_time_newest_first(self):
    foto = ARCHIVES.archive_for("https://www.foto-webcam.eu/webcam/innsbruck/")
    winter = datetime(2026, 11, 7, 9, 10, tzinfo=timezone.utc).timestamp()
    self.assertEqual(foto.addresses(winter - 600, winter + 300), [
      (int(winter), "https://www.foto-webcam.eu/webcam/innsbruck/2026/11/07/1010_lm.jpg"),
      (int(winter) - 600, "https://www.foto-webcam.eu/webcam/innsbruck/2026/11/07/1000_lm.jpg")])
    summer = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    self.assertEqual(foto.addresses(summer, summer)[0][1], "https://www.foto-webcam.eu/webcam/innsbruck/2026/07/01/1200_lm.jpg")
    snow = ARCHIVES.archive_for("https://cams.snow-online.de/images/ecu/content/c_webcam/a_n79474-1-0_l.jpg")
    morning = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc).timestamp()
    self.assertEqual(snow.addresses(morning, morning + 3599),
                     [(int(morning), "https://archive.snow-online.de/79474/2026/11/07/07/archived_l.jpg")])
    self.assertEqual(len(foto.addresses(winter - STORE.KEEP_S, winter)), 145)

  def test_a_servers_time(self):
    self.assertEqual(WEBCAM.http_time("Sat, 07 Nov 2026 09:10:22 GMT"),
                     int(datetime(2026, 11, 7, 9, 10, 22, tzinfo=timezone.utc).timestamp()))
    for bad in (None, "", "yesterday", "Sat, 99 Nov 2026 99:99:99 GMT"):
      self.assertIsNone(WEBCAM.http_time(bad), bad)

  def test_the_webcams_folder_of_earlier_betas_moves(self):
    with tempfile.TemporaryDirectory() as folder:
      old, new = Path(folder) / STORE.OLD_FOLDER, Path(folder) / STORE.FOLDER
      (old / "ht_cam_a").mkdir(parents=True)
      STORE.move_old_folder(old, new)
      self.assertTrue((new / "ht_cam_a").is_dir())
      self.assertFalse(old.exists())
      STORE.move_old_folder(old, new)


class DeclarationTests(unittest.TestCase):
  def test_only_clean_sources_pass(self):
    clean = SOURCES.clean_source
    self.assertEqual(clean({"id": "ht_radar_home"}), {"id": "ht_radar_home", "kind": "radar"})
    self.assertEqual(clean({"id": "ht_radar_zone_arbeit", "zone": "zone.arbeit", "n": "Arbeit"}),
                     {"id": "ht_radar_zone_arbeit", "kind": "radar", "zone": "zone.arbeit", "name": "Arbeit"})
    self.assertEqual(clean({"id": "ht_radar_4758_1217", "lat": 47.58, "lon": 12.17}),
                     {"id": "ht_radar_4758_1217", "kind": "radar", "lat": 47.58, "lon": 12.17, "name": ""})
    self.assertEqual(clean({"id": "ht_cam_ab12", "url": "https://cam.example/x", "n": "Lift"}),
                     {"id": "ht_cam_ab12", "kind": "webcam", "url": "https://cam.example/x", "name": "Lift"})
    for bad in ({"id": "camera.x"}, {"id": "ht_cam_a", "url": "http://cam.example/"},
                {"id": "ht_cam_a", "url": "https://192.168.1.1/x"}, {"id": "ht_radar_1", "lat": 91, "lon": 0},
                {"id": "ht_radar_1", "lat": 1, "lon": 181}, {"id": "ht_radar_1", "lat": True, "lon": 1},
                {"id": "ht_radar_1", "zone": "sensor.x"}, {"id": "ht_radar_../x"}, {"id": 5}, "x", None):
      self.assertIsNone(clean(bad), bad)

  def test_a_declaration_has_a_limit_and_no_duplicates(self):
    many = [{"id": f"ht_cam_{index}", "url": f"https://cam.example/{index}"} for index in range(100)]
    self.assertEqual(len(SOURCES.clean_sources(many)), SOURCES.MAX_PANEL_SOURCES)
    self.assertEqual(len(SOURCES.clean_sources(many[:1] + many[:1])), 1)
    self.assertEqual(SOURCES.clean_sources("x"), [])

  def test_ids_are_the_same_on_panel_and_bridge(self):
    self.assertEqual(SOURCES.coordinates_id(47.58, 12.17), "ht_radar_4758_1217")
    self.assertEqual(SOURCES.coordinates_id(-33.86, -151.2), "ht_radar_m3386_m15120")
    self.assertEqual(SOURCES.source_id_of("camera.ht_cam_ab12"), "ht_cam_ab12")
    self.assertIsNone(SOURCES.source_id_of("camera.terrasse"))
    self.assertIsNone(SOURCES.source_id_of("image.ht_cam_ab12"))
    self.assertIsNone(SOURCES.source_id_of(None))

  def test_a_day_of_times_fits_a_sealed_message(self):
    short = list(range(1000, 1000 + 13 * 600, 600))
    self.assertEqual(SOURCES.frames_payload(short), {"frames": short})
    day = [1793870000 + index * 600 for index in range(145)]
    payload = SOURCES.frames_payload(day)
    self.assertEqual(payload["frames_base"], day[0])
    self.assertEqual(payload["frames_min"][1], 10)
    self.assertEqual(payload["frames_min"][-1], 1440)
    message = json.dumps({"status": "ready", "entity_id": "camera.ht_cam_abcdef12", "url": "tcp://192.168.1.2:8124/" + "t" * 32,
                          "codec": "jpeg", "transport": "tcp-ack-v1", "framing": "x" * 16, "protocol_version": 1,
                          "width": 752, "height": 538, "fps": 10, "view": "popup", "window": 2, "chunk": 32768,
                          "kind": "still", "history": True, **payload})
    self.assertLess(len(message), 2048 - 64)

  def test_a_place_search_answer(self):
    answer = json.dumps({"results": [{"name": "Kufstein", "admin1": "Tirol", "country": "Österreich",
                                      "latitude": 47.58333, "longitude": 12.16667},
                                     {"name": "kaputt"}, 5]}).encode()
    self.assertEqual(SOURCES.parse_place_answer(answer), [("Kufstein", "Tirol", "Österreich", 47.58333, 12.16667)])
    self.assertEqual(SOURCES.parse_place_answer(b"x"), [])
    self.assertEqual(SOURCES.parse_place_answer(None), [])


class FakeSource:
  def __init__(self, spec):
    self.spec = spec
    self.failed = False
    self.refused = False
    self.running = False
    self.files_removed = False
    self.listener = None

  def add_listener(self, listener):
    self.listener = listener

  def start(self):
    self.running = True

  async def async_stop(self):
    self.running = False

  async def async_remove_files(self):
    self.files_removed = True


class RegistryTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.disk = {}
    self.entities = []
    self.removed = []

    async def load():
      return self.disk.get("data")

    async def save(data):
      self.disk["data"] = json.loads(json.dumps(data))

    async def remove(source_id):
      self.removed.append(source_id)

    self.registry = SOURCES.SourceRegistry(load=load, save=save, make_source=FakeSource,
                                           make_entity=lambda spec, source: spec["id"], remove_entity=remove)

  CAM = {"id": "ht_cam_a", "kind": "webcam", "url": "https://cam.example/a", "name": ""}
  PLACE = {"id": "ht_radar_4758_1217", "kind": "radar", "lat": 47.58, "lon": 12.17, "name": ""}

  async def test_the_home_radar_always_exists(self):
    self.registry.attach(self.entities.extend)
    self.assertEqual(self.entities, ["ht_radar_home"])
    self.assertEqual(self.registry.source_ids_of("nobody"), ["ht_radar_home"])
    self.assertFalse(self.registry.source("ht_radar_home").running, "Without a panel it loads only when shown")

  async def test_what_a_panel_uses_collects_by_itself(self):
    self.registry.attach(self.entities.extend)
    home = {"id": "ht_radar_home", "kind": "radar"}
    await self.registry.async_set_panel("p1", [home, self.PLACE])
    self.assertTrue(self.registry.source("ht_radar_home").running)
    self.assertTrue(self.registry.source("ht_radar_4758_1217").running)
    await self.registry.async_set_panel("p1", [self.PLACE])
    self.assertFalse(self.registry.source("ht_radar_home").running, "No panel uses it any more")
    self.assertIsNotNone(self.registry.source("ht_radar_home"), "The entity stays")

  async def test_one_entity_however_many_panels_use_a_thing(self):
    self.registry.attach(self.entities.extend)
    self.assertTrue(await self.registry.async_set_panel("p1", [self.CAM]))
    self.assertTrue(await self.registry.async_set_panel("p2", [self.CAM, self.PLACE]))
    self.assertEqual(self.entities, ["ht_radar_home", "ht_cam_a", "ht_radar_4758_1217"])
    self.assertEqual(self.registry.source_ids_of("p1"), ["ht_radar_home", "ht_cam_a"])

  async def test_it_goes_with_the_last_panel_that_uses_it(self):
    self.registry.attach(self.entities.extend)
    await self.registry.async_set_panel("p1", [self.CAM])
    await self.registry.async_set_panel("p2", [self.CAM])
    source = self.registry.source("ht_cam_a")
    await self.registry.async_set_panel("p1", [])
    self.assertEqual(self.removed, [])
    self.assertTrue(source.running)
    await self.registry.async_forget_panel("p2")
    self.assertEqual(self.removed, ["ht_cam_a"])
    self.assertFalse(source.running)
    self.assertTrue(source.files_removed)
    self.assertIsNone(self.registry.source("ht_cam_a"))

  async def test_the_same_declaration_changes_nothing(self):
    self.registry.attach(self.entities.extend)
    self.assertTrue(await self.registry.async_set_panel("p1", [self.CAM]))
    self.assertFalse(await self.registry.async_set_panel("p1", [self.CAM]))

  async def test_a_restart_brings_everything_back_before_a_panel_reports(self):
    await self.registry.async_set_panel("p1", [self.CAM], "https://radar.example/list.json")
    self.assertEqual(self.entities, [], "Nothing before the Bridge's platform is ready")
    again = SOURCES.SourceRegistry(load=self.registry._load, save=self.registry._save, make_source=FakeSource,
                                   make_entity=lambda spec, source: spec["id"], remove_entity=self.registry._remove_entity)
    await again.async_load()
    again.attach(self.entities.extend)
    self.assertEqual(self.entities, ["ht_radar_home", "ht_cam_a"])
    self.assertEqual(again.radar_source, "https://radar.example/list.json")

  async def test_a_webcam_without_a_picture_is_told_to_its_panel(self):
    self.registry.attach(self.entities.extend)
    await self.registry.async_set_panel("p1", [self.CAM, self.PLACE])
    self.assertEqual(self.registry.status_of("p1"), {"ht_cam_a": "ok"})
    self.registry.source("ht_cam_a").failed = True
    self.assertEqual(self.registry.status_of("p1"), {"ht_cam_a": "none"})
    self.registry.source("ht_cam_a").refused = True
    self.assertEqual(self.registry.status_of("p1"), {"ht_cam_a": "blocked"}, "A site that refuses comes first")

  async def test_listeners_hear_which_source_changed(self):
    heard = []
    self.registry.add_listener(heard.append)
    self.registry.attach(self.entities.extend)
    await self.registry.async_set_panel("p1", [self.CAM])
    self.registry.source("ht_cam_a").listener()
    self.assertEqual(heard, ["ht_cam_a"])

  async def test_the_emergency_address_must_be_public_https(self):
    self.assertEqual(SOURCES.clean_radar_source("https://radar.example/list.json"), "https://radar.example/list.json")
    for bad in ("http://radar.example/", "https://192.168.1.1/", "", None, 5):
      self.assertEqual(SOURCES.clean_radar_source(bad), "")


class CameraEntityTests(unittest.IsolatedAsyncioTestCase):
  """The entity on the fake camera platform of test_local_camera_platform.py."""

  async def asyncSetUp(self):
    from test_local_camera_platform import FakeMqtt, load_camera_module

    self.module = load_camera_module(FakeMqtt())

  class Source:
    def __init__(self):
      self.frames = {100: b"a", 200: b"b", 300: b"c"}
      self.listener = None
      self.picture = None
      self.picture_time = 300

    def times(self):
      return list(self.frames)

    def add_listener(self, listener):
      self.listener = listener
      return lambda: setattr(self, "listener", None)

    async def async_picture(self):
      return b"newest"

    async def async_stamped_frame(self, when):
      return self.frames[when]

  SPEC = {"id": "ht_radar_home", "kind": "radar"}

  def camera(self, spec=None):
    return self.module.HomeTilesCameraSource(spec or self.SPEC, self.Source(), "Regenradar")

  async def test_it_belongs_to_the_bridge_not_to_a_panel(self):
    camera = self.camera()
    self.assertEqual(camera.entity_id, "camera.ht_radar_home")
    self.assertEqual(camera._attr_unique_id, "hometiles_source_ht_radar_home")
    self.assertEqual(camera._attr_name, "Regenradar")
    self.assertEqual(camera._attr_device_info["identifiers"], {("tab5_lvgl", "hometiles_bridge")})
    self.assertEqual(camera._attr_icon, "mdi:weather-pouring")
    webcam = self.camera({"id": "ht_cam_a", "kind": "webcam", "url": "https://x.example/"})
    self.assertEqual(webcam._attr_icon, "mdi:webcam")

  async def test_a_panel_entry_has_no_radar_any_more(self):
    entry = types.SimpleNamespace(entry_id="e1", data={"device_id": "mac", "base_topic": "hometiles/panel"}, options={})
    result = []
    await self.module.async_setup_entry(types.SimpleNamespace(data={"tab5_lvgl": {}}), entry, result.extend)
    self.assertEqual(result, [])

  async def test_the_bridge_entry_hands_its_platform_to_the_registry(self):
    entry = types.SimpleNamespace(entry_id="e1", data={"hometiles_bridge_entry": True}, options={})
    calls = []
    registry = types.SimpleNamespace(attach=calls.append)
    add = object()
    await self.module.async_setup_entry(types.SimpleNamespace(data={"tab5_lvgl": {"camera_sources": registry}}), entry, add)
    self.assertEqual(calls, [add])

  async def test_a_new_picture_writes_the_state_until_it_is_removed(self):
    camera = self.camera()
    await camera.async_added_to_hass()
    camera._source.listener()
    self.assertEqual(camera.written, 1)
    await camera.async_will_remove_from_hass()
    self.assertIsNone(camera._source.listener)

  async def test_home_assistants_window_plays_a_loop_and_holds_the_newest(self):
    camera = self.camera()
    pictures = [await camera._async_next_loop_image() for _ in range(9)]
    self.assertEqual(pictures, [b"b", b"c", b"c", b"c", b"a", b"b", b"c", b"c", b"c"])

  async def test_the_panels_get_the_plain_newest_picture(self):
    self.assertEqual(await self.camera().async_camera_image(), b"newest")


class WiringTests(unittest.TestCase):
  """__init__.py needs Home Assistant, so its wiring is read from the source."""

  @classmethod
  def setUpClass(cls):
    cls.source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    cls.flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")

  def test_the_bridge_sets_up_its_own_entry_without_a_form(self):
    self.assertIn('context={"source": "system"}', self.source)
    self.assertIn("async def async_step_system", self.flow)
    self.assertIn("data={BRIDGE_ENTRY_KEY: True}", self.flow)
    self.assertIn("return not is_bridge_entry(config_entry)", self.flow)

  def test_nothing_else_takes_it_for_a_panel(self):
    for name, text in (("__init__.py", self.source), ("config_flow.py", self.flow)):
      for number, line in enumerate(text.splitlines(), 1):
        if "async_entries(DOMAIN)" in line:
          self.assertIn("is_bridge_entry", line, f"{name}:{number}")
    self.assertIn("super()._async_current_entries(include_ignore) if not is_bridge_entry(entry)", self.flow)
    self.assertIn('"hometiles_bridge_entry"', (ROOT / "link_mqtt.py").read_text(encoding="utf-8"))
    self.assertIn('if is_bridge_entry(entry):\n    return await _async_setup_bridge_entry(hass, entry)', self.source)

  def test_the_old_radars_of_each_panel_go(self):
    self.assertIn("is_rain_radar_unique_id(item.unique_id)", self.source)

  def test_a_panel_gets_its_own_sources_without_a_password(self):
    self.assertIn('"cameras": _unique_entities(source["cameras"] + camera_sources)', self.source)

  def test_the_pickers_do_not_offer_them_as_home_assistant_cameras(self):
    self.assertIn("[item for item in entity_ids if source_id_of(item) is None]", self.source)

  def test_the_ready_answer_tells_the_history(self):
    self.assertIn('response_payload["history"] = True', self.source)
    self.assertIn("response_payload.update(frames_payload(history.times()))", self.source)
    self.assertIn('**frames_payload(history.times())', self.source)
    self.assertIn('parsed.get("box")', self.source)

  def test_every_bridge_camera_keeps_its_pictures_in_one_folder(self):
    self.assertIn("directory = Path(hass.config.path(PICTURE_FOLDER, spec[\"id\"]))", self.source)
    self.assertIn("directory=directory, first_sources=first_sources", " ".join(self.source.split()))
    self.assertIn("await hass.async_add_executor_job(move_old_folder,", self.source)
    self.assertIn('WebcamAnswer(data, http_time(response.headers.get("Last-Modified")))', self.source)

  def test_a_removed_panel_is_forgotten(self):
    self.assertIn("await sources.async_forget_panel(str(entry_device_id(entry)))", self.source)


if __name__ == "__main__":
  unittest.main()
