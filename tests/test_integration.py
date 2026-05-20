"""End-to-end tests exercising the DBPF read/write + render pipeline.

These run headless (no wx windows are shown) and use the three committed
``City - *.sc4`` files as fixtures.
"""

import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import rgnReader  # noqa: E402  (imports wx/PIL; chdir first so the .ini loads)

CITY_FILES = ["City - Small.sc4", "City - Medium.sc4", "City - Large.sc4"]


@pytest.mark.parametrize("city", CITY_FILES)
def test_sc4file_reads_real_city(city):
    sc4 = rgnReader.SC4File(os.path.join(ROOT, city))
    sc4.ReadHeader()
    sc4.ReadEntries()
    assert hasattr(sc4, "heightMapEntry")
    assert sc4.cityXSize in (1, 2, 4)
    assert sc4.cityXSize == sc4.cityYSize
    # the height payload is a 2-byte tag followed by float32 elevations
    payload = sc4.heightMapEntry.content
    expected = sc4.ySize * sc4.xSize
    heights = np.frombuffer(payload[2:], np.float32)
    assert heights.size == expected
    assert np.isfinite(heights).all()


def test_render_pipeline_small_city():
    sc4 = rgnReader.SC4File(os.path.join(ROOT, "City - Small.sc4"))
    sc4.ReadHeader()
    sc4.ReadEntries()
    heights = np.frombuffer(sc4.heightMapEntry.content[2:], np.float32)
    heights = heights.reshape((sc4.ySize, sc4.xSize)).astype(np.float32)

    light = rgnReader.Normalize((1, -5, -1))
    rawRGB = rgnReader.tools3D.onePassColors(
        False, heights.shape, 250.0, heights,
        rgnReader.GradientReader.paletteWater,
        rgnReader.GradientReader.paletteLand, light)
    assert len(rawRGB) == sc4.ySize * sc4.xSize * 3

    minx, miny, maxx, maxy, raw = rgnReader.tools3D.generateImage(
        250.0, heights.shape, heights.tobytes(), rawRGB)
    assert len(raw) == 514 * 428 * 6


def test_save_roundtrip(tmp_path):
    """Save a synthetic small city, then read it back and compare heights."""
    folder = str(tmp_path)
    waterLevel = 250.0

    rng = np.random.default_rng(7)
    heightMap = (rng.random((65, 65), dtype=np.float32) * 400.0 + 50.0)

    city = rgnReader.CityProxy(waterLevel, 0, 0, 1, 1)
    city.heightMap = heightMap

    light = rgnReader.Normalize((1, -5, -1))
    rawRGB = rgnReader.tools3D.onePassColors(
        False, heightMap.shape, waterLevel, heightMap,
        rgnReader.GradientReader.paletteWater,
        rgnReader.GradientReader.paletteLand, light)

    assert rgnReader.Save(city, folder, rawRGB, waterLevel) is True

    saved = os.path.join(folder, "City - New city(000-000).sc4")
    assert os.path.isfile(saved)

    sc4 = rgnReader.SC4File(saved)
    sc4.ReadHeader()
    sc4.ReadEntries()
    back = np.frombuffer(sc4.heightMapEntry.content[2:], np.float32)
    back = back.reshape((65, 65))
    assert np.array_equal(back, heightMap)
