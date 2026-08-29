"""Tests for 16-bit grayscale PNG heightmap loading."""

import io

import numpy as np
from PIL import Image

from sc4mapper import png16


def _png_bytes(im, **save_kw):
    buf = io.BytesIO()
    im.save(buf, format="PNG", **save_kw)
    return buf.getvalue()


def _open_png(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def test_true_16bit_grayscale_png_is_accepted():
    """DEM-style uint16 PNGs open as I;16 in current Pillow, not I."""
    src = np.linspace(0, 65535, 65 * 65, dtype=np.uint16).reshape(65, 65)
    im = Image.fromarray(src)
    opened = _open_png(_png_bytes(im))
    assert png16.is_16bit_grayscale(opened)
    got = png16.to_uint16_array(opened)
    np.testing.assert_array_equal(got, src)


def test_mode_i_export_png_roundtrip():
    """PNGs written the way ExportAsPNG does (mode I) must still import."""
    src = np.array([[0, 250, 1000], [32767, 40000, 65535]], dtype=np.uint16)
    im = Image.frombytes("I", (src.shape[1], src.shape[0]),
                         src.astype(np.int32).tobytes())
    opened = _open_png(_png_bytes(im))
    assert png16.is_16bit_grayscale(opened)
    as_i = png16.as_mode_i(opened)
    assert as_i.mode == "I"
    raw = np.frombuffer(as_i.tobytes(), np.int32).reshape(src.shape)
    np.testing.assert_array_equal(raw.astype(np.uint16), src)


def test_8bit_grayscale_png_is_rejected():
    src = np.arange(65 * 65, dtype=np.uint8).reshape(65, 65)
    opened = _open_png(_png_bytes(Image.fromarray(src)))
    assert opened.mode == "L"
    assert not png16.is_16bit_grayscale(opened)


def test_as_mode_i_preserves_peaks():
    src = np.zeros((65, 65), dtype=np.uint16)
    src[10, 10] = 65535
    src[20, 30] = 250
    opened = _open_png(_png_bytes(Image.fromarray(src)))
    as_i = png16.as_mode_i(opened)
    assert as_i.mode == "I"
    got = np.frombuffer(as_i.tobytes(), np.int32).reshape(65, 65)
    assert got[10, 10] == 65535
    assert got[20, 30] == 250
    assert got[0, 0] == 0
