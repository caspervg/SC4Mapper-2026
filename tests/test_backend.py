"""Regression tests for the pure-Python backend (qfs, tools3d).

These guard the reimplementation of the former C/C++ extensions.  The three
committed ``City - *.sc4`` files serve as real-world fixtures.
"""

import os
import struct
from importlib.resources import as_file, files

import numpy as np
import pytest

from sc4mapper import qfs
from sc4mapper import tools3d

CITY_FILES = ["City - Small.sc4", "City - Medium.sc4", "City - Large.sc4"]
COMPRESSED_SIG = 0xFB10


def _roundtrip(data):
    return qfs.decode(qfs.encode(data))


@pytest.mark.parametrize("data", [
    b"",
    b"A",
    b"hello world",
    b"a" * 5000,
    bytes(range(256)) * 40,
    b"abcabcabc" * 1000,
])
def test_qfs_roundtrip_synthetic(data):
    assert _roundtrip(data) == data


def test_qfs_roundtrip_random():
    rng = np.random.default_rng(1234)
    for size in (1, 17, 1024, 70000):
        data = rng.integers(0, 256, size=size, dtype=np.uint8).tobytes()
        assert _roundtrip(data) == data


def _iter_compressed_subfiles(path):
    """Yield the raw QFS streams of compressed subfiles in a DBPF file."""
    with open(path, "rb") as fh:
        blob = fh.read()
    raw = struct.unpack("<4s17I24s", blob[:96])
    count, index_pos, index_len = raw[9], raw[10], raw[11]
    index = blob[index_pos:index_pos + index_len]
    for i in range(count):
        _, _, _, loc, size = struct.unpack("<3I2i", index[i * 20:i * 20 + 20])
        content = blob[loc:loc + size]
        if len(content) >= 8:
            sig = struct.unpack("<H", content[4:6])[0]
            if sig == COMPRESSED_SIG:
                yield content[4:]


@pytest.mark.parametrize("city", CITY_FILES)
def test_qfs_decode_real_subfiles(city):
    with as_file(files("sc4mapper").joinpath("assets", city)) as path:
        found = 0
        for stream in _iter_compressed_subfiles(path):
            found += 1
            decoded = qfs.decode(stream)
            declared = (stream[2] << 16) + (stream[3] << 8) + stream[4]
            assert len(decoded) == declared
            # round-tripping the *decoded* payload must be lossless
            assert qfs.decode(qfs.encode(decoded)) == decoded
    assert found > 0, "expected at least one compressed subfile"


def test_tools3d_version():
    assert tools3d.GetVersion() == "v1.0d"


def test_tools3d_onepasscolors_shape():
    water = {0: (123, 189, 214), 200: (0, 8, 74)}
    land = {0: (123, 189, 214), 100: (0, 206, 0), 1000: (255, 255, 255)}
    h = np.full((65, 65), 300.0, np.float32)
    h[20:40, 20:40] = 100.0          # a basin below the water level
    light = (1.0, -5.0, -1.0)
    norm = (light[0] ** 2 + light[1] ** 2 + light[2] ** 2) ** 0.5
    light = tuple(v / norm for v in light)
    raw = tools3d.onePassColors(False, h.shape, 250.0, h, water, land, light)
    assert len(raw) == 65 * 65 * 3
    assert isinstance(raw, bytes)


def test_tools3d_generateimage_shape():
    water = {0: (123, 189, 214), 200: (0, 8, 74)}
    land = {0: (123, 189, 214), 100: (0, 206, 0), 1000: (255, 255, 255)}
    h = np.linspace(100, 800, 65 * 65, dtype=np.float32).reshape((65, 65))
    light = (0.182, -0.913, -0.182)
    colors = tools3d.onePassColors(False, h.shape, 250.0, h, water, land, light)
    minx, miny, maxx, maxy, raw = tools3d.generateImage(250.0, h.shape,
                                                        h.tobytes(), colors)
    assert len(raw) == 514 * 428 * 6
    assert 0 <= minx <= maxx <= 514
    assert 0 <= miny <= maxy <= 428
