"""Create regions from real-world locations using tiled elevation data.

This module turns a point on the globe plus a scale into the ``uint16``
decimetre height grid the rest of SC4Mapper works with, so a region can be
built from a location instead of from a bitmap.

Nothing here imports wx, and every network access goes through an injected
fetcher, so the whole pipeline stays testable headless and offline.

Coordinate conventions
----------------------
* Elevation sources are slippy-map tiles in Web Mercator (EPSG:3857).
* The region grid is laid out on a *local* metric frame centred on the
  region -- east/north metres, converted back to lon/lat row by row.  Over a
  region-sized area (tens of km) this is accurate to well under a metre,
  which is far inside the noise of any global DEM, and it avoids the
  north-south stretching you get by sampling Web Mercator directly.
* Grid row 0 is the **north** edge and column 0 the **west** edge, matching
  config.bmp and the in-game region view.

Height convention
-----------------
Region height arrays are ``uint16`` decimetres, with SC4's sea level at
250 m (2500 dm).  That is the same representation the bitmap import paths
produce, so a geographic import drops straight into ``SC4Region.height``.
"""

import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np
from PIL import Image

# --- SC4 geometry ---------------------------------------------------------

#: Width of one terrain cell in metres.
CELL_SIZE_M = 16.0
#: Cells along one edge of a small city tile (one config.bmp pixel).
CELLS_PER_TILE = 64
#: SC4's sea level, in metres.
SEA_LEVEL_M = 250.0
#: Largest height the uint16 decimetre representation can hold, in metres.
MAX_HEIGHT_M = 65535 / 10.0

# --- Elevation source -----------------------------------------------------

#: Mapzen/AWS "terrarium" terrain tiles: global, open, and no API key.
DEFAULT_TILE_URL = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
#: Attribution that must be shown for the default source.
DEFAULT_ATTRIBUTION = (
    "Elevation: Mapzen Terrain Tiles / AWS Open Data. Sources include SRTM, "
    "USGS 3DEP/NED, Copernicus/EU-DEM, GMTED2010, ETOPO1 and national "
    "datasets. See https://registry.opendata.aws/terrain-tiles/"
)
#: Raster map sources for the underlay, offered as a starting point.
#:
#: There is deliberately no default. Unlike the elevation source, general
#: map and imagery tile servers each carry their own terms -- OpenStreetMap's
#: policy, for one, explicitly forbids a distributed application drawing on
#: their tiles -- so choosing a provider (and supplying any API key) has to
#: be the user's decision, not a hardcoded one. Set ``basemap_url`` in
#: SC4Mapper.ini to switch the underlay on.
BASEMAP_PRESETS = {}

#: Terrarium tiles are 256x256.
TILE_PIXELS = 256
#: Highest zoom the default source publishes.
SOURCE_MAX_ZOOM = 15
#: Ground resolution of one pixel at zoom 0 on the equator, in metres.
EQUATOR_RESOLUTION_M = 2 * math.pi * 6378137.0 / TILE_PIXELS
#: Latitude beyond which Web Mercator is undefined.
MERCATOR_MAX_LAT = 85.0511287798066

#: Refuse to assemble a mosaic larger than this many tiles.
MAX_MOSAIC_TILES = 512


class GeoImportError(Exception):
    """Raised when a location cannot be turned into a region."""


# --- Web Mercator / slippy map --------------------------------------------


def lonlat_to_tile(lon, lat, zoom):
    """Return fractional slippy-map tile coordinates for a lon/lat.

    Accepts scalars or numpy arrays.
    """
    lat = np.clip(lat, -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT)
    n = 2.0 ** zoom
    x = (np.asarray(lon, dtype=np.float64) + 180.0) / 360.0 * n
    lat_rad = np.radians(lat)
    y = (1.0 - np.arcsinh(np.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x, y, zoom):
    """Inverse of :func:`lonlat_to_tile`."""
    n = 2.0 ** zoom
    lon = np.asarray(x, dtype=np.float64) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * np.asarray(y, dtype=np.float64) / n))))
    return lon, lat


def tile_resolution_m(zoom, lat):
    """Ground resolution of one tile pixel, in metres per pixel."""
    return EQUATOR_RESOLUTION_M * math.cos(math.radians(lat)) / (2.0 ** zoom)


def zoom_for_resolution(target_m, lat, max_zoom=SOURCE_MAX_ZOOM):
    """Smallest zoom whose pixels are at least as fine as ``target_m``.

    Matching source resolution to the sample spacing keeps the download
    bounded no matter how far the user zooms out: a coarser region simply
    pulls a coarser zoom, so tile counts stay roughly constant.
    """
    if target_m <= 0:
        raise GeoImportError("Sample spacing must be positive")
    cos_lat = max(math.cos(math.radians(min(abs(lat), MERCATOR_MAX_LAT))), 1e-6)
    ideal = math.log2(EQUATOR_RESOLUTION_M * cos_lat / target_m)
    return int(max(0, min(max_zoom, math.ceil(ideal))))


# --- Local metric frame ---------------------------------------------------


#: Names the exact projection a georeference record describes, so a reader
#: in another language can reproduce it rather than guessing at a model.
PROJECTION_MODEL = "local_equirectangular_wgs84_series_v1"
#: Coordinates are plain WGS84 lat/lon, as served by the tile sources.
PROJECTION_CRS = "EPSG:4326"

#: Series coefficients for metres per degree on the WGS84 ellipsoid. They
#: are published in the record itself, so a reader needs no outside
#: knowledge -- and they live here as constants so the two cannot drift.
METRES_PER_DEGREE_LAT_COEFFS = (111132.92, -559.82, 1.175, -0.0023)
METRES_PER_DEGREE_LON_COEFFS = (111412.84, -93.5, 0.118)


def metres_per_degree_lat(lat):
    """Length of one degree of latitude, in metres, on the WGS84 ellipsoid."""
    a, b, c, d = METRES_PER_DEGREE_LAT_COEFFS
    phi = math.radians(lat)
    return (a
            + b * math.cos(2 * phi)
            + c * math.cos(4 * phi)
            + d * math.cos(6 * phi))


def metres_per_degree_lon(lat):
    """Length of one degree of longitude, in metres, on the WGS84 ellipsoid.

    Accepts scalars or numpy arrays, so it can be evaluated per grid row.
    """
    a, b, c = METRES_PER_DEGREE_LON_COEFFS
    phi = np.radians(lat)
    return (a * np.cos(phi)
            + b * np.cos(3 * phi)
            + c * np.cos(5 * phi))


def lonlat_to_local_offsets(center_lat, center_lon, lon, lat):
    """Inverse of :func:`local_offsets_to_lonlat`.

    Closed form, no iteration: longitude is scaled at the *target* latitude,
    which is already known.
    """
    north = ((np.asarray(lat, dtype=np.float64) - center_lat)
             * metres_per_degree_lat(center_lat))
    m_per_deg_lon = metres_per_degree_lon(lat)
    m_per_deg_lon = np.where(np.abs(m_per_deg_lon) < 1e-6, 1e-6, m_per_deg_lon)
    east = (np.asarray(lon, dtype=np.float64) - center_lon) * m_per_deg_lon
    return east, north


def local_offsets_to_lonlat(center_lat, center_lon, east_m, north_m):
    """Convert local east/north metre offsets to lon/lat.

    Latitude is derived first, then longitude is scaled at *that* latitude
    rather than at the region centre.  Over a tall region the difference is
    real: at 60 degrees north, cos(phi) changes by ~1.6% across 64 km, which
    would otherwise show up as an east-west scale error.
    """
    lat = center_lat + np.asarray(north_m, dtype=np.float64) / metres_per_degree_lat(center_lat)
    m_per_deg_lon = metres_per_degree_lon(lat)
    m_per_deg_lon = np.where(np.abs(m_per_deg_lon) < 1e-6, 1e-6, m_per_deg_lon)
    lon = center_lon + np.asarray(east_m, dtype=np.float64) / m_per_deg_lon
    return lon, lat


# --- Terrarium decoding ---------------------------------------------------


def decode_terrarium(rgb):
    """Decode terrarium-encoded RGB pixels to elevation in metres.

    ``height = (R * 256 + G + B / 256) - 32768``
    """
    arr = np.asarray(rgb, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise GeoImportError("Elevation tile is not an RGB image")
    return (arr[:, :, 0] * 256.0
            + arr[:, :, 1]
            + arr[:, :, 2] / 256.0) - 32768.0


# --- Tile fetching --------------------------------------------------------


class TileFetcher(Protocol):
    """Something that can return the bytes of an elevation tile."""

    def fetch(self, zoom: int, x: int, y: int) -> Optional[bytes]:
        """Return PNG bytes, or ``None`` when the tile does not exist."""


class HttpTileFetcher:
    """Fetch elevation tiles over HTTP, with an on-disk cache.

    Tiles are immutable, so the cache never needs invalidating; re-importing
    the same area is offline and instant.
    """

    def __init__(self, url_template=DEFAULT_TILE_URL, cache_dir=None,
                 user_agent=None, timeout=30):
        self.url_template = url_template
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.user_agent = user_agent or "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)"

    def _cache_path(self, zoom, x, y):
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, str(zoom), str(x), "%d.png" % y)

    def fetch(self, zoom, x, y):
        path = self._cache_path(zoom, x, y)
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()

        url = self.url_template.format(z=zoom, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            raise GeoImportError("Elevation server returned HTTP %d for %s"
                                 % (exc.code, url)) from exc
        except urllib.error.URLError as exc:
            raise GeoImportError("Could not reach the elevation server (%s). "
                                 "Check your network connection." % exc.reason) from exc

        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        return data


class DictTileFetcher:
    """In-memory fetcher backed by a ``{(z, x, y): bytes}`` mapping.

    Used by the tests, and handy for feeding a pre-downloaded tile set.
    """

    def __init__(self, tiles=None):
        self.tiles = dict(tiles or {})
        self.requests = []

    def fetch(self, zoom, x, y):
        self.requests.append((zoom, x, y))
        return self.tiles.get((zoom, x, y))


def _decode_tile(data):
    """Decode PNG bytes into a 2D float32 elevation array."""
    import io

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        return decode_terrarium(np.asarray(img))


def _decode_rgb_tile(data):
    """Decode image bytes into a float32 RGB array."""
    import io

    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.float32)


# --- Sampling -------------------------------------------------------------


def _bilinear(source, px, py):
    """Bilinearly sample ``source`` at continuous pixel coordinates.

    Handles both 2D (elevation) and 3D (RGB) sources.

    Pixel *centres* sit at ``i + 0.5``, so the half-pixel shift is applied
    here; skipping it biases every sample by half a source pixel, which at a
    coarse zoom is tens of metres on the ground.
    """
    height, width = source.shape[:2]
    fx = np.asarray(px, dtype=np.float64) - 0.5
    fy = np.asarray(py, dtype=np.float64) - 0.5

    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    tx = fx - x0
    ty = fy - y0
    if source.ndim == 3:
        tx = tx[..., None]
        ty = ty[..., None]

    x0c = np.clip(x0, 0, width - 1)
    x1c = np.clip(x0 + 1, 0, width - 1)
    y0c = np.clip(y0, 0, height - 1)
    y1c = np.clip(y0 + 1, 0, height - 1)

    v00 = source[y0c, x0c]
    v10 = source[y0c, x1c]
    v01 = source[y1c, x0c]
    v11 = source[y1c, x1c]

    top = v00 + (v10 - v00) * tx
    bottom = v01 + (v11 - v01) * tx
    return top + (bottom - top) * ty


# --- Requests and results -------------------------------------------------


@dataclass(frozen=True)
class GeoReference:
    """Everything needed to map a region cell back to the real world.

    Kept as its own object so it can later be written into the city saves as
    a sidecar DBPF record, letting in-game plugins line real-world data up
    with the imported terrain.
    """

    center_lat: float
    center_lon: float
    tiles_x: int
    tiles_y: int
    metres_per_cell: float
    rotation_deg: float
    sea_level_m: float
    vertical_scale: float
    sea_reference_m: float
    source: str = DEFAULT_TILE_URL
    zoom: int = 0

    @property
    def width_m(self):
        return self.tiles_x * CELLS_PER_TILE * self.metres_per_cell

    @property
    def height_m(self):
        return self.tiles_y * CELLS_PER_TILE * self.metres_per_cell

    def to_dict(self):
        """A plain dict, ready for JSON or a binary record."""
        return {
            "center_lat": self.center_lat,
            "center_lon": self.center_lon,
            "tiles_x": self.tiles_x,
            "tiles_y": self.tiles_y,
            "metres_per_cell": self.metres_per_cell,
            "rotation_deg": self.rotation_deg,
            "sea_level_m": self.sea_level_m,
            "vertical_scale": self.vertical_scale,
            "sea_reference_m": self.sea_reference_m,
            "source": self.source,
            "zoom": self.zoom,
        }


@dataclass
class GeoImportRequest:
    """A location, a footprint and a vertical mapping."""

    center_lat: float
    center_lon: float
    tiles_x: int
    tiles_y: int
    metres_per_cell: float = CELL_SIZE_M
    rotation_deg: float = 0.0
    #: "match" keeps real-world proportions, "true" keeps real metres, and
    #: "manual" uses vertical_scale as given.
    vertical_mode: str = "match"
    vertical_scale: float = 1.0
    sea_level_m: float = SEA_LEVEL_M
    #: Which real-world elevation becomes SC4's shoreline. "sea" uses real
    #: sea level, "lowest" drops the datum below the lowest ground in the
    #: area so everything imports as dry land, and "manual" uses
    #: sea_reference_m as given.
    water_datum_mode: str = "sea"
    sea_reference_m: float = 0.0
    ocean_depth_m: float = 20.0
    keep_bathymetry: bool = False
    despike_threshold_m: float = 200.0
    #: How water is decided. "elevation" uses the shoreline datum alone;
    #: "mask" trusts an OpenStreetMap water mask and makes everything else
    #: dry, whatever its elevation; "both" takes the union.
    water_source: str = "elevation"
    water_depth_m: float = 3.0
    #: Water bodies smaller than this are ignored, and any sitting more than
    #: min_water_rise_m above the shoreline are left as terrain rather than
    #: carved down to it.
    min_water_area_cells: int = 64
    max_water_rise_m: float = 30.0
    max_zoom: int = 14
    zoom: Optional[int] = None

    def effective_water_datum(self, elevation_m=None):
        """The real elevation that becomes SC4's shoreline.

        "lowest" needs the sampled ground to work from, so it is resolved
        once the elevation grid exists; before then it falls back to the
        configured value.
        """
        if self.water_datum_mode == "sea":
            return 0.0
        if self.water_datum_mode == "lowest":
            if elevation_m is None:
                return self.sea_reference_m
            # A margin below the lowest ground, so nothing floods.
            return float(np.min(elevation_m)) - 1.0
        return self.sea_reference_m

    def effective_vertical_scale(self):
        """The vertical scale actually applied, after resolving the mode."""
        if self.vertical_mode == "match":
            return isotropic_vertical_scale(self.metres_per_cell)
        if self.vertical_mode == "true":
            return 1.0
        return self.vertical_scale

    def validate(self):
        if not -90.0 <= self.center_lat <= 90.0:
            raise GeoImportError("Latitude must be between -90 and 90")
        if not -180.0 <= self.center_lon <= 180.0:
            raise GeoImportError("Longitude must be between -180 and 180")
        if abs(self.center_lat) > MERCATOR_MAX_LAT:
            raise GeoImportError(
                "Latitude %.4f is outside the coverage of Web Mercator tiles "
                "(+/-85.05 degrees)" % self.center_lat)
        if self.tiles_x < 1 or self.tiles_y < 1:
            raise GeoImportError("A region needs at least one tile on each side")
        if self.metres_per_cell <= 0:
            raise GeoImportError("Metres per cell must be positive")
        if self.vertical_scale <= 0:
            raise GeoImportError("Vertical scale must be positive")
        if self.vertical_mode not in ("match", "true", "manual"):
            raise GeoImportError(
                "Vertical mode must be 'match', 'true' or 'manual'")
        if self.water_datum_mode not in ("sea", "lowest", "manual"):
            raise GeoImportError(
                "Water datum mode must be 'sea', 'lowest' or 'manual'")
        if self.water_source not in ("elevation", "mask", "both"):
            raise GeoImportError(
                "Water source must be 'elevation', 'mask' or 'both'")

    @property
    def grid_shape(self):
        """(rows, columns) of terrain vertices -- cells plus a shared edge."""
        return (self.tiles_y * CELLS_PER_TILE + 1,
                self.tiles_x * CELLS_PER_TILE + 1)


@dataclass
class GeoImportResult:
    """The height grid plus enough detail to explain what happened."""

    height_dm: np.ndarray
    elevation_m: np.ndarray
    georeference: GeoReference
    zoom: int
    tiles_fetched: int = 0
    tiles_missing: int = 0
    clamped_vertices: int = 0
    despiked_vertices: int = 0
    water_cells: int = 0
    lifted_cells: int = 0
    water_bodies: int = 0
    dropped_water_bodies: int = 0
    water_fraction: float = 0.0
    slopes: dict = None
    attribution: str = DEFAULT_ATTRIBUTION

    @property
    def min_elevation_m(self):
        return float(np.min(self.elevation_m))

    @property
    def max_elevation_m(self):
        return float(np.max(self.elevation_m))

    def summary(self):
        geo = self.georeference
        lines = [
            "Centre: %.5f, %.5f" % (geo.center_lat, geo.center_lon),
            "Footprint: %.1f x %.1f km at %.1f m/cell"
            % (geo.width_m / 1000.0, geo.height_m / 1000.0, geo.metres_per_cell),
            "Elevation: %.0f m to %.0f m (real world)"
            % (self.min_elevation_m, self.max_elevation_m),
            "Vertical scale: %.2fx, shoreline at %.0f m real"
            % (geo.vertical_scale, geo.sea_reference_m),
            "In game: %.0f m to %.0f m, %.0f%% under water"
            % (float(self.height_dm.min()) / 10.0,
               float(self.height_dm.max()) / 10.0,
               self.water_fraction * 100),
            "Source zoom %d, %d tiles" % (self.zoom, self.tiles_fetched),
        ]
        if self.slopes:
            lines.append(
                "Slopes in game: %.0f%% median, %.0f%% at the 95th percentile, "
                "%.0f%% of edges steeper than %.0f%%"
                % (self.slopes["median_grade"] * 100,
                   self.slopes["p95_grade"] * 100,
                   self.slopes["steep_fraction"] * 100,
                   STEEP_GRADE * 100))
        if self.water_cells or self.lifted_cells or self.dropped_water_bodies:
            lines.append(
                "Mapped water: %d body(s) used, %d skipped as too small or "
                "too far above the waterline; %d vertex(es) flooded, %d "
                "lifted clear" % (self.water_bodies, self.dropped_water_bodies,
                                  self.water_cells, self.lifted_cells))
        if self.despiked_vertices:
            lines.append("%d source artifact(s) smoothed away"
                         % self.despiked_vertices)
        if self.tiles_missing:
            lines.append("%d tile(s) had no data and were treated as sea"
                         % self.tiles_missing)
        if self.clamped_vertices:
            lines.append("%d vertex height(s) clamped to the representable range"
                         % self.clamped_vertices)
        return "\n".join(lines)


# --- The pipeline ---------------------------------------------------------


def grid_lonlat(request):
    """Longitude/latitude of every terrain vertex in the region.

    Returns two ``(rows, cols)`` arrays.  Row 0 is the north edge.
    """
    rows, cols = request.grid_shape
    spacing = request.metres_per_cell

    # Vertex offsets from the region centre, in metres.
    east = (np.arange(cols, dtype=np.float64) - (cols - 1) / 2.0) * spacing
    north = ((rows - 1) / 2.0 - np.arange(rows, dtype=np.float64)) * spacing
    east_grid, north_grid = np.meshgrid(east, north)

    if request.rotation_deg:
        theta = math.radians(request.rotation_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        east_rot = east_grid * cos_t + north_grid * sin_t
        north_rot = -east_grid * sin_t + north_grid * cos_t
        east_grid, north_grid = east_rot, north_rot

    return local_offsets_to_lonlat(request.center_lat, request.center_lon,
                                   east_grid, north_grid)


def _sample_tiles(request, fetcher, decode, channels, label, progress=None,
                  fill=0.0):
    """Sample a slippy-map tile source over the region's vertex grid.

    ``decode`` turns PNG/JPEG bytes into an array; ``channels`` is ``None``
    for a 2D source (elevation) or 3 for RGB imagery.  Elevation and the
    basemap underlay go through here so they land on exactly the same grid
    -- a pixel of the underlay is the same patch of ground as the terrain
    vertex beneath it.

    Returns ``(sampled, zoom, tiles_fetched, tiles_missing)``.
    """
    request.validate()
    lon, lat = grid_lonlat(request)

    zoom = request.zoom
    if zoom is None:
        zoom = zoom_for_resolution(request.metres_per_cell, request.center_lat,
                                   max_zoom=request.max_zoom)

    tx, ty = lonlat_to_tile(lon, lat, zoom)
    tile_x0 = int(math.floor(float(np.min(tx))))
    tile_x1 = int(math.floor(float(np.max(tx))))
    tile_y0 = int(math.floor(float(np.min(ty))))
    tile_y1 = int(math.floor(float(np.max(ty))))

    n_x = tile_x1 - tile_x0 + 1
    n_y = tile_y1 - tile_y0 + 1
    total = n_x * n_y
    if total > MAX_MOSAIC_TILES:
        raise GeoImportError(
            "This area needs %d %s tiles (limit %d). Increase metres per "
            "cell, shrink the region, or lower the maximum zoom."
            % (total, label, MAX_MOSAIC_TILES))

    shape = (n_y * TILE_PIXELS, n_x * TILE_PIXELS)
    if channels:
        shape = shape + (channels,)
    mosaic = np.full(shape, fill, dtype=np.float32)
    fetched = 0
    missing = 0
    span = 2 ** zoom

    for iy in range(n_y):
        for ix in range(n_x):
            done = iy * n_x + ix
            if progress is not None:
                progress(done, total, "Downloading %s tile %d of %d"
                         % (label, done + 1, total))
            # Wrap in x so a region straddling the antimeridian still works;
            # y has no wrap, tiles above the pole simply do not exist.
            wrapped_x = (tile_x0 + ix) % span
            tile_y = tile_y0 + iy
            data = None
            if 0 <= tile_y < span:
                data = fetcher.fetch(zoom, wrapped_x, tile_y)
            if data is None:
                missing += 1
                continue
            fetched += 1
            patch = decode(data)
            if patch.shape[:2] != (TILE_PIXELS, TILE_PIXELS):
                raise GeoImportError(
                    "%s tile %d/%d/%d is %dx%d, expected %dx%d"
                    % (label.capitalize(), zoom, wrapped_x, tile_y,
                       patch.shape[1], patch.shape[0], TILE_PIXELS, TILE_PIXELS))
            mosaic[iy * TILE_PIXELS:(iy + 1) * TILE_PIXELS,
                   ix * TILE_PIXELS:(ix + 1) * TILE_PIXELS] = patch

    if fetched == 0:
        raise GeoImportError(
            "No %s data was available for this area." % label)

    px = (tx - tile_x0) * TILE_PIXELS
    py = (ty - tile_y0) * TILE_PIXELS
    sampled = _bilinear(mosaic, px, py)

    if progress is not None:
        progress(total, total, "Building terrain")
    return sampled, zoom, fetched, missing


def sample_elevation(request, fetcher, progress=None):
    """Sample the elevation source over the region's vertex grid.

    Returns ``(elevation_m, zoom, tiles_fetched, tiles_missing)``.
    """
    try:
        sampled, zoom, fetched, missing = _sample_tiles(
            request, fetcher, _decode_tile, None, "elevation", progress)
    except GeoImportError as exc:
        if "No elevation data" in str(exc):
            raise GeoImportError(
                "No elevation data was available for this area. If you are "
                "offline, import from a bitmap instead.") from exc
        raise
    return sampled.astype(np.float32), zoom, fetched, missing


def sample_basemap(request, fetcher, progress=None):
    """Sample a raster map source onto the region grid.

    The result is a ``uint8`` RGB array the same width and height as the
    region's terrain grid, so it can be drawn straight under the region
    overview: every pixel lines up with the terrain vertex it sits beneath,
    and therefore with the city tile boundaries drawn on top.

    Returns ``(rgb, zoom, tiles_fetched, tiles_missing)``.
    """
    sampled, zoom, fetched, missing = _sample_tiles(
        request, fetcher, _decode_rgb_tile, 3, "map", progress, fill=255.0)
    rgb = np.clip(np.rint(sampled), 0, 255).astype(np.uint8)
    return rgb, zoom, fetched, missing


def despike_elevation(elevation_m, threshold_m=200.0):
    """Replace isolated bad samples with the local median.

    Global DEM mosaics carry occasional junk pixels -- the tile covering the
    sea south-east of Hong Kong, for instance, holds a handful of 5000-6000 m
    readings. They are rare enough to ignore for most purposes, but a single
    one puts a needle through the terrain and, worse, drags any automatic
    vertical scaling along with it.

    Real ground almost never jumps by hundreds of metres between neighbouring
    samples, so anything that far from its 3x3 median is treated as an
    artifact. Cliffs and ridge lines sit well inside the threshold and are
    left alone.

    Returns ``(cleaned, count_replaced)``.
    """
    elevation = np.asarray(elevation_m, dtype=np.float32)
    if threshold_m <= 0 or elevation.ndim != 2:
        return elevation, 0
    if elevation.shape[0] < 3 or elevation.shape[1] < 3:
        return elevation, 0

    padded = np.pad(elevation, 1, mode="edge")
    neighbourhood = np.stack(
        [padded[dy:dy + elevation.shape[0], dx:dx + elevation.shape[1]]
         for dy in range(3) for dx in range(3)],
        axis=0)
    median = np.median(neighbourhood, axis=0)

    spikes = np.abs(elevation - median) > threshold_m
    count = int(np.count_nonzero(spikes))
    if count:
        elevation = np.where(spikes, median, elevation).astype(np.float32)
    return elevation, count


def elevation_to_height_dm(elevation_m, sea_level_m=SEA_LEVEL_M,
                           vertical_scale=1.0, sea_reference_m=0.0,
                           ocean_depth_m=20.0, keep_bathymetry=False):
    """Map real elevations onto SC4's height scale.

    Returns ``(height_dm, clamped_count)``.  Heights are ``uint16``
    decimetres with sea level at ``sea_level_m``.

    Real bathymetry plunges to -4000 m and, once scaled, would bottom out
    against zero as a vast pit, so by default anything below the shoreline
    is flattened to a shallow shelf.  ``keep_bathymetry`` keeps the real
    sea floor for people who want it.
    """
    elevation_m = np.asarray(elevation_m, dtype=np.float64)
    heights = sea_level_m + (elevation_m - sea_reference_m) * vertical_scale

    if not keep_bathymetry:
        shelf = sea_level_m - ocean_depth_m
        heights = np.where(elevation_m < sea_reference_m, shelf, heights)

    decimetres = np.rint(heights * 10.0)
    clamped = int(np.count_nonzero((decimetres < 0) | (decimetres > 65535)))
    decimetres = np.clip(decimetres, 0, 65535)
    return decimetres.astype(np.uint16), clamped


def suggested_vertical_scale(max_elevation_m, sea_reference_m=0.0,
                             headroom_m=MAX_HEIGHT_M - SEA_LEVEL_M - 100.0):
    """Largest vertical scale that keeps the terrain inside the height range."""
    relief = max_elevation_m - sea_reference_m
    if relief <= 0:
        return 1.0
    return min(1.0, headroom_m / relief)


def isotropic_vertical_scale(metres_per_cell):
    """Vertical scale that keeps the terrain in proportion with the ground.

    A SimCity 4 cell is always 16 m wide in game units, whatever slice of
    the real world it stands for.  Importing at 48 m per cell squeezes the
    ground to a third of its size horizontally while leaving elevations
    untouched, which makes every slope three times as steep as it really
    is.  Scaling heights by ``16 / metres_per_cell`` cancels that out, so
    hills keep the profile they have in life.
    """
    if metres_per_cell <= 0:
        raise GeoImportError("Metres per cell must be positive")
    return CELL_SIZE_M / float(metres_per_cell)


#: Grade above which SimCity 4 networks start to struggle. Approximate --
#: the game's real limits vary by network type -- but a useful warning line.
STEEP_GRADE = 0.15


def slope_statistics(height_dm, steep_grade=STEEP_GRADE):
    """Describe the steepness of a finished region height grid.

    Grades are measured the way the game sees them: rise in SC4 metres over
    the fixed 16 m cell.  That makes the numbers directly comparable to what
    roads and rail can climb, whatever real-world scale was imported.
    """
    heights = np.asarray(height_dm, dtype=np.float64) / 10.0
    runs = []
    if heights.shape[1] > 1:
        runs.append(np.abs(np.diff(heights, axis=1)).ravel())
    if heights.shape[0] > 1:
        runs.append(np.abs(np.diff(heights, axis=0)).ravel())
    if not runs:
        return {"max_grade": 0.0, "median_grade": 0.0, "p95_grade": 0.0,
                "steep_fraction": 0.0}
    rise = np.concatenate(runs)
    grade = rise / CELL_SIZE_M
    return {
        "max_grade": float(grade.max()),
        "median_grade": float(np.median(grade)),
        "p95_grade": float(np.percentile(grade, 95)),
        "steep_fraction": float(np.count_nonzero(grade > steep_grade) / grade.size),
    }


def fetch_water_mask(request, client, progress=None):
    """Fetch water areas for a region and burn them onto its grid."""
    if progress is not None:
        progress(0, 1, "Looking up water from OpenStreetMap")
    query = build_water_query(region_bbox(request))
    data = client.fetch(query)
    outers, inners = parse_overpass_water(data)
    if progress is not None:
        progress(1, 1, "Tracing %d water outline(s)" % len(outers))
    return rasterize_water(request, outers, inners)


def build_region_grid(request, fetcher, progress=None, water_mask=None):
    """Fetch, project and convert -- the whole import in one call.

    ``water_mask`` is an optional boolean grid from
    :func:`fetch_water_mask`, applied according to ``request.water_source``.
    """
    elevation, zoom, fetched, missing = sample_elevation(request, fetcher, progress)
    elevation, despiked = despike_elevation(elevation, request.despike_threshold_m)
    vertical_scale = request.effective_vertical_scale()
    sea_reference = request.effective_water_datum(elevation)
    height_dm, clamped = elevation_to_height_dm(
        elevation,
        sea_level_m=request.sea_level_m,
        vertical_scale=vertical_scale,
        sea_reference_m=sea_reference,
        ocean_depth_m=request.ocean_depth_m,
        keep_bathymetry=request.keep_bathymetry,
    )
    water_cells = 0
    lifted_cells = 0
    water_bodies = 0
    dropped_water_bodies = 0
    if water_mask is not None and request.water_source != "elevation":
        if request.water_source == "both":
            # Keep what the datum already flooded -- the sea, usually --
            # and add the mapped water on top of it.
            effective = np.asarray(water_mask, dtype=bool) | (
                height_dm < request.sea_level_m * 10)
        else:
            # The mask is the whole truth: anything unmapped becomes land,
            # however low it sits.
            effective = np.asarray(water_mask, dtype=bool)
        effective, kept, small, high = filter_water_bodies(
            effective, height_dm, sea_level_m=request.sea_level_m,
            min_area_cells=request.min_water_area_cells,
            max_rise_m=request.max_water_rise_m)
        dropped_water_bodies = small + high
        water_bodies = kept
        height_dm, water_cells, lifted_cells = apply_water_mask(
            height_dm, effective, sea_level_m=request.sea_level_m,
            water_depth_m=request.water_depth_m)

    georeference = GeoReference(
        center_lat=request.center_lat,
        center_lon=request.center_lon,
        tiles_x=request.tiles_x,
        tiles_y=request.tiles_y,
        metres_per_cell=request.metres_per_cell,
        rotation_deg=request.rotation_deg,
        sea_level_m=request.sea_level_m,
        vertical_scale=vertical_scale,
        sea_reference_m=sea_reference,
        zoom=zoom,
    )
    return GeoImportResult(
        height_dm=height_dm,
        elevation_m=elevation,
        georeference=georeference,
        zoom=zoom,
        tiles_fetched=fetched,
        tiles_missing=missing,
        clamped_vertices=clamped,
        despiked_vertices=despiked,
        water_cells=water_cells,
        lifted_cells=lifted_cells,
        water_bodies=water_bodies,
        dropped_water_bodies=dropped_water_bodies,
        slopes=slope_statistics(height_dm),
        water_fraction=float(np.count_nonzero(height_dm < request.sea_level_m * 10)
                             / height_dm.size),
    )


# --- Water mask -----------------------------------------------------------

#: Overpass mirrors are volunteer-run and rate limited. One query per import,
#: cached to disk afterwards, is well within what they ask for.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: Tags that describe a body of water as an *area*.
#:
#: Coastline is deliberately absent. It is tagged as open ways with land on
#: the left rather than closed polygons, and turning that into a sea polygon
#: means assembling and clipping it against the region -- a job in itself.
#: The sea is also the one case elevation already handles well, so the mask
#: covers what elevation cannot: inland water, and water sitting on ground
#: below sea level.
WATER_AREA_TAGS = (
    ("natural", "water"),
    ("waterway", "riverbank"),
    ("landuse", "reservoir"),
    ("landuse", "basin"),
)


def build_water_query(bbox, timeout=90):
    """Overpass QL selecting water areas within ``bbox``.

    ``bbox`` is ``(south, west, north, east)``, the order Overpass uses.
    """
    south, west, north, east = bbox
    box = "%.6f,%.6f,%.6f,%.6f" % (south, west, north, east)
    clauses = []
    for key, value in WATER_AREA_TAGS:
        clauses.append('way["%s"="%s"](%s);' % (key, value, box))
        clauses.append('relation["%s"="%s"](%s);' % (key, value, box))
    return ("[out:json][timeout:%d];\n(\n  %s\n);\nout geom;"
            % (int(timeout), "\n  ".join(clauses)))


def region_bbox(request, margin_cells=2):
    """Bounding box of a region as ``(south, west, north, east)``.

    A small margin keeps water that laps over the edge from being clipped
    into a straight line at the boundary.
    """
    lon, lat = grid_lonlat(request)
    margin_deg_lat = (margin_cells * request.metres_per_cell
                      / metres_per_degree_lat(request.center_lat))
    margin_deg_lon = (margin_cells * request.metres_per_cell
                      / float(metres_per_degree_lon(request.center_lat)))
    return (float(lat.min()) - margin_deg_lat,
            float(lon.min()) - margin_deg_lon,
            float(lat.max()) + margin_deg_lat,
            float(lon.max()) + margin_deg_lon)


class OverpassClient:
    """Fetch water areas from Overpass, cached on disk.

    Responses are cached under a hash of the query, so re-importing the same
    area costs nothing and works offline. ``opener`` exists so tests (and
    anyone wanting a different transport) can substitute the network call.
    """

    def __init__(self, url=OVERPASS_URL, cache_dir=None, timeout=180,
                 user_agent=None, opener=None):
        self.url = url
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.user_agent = user_agent or (
            "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)")
        self.opener = opener

    def _cache_path(self, query):
        if not self.cache_dir:
            return None
        import hashlib
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.cache_dir, digest + ".json")

    def fetch(self, query):
        """Return the decoded Overpass response for a query."""
        import json

        path = self._cache_path(query)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    return json.load(fh)
                except ValueError:
                    pass  # a truncated cache entry; refetch below

        if self.opener is not None:
            payload = self.opener(self.url, query)
        else:
            request = urllib.request.Request(
                self.url,
                data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
                headers={"User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 504):
                    raise GeoImportError(
                        "The OpenStreetMap query service is busy (HTTP %d). "
                        "Wait a minute and try again." % exc.code) from exc
                raise GeoImportError(
                    "OpenStreetMap query failed with HTTP %d" % exc.code) from exc
            except urllib.error.URLError as exc:
                raise GeoImportError(
                    "Could not reach the OpenStreetMap query service (%s)."
                    % getattr(exc, "reason", exc)) from exc

        try:
            data = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        except ValueError as exc:
            raise GeoImportError(
                "The OpenStreetMap query service returned an unreadable reply"
            ) from exc

        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        return data


def parse_overpass_water(data):
    """Pull water rings out of an Overpass ``out geom`` response.

    Returns ``(outers, inners)``: two lists of rings, each ring a list of
    ``(lon, lat)``. Inner rings are the holes in multipolygons -- islands --
    and are punched back out after the outers are filled.
    """
    outers = []
    inners = []
    if not isinstance(data, dict):
        return outers, inners

    for element in data.get("elements", []) or []:
        if not isinstance(element, dict):
            continue
        kind = element.get("type")
        if kind == "way":
            ring = [(float(p["lon"]), float(p["lat"]))
                    for p in element.get("geometry", []) or []
                    if isinstance(p, dict) and "lon" in p and "lat" in p]
            if len(ring) >= 3:
                outers.append(ring)
        elif kind == "relation":
            for member in element.get("members", []) or []:
                if not isinstance(member, dict):
                    continue
                ring = [(float(p["lon"]), float(p["lat"]))
                        for p in member.get("geometry", []) or []
                        if isinstance(p, dict) and "lon" in p and "lat" in p]
                if len(ring) < 3:
                    continue
                if member.get("role") == "inner":
                    inners.append(ring)
                else:
                    outers.append(ring)
    return outers, inners


def rasterize_water(request, outers, inners=(), fetcher_shape=None):
    """Burn water rings onto the region grid.

    Returns a boolean array shaped like the height grid, True where water
    covers the ground. Rings are projected through the same local frame the
    terrain was sampled on, so the mask lands exactly on the right cells.
    """
    from PIL import ImageDraw

    rows, cols = fetcher_shape or request.grid_shape
    canvas = Image.new("1", (cols, rows), 0)

    def to_pixels(ring):
        lons = np.array([p[0] for p in ring], dtype=np.float64)
        lats = np.array([p[1] for p in ring], dtype=np.float64)
        east, north = lonlat_to_local_offsets(
            request.center_lat, request.center_lon, lons, lats)
        if request.rotation_deg:
            theta = math.radians(request.rotation_deg)
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            east, north = (east * cos_t - north * sin_t,
                           east * sin_t + north * cos_t)
        column = east / request.metres_per_cell + (cols - 1) / 2.0
        row = (rows - 1) / 2.0 - north / request.metres_per_cell
        return list(zip(column.tolist(), row.tolist()))

    draw = ImageDraw.Draw(canvas)
    for ring in outers:
        points = to_pixels(ring)
        if len(points) >= 3:
            draw.polygon(points, fill=1, outline=1)
    for ring in inners:
        points = to_pixels(ring)
        if len(points) >= 3:
            draw.polygon(points, fill=0, outline=0)

    return np.array(canvas, dtype=bool)


def label_water_bodies(mask):
    """Label connected runs of water. Returns ``(labels, count)``.

    Four-connected, iterative, and numpy-only -- SC4Mapper deliberately has
    no SciPy dependency. Only masked cells are visited, so the cost tracks
    the amount of water rather than the size of the region.
    """
    from collections import deque

    mask = np.asarray(mask, dtype=bool)
    labels = np.zeros(mask.shape, dtype=np.int32)
    rows, cols = mask.shape
    count = 0

    for start_r in range(rows):
        row_mask = mask[start_r]
        if not row_mask.any():
            continue
        for start_c in np.flatnonzero(row_mask):
            if labels[start_r, start_c]:
                continue
            count += 1
            queue = deque([(start_r, int(start_c))])
            labels[start_r, start_c] = count
            while queue:
                r, c = queue.popleft()
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if mask[nr, nc] and not labels[nr, nc]:
                            labels[nr, nc] = count
                            queue.append((nr, nc))
    return labels, count


def filter_water_bodies(mask, height_dm, sea_level_m=SEA_LEVEL_M,
                        min_area_cells=64, max_rise_m=30.0):
    """Drop water bodies that would wreck the terrain if flooded.

    SimCity 4 has exactly one water level, so a mapped body only makes
    sense as water if it already sits near it. Carving one down from higher
    up gouges a canyon: an alpine stream several hundred metres above the
    shoreline would be cut straight through the mountain it runs down.
    Bodies whose surface sits more than ``max_rise_m`` above the waterline
    are therefore left as terrain -- they are simply not representable.

    ``min_area_cells`` drops the specks: creeks a cell or two wide, and
    ponds too small to read as water once they are on the map.

    Returns ``(mask, kept, dropped_small, dropped_high)``.
    """
    mask = np.asarray(mask, dtype=bool)
    heights = np.asarray(height_dm, dtype=np.float64)
    if heights.shape != mask.shape:
        raise GeoImportError(
            "Water mask is %s but the region is %s" % (mask.shape, heights.shape))
    if not mask.any():
        return mask, 0, 0, 0

    # Either filter can be switched off by passing None (or 0 for the area).
    ceiling = None
    if max_rise_m is not None:
        ceiling = sea_level_m * 10.0 + float(max_rise_m) * 10.0
    minimum_area = int(min_area_cells or 0)

    labels, count = label_water_bodies(mask)
    keep = np.zeros(mask.shape, dtype=bool)
    kept = dropped_small = dropped_high = 0

    for label in range(1, count + 1):
        body = labels == label
        area = int(np.count_nonzero(body))
        if area < minimum_area:
            dropped_small += 1
            continue
        # The median is robust to a few stray cells clipped off a bank.
        if ceiling is not None and np.median(heights[body]) > ceiling:
            dropped_high += 1
            continue
        keep |= body
        kept += 1

    return keep, kept, dropped_small, dropped_high


def apply_water_mask(height_dm, mask, sea_level_m=SEA_LEVEL_M,
                     water_depth_m=3.0, land_margin_m=0.5, lift_land=True):
    """Force water where the mask says water, and dry land where it does not.

    Both halves matter. Pushing masked ground under the waterline is the
    obvious one; lifting everything else *above* it is what finally makes a
    polder work -- ground that really does sit below sea level but is dry.
    Together they cut the wet/dry question loose from elevation, which a
    shoreline datum alone can never do.

    Returns ``(height_dm, wet_count, lifted_count)``.
    """
    heights = np.array(height_dm, dtype=np.int64)
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != heights.shape:
        raise GeoImportError(
            "Water mask is %s but the region is %s"
            % (mask.shape, heights.shape))

    waterline = int(round(sea_level_m * 10))
    bed = waterline - int(round(water_depth_m * 10))

    wet_before = heights >= waterline
    heights = np.where(mask, np.minimum(heights, bed), heights)
    wet_count = int(np.count_nonzero(mask & wet_before))

    lifted_count = 0
    if lift_land:
        shore = waterline + int(round(land_margin_m * 10))
        needs_lift = (~mask) & (heights < waterline)
        lifted_count = int(np.count_nonzero(needs_lift))
        heights = np.where(needs_lift, np.maximum(heights, shore), heights)

    heights = np.clip(heights, 0, 65535)
    return heights.astype(np.uint16), wet_count, lifted_count


# --- Georeference sidecar -------------------------------------------------

#: TGI of the georeference record written into each city save.
#:
#: SimCity 4 saves are DBPF archives, and an entry the game does not
#: recognise rides along beside the ones it does. Stashing the import's
#: georeference there means it travels with the city: share the save and
#: whoever opens it -- SC4Mapper, or a DLL plugin inside the game -- can
#: work out which patch of the real world each cell stands for, with no
#: sidecar file to lose.
GEOREF_TYPE = 0x9A6D5C21
GEOREF_GROUP = 0x53434752  # 'SCGR'
GEOREF_INSTANCE = 0x00000001
GEOREF_TGI = (GEOREF_TYPE, GEOREF_GROUP, GEOREF_INSTANCE)

#: Bumped when the meaning of a field changes, so a reader can refuse a
#: record it would misinterpret.
GEOREF_VERSION = 1


def build_georef_record(georeference, offset_x=0, offset_z=0, tile_size=1,
                        region_name=None, import_id=None,
                        ocean_depth_m=20.0, keep_bathymetry=False):
    """Describe where one city tile sits in the real world.

    ``offset_x`` / ``offset_z`` are the tile's origin in **cells**, measured
    from the north-west corner of the imported grid, so a reader can place
    any cell without knowing how the region was cropped afterwards.

    The payload is UTF-8 JSON. It is a few hundred bytes, and being text it
    can be versioned, read from any language without tooling, and eyeballed
    by anyone who opens a save in a hex editor -- which matters more for a
    community format than the bytes a binary encoding would save.
    """
    import json

    record = {
        "format": "sc4mapper.georef",
        "version": GEOREF_VERSION,
        "frame": {
            # The local metric frame the whole region was sampled on.
            "center_lat": georeference.center_lat,
            "center_lon": georeference.center_lon,
            "grid_width": georeference.tiles_x * CELLS_PER_TILE + 1,
            "grid_height": georeference.tiles_y * CELLS_PER_TILE + 1,
            "metres_per_cell": georeference.metres_per_cell,
            "rotation_deg": georeference.rotation_deg,
        },
        "tile": {
            # Where this city sits inside that frame.
            "offset_x": int(offset_x),
            "offset_z": int(offset_z),
            "size": int(tile_size),
            "cells": int(tile_size) * CELLS_PER_TILE,
        },
        "projection": {
            # Everything a reader needs to reproduce the mapping exactly.
            "model": PROJECTION_MODEL,
            "crs": PROJECTION_CRS,
            "metres_per_degree_lat_coeffs": list(METRES_PER_DEGREE_LAT_COEFFS),
            "metres_per_degree_lon_coeffs": list(METRES_PER_DEGREE_LON_COEFFS),
        },
        "heights": {
            "sea_level_m": georeference.sea_level_m,
            "vertical_scale": georeference.vertical_scale,
            "sea_reference_m": georeference.sea_reference_m,
            # Ground below the shoreline was flattened to a shelf unless
            # bathymetry was kept, so the height mapping is not invertible
            # there. Recorded so a reader knows which cells to distrust.
            "ocean_depth_m": ocean_depth_m,
            "keep_bathymetry": bool(keep_bathymetry),
        },
        "source": {
            "elevation": georeference.source,
            "zoom": georeference.zoom,
        },
    }
    if region_name:
        record["region"] = str(region_name)
    if import_id:
        record["import_id"] = str(import_id)
    return json.dumps(record, indent=1, sort_keys=True).encode("utf-8")


def parse_georef_record(payload):
    """Read a georeference record back. Returns ``None`` if it is not one."""
    import json

    if not payload:
        return None
    try:
        record = json.loads(bytes(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format") != "sc4mapper.georef":
        return None
    try:
        if int(record.get("version", 0)) > GEOREF_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return record


def georef_cell_to_lonlat(record, cell_x, cell_z):
    """Longitude/latitude of a cell within the tile a record describes.

    ``cell_x`` / ``cell_z`` are local to the city tile, so (0, 0) is its
    north-west corner. This inverts the sampling grid, and exists so the
    format has a reference implementation that other readers -- a DLL
    plugin, say -- can be checked against.
    """
    frame = record["frame"]
    tile = record["tile"]
    spacing = float(frame["metres_per_cell"])

    column = float(tile["offset_x"]) + np.asarray(cell_x, dtype=np.float64)
    row = float(tile["offset_z"]) + np.asarray(cell_z, dtype=np.float64)

    east = (column - (float(frame["grid_width"]) - 1) / 2.0) * spacing
    north = ((float(frame["grid_height"]) - 1) / 2.0 - row) * spacing

    rotation = float(frame.get("rotation_deg", 0.0))
    if rotation:
        theta = math.radians(rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        east, north = (east * cos_t + north * sin_t,
                       -east * sin_t + north * cos_t)

    return local_offsets_to_lonlat(frame["center_lat"], frame["center_lon"],
                                   east, north)


def georef_lonlat_to_cell(record, lon, lat):
    """Where a real-world point falls in the tile a record describes.

    Returns fractional ``(cell_x, cell_z)`` local to the city tile, so
    (0, 0) is its north-west corner and negative or over-size values mean
    the point lies outside this city. This is the direction a plugin
    actually needs -- given an OpenStreetMap node, where does it go? -- and
    it is the exact inverse of :func:`georef_cell_to_lonlat`.
    """
    frame = record["frame"]
    tile = record["tile"]
    spacing = float(frame["metres_per_cell"])

    east, north = lonlat_to_local_offsets(
        frame["center_lat"], frame["center_lon"], lon, lat)

    rotation = float(frame.get("rotation_deg", 0.0))
    if rotation:
        # Undo the forward rotation.
        theta = math.radians(rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        east, north = (east * cos_t - north * sin_t,
                       east * sin_t + north * cos_t)

    column = east / spacing + (float(frame["grid_width"]) - 1) / 2.0
    row = (float(frame["grid_height"]) - 1) / 2.0 - north / spacing
    return column - float(tile["offset_x"]), row - float(tile["offset_z"])


# --- City tile layout -----------------------------------------------------

# config.bmp encodes city size as a colour: one pixel is one small tile.
CONFIG_COLORS = {1: (255, 0, 0), 2: (0, 255, 0), 4: (0, 0, 255)}
#: Anything the game does not recognise as a city colour is a hole.
CONFIG_VOID = (0, 0, 0)
CITY_SIZES = (1, 2, 4)
CITY_SIZE_NAMES = {1: "Small", 2: "Medium", 4: "Large"}


def build_config_image(size, preferred=4):
    """Lay a region footprint out into city tiles.

    ``size`` is ``(width, height)`` in small tiles -- that is, config.bmp
    pixels.  ``preferred`` is the largest city size to place (1, 2 or 4);
    whatever will not fit is filled with progressively smaller cities, so
    the whole footprint is covered.

    This only produces a starting layout.  The user reshapes it afterwards
    in the region editor, where they can paint individual small, medium and
    large tiles or punch holes.
    """
    width, height = int(size[0]), int(size[1])
    if width < 1 or height < 1:
        raise GeoImportError("A region needs at least one tile on each side")
    if preferred not in CITY_SIZES:
        raise GeoImportError("City size must be 1, 2 or 4 small tiles")

    image = Image.new("RGB", (width, height), CONFIG_VOID)
    taken = np.zeros((height, width), dtype=bool)

    for city in (4, 2, 1):
        if city > preferred:
            continue
        for y in range(height - city + 1):
            for x in range(width - city + 1):
                if taken[y:y + city, x:x + city].any():
                    continue
                taken[y:y + city, x:x + city] = True
                image.paste(CONFIG_COLORS[city], (x, y, x + city, y + city))
    return image


def describe_layout(size, preferred=4):
    """Count the cities :func:`build_config_image` would place."""
    width, height = int(size[0]), int(size[1])
    image = build_config_image((width, height), preferred)
    pixels = np.asarray(image)
    counts = {}
    for city_size, colour in CONFIG_COLORS.items():
        matching = np.all(pixels == np.array(colour, dtype=np.uint8), axis=-1)
        counts[city_size] = int(matching.sum()) // (city_size * city_size)
    return counts


# --- Locating -------------------------------------------------------------

_DECIMAL_PAIR = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[,;/\s]\s*(-?\d+(?:\.\d+)?)\s*$")

_SIGNED_PAIR = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)")

_OSM_HASH = re.compile(r"#map=\d+(?:\.\d+)?/(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)")

_GOOGLE_AT = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")

_GEO_URI = re.compile(r"^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")

_DMS = re.compile(
    r"(\d+(?:\.\d+)?)\s*[^\d\w]?\s*"          # degrees
    r"(?:(\d+(?:\.\d+)?)\s*['′´]?\s*)?"  # minutes
    r"(?:(\d+(?:\.\d+)?)\s*[\"″]?\s*)?"       # seconds
    r"([NSEW])", re.IGNORECASE)


def _dms_to_decimal(degrees, minutes, seconds, hemisphere):
    value = float(degrees)
    if minutes:
        value += float(minutes) / 60.0
    if seconds:
        value += float(seconds) / 3600.0
    if hemisphere.upper() in ("S", "W"):
        value = -value
    return value


def parse_location(text):
    """Parse a latitude/longitude out of whatever the user pasted.

    Understands decimal pairs, degrees/minutes/seconds, ``geo:`` URIs, and
    OpenStreetMap and Google Maps URLs.  Returns ``(lat, lon)`` or ``None``.
    """
    if not text:
        return None
    text = text.strip()

    for pattern in (_OSM_HASH, _GOOGLE_AT, _GEO_URI):
        match = pattern.search(text)
        if match:
            return _validated(float(match.group(1)), float(match.group(2)))

    # Query strings such as ?mlat=52.37&mlon=4.90 or ?q=52.37,4.90
    parsed = urllib.parse.urlparse(text)
    if parsed.query:
        params = urllib.parse.parse_qs(parsed.query)
        if "mlat" in params and "mlon" in params:
            try:
                return _validated(float(params["mlat"][0]), float(params["mlon"][0]))
            except ValueError:
                pass
        for key in ("q", "query", "ll", "center"):
            if key in params:
                match = _SIGNED_PAIR.search(params[key][0])
                if match:
                    return _validated(float(match.group(1)), float(match.group(2)))

    dms = _DMS.findall(text)
    if len(dms) >= 2:
        values = {}
        for degrees, minutes, seconds, hemisphere in dms[:2]:
            decimal = _dms_to_decimal(degrees, minutes, seconds, hemisphere)
            axis = "lat" if hemisphere.upper() in ("N", "S") else "lon"
            values.setdefault(axis, decimal)
        if "lat" in values and "lon" in values:
            return _validated(values["lat"], values["lon"])

    match = _DECIMAL_PAIR.match(text)
    if match:
        return _validated(float(match.group(1)), float(match.group(2)))

    return None


def _validated(lat, lon):
    """Return the pair only if it is a plausible coordinate."""
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return (lat, lon)
    return None


@dataclass
class Place:
    """One geocoding result."""

    name: str
    lat: float
    lon: float


#: Nominatim's usage policy requires an identifying User-Agent and only
#: light, user-initiated traffic -- one search per button press, never
#: search-as-you-type.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def geocode(query, limit=8, timeout=20, user_agent=None, opener=None):
    """Look a place name up with Nominatim.

    ``opener`` lets tests substitute the network call; it is given the
    request URL and must return the raw JSON bytes.
    """
    import json

    query = (query or "").strip()
    if not query:
        return []

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": str(int(limit)),
    })
    url = "%s?%s" % (NOMINATIM_URL, params)
    agent = user_agent or "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)"

    if opener is not None:
        payload = opener(url)
    else:
        request = urllib.request.Request(url, headers={"User-Agent": agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.URLError as exc:
            raise GeoImportError("Could not reach the search service (%s)."
                                 % getattr(exc, "reason", exc)) from exc

    try:
        results = json.loads(payload)
    except ValueError as exc:
        raise GeoImportError("The search service returned an unreadable reply") from exc

    places = []
    for item in results:
        try:
            places.append(Place(name=item.get("display_name", "?"),
                                lat=float(item["lat"]),
                                lon=float(item["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return places
