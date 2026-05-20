"""Pure-Python / NumPy reimplementation of the former C++ extension ``tools3D``
(``Modules/tools3D.cpp``, copyright (c) 2013 Wouanagaine).

It generates terrain colour maps (:func:`onePassColors`) and isometric region
thumbnails (:func:`generateImage`).  Output is byte-for-byte compatible with
the original extension, so callers need only change the import name.
"""

import numpy as np

_THUMB_W = 514
_THUMB_H = 428


def GetVersion():
    """Return the codec version string expected by SC4MapApp."""
    return "v1.0d"


def _gradient_color(palette, keys, value):
    """Linearly interpolate ``palette`` (``{int: (r, g, b)}``) at ``value``.

    Mirrors ``Gradient::GetColor`` from tools3D.cpp, including the integer
    truncation of the interpolated components.
    """
    if value < keys[0]:
        return palette[keys[0]]
    prev = keys[0]
    for k in keys:
        if value < k:
            c0 = palette[prev]
            c1 = palette[k]
            alpha = float(value - prev) / float(k - prev)
            return tuple(int((1.0 - alpha) * c0[j] + c1[j] * alpha) for j in range(3))
        prev = k
    return palette[keys[-1]]


def _build_lut(palette):
    """Build an ``(N, 3)`` int32 lookup table for an integer-keyed gradient."""
    keys = sorted(int(k) for k in palette)
    lut = np.empty((keys[-1] + 1, 3), np.int32)
    for value in range(keys[-1] + 1):
        lut[value] = _gradient_color(palette, keys, value)
    return lut


def onePassColors(bLight, shape, waterLevel, height, paletteWater, paletteLand, lightDir):
    """Return raw RGB bytes (``xSize * ySize * 3``) shading a height field.

    ``shape`` is ``(ySize, xSize)``; ``height`` is a 2-D array of elevations.
    The ``bLight`` shadow-casting path of the original extension is not used
    anywhere in the application and is therefore not reimplemented.
    """
    ySize, xSize = int(shape[0]), int(shape[1])
    waterLevel = float(waterLevel)
    H = np.ascontiguousarray(height, dtype=np.float32).reshape((ySize, xSize))

    # surface normals from neighbouring heights (border pixels stay zero)
    dx = np.zeros((ySize, xSize), np.float32)
    dy = np.zeros((ySize, xSize), np.float32)
    interior = np.zeros((ySize, xSize), bool)
    if ySize > 2 and xSize > 2:
        dx[1:-1, 1:-1] = H[1:-1, 0:-2] - H[1:-1, 2:]
        dy[1:-1, 1:-1] = H[0:-2, 1:-1] - H[2:, 1:-1]
        interior[1:-1, 1:-1] = True
    dz = 2.0
    mag = np.sqrt(dx * dx + dy * dy + dz * dz)

    nx = np.where(interior, dx / mag, 0.0)
    ny = np.where(interior, dz / mag, 0.0)
    nz = np.where(interior, dy / mag, 0.0)

    n = ny * 255.0
    light = nx * lightDir[0] + ny * lightDir[1] + nz * lightDir[2]
    c = np.where(light < 0.0, 191 - (light * 64.0).astype(np.int32), 255).astype(np.int32)

    water_lut = _build_lut(paletteWater)
    land_lut = _build_lut(paletteLand)
    cc = c[:, :, None]

    gray = n < 20.0
    water = (~gray) & (H < waterLevel)

    half = (c // 2).astype(np.uint8)
    gray_rgb = np.dstack([half, half, half])

    wv = np.clip((waterLevel - H).astype(np.int32), 0, water_lut.shape[0] - 1)
    wrgb = ((water_lut[wv] * cc) >> 8).astype(np.uint8)

    lv = np.clip((H - waterLevel).astype(np.int32), 0, land_lut.shape[0] - 1)
    lrgb = ((land_lut[lv] * cc) >> 8).astype(np.uint8)

    out = np.where(gray[:, :, None], gray_rgb,
                   np.where(water[:, :, None], wrgb, lrgb))
    return out.astype(np.uint8).tobytes()


def _as_bytes(buf):
    if isinstance(buf, (bytes, bytearray, memoryview)):
        return bytes(buf)
    return np.ascontiguousarray(buf).tobytes()


def generateImage(waterLevel, shape, heights, colors):
    """Build the isometric region-view thumbnail (normal + alpha layers).

    Returns ``(minx, miny, maxx, maxy, raw)`` where ``raw`` is two stacked
    ``514 x 428`` RGB images (``514 * 428 * 6`` bytes total).
    """
    ySize, xSize = int(shape[0]), int(shape[1])
    waterLevel = float(waterLevel)
    H = np.frombuffer(_as_bytes(heights), np.float32).reshape((ySize, xSize))
    C = np.frombuffer(_as_bytes(colors), np.uint8).reshape((ySize, xSize, 3))

    img1 = np.zeros((_THUMB_H, _THUMB_W, 3), np.uint8)
    img2 = np.zeros((_THUMB_H, _THUMB_W, 3), np.uint8)
    _SENTINEL = 0x7F7F7F7F
    minYmap = np.full(_THUMB_W, _SENTINEL, np.int64)
    maxYmap = np.zeros(_THUMB_W, np.int64)

    minx, miny, maxx, maxy = _THUMB_W, _THUMB_H, 0, 0

    for y in range(ySize):
        for x in range(xSize):
            x2 = x * (512.0 - 150.0) / 256.0 + 150.0 - (150.0 * y / 256.0)
            y2 = y * 181.0 / 256.0 + 75.0 * x / 256.0
            yBase = y2
            h = H[y, x]
            if h < waterLevel:
                h = waterLevel
            h *= 21.0 / 250.0
            y2 -= h
            y2 += _THUMB_H - 256
            yBase += _THUMB_H - 256
            if y2 < 0.0:
                y2 = 0.0
            if y2 < miny:
                miny = int(y2)
            if yBase > maxy:
                maxy = int(yBase)
            if x2 < minx:
                minx = int(x2)
            if x2 > maxx:
                maxx = int(x2)

            ix2 = int(x2)
            iy2 = int(y2)
            if minYmap[ix2] > iy2:
                minYmap[ix2] = iy2
            if maxYmap[ix2] < iy2:
                maxYmap[ix2] = iy2

            j1 = int(yBase)
            if j1 > iy2:
                img1[iy2:j1, ix2] = C[y, x]
                img2[iy2:j1, ix2, 2] = 255

    for x in range(int(minx), int(maxx)):
        lo = minYmap[x]
        if lo == _SENTINEL:        # column never drawn into
            continue
        img2[lo, x, 0] = 255
        img2[lo, x, 1] = 255
        img2[maxYmap[x], x, 0] = 255
        img2[maxYmap[x], x, 1] = 255

    raw = img1.tobytes() + img2.tobytes()
    return (int(minx), int(miny), int(maxx), int(maxy), raw)
