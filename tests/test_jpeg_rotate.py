"""Quarter turns of panel JPEGs (camera mounted sideways, "rotate": 90)."""

from __future__ import annotations

import io
import unittest

from test_view_navigation import load_module

JR = load_module("jpeg_rotate")

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Home Assistant always ships Pillow
    Image = None


def portrait_jpeg(width=544, height=960):
    """A panel-like portrait frame (4:2:0) with a red block top left."""
    image = Image.new("RGB", (width, height), (20, 20, 20))
    for x in range(0, 128):
        for y in range(0, 128):
            image.putpixel((x, y), (230, 20, 20))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85, subsampling=2)
    return output.getvalue()


def open_jpeg(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def is_red(pixel):
    return pixel[0] > 150 and pixel[1] < 90 and pixel[2] < 90


@unittest.skipIf(Image is None, "Pillow is required")
class JpegRotateTest(unittest.TestCase):
    def check_clockwise(self, turned):
        image = open_jpeg(turned)
        # 544x960 portrait -> 960x544 landscape; clockwise moves top left to top right.
        self.assertEqual(image.size, (960, 544))
        self.assertTrue(is_red(image.getpixel((960 - 40, 40))))
        self.assertFalse(is_red(image.getpixel((40, 40))))
        self.assertFalse(is_red(image.getpixel((960 - 40, 544 - 40))))

    def test_pillow_turns_clockwise(self):
        self.check_clockwise(JR._pillow_rotate(portrait_jpeg(), 90))

    def test_best_backend_turns_clockwise(self):
        self.check_clockwise(JR.rotate_jpeg(portrait_jpeg(), 90))

    def test_other_quarter_turns(self):
        source = portrait_jpeg()
        half = open_jpeg(JR.rotate_jpeg(source, 180))
        self.assertEqual(half.size, (544, 960))
        self.assertTrue(is_red(half.getpixel((544 - 40, 960 - 40))))
        counter = open_jpeg(JR.rotate_jpeg(source, 270))
        self.assertEqual(counter.size, (960, 544))
        self.assertTrue(is_red(counter.getpixel((40, 544 - 40))))

    def test_no_turn_and_invalid_input(self):
        source = portrait_jpeg()
        self.assertIs(JR.rotate_jpeg(source, 0), source)
        self.assertIsNone(JR.rotate_jpeg(source, 45))
        self.assertIsNone(JR.rotate_jpeg(b"\xff\xd8broken\xff\xd9", 90))

    def test_backend_is_reported(self):
        self.assertIn(JR.backend(), {"turbojpeg", "pillow"})

    def test_lossless_path_stays_off_until_validated(self):
        # ctypes into libturbojpeg is enabled only after a container test.
        self.assertFalse(JR.LOSSLESS_ENABLED)
        self.assertIsNone(JR._turbojpeg())
        self.assertEqual(JR.backend(), "pillow")

    @unittest.skipUnless(JR.backend() == "turbojpeg", "libturbojpeg not installed")
    def test_turbojpeg_turn_is_lossless(self):
        # Four quarter turns give back exactly the same pixels: the coded
        # blocks were only rearranged, never decoded and re-encoded.
        source = portrait_jpeg()
        data = source
        for _ in range(4):
            data = JR.rotate_jpeg(data, 90)
        self.assertEqual(open_jpeg(data).tobytes(), open_jpeg(source).tobytes())


if __name__ == "__main__":
    unittest.main()
