"""Quarter turns of JPEG frames from panels whose camera is mounted sideways.

A panel whose camera sits a quarter turn from its landscape UI sends portrait
JPEGs and announces ``"rotate": 90`` (clockwise degrees) in its retained
camera status. The Bridge turns every frame once, before Home Assistant or
any viewer sees it.

The turn is lossless when libjpeg-turbo's TurboJPEG library is available
(Home Assistant OS and the container ship it for the camera integration): the
coded blocks are rearranged without decoding pixels. Panels encode 4:2:0 with
whole 16x16 blocks for this, so the result stays a common 4:2:0 JPEG.
Without the library, Pillow decodes, turns and re-encodes the frame.

The module has no Home Assistant imports. Every function is blocking and
belongs in an executor.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import logging
import threading
from typing import Final

_LOGGER = logging.getLogger(__name__)

ROTATIONS: Final = frozenset({0, 90, 180, 270})

# TurboJPEG transform operations (turbojpeg.h): TJXOP_ROT90/180/270 turn
# clockwise.
_TJXOP: Final = {90: 5, 180: 6, 270: 7}
# TJXOPT_PERFECT: fail instead of silently dropping partial edge blocks.
_TJXOPT_PERFECT: Final = 1
# Pillow fallback quality: close to the panel's own range, 4:2:0 like the panel.
_PILLOW_QUALITY: Final = 90

_LIBRARY_CANDIDATES: Final = (
    "libturbojpeg.so.0",
    "/usr/lib/libturbojpeg.so.0",
    "/usr/lib/x86_64-linux-gnu/libturbojpeg.so.0",
    "/usr/lib/aarch64-linux-gnu/libturbojpeg.so.0",
    "/usr/lib/arm-linux-gnueabihf/libturbojpeg.so.0",
    "/usr/local/lib/libturbojpeg.so.0",
    "/opt/libjpeg-turbo/lib64/libturbojpeg.so",
    "/opt/libjpeg-turbo/lib32/libturbojpeg.so",
    "/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib",
    "/usr/local/opt/jpeg-turbo/lib/libturbojpeg.dylib",
    "C:/libjpeg-turbo64/bin/turbojpeg.dll",
)


class _TjRegion(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


class _TjTransform(ctypes.Structure):
    _fields_ = [("r", _TjRegion), ("op", ctypes.c_int), ("options", ctypes.c_int),
                ("data", ctypes.c_void_p), ("customFilter", ctypes.c_void_p)]


class _TurboJpeg:
    """The TurboJPEG 2 transform API (also exported by libjpeg-turbo 3)."""

    def __init__(self, library: ctypes.CDLL) -> None:
        self._init = library.tjInitTransform
        self._init.restype = ctypes.c_void_p
        self._init.argtypes = []
        self._destroy = library.tjDestroy
        self._destroy.restype = ctypes.c_int
        self._destroy.argtypes = [ctypes.c_void_p]
        self._free = library.tjFree
        self._free.restype = None
        self._free.argtypes = [ctypes.c_void_p]
        self._transform = library.tjTransform
        self._transform.restype = ctypes.c_int
        self._transform.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(_TjTransform), ctypes.c_int,
        ]

    def rotate(self, data: bytes, degrees: int) -> bytes | None:
        handle = self._init()
        if not handle:
            return None
        destination = ctypes.c_void_p()
        size = ctypes.c_ulong(0)
        transform = _TjTransform()
        transform.op = _TJXOP[degrees]
        transform.options = _TJXOPT_PERFECT
        try:
            status = self._transform(handle, data, len(data), 1, ctypes.byref(destination),
                                     ctypes.byref(size), ctypes.byref(transform), 0)
            if status != 0 or not destination.value or size.value == 0:
                return None
            return ctypes.string_at(destination.value, size.value)
        finally:
            if destination.value:
                self._free(destination.value)
            self._destroy(handle)


_lock = threading.Lock()
_turbo: _TurboJpeg | None = None
_turbo_checked = False


def _library_candidates() -> list[str]:
    candidates: list[str] = []
    found = ctypes.util.find_library("turbojpeg")
    if found:
        candidates.append(found)
    try:  # PyTurboJPEG, where installed, knows the platform's usual paths.
        from turbojpeg import DEFAULT_LIB_PATHS  # type: ignore[import-not-found]
        import platform

        candidates.extend(DEFAULT_LIB_PATHS.get(platform.system(), []))
    except Exception:  # noqa: BLE001 - optional helper only
        pass
    candidates.extend(_LIBRARY_CANDIDATES)
    return candidates


def _turbojpeg() -> _TurboJpeg | None:
    global _turbo, _turbo_checked
    with _lock:
        if _turbo_checked:
            return _turbo
        _turbo_checked = True
        for candidate in _library_candidates():
            try:
                _turbo = _TurboJpeg(ctypes.CDLL(candidate))
            except (OSError, AttributeError):
                continue
            _LOGGER.debug("HomeTiles camera turns frames losslessly with %s", candidate)
            return _turbo
        _LOGGER.debug("HomeTiles camera turns frames with Pillow (libturbojpeg not found)")
        return None


def backend() -> str:
    """``turbojpeg`` (lossless), ``pillow`` (re-encoded) or ``none``."""
    if _turbojpeg() is not None:
        return "turbojpeg"
    try:
        import PIL  # noqa: F401
    except ImportError:
        return "none"
    return "pillow"


def _pillow_rotate(data: bytes, degrees: int) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    # Pillow's ROTATE_* turn counterclockwise.
    method = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_90}[degrees]
    try:
        with Image.open(io.BytesIO(data)) as image:
            turned = image.transpose(method)
        output = io.BytesIO()
        turned.save(output, format="JPEG", quality=_PILLOW_QUALITY, subsampling=2)
        return output.getvalue()
    except Exception:  # noqa: BLE001 - a broken frame must not stop the stream
        return None


def rotate_jpeg(data: bytes, degrees: int) -> bytes | None:
    """Return *data* turned clockwise by *degrees*; None if that failed.

    0 returns *data* unchanged. Blocking: run it in an executor.
    """
    if degrees == 0:
        return data
    if degrees not in _TJXOP:
        return None
    turbo = _turbojpeg()
    if turbo is not None:
        try:
            turned = turbo.rotate(data, degrees)
        except Exception:  # noqa: BLE001 - fall back to Pillow
            turned = None
        if turned is not None:
            return turned
    return _pillow_rotate(data, degrees)
