"""Load 16-bit grayscale PNG heightmaps.

Older PIL reported 16-bit grayscale PNGs as mode ``I`` (32-bit int). Current
Pillow keeps them as ``I;16`` / ``I;16B`` / ``I;16L``, which the original
``im.mode != "I"`` check treated as invalid.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# Pillow mode strings used for 16-bit unsigned grayscale.
PNG16_MODES = ("I", "I;16", "I;16L", "I;16B")


def is_16bit_grayscale(im: Image.Image) -> bool:
    """Return True if *im* is a 16-bit (or 32-bit integer) grayscale image."""
    return im.mode in PNG16_MODES


def as_mode_i(im: Image.Image) -> Image.Image:
    """Return *im* in mode ``I``, preserving 16-bit sample values.

    Mode ``I`` is what the rest of the importer expects: 32-bit integers whose
    values still sit in ``0..65535`` for a 16-bit PNG.
    """
    if im.mode == "I":
        return im
    return im.convert("I")


def to_uint16_array(im: Image.Image) -> np.ndarray:
    """Convert a 16-bit grayscale PIL image to a ``uint16`` ``(H, W)`` array."""
    if im.mode in ("I;16", "I;16L", "I;16B"):
        return np.asarray(im, dtype=np.uint16)
    if im.mode == "I":
        return np.clip(np.asarray(im, dtype=np.int32), 0, 65535).astype(np.uint16)
    raise ValueError(
        "not a 16-bit grayscale image (mode %r)" % (im.mode,)
    )


def clamp_to_16bit(im: Image.Image) -> Image.Image:
    """Clamp a mode ``I`` image to ``0..65535``.

    Resampling filters such as bicubic overshoot outside the input range, and
    mode ``I`` is signed 32-bit with no clamping of its own, so a resized
    heightmap can hold negative or >65535 samples. Those wrap into wildly
    wrong altitudes once the importer narrows them to ``uint16``.
    """
    a = np.asarray(im, dtype=np.int32)
    if a.min() >= 0 and a.max() <= 65535:
        return im
    return Image.fromarray(np.clip(a, 0, 65535).astype(np.int32), "I")
