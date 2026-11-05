"""The rain radar: sources in order, on demand, the location rounded, the picture."""

from __future__ import annotations

import ast
import asyncio
import gzip
import importlib.util
import json
import re
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageFont

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tab5_lvgl"
_spec = importlib.util.spec_from_file_location("hometiles_rain_radar_under_test", ROOT / "rain_radar.py")
RR = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = RR  # dataclasses look their module up
_spec.loader.exec_module(RR)

LIST_URL = RR.SOURCES[0].url
HOME_DE = (50.9123, 9.5678)  # inside the DWD's area
HOME_AT = (47.5612, 13.3456)  # outside it
HOME_US = (40.7128, -74.0060)


def png(color=(0, 0, 255, 255), size=(512, 512)):
  output = BytesIO()
  Image.new("RGBA", size, color).save(output, format="PNG")
  return output.getvalue()


def frame_list(when=1793870000, path="/v2/radar/abc123", host="https://tilecache.rainviewer.com"):
  return json.dumps({"version": "2.0", "host": host,
                     "radar": {"past": [{"time": when - 600, "path": "/v2/radar/older"},
                                        {"time": when, "path": path}], "nowcast": []}}).encode()


def capabilities(layer, default="2026-11-05T05:30:00Z", reference=None):
  reference_tag = f'<Dimension name="REFERENCE_TIME" default="{reference}" units="ISO8601">x</Dimension>' if reference else ""
  return (f'<Layer><Name>other</Name><Dimension name="time" default="2001-01-01T00:00:00Z"/></Layer>'
          f'<Layer queryable="1"><Name>{layer}</Name><Title>t</Title>'
          f'<Dimension name="time" units="ISO8601" default="{default}">x</Dimension>{reference_tag}</Layer>').encode()


class Network:
  """Answers by the start of the address; everything else fails."""

  def __init__(self, answers=None):
    self.answers = dict(answers or {})
    self.requests = []

  async def fetch(self, url, limit):
    self.requests.append(url)
    for start, answer in self.answers.items():
      if url.startswith(start):
        if isinstance(answer, Exception):
          raise answer
        return answer
    return None

  def count(self, start):
    return sum(url.startswith(start) for url in self.requests)


def working_rainviewer(when=1793870000):
  return {LIST_URL: frame_list(when), "https://tilecache.rainviewer.com/": png()}


class Clock:
  def __init__(self):
    self.now = 1000.0

  def __call__(self):
    return self.now


async def direct(fn, *args):
  return await asyncio.to_thread(fn, *args)


def make_radar(network, home=HOME_AT, clock=None, **kwargs):
  return RR.RainRadar(fetch=network.fetch, location=lambda: home, language=lambda: "de",
                      format_time=lambda when: "12:34", run_blocking=direct, clock=clock or Clock(), **kwargs)


async def settle(radar):
  """The running refresh and the older frames behind it."""
  for attribute in ("_task", "_fill_task"):
    task = getattr(radar, attribute)
    if task is not None:
      await task


class GeometryTests(unittest.TestCase):
  def test_the_location_is_rounded_for_the_sources_and_exact_for_the_mark(self):
    view = RR.make_view(*HOME_AT)
    self.assertEqual((view.lat, view.lon), (47.6, 13.3))
    self.assertEqual((view.home_lat, view.home_lon), HOME_AT)

  def test_the_centre_is_the_picture_centre(self):
    view = RR.make_view(*HOME_AT)
    self.assertEqual(RR.project(view, view.lat, view.lon), (RR.PICTURE_WIDTH / 2, RR.PICTURE_HEIGHT / 2))

  def test_the_home_mark_stays_near_the_centre(self):
    x, y = RR.project(RR.make_view(*HOME_AT), HOME_AT[0], HOME_AT[1])
    self.assertLess(abs(x - RR.PICTURE_WIDTH / 2), 20)
    self.assertLess(abs(y - RR.PICTURE_HEIGHT / 2), 20)

  def test_north_is_up_and_east_is_right(self):
    view = RR.make_view(*HOME_AT)
    x, y = RR.project(view, view.lat + 1, view.lon + 1)
    self.assertGreater(x, RR.PICTURE_WIDTH / 2)
    self.assertLess(y, RR.PICTURE_HEIGHT / 2)

  def test_the_bounding_box_matches_the_picture(self):
    west, south, east, north = RR.bbox_meters(RR.make_view(0.0, 0.0))
    self.assertAlmostEqual(west, -east, places=3)
    self.assertAlmostEqual(south, -north, places=3)
    per_px = RR.WORLD_METERS / (RR.SOURCE_SIZE * 2 ** RR.ZOOM * RR.SCALE)
    self.assertAlmostEqual((east - west) / per_px, RR.PICTURE_WIDTH, places=3)
    self.assertAlmostEqual((north - south) / per_px, RR.PICTURE_HEIGHT, places=3)

  def test_poles_do_not_break_the_projection(self):
    view = RR.make_view(89.9, 179.99)
    self.assertLessEqual(view.lat, 85.0)
    RR.bbox_meters(view)

  def test_a_regional_source_covers_only_its_area(self):
    dwd = next(source for source in RR.SOURCES if source.id == "dwd")
    self.assertTrue(RR.covers(dwd, RR.make_view(*HOME_DE)))
    self.assertFalse(RR.covers(dwd, RR.make_view(*HOME_US)))
    self.assertTrue(RR.covers(RR.SOURCES[0], RR.make_view(*HOME_US)))


class ParsingTests(unittest.TestCase):
  def test_a_list_gives_every_frame_oldest_first(self):
    self.assertEqual(RR.parse_frame_list(frame_list()),
                     [(1793869400, "https://tilecache.rainviewer.com/v2/radar/older"),
                      (1793870000, "https://tilecache.rainviewer.com/v2/radar/abc123")])

  def test_a_bad_older_frame_is_left_out(self):
    data = json.dumps({"host": "https://h", "radar": {"past": [
      {"time": 5, "path": "/../../secret"}, {"time": 6, "path": "/v2/radar/ok"}, "nonsense",
      {"time": 7, "path": "/v2/radar/new"}]}}).encode()
    self.assertEqual(RR.parse_frame_list(data), [(6, "https://h/v2/radar/ok"), (7, "https://h/v2/radar/new")])

  def test_a_long_list_keeps_the_newest_frames(self):
    frames = [{"time": 100 + index, "path": f"/v2/radar/f{index}"} for index in range(RR.MAX_FRAMES + 5)]
    parsed = RR.parse_frame_list(json.dumps({"host": "https://h", "radar": {"past": frames}}).encode())
    self.assertEqual(len(parsed), RR.MAX_FRAMES)
    self.assertEqual(parsed[-1][0], 100 + RR.MAX_FRAMES + 4)

  def test_a_bad_list_is_no_frame(self):
    for bad in (b"", b"[]", b"not json", b'{"host":"https://a","radar":{"past":[]}}',
                frame_list(host="http://tilecache.rainviewer.com"),
                frame_list(host="https://evil.example/path"),
                frame_list(path="/../../secret"),
                frame_list(path="https://elsewhere/x"),
                frame_list(when=0),
                json.dumps({"host": "https://a", "radar": {"past": [{"time": True, "path": "/v2/radar/x"}]}}).encode()):
      self.assertIsNone(RR.parse_frame_list(bad), bad)

  def test_an_image_address_holds_only_the_rounded_location(self):
    url = RR.frame_image_url("https://host/v2/radar/x", RR.make_view(*HOME_AT))
    self.assertEqual(url, "https://host/v2/radar/x/512/6/47.6/13.3/2/1_1.png")

  def test_a_layer_with_a_default_time(self):
    when = RR.parse_wms_time(capabilities("layer"), "layer")
    self.assertEqual(when[0], "2026-11-05T05:30:00Z")
    self.assertGreater(when[1], 1700000000)

  def test_only_the_named_layer_counts(self):
    self.assertEqual(RR.parse_wms_time(capabilities("layer"), "layer")[0], "2026-11-05T05:30:00Z")
    self.assertIsNone(RR.parse_wms_time(capabilities("layer"), "missing"))

  def test_a_current_layer_tells_its_age_by_the_reference_time(self):
    data = capabilities("dwd:radar", default="current", reference="2026-11-05T12:00:00.000Z")
    time_value, when = RR.parse_wms_time(data, "radar")
    self.assertIsNone(time_value)
    self.assertEqual(when, RR._epoch("2026-11-05T12:00:00Z"))

  def test_a_map_address(self):
    nasa = RR.SOURCES[-1]
    url = RR.wms_image_url(nasa, RR.make_view(*HOME_AT), "2026-11-05T05:30:00Z")
    self.assertIn("LAYERS=IMERG_Precipitation_Rate_30min", url)
    self.assertIn("CRS=EPSG%3A3857", url)
    self.assertIn("WIDTH=1024", url)
    self.assertIn("TIME=2026-11-05T05%3A30%3A00Z", url)
    self.assertNotIn("TIME=", RR.wms_image_url(nasa, RR.make_view(*HOME_AT), None))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_nothing_is_loaded_before_somebody_asks(self):
    network = Network(working_rainviewer())
    make_radar(network)
    await asyncio.sleep(0)
    self.assertEqual(network.requests, [])

  async def test_the_first_answer_is_a_picture(self):
    network = Network(working_rainviewer())
    radar = make_radar(network)
    jpeg = await radar.async_picture()
    self.assertTrue(jpeg.startswith(b"\xff\xd8"))
    self.assertEqual(radar.picture.source, "rainviewer")
    self.assertEqual(radar.picture.when, 1793870000)

  async def test_the_exact_location_never_leaves_the_bridge(self):
    network = Network(working_rainviewer())
    await make_radar(network).async_picture()
    self.assertTrue(network.requests)
    for url in network.requests:
      self.assertNotIn("47.56", url)
      self.assertNotIn("13.34", url)

  async def test_many_callers_share_one_request_and_no_second_within_the_interval(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    await asyncio.gather(*(radar.async_picture() for _ in range(5)))
    await radar.async_picture()
    clock.now += RR.REFRESH_S - 1
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(network.count(LIST_URL), 1)

  async def test_a_frame_already_loaded_is_not_loaded_again(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(network.count("https://tilecache.rainviewer.com/"), 2)
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(network.count(LIST_URL), 2)
    self.assertEqual(network.count("https://tilecache.rainviewer.com/"), 2)

  async def test_the_history_loads_behind_the_first_picture(self):
    gate = asyncio.Event()
    network = Network(working_rainviewer())
    fetch = network.fetch

    async def slow_older_frames(url, limit):
      if "/older/" in url:
        await gate.wait()
      return await fetch(url, limit)

    network.fetch = slow_older_frames
    radar = make_radar(network)
    told = []
    radar.add_listener(lambda: told.append(radar.times()))
    self.assertIsNotNone(await radar.async_picture())
    self.assertEqual(radar.times(), [1793870000])  # the first answer did not wait for the rest
    gate.set()
    await settle(radar)
    self.assertEqual(radar.times(), [1793869400, 1793870000])
    self.assertEqual(told, [[1793870000], [1793869400, 1793870000]])

  async def test_a_frame_is_found_by_its_time(self):
    network = Network(working_rainviewer())
    radar = make_radar(network)
    await radar.async_picture()
    await settle(radar)
    self.assertTrue(radar.frame_at(1793869400).startswith(b"\xff\xd8"))
    self.assertIsNone(radar.frame_at(12345))

  async def test_a_frame_that_left_the_list_is_dropped(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    await radar.async_picture()
    await settle(radar)
    network.answers[LIST_URL] = json.dumps({"host": "https://tilecache.rainviewer.com", "radar": {"past": [
      {"time": 1793870000, "path": "/v2/radar/abc123"}, {"time": 1793870600, "path": "/v2/radar/new"}]}}).encode()
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.times(), [1793870000, 1793870600])

  async def test_an_older_frame_that_fails_is_tried_at_the_next_refresh(self):
    network = Network({LIST_URL: frame_list(), "https://tilecache.rainviewer.com/v2/radar/abc123": png()})
    clock = Clock()
    radar = make_radar(network, clock=clock)
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.times(), [1793870000])
    network.answers["https://tilecache.rainviewer.com/v2/radar/older"] = png()
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.times(), [1793869400, 1793870000])

  async def test_a_fallback_source_has_one_frame_and_replaces_the_history(self):
    nasa = RR.SOURCES[-1]
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    await radar.async_picture()
    await settle(radar)
    network.answers = {nasa.url + "?service": capabilities(nasa.layer),
                       nasa.url + "?SERVICE": png(size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))}
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.picture.source, "nasa")
    self.assertEqual(len(radar.times()), 1)

  async def test_a_new_frame_replaces_the_picture_and_tells_the_listeners(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    told = []
    stop = radar.add_listener(lambda: told.append(True))
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(len(told), 2)  # the newest picture, then the older one
    network.answers[LIST_URL] = frame_list(1793870600)
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.picture.when, 1793870600)
    self.assertEqual(len(told), 3)
    stop()
    network.answers[LIST_URL] = frame_list(1793871200)
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(len(told), 3)

  async def test_in_germany_the_dwd_follows_when_the_first_source_fails(self):
    dwd = next(source for source in RR.SOURCES if source.id == "dwd")
    network = Network({dwd.url + "?service": capabilities(dwd.layer, default="current", reference="2026-11-05T12:00:00Z"),
                       dwd.url + "?SERVICE": png(size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))})
    radar = make_radar(network, home=HOME_DE)
    self.assertIsNotNone(await radar.async_picture())
    self.assertEqual(radar.picture.source, "dwd")
    self.assertEqual(radar.picture.credit, "DWD")

  async def test_elsewhere_a_national_source_is_not_even_asked_and_nasa_follows(self):
    nasa = RR.SOURCES[-1]
    network = Network({nasa.url + "?service": capabilities(nasa.layer),
                       nasa.url + "?SERVICE": png(size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))})
    radar = make_radar(network, home=HOME_US)
    await radar.async_picture()
    self.assertEqual(radar.picture.source, "nasa")
    self.assertEqual(network.count("https://maps.dwd.de"), 0)

  async def test_a_source_that_raises_does_not_stop_the_next(self):
    nasa = RR.SOURCES[-1]
    network = Network({LIST_URL: RuntimeError("boom"), nasa.url + "?service": capabilities(nasa.layer),
                       nasa.url + "?SERVICE": png(size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))})
    radar = make_radar(network)
    with self.assertLogs(RR._LOGGER, "WARNING"):
      await radar.async_picture()
    self.assertEqual(radar.picture.source, "nasa")

  async def test_a_broken_answer_from_the_first_source_falls_over_too(self):
    nasa = RR.SOURCES[-1]
    network = Network({LIST_URL: b"<html>sorry</html>", nasa.url + "?service": capabilities(nasa.layer),
                       nasa.url + "?SERVICE": png(size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))})
    radar = make_radar(network)
    await radar.async_picture()
    self.assertEqual(radar.picture.source, "nasa")

  async def test_when_every_source_fails_the_last_picture_stays_and_it_tries_again_soon(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    first = await radar.async_picture()
    network.answers.clear()
    clock.now += RR.REFRESH_S
    with self.assertLogs(RR._LOGGER, "WARNING"):
      self.assertEqual(await radar.async_picture(), first)
      await settle(radar)
    self.assertEqual(await radar.async_picture(), first)
    # Back after the short retry instead of a whole interval.
    network.answers.update(working_rainviewer(1793870600))
    clock.now += RR.RETRY_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.picture.when, 1793870600)

  async def test_no_source_at_all_is_no_picture_and_no_error(self):
    radar = make_radar(Network())
    with self.assertLogs(RR._LOGGER, "WARNING"):
      self.assertIsNone(await radar.async_picture())

  async def test_a_moved_home_draws_a_new_view_for_the_same_frame(self):
    network = Network(working_rainviewer())
    clock = Clock()
    home = {"at": HOME_AT}
    radar = RR.RainRadar(fetch=network.fetch, location=lambda: home["at"], language=lambda: "de",
                         format_time=lambda when: "12:34", run_blocking=direct, clock=clock)
    await radar.async_picture()
    home["at"] = HOME_US
    clock.now += RR.REFRESH_S
    await radar.async_picture()
    await settle(radar)
    self.assertEqual(radar.picture.view.lat, 40.7)


class PictureTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.view = RR.make_view(*HOME_AT)
    cls.layers = RR.draw_layers(cls.view, "de")

  def picture(self, overlay):
    return Image.open(BytesIO(RR.render_picture(self.layers, overlay, "12:34  Test")))

  def test_a_baseline_jpeg_in_the_picture_size(self):
    image = self.picture(png((0, 0, 0, 0)))
    self.assertEqual(image.size, (RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT))
    self.assertEqual(image.format, "JPEG")
    self.assertEqual(image.mode, "RGB")
    self.assertFalse(image.info.get("progressive"))

  def test_rain_covers_the_map_where_it_is_and_only_there(self):
    near_land = (RR.PICTURE_WIDTH // 4, RR.PICTURE_HEIGHT // 3)
    dry = self.picture(png((0, 0, 0, 0))).getpixel(near_land)
    wet = self.picture(png((0, 0, 255, 255))).getpixel(near_land)
    self.assertLess(abs(dry[0] - RR.LAND[0]), 12)
    self.assertGreater(wet[2], 200)
    self.assertLess(wet[0], 80)

  def test_a_square_image_shows_its_middle_band_not_the_whole(self):
    square = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    square.paste((255, 0, 0, 255), (0, 0, 512, 100))  # above the band
    square.paste((0, 255, 0, 255), (0, 112, 512, 400))  # the band
    output = BytesIO()
    square.save(output, format="PNG")
    image = self.picture(output.getvalue())
    self.assertGreater(image.getpixel((RR.PICTURE_WIDTH // 4, 40))[1], 150)
    self.assertLess(image.getpixel((RR.PICTURE_WIDTH // 4, 40))[0], 100)

  def test_a_source_in_the_picture_size_is_not_cut(self):
    image = self.picture(png((0, 255, 0, 255), size=(RR.PICTURE_WIDTH, RR.PICTURE_HEIGHT)))
    self.assertGreater(image.getpixel((5, 5))[1], 150)

  def test_the_home_mark_is_drawn(self):
    image = self.picture(png((0, 0, 0, 0)))
    x, y = RR.project(self.view, HOME_AT[0], HOME_AT[1])
    self.assertGreater(min(image.getpixel((round(x), round(y)))), 230)

  def test_the_font_has_the_letters_of_the_names(self):
    font = ImageFont.truetype(str(RR.FONT_FILE), 40)
    missing = font.getmask("￿").getbbox()
    for letter in "üöäßéłąćМюж":
      self.assertNotEqual(font.getmask(letter).getbbox(), missing, letter)


class MapDataTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.data = json.loads(gzip.decompress(RR.MAP_FILE.read_bytes()))

  def test_the_world_is_in_it(self):
    self.assertGreater(len(self.data["land"]), 1000)
    self.assertGreater(len(self.data["borders"]), 300)
    self.assertGreater(len(self.data["places"]), 5000)

  def test_every_shape_holds_whole_points(self):
    for shape in self.data["land"] + self.data["borders"]:
      self.assertEqual(len(shape) % 2, 0)
      self.assertGreaterEqual(len(shape), 4)

  def test_places_have_their_names_and_the_big_ones_come_first(self):
    places = {place[3]: place for place in self.data["places"]}
    self.assertEqual(places["Munich"][4]["de"], "München")
    self.assertEqual(places["Salzburg"][3], "Salzburg")
    self.assertAlmostEqual(places["Salzburg"][1] / 100, 47.8, delta=0.2)
    ranks = [place[2] for place in self.data["places"]]
    self.assertEqual(ranks, sorted(ranks))

  def test_the_file_stays_small(self):
    self.assertLess(RR.MAP_FILE.stat().st_size, 1024 * 1024)


class CameraEntityTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    from test_local_camera_platform import FakeMqtt, load_camera_module

    self.module = load_camera_module(FakeMqtt())

  def entry(self):
    return types.SimpleNamespace(entry_id="e1", data={"device_id": "mac", "base_topic": "hometiles/panel"}, options={})

  async def test_every_panel_gets_one_without_announcing_anything(self):
    radar = make_radar(Network(working_rainviewer()))
    hass = types.SimpleNamespace(data={"tab5_lvgl": {RR.DATA_RAIN_RADAR: radar}})
    result = []
    await self.module.async_setup_entry(hass, self.entry(), result.extend)
    self.assertEqual(len(result), 1)
    camera = result[0]
    self.assertEqual(camera._attr_unique_id, "mac_rain_radar")
    self.assertEqual(camera._attr_translation_key, "rain_radar")
    self.assertEqual(camera._attr_icon, "mdi:weather-pouring")

  async def test_the_entity_has_no_device_so_its_name_stays_the_plain_name(self):
    camera = self.module.HomeTilesRainRadar(self.entry(), make_radar(Network()))
    self.assertFalse(hasattr(camera, "_attr_device_info"))

  async def test_it_serves_the_radar_picture_and_tells_where_it_comes_from(self):
    radar = make_radar(Network(working_rainviewer()))
    camera = self.module.HomeTilesRainRadar(self.entry(), radar)
    self.assertIsNone(camera.attribution)
    self.assertEqual(camera.extra_state_attributes, {})
    jpeg = await camera.async_camera_image()
    self.assertTrue(jpeg.startswith(b"\xff\xd8"))
    self.assertEqual(camera.attribution, "Radar data: RainViewer")
    self.assertEqual(camera.extra_state_attributes, {"source": "rainviewer", "picture_time": 1793870000})
    await settle(radar)

  async def test_a_new_picture_writes_the_state_until_it_is_removed(self):
    network = Network(working_rainviewer())
    clock = Clock()
    radar = make_radar(network, clock=clock)
    camera = self.module.HomeTilesRainRadar(self.entry(), radar)
    await camera.async_added_to_hass()
    await camera.async_camera_image()
    await settle(radar)
    self.assertEqual(camera.written, 2)  # the newest picture, then the older one
    await camera.async_will_remove_from_hass()
    network.answers[LIST_URL] = frame_list(1793870600)
    clock.now += RR.REFRESH_S
    await camera.async_camera_image()
    await settle(radar)
    self.assertEqual(camera.written, 2)


class WiringTests(unittest.TestCase):
  """__init__.py needs Home Assistant, so its wiring is read from the source."""

  @classmethod
  def setUpClass(cls):
    cls.source = (ROOT / "__init__.py").read_text(encoding="utf-8")

  def test_one_radar_for_all_panels(self):
    self.assertIn("domain_data[DATA_RAIN_RADAR] = _build_rain_radar(hass)", self.source)
    self.assertRegex(self.source, r"if DATA_RAIN_RADAR not in domain_data:")

  def test_its_entity_is_always_released_to_its_panel(self):
    self.assertIn('rain_radar = self._resolve_rain_radar_entities()', self.source)
    self.assertIn('"cameras": _unique_entities(source["cameras"] + rain_radar)', self.source)

  def test_the_full_search_hides_only_other_panels_radars(self):
    self.assertIn("if not self._foreign_rain_radar(state.entity_id)", self.source)
    self.assertRegex(self.source, r"entry\.config_entry_id != self\.entry\.entry_id\)")

  def test_the_location_comes_from_home_assistant(self):
    self.assertIn("location=lambda: (hass.config.latitude, hass.config.longitude)", self.source)

  def test_a_frame_command_needs_a_time_and_gets_no_answer(self):
    self.assertIn('if command == "frame":', self.source)
    self.assertIn('isinstance(when, bool) or not isinstance(when, int) or when < 0', self.source)
    self.assertIn("manager.pick_frame(self.device_id, entity_id, frame)", self.source)

  def test_the_ready_answer_tells_the_times_and_the_kind(self):
    self.assertIn('response_payload["frames"] = radar.times()', self.source)
    self.assertIn('response_payload["kind"] = "live" if session.source or session.live is not None else "still"', self.source)

  def test_a_popup_on_the_radar_follows_new_frames(self):
    self.assertIn('"status": "frames"', self.source)
    self.assertIn("radar.add_listener(bridge.on_rain_radar_changed)", self.source)

  def test_every_language_names_it(self):
    for path in [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]:
      data = json.loads(path.read_text(encoding="utf-8"))
      self.assertTrue(data["entity"]["camera"]["rain_radar"]["name"], path.name)

  def test_nobody_is_asked_for_anything(self):
    flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    self.assertNotRegex(flow, re.compile("radar", re.IGNORECASE))


class ReleaseTests(unittest.TestCase):
  """The real Tab5Bridge methods, taken from the source without Home Assistant."""

  NAMES = {"_resolve_rain_radar_entities", "_foreign_rain_radar", "_rain_radar_of", "_pick_camera_frame"}

  def bridge(self, registered=()):
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    bridge_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tab5Bridge")
    functions = [node for node in bridge_class.body if getattr(node, "name", None) in self.NAMES]
    self.assertEqual({node.name for node in functions}, self.NAMES)
    entities = {item.entity_id: item for item in registered}

    def entity_id_of(domain, platform, unique_id):
      for item in registered:
        if (item.domain, item.platform, item.unique_id) == (domain, platform, unique_id):
          return item.entity_id
      return None

    registry = types.SimpleNamespace(async_get=entities.get, async_get_entity_id=entity_id_of)
    scope = {"List": list, "DOMAIN": "tab5_lvgl", "er": types.SimpleNamespace(async_get=lambda hass: registry),
             "Optional": Optional, "Dict": Dict, "Any": Any, "RainRadar": RR.RainRadar,
             "DATA_RAIN_RADAR": RR.DATA_RAIN_RADAR,
             "_LOGGER": types.SimpleNamespace(warning=lambda *args: None),
             "entry_device_id": lambda entry: entry.data["device_id"],
             "rain_radar_unique_id": RR.rain_radar_unique_id, "is_rain_radar_unique_id": RR.is_rain_radar_unique_id}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "__init__.py", "exec"), scope)
    bridge = type("Bridge", (), {name: scope[name] for name in self.NAMES})()
    self.radar = types.SimpleNamespace(frame_at={100: b"old"}.get)
    bridge.hass = types.SimpleNamespace(data={"tab5_lvgl": {RR.DATA_RAIN_RADAR: self.radar}})
    bridge.entry = types.SimpleNamespace(entry_id="e1", data={"device_id": "mac"})
    bridge.device_id = "mac"
    bridge.base_topic = "hometiles/panel"
    return bridge

  @staticmethod
  def camera(entity_id, unique_id, config_entry_id="e1", platform="tab5_lvgl"):
    return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id, config_entry_id=config_entry_id,
                                 platform=platform, domain="camera")

  def test_a_panel_is_released_its_own_radar(self):
    mine = self.camera("camera.regenradar", "mac_rain_radar")
    other = self.camera("camera.regenradar_2", "other_rain_radar", "e2")
    self.assertEqual(self.bridge([other, mine])._resolve_rain_radar_entities(), ["camera.regenradar"])

  def test_before_the_entity_exists_nothing_is_released(self):
    self.assertEqual(self.bridge()._resolve_rain_radar_entities(), [])

  def test_only_the_radar_has_a_history(self):
    bridge = self.bridge([self.camera("camera.regenradar", "mac_rain_radar"),
                          self.camera("camera.tuer", "x", platform="generic")])
    self.assertIs(bridge._rain_radar_of("camera.regenradar"), self.radar)
    self.assertIsNone(bridge._rain_radar_of("camera.tuer"))
    self.assertIsNone(bridge._rain_radar_of("camera.unknown"))
    self.assertIsNone(bridge._rain_radar_of(None))

  def test_a_picked_time_shows_its_frame_and_zero_the_newest(self):
    bridge = self.bridge([self.camera("camera.regenradar", "mac_rain_radar")])
    picks = []
    manager = types.SimpleNamespace(pick_frame=lambda *args: picks.append(args))
    bridge._pick_camera_frame({"time": 100}, "camera.regenradar", manager)
    bridge._pick_camera_frame({"time": 0}, "camera.regenradar", manager)
    self.assertEqual(picks, [("mac", "camera.regenradar", b"old"), ("mac", "camera.regenradar", None)])

  def test_a_time_that_is_gone_or_invalid_changes_nothing(self):
    bridge = self.bridge([self.camera("camera.regenradar", "mac_rain_radar"),
                          self.camera("camera.tuer", "x", platform="generic")])
    picks = []
    manager = types.SimpleNamespace(pick_frame=lambda *args: picks.append(args))
    for parsed, entity in (({"time": 999}, "camera.regenradar"), ({"time": -1}, "camera.regenradar"),
                           ({"time": True}, "camera.regenradar"), ({"time": "5"}, "camera.regenradar"),
                           ({}, "camera.regenradar"), ({"time": 100}, "camera.tuer")):
      bridge._pick_camera_frame(parsed, entity, manager)
    self.assertEqual(picks, [])

  def test_only_another_panels_radar_is_foreign(self):
    bridge = self.bridge([
      self.camera("camera.regenradar", "mac_rain_radar"),
      self.camera("camera.regenradar_2", "other_rain_radar", "e2"),
      self.camera("camera.other_panel", "other_local_camera", "e2"),
      self.camera("camera.someone_elses", "x_rain_radar", "e9", platform="another_integration"),
    ])
    self.assertFalse(bridge._foreign_rain_radar("camera.regenradar"))
    self.assertTrue(bridge._foreign_rain_radar("camera.regenradar_2"))
    self.assertFalse(bridge._foreign_rain_radar("camera.other_panel"))
    self.assertFalse(bridge._foreign_rain_radar("camera.someone_elses"))
    self.assertFalse(bridge._foreign_rain_radar("camera.not_registered"))


class PickedFrameTests(unittest.IsolatedAsyncioTestCase):
  """The stream's feeder with a frame the panel picked (camera_stream.py)."""

  async def asyncSetUp(self):
    from test_camera_stream_lag import FakeHass
    from test_local_camera_stream import load_camera_stream_module

    self.module = load_camera_stream_module()
    self.hass = FakeHass()
    self.manager = self.module.CameraStreamManager(self.hass)
    self.connection = self.manager.tcp_connection

  def session(self, has_history=True):
    return self.module.CameraStreamSession(
      token="t", device_id="mac", entity_id="camera.regenradar", source=None, first_image=b"NEWEST",
      width=752, height=424, fps=10, expires_at=0.0, stop_event=asyncio.Event(), has_history=has_history)

  class Writes:
    def __init__(self):
      self.written = []
      self.returncode = None
      self.stdin = self

    def write(self, data):
      self.written.append(bytes(data))

    async def drain(self):
      await asyncio.sleep(0)

    def close(self):
      pass

    def is_closing(self):
      return True

  async def run_feeder(self, session, process, image=b"NEWEST"):
    calls = []

    async def fake_image(hass, entity_id, timeout=None):
      calls.append(entity_id)
      return types.SimpleNamespace(content=image)

    self.module.async_get_image = fake_image
    task = asyncio.create_task(self.connection._async_feed_camera_images(session, process))
    return task, calls

  async def stop(self, process, task):
    process.returncode = 0
    await asyncio.wait_for(task, 2)

  async def test_a_picked_frame_is_written_twice_and_at_once(self):
    session, process = self.session(), self.Writes()
    task, _calls = await self.run_feeder(session, process)
    await asyncio.sleep(0.05)
    session.picked_frame = b"OLD"
    session.flush_next_frame = True
    await asyncio.sleep(0.1)
    await self.stop(process, task)
    # Twice: FFmpeg would hold the first one until the next begins.
    self.assertEqual(process.written.count(b"OLD"), 2)

  async def test_a_historys_first_picture_is_written_twice_to_show_at_once(self):
    session, process = self.session(), self.Writes()
    task, _calls = await self.run_feeder(session, process)
    await asyncio.sleep(0.05)
    await self.stop(process, task)
    self.assertEqual(process.written, [b"NEWEST", b"NEWEST"])

  async def test_while_a_frame_is_picked_the_camera_is_not_asked(self):
    session, process = self.session(), self.Writes()
    session.picked_frame = b"OLD"
    task, calls = await self.run_feeder(session, process)
    await asyncio.sleep(0.15)
    await self.stop(process, task)
    self.assertEqual(calls, [])
    self.assertIn(b"OLD", process.written)

  async def test_a_newer_camera_picture_does_not_replace_a_picked_frame(self):
    session, process = self.session(), self.Writes()
    task, _calls = await self.run_feeder(session, process, image=b"CHANGED")
    session.picked_frame = b"OLD"
    await asyncio.sleep(0.15)
    await self.stop(process, task)
    self.assertEqual(session.latest_image, b"OLD")

  async def test_back_to_now_follows_the_camera_again(self):
    session, process = self.session(), self.Writes()
    session.picked_frame = b"OLD"
    task, calls = await self.run_feeder(session, process, image=b"CHANGED")
    await asyncio.sleep(0.05)
    session.picked_frame = None
    session.flush_next_frame = True
    await asyncio.sleep(0.2)
    await self.stop(process, task)
    self.assertTrue(calls)
    self.assertEqual(session.latest_image, b"CHANGED")

  async def test_a_camera_without_a_history_keeps_its_rhythm(self):
    session, process = self.session(has_history=False), self.Writes()
    task, _calls = await self.run_feeder(session, process)
    await asyncio.sleep(0.1)
    await self.stop(process, task)
    # The first frame once, no second write: nothing was picked.
    self.assertEqual(process.written, [b"NEWEST"])

  async def test_pick_frame_reaches_only_the_panels_running_camera(self):
    session = self.session()
    self.manager._panel_sessions["mac"] = session
    self.assertTrue(self.manager.pick_frame("mac", "camera.regenradar", b"OLD"))
    self.assertEqual(session.picked_frame, b"OLD")
    self.assertTrue(session.flush_next_frame)
    self.assertFalse(self.manager.pick_frame("other", "camera.regenradar", b"X"))
    self.assertFalse(self.manager.pick_frame("mac", "camera.tuer", b"X"))
    session.has_history = False
    self.assertFalse(self.manager.pick_frame("mac", "camera.regenradar", b"X"))
    self.assertEqual(session.picked_frame, b"OLD")


if __name__ == "__main__":
  unittest.main()
