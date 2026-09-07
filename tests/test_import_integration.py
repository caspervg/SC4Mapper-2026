"""End-to-end import checks against the real example PNGs in tests/data.

These exercise the same sequence ``OverView.ImportPNG`` runs -- open, mode
gate, convert, tile assembly -- on genuine files rather than synthesized
arrays, which is where the Pillow mode regression actually showed up.
"""

import pathlib

import numpy as np
import pytest
from PIL import Image

from sc4mapper import png16

DATA = pathlib.Path(__file__).parent / "data"

# example.png is 1025x1025, i.e. a 16x16 city region (16 * 64 + 1).
REGION_SIZE = (16, 16)


@pytest.fixture(scope="module")
def example16():
    im = Image.open(DATA / "example.png")
    im.load()
    return im


def test_real_16bit_png_opens_as_i16(example16):
    """The mode the old `im.mode != "I"` gate wrongly rejected."""
    assert example16.mode == "I;16"
    assert example16.size == (1025, 1025)


def test_real_16bit_png_passes_the_import_gate(example16):
    assert png16.is_16bit_grayscale(example16)
    assert png16.as_mode_i(example16).mode == "I"


def test_real_8bit_png_is_rejected():
    im = Image.open(DATA / "8bit_example.png")
    im.load()
    assert im.mode == "L"
    assert not png16.is_16bit_grayscale(im)
    with pytest.raises(ValueError):
        png16.to_uint16_array(im)


def test_import_assembles_full_region(example16):
    """Tile assembly must reproduce the source image pixel for pixel."""
    heights = png16.tiles_to_heightmap(example16, REGION_SIZE)
    assert heights.shape == (1025, 1025)
    assert heights.dtype == np.uint16
    np.testing.assert_array_equal(heights, np.asarray(example16, dtype=np.uint16))


def test_import_preserves_terrain_range(example16):
    """Elevations must survive the round trip unscaled and unclipped."""
    src = np.asarray(example16, dtype=np.uint16)
    heights = png16.tiles_to_heightmap(example16, REGION_SIZE)
    assert (heights.min(), heights.max()) == (src.min(), src.max())
    assert len(np.unique(heights)) == len(np.unique(src))


def test_import_after_resize_round_trips(example16):
    """The resize path must yield a correctly sized, in-range region.

    example.png is gentle terrain, so bicubic does not overshoot here; the
    clamp itself is regression-tested in test_png16.py against a source that
    provably does.
    """
    im = png16.as_mode_i(example16)
    resized = png16.clamp_to_16bit(
        im.resize((8 * 64 + 1, 8 * 64 + 1), Image.Resampling.BICUBIC))
    heights = png16.tiles_to_heightmap(resized, (8, 8))
    assert heights.shape == (513, 513)
    assert heights.max() <= np.asarray(example16, dtype=np.uint16).max()
