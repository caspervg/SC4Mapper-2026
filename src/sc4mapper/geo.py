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

import contextlib
import gzip
import hashlib
import http.client
import math
import os
import re
import socket
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
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
#: Refuse a terrain grid larger than this many vertices, to fail with an
#: explanation rather than an out-of-memory crash. The pipeline needs some
#: tens of bytes per vertex while sampling and converting, so the ceiling is
#: about working memory, not about anything SC4 cares about. It is set to
#: clear every standard region shape, including a 16x16 grid of large city
#: tiles (4097 x 4097); such a region will usually run into
#: :data:`MAX_MOSAIC_TILES` first unless its maximum zoom is lowered.
MAX_REGION_VERTICES = 17_000_000


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
    # Keep nearby points nearby when the centre is on the antimeridian.
    delta_lon = (np.asarray(lon, dtype=np.float64) - center_lon + 180.0) % 360.0 - 180.0
    east = delta_lon * m_per_deg_lon
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


def _validate_mercator_coverage(request, extra_east_m=0.0, extra_north_m=0.0):
    """Reject a footprint whose sampled rows leave Web Mercator coverage."""
    width = request.tiles_x * CELLS_PER_TILE * request.metres_per_cell
    height = request.tiles_y * CELLS_PER_TILE * request.metres_per_cell
    half_e = width / 2.0 + extra_east_m
    half_n = height / 2.0 + extra_north_m
    corners_e = np.array([-half_e, half_e, half_e, -half_e])
    corners_n = np.array([half_n, half_n, -half_n, -half_n])
    theta = math.radians(request.rotation_deg)
    east = corners_e * math.cos(theta) + corners_n * math.sin(theta)
    north = -corners_e * math.sin(theta) + corners_n * math.cos(theta)
    _, lat = local_offsets_to_lonlat(
        request.center_lat, request.center_lon, east, north)
    if float(np.min(lat)) < -MERCATOR_MAX_LAT or float(np.max(lat)) > MERCATOR_MAX_LAT:
        raise GeoImportError(
            "This footprint extends beyond Web Mercator coverage (+/-85.05 "
            "degrees); use a lower-latitude centre or a smaller area.")


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


#: How many tile downloads one fetcher runs at once. Tile servers are
#: latency-bound rather than bandwidth-bound -- a single tile spends most of
#: its time waiting -- so the useful concurrency is well above the core
#: count. Past about eight the provider, not the client, becomes the limit.
DEFAULT_TILE_WORKERS = 8
#: Give up on a keep-alive connection after this many consecutive failures.
_CONNECT_ATTEMPTS = 2
#: Redirects to follow before declaring a tile URL broken.
_MAX_REDIRECTS = 4


class _ConnectionPool:
    """Keep-alive HTTP(S) connections, shared across fetcher threads.

    ``urllib.request.urlopen`` opens a fresh socket for every call, so each
    tile pays a TCP handshake and a TLS negotiation before a single byte of
    image arrives -- against the default elevation bucket that is roughly a
    third of the time spent on a tile. Reusing connections removes it.

    Connections are pooled per origin rather than per thread, so subdomain
    rotation and a worker count larger than the number of hosts both behave.
    """

    def __init__(self, timeout=30, max_idle_per_host=8):
        self.timeout = timeout
        self.max_idle_per_host = max_idle_per_host
        self._idle = defaultdict(list)
        self._lock = threading.Lock()

    def _connect(self, scheme, host, port):
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=self.timeout)
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def _checkout(self, origin):
        with self._lock:
            idle = self._idle[origin]
            if idle:
                return idle.pop()
        return self._connect(*origin)

    def _checkin(self, origin, connection):
        with self._lock:
            idle = self._idle[origin]
            if len(idle) >= self.max_idle_per_host:
                keep = False
            else:
                idle.append(connection)
                keep = True
        if not keep:
            _close(connection)

    def close(self):
        """Drop every pooled connection. Safe to call more than once."""
        with self._lock:
            connections = [c for idle in self._idle.values() for c in idle]
            self._idle.clear()
        for connection in connections:
            _close(connection)

    def request(self, url, headers):
        """GET ``url``, following redirects. Returns ``(status, body)``.

        The body is always read in full, which is what makes the connection
        reusable; a half-read response would poison it for the next tile.
        """
        for _ in range(_MAX_REDIRECTS + 1):
            status, body, location = self._request_once(url, headers)
            if status in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
            return status, body
        raise GeoImportError("Tile URL redirected more than %d times: %s"
                             % (_MAX_REDIRECTS, url))

    def _request_once(self, url, headers):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise GeoImportError("Tile URL must be http or https: %s" % url)
        origin = (parts.scheme, parts.hostname,
                  parts.port or (443 if parts.scheme == "https" else 80))
        target = urllib.parse.urlunsplit(("", "", parts.path or "/",
                                          parts.query, ""))

        last_error = None
        for attempt in range(_CONNECT_ATTEMPTS):
            connection = self._checkout(origin)
            try:
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
                body = response.read()
            except (http.client.HTTPException, OSError) as exc:
                _close(connection)
                last_error = exc
                # A pooled connection the server closed while idle fails on
                # use, not on checkout. One clean retry tells that apart
                # from a genuinely unreachable host.
                continue
            if response.will_close:
                _close(connection)
            else:
                self._checkin(origin, connection)
            return response.status, body, response.getheader("Location")
        raise last_error


def _close(connection):
    with contextlib.suppress(Exception):
        connection.close()


class HttpTileFetcher:
    """Fetch elevation tiles over HTTP, with an on-disk cache.

    Tiles are immutable, so the cache never needs invalidating; re-importing
    the same area is offline and instant.

    Downloads run on several threads at once and share a pool of keep-alive
    connections, so a mosaic costs one handshake per origin rather than one
    per tile. Instances are safe to use from multiple threads.
    """

    def __init__(self, url_template=DEFAULT_TILE_URL, cache_dir=None,
                 user_agent=None, timeout=30, attribution=None,
                 subdomains=None, layer="m", max_workers=DEFAULT_TILE_WORKERS):
        self.url_template = url_template
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.attribution = attribution
        self.max_workers = max(1, int(max_workers))
        self._pool = _ConnectionPool(timeout=timeout)
        # Proxied setups need urllib's proxy handling, which http.client does
        # not have. Falling back keeps them working, at the old speed.
        self._proxies = urllib.request.getproxies()
        if subdomains is None:
            if "{s}.google." in url_template:
                subdomains = ("mt0", "mt1", "mt2", "mt3")
            elif "mt{s}.google." in url_template:
                subdomains = ("0", "1", "2", "3")
            else:
                subdomains = ("a", "b", "c")
        self.subdomains = tuple(subdomains)
        self.layer = layer
        self.last_missing = None
        self.user_agent = user_agent or "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)"

    def _tile_url(self, zoom, x, y):
        subdomain = ""
        if self.subdomains:
            subdomain = self.subdomains[(x + y) % len(self.subdomains)]
        try:
            return self.url_template.format(
                z=zoom, x=x, y=y, s=subdomain, l=self.layer)
        except KeyError as exc:
            placeholder = str(exc.args[0])
            raise GeoImportError(
                "Tile URL uses unsupported placeholder {%s}; use only "
                "{z}, {x}, {y}, {s} and {l}." % placeholder) from exc

    def _cache_path(self, zoom, x, y):
        if not self.cache_dir:
            return None
        # Tile coordinates are meaningful only within one provider.  Keep a
        # stable namespace per URL template so switching providers cannot
        # silently reuse or mix tiles from the previous source.
        identity = "%s\n%s\n%s" % (
            self.url_template, ",".join(self.subdomains), self.layer)
        source = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return os.path.join(
            self.cache_dir, source, str(zoom), str(x), "%d.png" % y)

    def fetch(self, zoom, x, y):
        path = self._cache_path(zoom, x, y)
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()

        url = self._tile_url(zoom, x, y)
        headers = {"User-Agent": self.user_agent, "Accept": "image/*"}
        try:
            if self._proxy_for(url):
                status, data = self._fetch_via_urllib(url, headers)
            else:
                status, data = self._pool.request(url, headers)
        except (http.client.HTTPException, urllib.error.URLError,
                TimeoutError, socket.timeout, OSError) as exc:
            raise GeoImportError("Could not reach the elevation server (%s). "
                                 "Check your network connection."
                                 % getattr(exc, "reason", exc)) from exc

        if status in (403, 404):
            self.last_missing = (status, url)
            return None
        if status >= 400:
            raise GeoImportError("Elevation server returned HTTP %d for %s"
                                 % (status, url))

        self._store(path, data)
        return data

    def _proxy_for(self, url):
        """The proxy urllib would use for ``url``, if any."""
        if not self._proxies:
            return None
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme not in self._proxies:
            return None
        host = urllib.parse.urlsplit(url).hostname or ""
        if urllib.request.proxy_bypass(host):
            return None
        return self._proxies[scheme]

    def _fetch_via_urllib(self, url, headers):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                return exc.code, exc.read()

    def _store(self, path, data):
        """Write a tile into the cache, atomically enough for many threads."""
        if not path:
            return
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=".tile-", suffix=".part",
            delete=False)
        try:
            with tmp:
                tmp.write(data)
            os.replace(tmp.name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp.name)

    def close(self):
        """Release pooled sockets. The fetcher still works afterwards."""
        self._pool.close()


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


def _bilinear(source, px, py, band_rows=256):
    """Bilinearly sample ``source`` at continuous pixel coordinates.

    Handles both 2D (elevation) and 3D (RGB) sources.

    Pixel *centres* sit at ``i + 0.5``, so the half-pixel shift is applied
    here; skipping it biases every sample by half a source pixel, which at a
    coarse zoom is tens of metres on the ground.

    Interpolating a whole region grid in one go needs a couple of dozen
    float64 and int64 temporaries the size of the grid, which on a large
    region runs to gigabytes for a result of a few tens of megabytes. A 2D
    request is therefore worked in row bands, which changes nothing about
    the answer.
    """
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    if px.ndim == 2 and px.shape[0] > band_rows:
        tail = source.shape[2:]
        out = np.empty(px.shape + tail, dtype=np.float32)
        for top in range(0, px.shape[0], band_rows):
            bottom = min(top + band_rows, px.shape[0])
            out[top:bottom] = _bilinear(source, px[top:bottom], py[top:bottom],
                                        band_rows=band_rows)
        return out

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

    def effective_water_datum(self, elevation_m=None, water_mask=None,
                              has_ocean=False):
        """The real elevation that becomes SC4's shoreline.

        "lowest" needs the sampled ground to work from, so it is resolved
        once the elevation grid exists; before then it falls back to the
        configured value.
        """
        if self.water_datum_mode == "sea":
            return 0.0
        if self.water_datum_mode == "mapped" and elevation_m is not None:
            return mapped_water_datum(elevation_m, water_mask, has_ocean)
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
        numeric = {
            "latitude": self.center_lat,
            "longitude": self.center_lon,
            "metres per cell": self.metres_per_cell,
            "rotation": self.rotation_deg,
            "vertical scale": self.vertical_scale,
            "sea level": self.sea_level_m,
            "shoreline elevation": self.sea_reference_m,
            "ocean depth": self.ocean_depth_m,
            "water depth": self.water_depth_m,
        }
        if self.max_water_rise_m is not None:
            numeric["maximum water rise"] = self.max_water_rise_m
        if self.despike_threshold_m is not None:
            numeric["despike threshold"] = self.despike_threshold_m
        for label, value in numeric.items():
            if not math.isfinite(float(value)):
                raise GeoImportError("%s must be a finite number" % label.capitalize())
        if not -90.0 <= self.center_lat <= 90.0:
            raise GeoImportError("Latitude must be between -90 and 90")
        if not -180.0 <= self.center_lon <= 180.0:
            raise GeoImportError("Longitude must be between -180 and 180")
        if abs(self.center_lat) > MERCATOR_MAX_LAT:
            raise GeoImportError(
                "Latitude %.4f is outside the coverage of Web Mercator tiles "
                "(+/-85.05 degrees)" % self.center_lat)
        if (not isinstance(self.tiles_x, (int, np.integer))
                or not isinstance(self.tiles_y, (int, np.integer))):
            raise GeoImportError("Region dimensions must be whole numbers")
        if self.tiles_x < 1 or self.tiles_y < 1:
            raise GeoImportError("A region needs at least one tile on each side")
        vertices = (self.tiles_x * CELLS_PER_TILE + 1) * (
            self.tiles_y * CELLS_PER_TILE + 1)
        if vertices > MAX_REGION_VERTICES:
            raise GeoImportError(
                "This region has %d terrain vertices; the limit is %d "
                "before assembling elevation tiles. Use fewer tiles or "
                "import smaller areas." %
                (vertices, MAX_REGION_VERTICES))
        if self.metres_per_cell <= 0:
            raise GeoImportError("Metres per cell must be positive")
        if self.vertical_scale <= 0:
            raise GeoImportError("Vertical scale must be positive")
        if self.ocean_depth_m < 0 or self.water_depth_m <= 0:
            raise GeoImportError(
                "Water depth must be positive and ocean depth non-negative")
        if self.max_water_rise_m is not None and self.max_water_rise_m < 0:
            raise GeoImportError("Maximum water rise cannot be negative")
        if self.despike_threshold_m is not None and self.despike_threshold_m < 0:
            raise GeoImportError("Despike threshold cannot be negative")
        if (not isinstance(self.min_water_area_cells, (int, np.integer))
                or self.min_water_area_cells < 0):
            raise GeoImportError("Minimum water size must be a whole number")
        if self.vertical_mode not in ("match", "true", "manual"):
            raise GeoImportError(
                "Vertical mode must be 'match', 'true' or 'manual'")
        if self.water_datum_mode not in ("sea", "lowest", "manual", "mapped"):
            raise GeoImportError(
                "Water datum mode must be 'sea', 'lowest', 'manual' or 'mapped'")
        if self.water_datum_mode == "mapped" and self.water_source == "elevation":
            raise GeoImportError("Automatic mapped water level requires mapped water")
        if self.water_source not in ("elevation", "mask", "both"):
            raise GeoImportError(
                "Water source must be 'elevation', 'mask' or 'both'")
        _validate_mercator_coverage(self)

    @property
    def sampling_key(self):
        """Parameters that change the sampled elevation or mapped outlines."""
        return (self.center_lat, self.center_lon, self.tiles_x, self.tiles_y,
                self.metres_per_cell, self.rotation_deg, self.zoom, self.max_zoom)

    @property
    def grid_shape(self):
        """(rows, columns) of terrain vertices -- cells plus a shared edge."""
        return (self.tiles_y * CELLS_PER_TILE + 1,
                self.tiles_x * CELLS_PER_TILE + 1)



def coarsen(request, max_samples):
    """The same ground on a coarser grid, for a quicker preview.

    A preview is only a few hundred pixels across, so sampling it on the
    import's own grid downloads detail that is thrown away on the way to the
    screen -- and for a fixed footprint the tile count grows with the square
    of the sample density, which is why a large region crawls while a small
    one feels instant.

    The result keeps the centre, footprint, rotation and vertical mapping of
    ``request``, so water, heights and colours all come out the same, just
    computed on fewer samples. Holding the footprint exactly is what keeps
    the city grid drawn over the preview lined up with the terrain under it,
    and it is the one real constraint: the coarse grid has to keep the
    region's proportions, so its tile counts must stay a whole multiple of
    the region's aspect ratio in lowest terms. ``request`` comes back
    unchanged when the grid is already small enough, or when its proportions
    admit no smaller grid -- a 13 by 7 region, say.
    """
    rows, cols = request.grid_shape
    if max_samples < 2 or max(rows, cols) <= max_samples:
        return request

    # 32 x 20 tiles has the proportions 8 x 5, so the usable coarse grids are
    # 8 x 5, 16 x 10, 24 x 15 and 32 x 20 tiles. Pick the smallest that still
    # carries max_samples along its longer edge.
    steps = math.gcd(request.tiles_x, request.tiles_y)
    aspect = max(request.tiles_x, request.tiles_y) // steps
    wanted = (max_samples - 1) / float(aspect * CELLS_PER_TILE)
    multiple = min(steps, max(1, math.ceil(wanted)))
    if multiple >= steps:
        return request

    tiles_x = request.tiles_x // steps * multiple
    tiles_y = request.tiles_y // steps * multiple
    factor = steps / float(multiple)
    return replace(
        request,
        tiles_x=tiles_x,
        tiles_y=tiles_y,
        # Straight from the footprint, so the coarse region covers exactly
        # the ground the real one does.
        metres_per_cell=(request.tiles_x * request.metres_per_cell) / tiles_x,
        # "match" derives the vertical scale from the cell size, so resolving
        # it here is what keeps the coarse terrain the same shape as the fine
        # one rather than flattening it by the coarsening factor.
        vertical_mode="manual",
        vertical_scale=request.effective_vertical_scale(),
        # A pond covering n cells covers n / factor^2 of them now.
        min_water_area_cells=max(
            1, int(round(request.min_water_area_cells / factor ** 2))),
    )


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


def _mosaic_layout(request, lon, lat, sample_resolution_m=None):
    """Work out which tiles cover a set of sample points.

    Returns ``(zoom, tx, ty, tile_x0, tile_y0, n_x, n_y)``, where ``tx`` and
    ``ty`` are the fractional tile coordinates of the samples themselves.
    """
    zoom = request.zoom
    if zoom is None:
        resolution = sample_resolution_m or request.metres_per_cell
        zoom = zoom_for_resolution(resolution, request.center_lat,
                                   max_zoom=request.max_zoom)

    tx, ty = lonlat_to_tile(lon, lat, zoom)
    # Keep a dateline-crossing footprint as one short continuous strip.
    span = 2 ** zoom
    centre_tx, _ = lonlat_to_tile(request.center_lon, request.center_lat, zoom)
    tx = (tx - centre_tx + span / 2.0) % span - span / 2.0 + centre_tx

    # _bilinear samples around pixel centres, so the required pixel indices
    # need a one-pixel halo at every mosaic edge. Take the extremes straight
    # off the tile coordinates rather than materialising two more arrays the
    # size of the grid, which on a large region is hundreds of megabytes.
    needed_x0 = math.floor(float(tx.min()) * TILE_PIXELS - 0.5)
    needed_x1 = math.floor(float(tx.max()) * TILE_PIXELS - 0.5) + 1
    needed_y0 = math.floor(float(ty.min()) * TILE_PIXELS - 0.5)
    needed_y1 = math.floor(float(ty.max()) * TILE_PIXELS - 0.5) + 1
    tile_x0 = int(math.floor(needed_x0 / TILE_PIXELS))
    tile_x1 = int(math.floor(needed_x1 / TILE_PIXELS))
    tile_y0 = int(math.floor(needed_y0 / TILE_PIXELS))
    tile_y1 = int(math.floor(needed_y1 / TILE_PIXELS))
    return (zoom, tx, ty, tile_x0, tile_y0,
            tile_x1 - tile_x0 + 1, tile_y1 - tile_y0 + 1)


def tile_count(request):
    """How many source tiles sampling ``request`` will download.

    Cheap enough to ask before committing to a download, which is how the
    preview decides whether a rough first pass is worth showing at all.
    """
    request.validate()
    lon, lat = grid_lonlat(request)
    _, _, _, _, _, n_x, n_y = _mosaic_layout(request, lon, lat)
    return n_x * n_y


def _download_in_parallel(positions, load, place, report, workers,
                          queue_depth=2):
    """Run ``load`` over ``positions`` on a continuously fed thread pool.

    Results are handed to ``place`` on the calling thread as they arrive, in
    completion order, so one slow tile never holds up the ones behind it --
    the lockstep alternative, waiting for a whole batch before starting the
    next, runs every round at the speed of its worst tile.

    ``report`` is called on the calling thread too, and may raise to cancel
    the download. At most ``workers * queue_depth`` tiles are outstanding, so
    a cancelled preview abandons a handful of requests rather than hundreds.
    """
    upcoming = iter(positions)
    pending = set()
    done = 0
    depth = max(workers, workers * queue_depth)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sc4-tile")
    try:
        report(0)
        while True:
            for position in upcoming:
                pending.add(pool.submit(load, position))
                if len(pending) >= depth:
                    break
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                place(future.result())
                done += 1
            report(done)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _sample_tiles(request, fetcher, decode, channels, label, progress=None,
                  fill=0.0, sample_lonlat=None, sample_resolution_m=None):
    """Sample a slippy-map tile source over the region's vertex grid.

    ``decode`` turns PNG/JPEG bytes into an array; ``channels`` is ``None``
    for a 2D source (elevation) or 3 for RGB imagery.  Elevation and the
    basemap underlay go through here so they land on exactly the same grid
    -- a pixel of the underlay is the same patch of ground as the terrain
    vertex beneath it.

    Returns ``(sampled, zoom, tiles_fetched, tiles_missing)``.
    """
    request.validate()
    if sample_lonlat is None:
        lon, lat = grid_lonlat(request)
    else:
        lon, lat = sample_lonlat

    zoom, tx, ty, tile_x0, tile_y0, n_x, n_y = _mosaic_layout(
        request, lon, lat, sample_resolution_m)
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

    def load_tile(position):
        iy, ix = position
        wrapped_x = (tile_x0 + ix) % span
        tile_y = tile_y0 + iy
        data = fetcher.fetch(zoom, wrapped_x, tile_y) if 0 <= tile_y < span else None
        if data is None:
            return iy, ix, None
        patch = decode(data)
        if patch.shape[:2] != (TILE_PIXELS, TILE_PIXELS):
            raise GeoImportError(
                "%s tile %d/%d/%d is %dx%d, expected %dx%d"
                % (label.capitalize(), zoom, wrapped_x, tile_y,
                   patch.shape[1], patch.shape[0], TILE_PIXELS, TILE_PIXELS))
        return iy, ix, patch

    # Injected fetchers stay serial unless they opt in.
    workers = max(1, min(total, getattr(fetcher, "max_workers", 1)))
    positions = [(iy, ix) for iy in range(n_y) for ix in range(n_x)]

    def place(result):
        nonlocal fetched, missing
        iy, ix, patch = result
        if patch is None:
            missing += 1
            return
        fetched += 1
        mosaic[iy * TILE_PIXELS:(iy + 1) * TILE_PIXELS,
               ix * TILE_PIXELS:(ix + 1) * TILE_PIXELS] = patch

    def report(done):
        if progress is not None:
            progress(done, total, "Downloading %s tiles: %d of %d"
                     % (label, done, total))

    if workers == 1:
        report(0)
        for done, position in enumerate(positions):
            place(load_tile(position))
            report(done + 1)
    else:
        _download_in_parallel(positions, load_tile, place, report, workers)

    if fetched == 0:
        message = "No %s data was available for this area." % label
        last_missing = getattr(fetcher, "last_missing", None)
        if last_missing:
            status, url = last_missing
            message += (" The provider returned HTTP %d for %s; check the "
                        "tile URL template and any required API key."
                        % (status, url))
        raise GeoImportError(message)

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


def footprint_preview_extent(request, size=(560, 420), margin=0.18):
    """Return the north-up local extent used by the footprint preview."""
    request.validate()
    width_px, height_px = int(size[0]), int(size[1])
    if width_px < 32 or height_px < 32:
        raise GeoImportError("Preview must be at least 32 pixels on each side")

    width_m = request.tiles_x * CELLS_PER_TILE * request.metres_per_cell
    height_m = request.tiles_y * CELLS_PER_TILE * request.metres_per_cell
    corners_e = np.array([-width_m / 2, width_m / 2,
                          width_m / 2, -width_m / 2], dtype=np.float64)
    corners_n = np.array([height_m / 2, height_m / 2,
                          -height_m / 2, -height_m / 2], dtype=np.float64)
    theta = math.radians(request.rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    rotated_e = corners_e * cos_t + corners_n * sin_t
    rotated_n = -corners_e * sin_t + corners_n * cos_t

    span_e = max(float(np.ptp(rotated_e)) * (1.0 + 2.0 * margin), 1.0)
    span_n = max(float(np.ptp(rotated_n)) * (1.0 + 2.0 * margin), 1.0)
    target_aspect = width_px / float(height_px)
    if span_e / span_n < target_aspect:
        span_e = span_n * target_aspect
    else:
        span_n = span_e / target_aspect
    west, east = -span_e / 2.0, span_e / 2.0
    south, north = -span_n / 2.0, span_n / 2.0
    context_e = np.array([west, east, east, west])
    context_n = np.array([north, north, south, south])
    _, context_lat = local_offsets_to_lonlat(
        request.center_lat, request.center_lon, context_e, context_n)
    if (float(np.min(context_lat)) < -MERCATOR_MAX_LAT
            or float(np.max(context_lat)) > MERCATOR_MAX_LAT):
        raise GeoImportError(
            "This preview extends beyond Web Mercator coverage (+/-85.05 "
            "degrees); use a lower-latitude centre or a smaller area.")
    return west, east, south, north, span_e, span_n, cos_t, sin_t


def build_footprint_preview(request, fetcher, imagery=True, size=(560, 420),
                            city_size=4, progress=None, margin=0.18,
                            terrain_rgb=None, basemap_opacity=0.0,
                            layout_request=None):
    """Build a north-up context preview with the region grid overlaid.

    ``imagery`` selects regular RGB XYZ tiles.  When false, Terrarium
    elevation tiles are converted into a lightweight hillshade, providing a
    useful fallback when no basemap provider has been configured.
    ``terrain_rgb`` overlays the finished import's terrain colours inside
    the footprint, keeping water, relief and rotation faithful to the import.

    ``layout_request`` supplies the city grid to draw on top, for when
    ``request`` has been through :func:`coarsen`: the two cover the same
    ground, but only the original knows how many city tiles divide it.
    """
    from PIL import ImageDraw

    width_px, height_px = int(size[0]), int(size[1])
    extent = footprint_preview_extent(request, size, margin)
    west, east, south, north, span_e, span_n, cos_t, sin_t = extent
    width_m = request.tiles_x * CELLS_PER_TILE * request.metres_per_cell
    height_m = request.tiles_y * CELLS_PER_TILE * request.metres_per_cell

    sample_e = np.linspace(west, east, width_px, dtype=np.float64)
    sample_n = np.linspace(north, south, height_px, dtype=np.float64)
    east_grid, north_grid = np.meshgrid(sample_e, sample_n)
    lon, lat = local_offsets_to_lonlat(
        request.center_lat, request.center_lon, east_grid, north_grid)
    resolution = max(span_e / max(1, width_px - 1),
                     span_n / max(1, height_px - 1))

    if imagery:
        sampled, zoom, fetched, missing = _sample_tiles(
            request, fetcher, _decode_rgb_tile, 3, "preview map", progress,
            fill=255.0, sample_lonlat=(lon, lat),
            sample_resolution_m=resolution)
        rgb = np.clip(np.rint(sampled), 0, 255).astype(np.uint8)
    elif terrain_rgb is None:
        sampled, zoom, fetched, missing = _sample_tiles(
            request, fetcher, _decode_tile, None, "preview elevation", progress,
            sample_lonlat=(lon, lat), sample_resolution_m=resolution)
        elevation = np.asarray(sampled, dtype=np.float32)
        low, high = np.percentile(elevation, (2, 98))
        relief = np.clip((elevation - low) / max(float(high - low), 1.0), 0, 1)
        gradient_y, gradient_x = np.gradient(elevation, resolution, resolution)
        shade = np.clip(0.72 - gradient_x * 0.8 + gradient_y * 0.5, 0.3, 1.0)
        rgb = np.stack((55 + relief * 125,
                        105 + relief * 90,
                        55 + relief * 105), axis=-1)
        rgb *= shade[..., None]
        rgb[elevation <= 0] = (92, 135, 166)
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    else:
        rgb = np.full((height_px, width_px, 3), (235, 238, 240), dtype=np.uint8)
        zoom = fetched = missing = 0

    if terrain_rgb is not None:
        colours = np.asarray(terrain_rgb, dtype=np.float32)
        if colours.shape != (*request.grid_shape, 3):
            raise GeoImportError("Preview colours must match the imported terrain grid")
        rows, cols = request.grid_shape
        local_e = east_grid * cos_t - north_grid * sin_t
        local_n = east_grid * sin_t + north_grid * cos_t
        column = local_e / request.metres_per_cell + (cols - 1) / 2
        row = (rows - 1) / 2 - local_n / request.metres_per_cell
        inside = (column >= 0) & (column <= cols - 1) & (row >= 0) & (row <= rows - 1)
        terrain_view = _bilinear(colours, column + 0.5, row + 0.5)
        alpha = min(1.0, max(0.0, basemap_opacity)) if imagery else 0.0
        rgb[inside] = (terrain_view[inside] * (1 - alpha) + rgb[inside] * alpha).astype(np.uint8)

    image = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(image)

    def to_pixel(local_e, local_n):
        map_e = local_e * cos_t + local_n * sin_t
        map_n = -local_e * sin_t + local_n * cos_t
        x = (map_e - west) / span_e * (width_px - 1)
        y = (north - map_n) / span_n * (height_px - 1)
        return (float(x), float(y))

    layout = layout_request or request
    tile_m = CELLS_PER_TILE * layout.metres_per_cell
    for x, y, city in _city_layout((layout.tiles_x, layout.tiles_y), city_size):
        corners = [(x, y), (x + city, y), (x + city, y + city),
                   (x, y + city), (x, y)]
        draw.line([to_pixel(-width_m / 2 + col * tile_m,
                            height_m / 2 - row * tile_m)
                   for col, row in corners], fill=(20, 55, 255), width=2)
    return image, zoom, fetched, missing


def despike_elevation(elevation_m, threshold_m=200.0, band_rows=256):
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

    rows, cols = elevation.shape
    padded = np.pad(elevation, 1, mode="edge")
    cleaned = None
    count = 0
    # A 3x3 median over the whole grid at once needs nine copies of it plus
    # whatever the median itself allocates, which on a large region is a few
    # hundred megabytes for a result of a few tens. Row bands give the same
    # answer with the working set bounded by the band.
    for top in range(0, rows, band_rows):
        bottom = min(top + band_rows, rows)
        height = bottom - top
        window = padded[top:bottom + 2]
        neighbourhood = np.stack(
            [window[dy:dy + height, dx:dx + cols]
             for dy in range(3) for dx in range(3)],
            axis=0)
        median = np.median(neighbourhood, axis=0)

        band = elevation[top:bottom]
        spikes = np.abs(band - median) > threshold_m
        found = int(np.count_nonzero(spikes))
        if found:
            if cleaned is None:
                cleaned = elevation.copy()
            cleaned[top:bottom] = np.where(spikes, median, band)
            count += found
    return (elevation if cleaned is None else cleaned), count


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
    if not np.isfinite(elevation_m).all():
        raise GeoImportError("Elevation data contains a non-finite value")
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


def fetch_water_outlines(request, client, progress=None):
    """The mapped water outlines covering a region, straight from Overpass.

    Kept separate from rasterizing them so one query can serve several grids
    -- a preview drawn at two resolutions asks the volunteer-run Overpass
    mirrors once, not once per pass.

    Returns ``(features, coastlines)``, ready for :func:`fetch_water_mask`.
    """
    if progress is not None:
        progress(0, 1, "Looking up water from OpenStreetMap")
    data = client.fetch(build_water_query(region_bbox(request)))
    return parse_overpass_water_features(data), parse_overpass_coastlines(data)


def fetch_water_mask(request, client, progress=None, outlines=None):
    """Fetch water areas for a region and burn them onto its grid.

    ``outlines`` reuses a result from :func:`fetch_water_outlines` instead of
    querying again.
    """
    client.last_warning = None
    if outlines is None:
        outlines = fetch_water_outlines(request, client, progress)
    features, coastlines = outlines
    if progress is not None:
        progress(1, 1, "Tracing %d water outline(s)"
                 % (sum(len(outers) for outers, _ in features)
                    + len(coastlines)))
    mask = rasterize_water(request, features=features)
    ocean = rasterize_coastlines(request, coastlines)
    client.has_ocean = bool(ocean.any())
    if not features and not coastlines:
        # Empty boundaries do not prove dry land: offshore footprints and
        # enclosing lakes can both be absent from this local query.
        setattr(client, "last_warning", (
            "No mapped water boundaries were returned. This may be ambiguous "
            "for offshore areas or large enclosing lakes; elevation water "
            "mode is safer unless you choose to continue with an empty mask."))
    elif coastlines and not ocean.any():
        client.last_warning = (
            "The returned coastlines did not establish a sea area. They may "
            "be incomplete or outside the footprint. Check the selected "
            "area; unmapped sea will become land in Mapped water only mode.")
    return mask | ocean


def build_region_grid(request, fetcher, progress=None, water_mask=None,
                      water_client=None, sampled_elevation=None,
                      water_has_ocean=False):
    """Fetch, project and convert -- the whole import in one call.

    ``water_mask`` is an optional boolean grid from
    :func:`fetch_water_mask`, applied according to ``request.water_source``.
    Alternatively, ``water_client`` fetches the mask alongside elevation.
    ``sampled_elevation`` can reuse the tuple from :func:`sample_elevation`
    when only water/height settings changed on the same footprint.
    """
    needs_water = request.water_source != "elevation" and water_mask is None
    if needs_water and water_client is None:
        raise GeoImportError("Mapped water requires a water mask")
    request.validate()
    pool = ThreadPoolExecutor(max_workers=1) if needs_water else None
    try:
        if pool:
            water_future = pool.submit(fetch_water_mask, request, water_client, progress)
        if sampled_elevation is None:
            sampled_elevation = sample_elevation(request, fetcher, progress)
        elevation, zoom, fetched, missing = sampled_elevation
        if elevation.shape != request.grid_shape:
            raise GeoImportError("Sampled elevation must match the region grid")
        if pool:
            water_mask = water_future.result()
            water_has_ocean = getattr(water_client, "has_ocean", False)
    finally:
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
    if progress is not None:
        progress(1, 1, "Applying heights and water")
    elevation, despiked = despike_elevation(elevation, request.despike_threshold_m)
    vertical_scale = request.effective_vertical_scale()
    sea_reference = request.effective_water_datum(
        elevation, water_mask, water_has_ocean)
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
        mapped, kept, small, high = filter_water_bodies(
            np.asarray(water_mask, dtype=bool), height_dm,
            sea_level_m=request.sea_level_m,
            min_area_cells=request.min_water_area_cells,
            max_rise_m=request.max_water_rise_m)
        dropped_water_bodies = small + high
        water_bodies = kept
        height_dm, water_cells, lifted_cells = apply_water_mask(
            height_dm, mapped, sea_level_m=request.sea_level_m,
            water_depth_m=request.water_depth_m,
            lift_land=request.water_source == "mask",
            keep_bathymetry=request.keep_bathymetry)

    source = getattr(fetcher, "url_template", DEFAULT_TILE_URL)
    attribution = getattr(fetcher, "attribution", None)
    if not attribution:
        if source == DEFAULT_TILE_URL:
            attribution = DEFAULT_ATTRIBUTION
        else:
            attribution = "Elevation source: %s" % source

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
        source=source,
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
        attribution=attribution,
    )


# --- Water mask -----------------------------------------------------------

#: Overpass mirrors are volunteer-run and rate limited. One query per import,
#: cached to disk afterwards, is well within what they ask for.
#:
#: Tried in order. The main instance currently answers 406 to some
#: otherwise valid requests, so a second endpoint is not a luxury.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_URL = OVERPASS_MIRRORS[0]
OVERPASS_QUERY_TIMEOUT = 90

#: Tags that describe a body of water as an *area*.
WATER_AREA_TAGS = (
    ("natural", "water"),
    ("waterway", "riverbank"),
    ("landuse", "reservoir"),
    ("landuse", "basin"),
)


def build_water_query(bbox, timeout=OVERPASS_QUERY_TIMEOUT):
    """Overpass QL selecting water areas within ``bbox``.

    ``bbox`` is ``(south, west, north, east)``, the order Overpass uses.
    """
    south, west, north, east = bbox
    west = ((west + 180.0) % 360.0) - 180.0
    east = ((east + 180.0) % 360.0) - 180.0
    box = "%.6f,%.6f,%.6f,%.6f" % (south, west, north, east)
    clauses = []
    for key, value in WATER_AREA_TAGS:
        # Unquoted tag filters. Overpass QL allows them for plain
        # identifiers, and overpass-api.de currently rejects some requests
        # carrying quoted values with a 406, which is indistinguishable
        # from the service being down.
        clauses.append("way[%s=%s](%s);" % (key, value, box))
        clauses.append("relation[%s=%s](%s);" % (key, value, box))
    # Coastlines are directed open ways, with land on the left. They need a
    # separate raster pass rather than being treated as closed water areas.
    clauses.append("way[natural=coastline](%s);" % box)
    return ("[out:json][timeout:%d];\n(\n  %s\n);\nout geom;"
            % (int(timeout), "\n  ".join(clauses)))


#: Round query bounds out onto a grid this fraction of the region's span.
#: Overpass is volunteer-run and its answers are cached on disk under the
#: exact query text, so nudging the centre or the rotation by a hair would
#: otherwise mean a fresh query for what is nearly the same ground. Snapping
#: buys a margin of slack at the cost of a slightly wider area.
OVERPASS_BBOX_SNAP = 0.05


def _snap_step(span):
    """A stable rounding step, one significant figure, for ``span``."""
    step = OVERPASS_BBOX_SNAP * max(float(span), 1e-9)
    magnitude = math.floor(math.log10(step))
    return max(round(step / 10 ** magnitude) * 10 ** magnitude, 1e-6)


def region_bbox(request, margin_cells=2, snap=True):
    """Bounding box of a region as ``(south, west, north, east)``.

    A small margin keeps water that laps over the edge from being clipped
    into a straight line at the boundary. ``snap`` then rounds the bounds
    outward onto a coarse grid, so small edits keep asking the same question
    and keep hitting the cached answer; it only ever widens the box.
    """
    request.validate()
    lon, lat = grid_lonlat(request)
    margin_deg_lat = (margin_cells * request.metres_per_cell
                      / metres_per_degree_lat(request.center_lat))
    margin_deg_lon = (margin_cells * request.metres_per_cell
                      / float(metres_per_degree_lon(request.center_lat)))
    south = float(lat.min()) - margin_deg_lat
    north = float(lat.max()) + margin_deg_lat
    # Keep the two bounds in the same continuous local frame.  The query
    # builder normalizes them to an Overpass dateline-crossing bbox.
    west = float(lon.min()) - margin_deg_lon
    east = float(lon.max()) + margin_deg_lon
    if snap:
        lat_step = _snap_step(north - south)
        lon_step = _snap_step(east - west)
        south = math.floor(south / lat_step) * lat_step
        north = math.ceil(north / lat_step) * lat_step
        west = math.floor(west / lon_step) * lon_step
        east = math.ceil(east / lon_step) * lon_step
        south = max(south, -MERCATOR_MAX_LAT)
        north = min(north, MERCATOR_MAX_LAT)
    return (south, west, north, east)


def _decompressed(response):
    """Read a response body, undoing gzip when the server used it.

    Substituted transports need not carry headers, so a response without
    them is taken at face value.
    """
    data = response.read()
    headers = getattr(response, "headers", None)
    if headers is not None and headers.get("Content-Encoding", "").lower() == "gzip":
        return gzip.decompress(data)
    return data


class OverpassClient:
    """Fetch water areas from Overpass, cached on disk.

    Responses are cached under a hash of the query, so re-importing the same
    area costs nothing and works offline. ``opener`` exists so tests (and
    anyone wanting a different transport) can substitute the network call.
    """

    def __init__(self, url=None, cache_dir=None, timeout=180,
                 user_agent=None, opener=None, mirrors=None):
        # An explicit url pins that endpoint; otherwise work down the
        # mirror list, since the main instance is not always answering.
        self.mirrors = ([url] if url else list(mirrors or OVERPASS_MIRRORS))
        self.url = self.mirrors[0]
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.user_agent = user_agent or (
            "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)")
        self.opener = opener
        self._fetch_lock = threading.Lock()

    def _cache_path(self, query):
        if not self.cache_dir:
            return None
        import hashlib
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.cache_dir, digest + ".json")

    def _fetch_from_mirrors(self, query):
        """Try each endpoint in turn, returning the first that answers."""
        body = urllib.parse.urlencode({"data": query}).encode("utf-8")
        headers = {"User-Agent": self.user_agent,
                   "Accept": "application/json",
                   # Overpass answers are JSON full of coordinates, which
                   # compress about six to one. Asking for that is the single
                   # biggest saving available on a water query.
                   "Accept-Encoding": "gzip"}
        problems = []
        for url in self.mirrors:
            request = urllib.request.Request(url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.url = url
                    return _decompressed(response)
            except urllib.error.HTTPError as exc:
                problems.append("%s: HTTP %d" % (url, exc.code))
                # 406, 429 and 504 are all "this mirror, right now" --
                # worth asking the next one.
                if exc.code not in (406, 429, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                problems.append("%s: %s" % (url, getattr(exc, "reason", exc)))

        raise GeoImportError(
            "Could not reach an OpenStreetMap query service. Tried:\n  "
            + "\n  ".join(problems))

    def fetch(self, query):
        """Return the decoded Overpass response for a query."""
        # Preview and import share this client: let the first request fill
        # the disk cache before the next caller checks it.
        with self._fetch_lock:
            return self._fetch(query)

    def _fetch(self, query):
        import json

        path = self._cache_path(query)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    data = json.load(fh)
                    _validate_overpass_response(data)
                    return data
                except ValueError:
                    pass  # a truncated cache entry; refetch below
                except GeoImportError:
                    pass  # stale runtime-error cache; refetch below

        if self.opener is not None:
            payload = self.opener(self.url, query)
        else:
            payload = self._fetch_from_mirrors(query)

        try:
            data = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        except (TypeError, ValueError) as exc:
            raise GeoImportError(
                "The OpenStreetMap query service returned an unreadable reply"
            ) from exc
        _validate_overpass_response(data)

        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(path),
                prefix=".osm-", suffix=".part", delete=False)
            try:
                with tmp:
                    json.dump(data, tmp)
                os.replace(tmp.name, path)
            finally:
                try:
                    os.unlink(tmp.name)
                except FileNotFoundError:
                    pass
        return data


def _validate_overpass_response(data):
    """Reject protocol/runtime failures before they can become cached dry land."""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise GeoImportError(
            "The OpenStreetMap query service returned an invalid response")
    remark = str(data.get("remark") or "").lower()
    if any(word in remark for word in ("runtime error", "timed out", "timeout")):
        raise GeoImportError(
            "The OpenStreetMap query service reported a runtime error; "
            "try again or choose elevation water mode.")


def _same_ring_point(left, right, tolerance=1e-9):
    """Whether two lon/lat vertices describe the same OSM node."""
    return (abs(left[0] - right[0]) <= tolerance
            and abs(left[1] - right[1]) <= tolerance)


def _assemble_rings(fragments):
    """Join open Overpass member ways into closed polygon rings.

    Multipolygon relations normally split a boundary across several member
    ways.  ``out geom`` gives each member's coordinates separately, so they
    must be joined at matching endpoints before Pillow is allowed to fill
    them.  Any fragment chain that cannot be closed is ignored rather than
    inventing a straight closing edge across the region.
    """
    pending = [list(fragment) for fragment in fragments if len(fragment) >= 2]
    rings = []
    while pending:
        chain = pending.pop(0)
        while not _same_ring_point(chain[0], chain[-1]):
            joined = False
            for index, fragment in enumerate(pending):
                if _same_ring_point(chain[-1], fragment[0]):
                    chain.extend(fragment[1:])
                elif _same_ring_point(chain[-1], fragment[-1]):
                    chain.extend(reversed(fragment[:-1]))
                elif _same_ring_point(chain[0], fragment[-1]):
                    chain = fragment[:-1] + chain
                elif _same_ring_point(chain[0], fragment[0]):
                    chain = list(reversed(fragment[1:])) + chain
                else:
                    continue
                pending.pop(index)
                joined = True
                break
            if not joined:
                break
        if len(chain) >= 4 and _same_ring_point(chain[0], chain[-1]):
            rings.append(chain)
    return rings


def parse_overpass_water_features(data):
    """Return ``(outer_rings, inner_rings)`` per independent water feature."""
    features = []
    way_features = []
    outer_members = set()
    if not isinstance(data, dict):
        return features

    for element in data.get("elements", []) or []:
        if not isinstance(element, dict):
            continue
        kind = element.get("type")
        if kind == "way":
            if (element.get("tags") or {}).get("natural") == "coastline":
                continue
            ring = [(float(p["lon"]), float(p["lat"]))
                    for p in element.get("geometry", []) or []
                    if isinstance(p, dict) and "lon" in p and "lat" in p]
            if (len(ring) >= 4
                    and _same_ring_point(ring[0], ring[-1])):
                way_features.append((element.get("id"), ([ring], [])))
        elif kind == "relation":
            fragments = {"outer": [], "inner": []}
            for member in element.get("members", []) or []:
                if not isinstance(member, dict):
                    continue
                ring = [(float(p["lon"]), float(p["lat"]))
                        for p in member.get("geometry", []) or []
                        if isinstance(p, dict) and "lon" in p and "lat" in p]
                if len(ring) < 2:
                    continue
                role = "inner" if member.get("role") == "inner" else "outer"
                fragments[role].append(ring)
            outers = _assemble_rings(fragments["outer"])
            inners = _assemble_rings(fragments["inner"])
            if outers:
                features.append((outers, inners))
                outer_members.update(
                    member.get("ref") for member in element.get("members", [])
                    if isinstance(member, dict) and member.get("role") != "inner")
    # A tagged outer way may also be returned separately. Drawing it again
    # would fill the relation's islands back in. Independent ponds survive.
    return features + [feature for way_id, feature in way_features
                       if way_id is None or way_id not in outer_members]


def parse_overpass_water(data):
    """Pull water rings out of an Overpass response.

    The historical flattened return value remains for callers and tests;
    imports use :func:`parse_overpass_water_features` to preserve ownership.
    """
    features = parse_overpass_water_features(data)
    return ([ring for outers, _ in features for ring in outers],
            [ring for _, inners in features for ring in inners])


def parse_overpass_coastlines(data):
    """Return directed OSM coastline ways as lists of ``(lon, lat)``.

    OpenStreetMap stores coastlines with land on the left and sea on the
    right. Preserving the node order lets :func:`rasterize_coastlines` decide
    which side of each line is ocean without consulting elevation.
    """
    coastlines = []
    if not isinstance(data, dict):
        return coastlines
    for element in data.get("elements", []) or []:
        if not isinstance(element, dict) or element.get("type") != "way":
            continue
        if (element.get("tags") or {}).get("natural") != "coastline":
            continue
        line = [(float(p["lon"]), float(p["lat"]))
                for p in element.get("geometry", []) or []
                if isinstance(p, dict) and "lon" in p and "lat" in p]
        if len(line) >= 2:
            coastlines.append(line)
    return coastlines


def rasterize_water(request, outers=(), inners=(), fetcher_shape=None,
                    features=None):
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

    if features is None:
        features = [([ring], []) for ring in outers]
        # Preserve the old direct-call contract for a single flattened group.
        if inners:
            features = [(list(outers), list(inners))]
    for feature_outers, feature_inners in features:
        # Most water areas have no islands and can be drawn directly.
        feature = Image.new("1", (cols, rows), 0) if feature_inners else canvas
        draw = ImageDraw.Draw(feature)
        for ring in feature_outers:
            points = to_pixels(ring)
            if len(points) >= 3:
                draw.polygon(points, fill=1, outline=1)
        for ring in feature_inners:
            points = to_pixels(ring)
            if len(points) >= 3:
                draw.polygon(points, fill=0, outline=0)
        if feature_inners:
            canvas.paste(1, mask=feature)

    return np.array(canvas, dtype=bool)


def rasterize_coastlines(request, coastlines, fetcher_shape=None):
    """Fill the sea on the right-hand side of directed OSM coastlines.

    The coastline pixels form a barrier. The connected areas on either side
    are scored from directed coastline samples, with right-side length voting
    for sea and left-side length voting for land. This works for both mainland
    coasts and islands without mistaking below-sea-level land for sea.
    """
    from PIL import ImageDraw

    rows, cols = fetcher_shape or request.grid_shape
    barrier_image = Image.new("1", (cols, rows), 0)
    draw = ImageDraw.Draw(barrier_image)
    side_samples = []

    def to_pixels(line):
        lons = np.array([p[0] for p in line], dtype=np.float64)
        lats = np.array([p[1] for p in line], dtype=np.float64)
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

    def clipped_midpoint(x0, y0, x1, y1):
        """Midpoint of the part of a segment visible on the grid."""
        dx, dy = x1 - x0, y1 - y0
        low, high = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, cols - 1 - x0),
                     (-dy, y0), (dy, rows - 1 - y0)):
            if abs(p) < 1e-12:
                if q < 0:
                    return None
                continue
            ratio = q / p
            if p < 0:
                low = max(low, ratio)
            else:
                high = min(high, ratio)
            if low > high:
                return None
        middle = (low + high) / 2.0
        return (x0 + middle * dx, y0 + middle * dy,
                (high - low) * math.hypot(dx, dy))

    for coastline in coastlines:
        points = to_pixels(coastline)
        if len(points) < 2:
            continue
        draw.line(points, fill=1, width=1)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            visible = clipped_midpoint(x0, y0, x1, y1)
            length = math.hypot(x1 - x0, y1 - y0)
            if visible is None or length < 1e-9:
                continue
            x, y, visible_length = visible
            # With screen coordinates (y grows down), (-dy, dx) is the
            # right-hand normal -- the sea side of an OSM coastline.
            nx = -(y1 - y0) / length
            ny = (x1 - x0) / length
            # Clear the rasterized barrier even when a diagonal normal
            # rounds toward it. One cell is not enough at 45 degrees.
            side_samples.append((
                (int(round(y + 1.5 * ny)), int(round(x + 1.5 * nx))),
                (int(round(y - 1.5 * ny)), int(round(x - 1.5 * nx))),
                visible_length,
            ))

    if not side_samples:
        return np.zeros((rows, cols), dtype=bool)

    barrier = np.array(barrier_image, dtype=bool)
    components, count = label_water_bodies(~barrier)
    scores = np.zeros(count + 1, dtype=np.float64)
    for right, left, weight in side_samples:
        sides = []
        for row, col in (right, left):
            if (not (0 <= row < rows and 0 <= col < cols)
                    or barrier[row, col]):
                break
            sides.append(components[row, col])
        # A gap connects land and sea around the line's ends. Such a pair
        # cannot classify either side; neither can a sample on the barrier.
        # Counting only its other half could flood the entire land mass.
        if len(sides) != 2 or sides[0] == sides[1]:
            continue
        scores[sides[0]] += weight
        scores[sides[1]] -= weight

    # A tight harbour turn can put an individual rounded sample on the wrong
    # side. Classifying whole connected components by the accumulated
    # right-versus-left coastline length prevents that one sample from
    # flooding the entire land mass.
    tolerance = max(1e-6, sum(sample[2] for sample in side_samples) * 1e-9)
    return scores[components] > tolerance


def label_water_bodies(mask):
    """Label four-connected water using horizontal runs, not per-pixel walks."""
    mask = np.asarray(mask, dtype=bool)
    labels = np.zeros(mask.shape, dtype=np.int32)
    parents = [0]

    def root(label):
        while parents[label] != label:
            parents[label] = parents[parents[label]]
            label = parents[label]
        return label

    previous = []
    for row, values in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.pad(values, (1, 1))))
        current = []
        above = 0
        for start, stop in edges.reshape(-1, 2):
            while above < len(previous) and previous[above][1] <= start:
                above += 1
            label = len(parents)
            parents.append(label)
            overlap = above
            while overlap < len(previous) and previous[overlap][0] < stop:
                other = root(previous[overlap][2])
                parents[root(label)] = other
                label = other
                overlap += 1
            labels[row, start:stop] = label
            current.append((start, stop, label))
        previous = current

    roots = np.array([root(label) for label in range(len(parents))])
    _, compact = np.unique(roots, return_inverse=True)
    return compact.astype(np.int32)[labels], int(compact.max())


def mapped_water_datum(elevation_m, mask, has_ocean=False):
    """Use sea level on coasts, or the dominant mapped water body's surface.

    A median of DEM samples resists bank pixels and isolated artifacts.
    Smaller lakes at different elevations still share SC4's one water plane;
    the height filter decides whether they can be represented.
    """
    elevation = np.asarray(elevation_m)
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != elevation.shape:
        raise GeoImportError("Automatic water level needs a matching water mask")
    if has_ocean:
        return 0.0
    if not mask.any():
        return float(elevation.min()) - 1.0
    labels, _ = label_water_bodies(mask)
    areas = np.bincount(labels.ravel())
    areas[0] = 0
    return float(np.median(elevation[labels == areas.argmax()]))


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
    water_labels = labels[mask]
    areas = np.bincount(water_labels, minlength=count + 1)
    keep = areas >= minimum_area
    keep[0] = False
    dropped_small = count - int(np.count_nonzero(keep))
    dropped_high = 0
    if ceiling is not None:
        # Group once, instead of scanning the entire region for each pond.
        order = np.argsort(water_labels, kind="stable")
        heights_by_body = heights[mask][order]
        offsets = np.concatenate(([0], np.cumsum(areas)))
        # The median is robust to a few stray cells clipped off a bank.
        for label in np.flatnonzero(keep):
            if np.median(heights_by_body[offsets[label]:offsets[label + 1]]) > ceiling:
                keep[label] = False
                dropped_high += 1

    return keep[labels], int(np.count_nonzero(keep)), dropped_small, dropped_high


def apply_water_mask(height_dm, mask, sea_level_m=SEA_LEVEL_M,
                     water_depth_m=3.0, land_margin_m=0.5, lift_land=True,
                     keep_bathymetry=True):
    """Force water where the mask says water, and dry land where it does not.

    Both halves matter. Pushing masked ground under the waterline is the
    obvious one; lifting everything else *above* it is what finally makes a
    polder work -- ground that really does sit below sea level but is dry.
    Together they cut the wet/dry question loose from elevation, which a
    shoreline datum alone can never do.

    With ``keep_bathymetry=False``, mapped beds use the requested depth,
    including ground previously flattened to the elevation-mode ocean shelf.

    Returns ``(height_dm, wet_count, lifted_count)``.
    """
    heights = np.array(height_dm, dtype=np.int64)
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != heights.shape:
        raise GeoImportError(
            "Water mask is %s but the region is %s"
            % (mask.shape, heights.shape))

    waterline = int(round(sea_level_m * 10))
    bed = waterline - max(1, int(round(water_depth_m * 10)))

    wet_before = heights >= waterline
    water_heights = np.minimum(heights, bed) if keep_bathymetry else bed
    heights = np.where(mask, water_heights, heights)
    wet_count = int(np.count_nonzero(mask & wet_before))

    lifted_count = 0
    if lift_land:
        shore = waterline + max(1, int(round(land_margin_m * 10)))
        needs_lift = (~mask) & (heights < shore)
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
    layout = list(_city_layout(size, preferred))
    image = Image.new("RGB", (int(size[0]), int(size[1])), CONFIG_VOID)
    for x, y, city in layout:
        image.paste(CONFIG_COLORS[city], (x, y, x + city, y + city))
    return image


def _city_layout(size, preferred):
    """Yield the same (x, y, size) cities for config.bmp and the preview."""
    width, height = int(size[0]), int(size[1])
    if width < 1 or height < 1:
        raise GeoImportError("A region needs at least one tile on each side")
    if preferred not in CITY_SIZES:
        raise GeoImportError("City size must be 1, 2 or 4 small tiles")

    taken = np.zeros((height, width), dtype=bool)

    for city in (4, 2, 1):
        if city > preferred:
            continue
        for y in range(height - city + 1):
            for x in range(width - city + 1):
                if taken[y:y + city, x:x + city].any():
                    continue
                taken[y:y + city, x:x + city] = True
                yield x, y, city


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
    kind: str = ""
    category: str = ""
    osm_type: str = ""
    importance: float = 0.0
    bounding_box: Optional[tuple] = None

    @property
    def label(self):
        """Compact result label that distinguishes administrative layers."""
        kind = self.kind.replace("_", " ").strip()
        if not kind:
            return self.name
        suffix = "%s boundary" % kind if self.category == "boundary" else kind
        return "[%s] %s" % (suffix.title(), self.name)

    def details(self):
        """Human-readable type and approximate extent for the result."""
        parts = []
        kind = self.kind.replace("_", " ").strip()
        if kind:
            parts.append(("%s boundary" % kind
                          if self.category == "boundary" else kind).title())
        if self.bounding_box is not None:
            south, north, west, east = self.bounding_box
            width_km = abs(east - west) * float(metres_per_degree_lon(self.lat)) / 1000.0
            height_km = abs(north - south) * metres_per_degree_lat(self.lat) / 1000.0
            parts.append("approximately %.0f x %.0f km" % (width_km, height_km))
        if self.osm_type:
            parts.append("OSM %s" % self.osm_type)
        return " - ".join(parts)


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
        "addressdetails": "1",
        "dedupe": "1",
    })
    url = "%s?%s" % (NOMINATIM_URL, params)
    agent = user_agent or "SC4Mapper/2026 (+https://github.com/caspervg/SC4Mapper-2026)"

    try:
        if opener is not None:
            payload = opener(url)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": agent})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise GeoImportError("Could not reach the search service (%s)."
                             % getattr(exc, "reason", exc)) from exc

    try:
        results = json.loads(payload)
    except ValueError as exc:
        raise GeoImportError("The search service returned an unreadable reply") from exc

    places = {}
    for item in results:
        try:
            name = item.get("display_name", "?")
            kind = item.get("addresstype") or item.get("type") or ""
            raw_box = item.get("boundingbox")
            bounding_box = None
            if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
                bounding_box = tuple(float(value) for value in raw_box)
            place = Place(
                name=name,
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                kind=str(kind),
                category=str(item.get("category") or ""),
                osm_type=str(item.get("osm_type") or ""),
                importance=float(item.get("importance") or 0.0),
                bounding_box=bounding_box,
            )
        except (KeyError, TypeError, ValueError):
            continue
        key = (name.casefold(), place.kind.casefold())
        previous = places.get(key)
        if previous is None or place.importance > previous.importance:
            places[key] = place
    return list(places.values())
