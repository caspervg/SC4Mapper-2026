"""Offline coastal import benchmark: uv run python scripts/benchmark_geo_import.py.

The download case simulates 25 ms per tile, not a live provider's latency.
Run on each revision to compare the complete sampling/water/height pipeline.
"""

import io
import statistics
import time

import numpy as np
from PIL import Image

from sc4mapper import geo


class Tiles:
    max_workers = 4

    def __init__(self, delay):
        self.delay = delay
        buffer = io.BytesIO()
        # Terrarium -3 m: deliberately below sea level, including the polders.
        Image.new("RGB", (256, 256), (127, 253, 0)).save(buffer, format="PNG")
        self.data = buffer.getvalue()

    def fetch(self, *coordinates):
        if self.delay:
            time.sleep(self.delay)
        return self.data


def run(tiles, delay):
    request = geo.GeoImportRequest(51.22, 2.92, tiles, tiles, water_source="mask")
    span = request.tiles_x * 64 * request.metres_per_cell
    lon, lat = geo.local_offsets_to_lonlat(
        51.22, 2.92, np.array([span, -span]), np.full(2, span / 8 - 4))
    source = Tiles(delay)
    start = time.perf_counter()
    mask = geo.rasterize_coastlines(request, [list(zip(lon, lat))])
    mask[request.grid_shape[0] // 2::8, ::8] = True  # many isolated ponds
    result = geo.build_region_grid(request, source, water_mask=mask)
    elapsed = time.perf_counter() - start
    assert 0.25 < result.water_fraction < 0.4
    assert result.lifted_cells > 0
    return elapsed


if __name__ == "__main__":
    for tiles in (8, 16):
        for delay in (0, 0.025):
            seconds = statistics.median(run(tiles, delay) for _ in range(3))
            print(f"{tiles * 64 + 1} square, "
                  f"{'25 ms/tile' if delay else 'memory tiles'}: {seconds:.3f} s")
