"""An image entity's popup plays like a still camera: FFmpeg gets JPEGs only."""

from __future__ import annotations

import ast
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from test_local_camera_stream import load_camera_stream_module

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl"


def picture(format_name: str, color=(10, 200, 30)) -> bytes:
  output = BytesIO()
  Image.new("RGB", (64, 32), color).save(output, format=format_name)
  return output.getvalue()


class Hass:
  def __init__(self) -> None:
    self.converted = 0

  async def async_add_executor_job(self, job, *args):
    self.converted += 1
    return job(*args)


class StillTests(unittest.IsolatedAsyncioTestCase):
  def setUp(self) -> None:
    self.module = load_camera_stream_module()
    self.hass = Hass()
    self.manager = self.module.CameraStreamManager(hass=self.hass)
    self.content = picture("PNG")
    self.asked = []

    async def get_picture(hass, entity_id, timeout=10):
      self.asked.append(entity_id)
      return SimpleNamespace(content=self.content, content_type="image/png")

    component = types.ModuleType("homeassistant.components.image")
    component.async_get_image = get_picture
    patcher = mock.patch.dict(sys.modules, {
      "homeassistant.components.image": component,
    })
    patcher.start()
    self.addCleanup(patcher.stop)
    source = types.ModuleType(self.module.__package__ + ".webcam_source") if self.module.__package__ else None
    if source is not None:
      def to_jpeg(data: bytes) -> bytes:
        output = BytesIO()
        with Image.open(BytesIO(data)) as opened:
          opened.convert("RGB").save(output, format="JPEG")
        return output.getvalue()
      source.to_jpeg = to_jpeg
      files = mock.patch.dict(sys.modules, {source.__name__: source})
      files.start()
      self.addCleanup(files.stop)

  async def test_a_png_reaches_ffmpeg_as_jpeg_converted_once_per_picture(self):
    first = await self.manager._async_get_still("image.map", 5)
    self.assertEqual(first.content[:2], b"\xff\xd8")
    self.assertEqual(self.hass.converted, 1)
    again = await self.manager._async_get_still("image.map", 5)
    self.assertIs(again.content, first.content, "The same picture is not converted again")
    self.assertEqual(self.hass.converted, 1)
    self.content = picture("PNG", (200, 10, 30))
    await self.manager._async_get_still("image.map", 5)
    self.assertEqual(self.hass.converted, 2, "A new picture is")

  async def test_a_jpeg_stays_as_it_is(self):
    self.content = picture("JPEG")
    image = await self.manager._async_get_still("image.photo", 5)
    self.assertEqual(image.content, self.content)
    self.assertEqual(self.hass.converted, 0)

  async def test_only_a_few_pictures_are_kept(self):
    for index in range(7):
      await self.manager._async_get_still(f"image.m{index}", 5)
    self.assertLessEqual(len(self.manager._image_jpegs), 4)

  async def test_a_camera_stays_a_camera(self):
    called = []

    async def get_image(hass, entity_id, timeout=10):
      called.append((entity_id, timeout))
      return SimpleNamespace(content=b"\xff\xd8x")

    self.module.async_get_image = get_image
    await self.manager._async_get_still("camera.door", 5)
    self.assertEqual(called, [("camera.door", 5)])
    self.assertEqual(self.asked, [])


class WiringTests(unittest.TestCase):
  def test_the_camera_command_accepts_declared_images(self):
    text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    ast.parse(text)
    self.assertIn('self._resolve_target_entity(requested_entity, self.cameras + list(getattr(self, "images", [])))', text)

  def test_session_and_feeder_use_the_same_way(self):
    text = (ROOT / "camera_stream.py").read_text(encoding="utf-8")
    self.assertIn("image = await self._async_get_still(entity_id, 10)", text)
    self.assertIn("self._manager._async_get_still(session.entity_id, 5)", text)


if __name__ == "__main__":
  unittest.main()
