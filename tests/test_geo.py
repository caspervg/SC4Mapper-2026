"""Tests for the real-world terrain import pipeline.

Everything here runs offline: elevation tiles are synthesised in-memory and
handed to the code through :class:`sc4mapper.geo.DictTileFetcher`.
"""

import io
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sc4mapper import geo


# --- helpers --------------------------------------------------------------


def encode_terrarium(elevation):
    """Encode a 2D elevation array (metres) as terrarium PNG bytes."""
    value = np.asarray(elevation, dtype=np.float64) + 32768.0
    value = np.clip(value, 0, 256 * 256 - 1.0 / 256)
    red = np.floor(value / 256.0)
    green = np.floor(value - red * 256.0)
    blue = np.floor((value - red * 256.0 - green) * 256.0)
    rgb = np.stack([red, green, blue], axis=-1).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def constant_tile(elevation_m):
    return encode_terrarium(np.full((geo.TILE_PIXELS, geo.TILE_PIXELS),
                                    float(elevation_m)))


class ConstantFetcher:
    """Returns the same elevation for every tile requested."""

    def __init__(self, elevation_m):
        self._data = constant_tile(elevation_m)
        self.requests = []

    def fetch(self, zoom, x, y):
        self.requests.append((zoom, x, y))
        return self._data


class RampFetcher:
    """Elevation rises linearly eastward, measured from ``origin_px``.

    Global pixel coordinates run past a million at useful zooms, well
    outside what terrarium can encode, so the ramp is anchored near the
    area being sampled.
    """

    def __init__(self, origin_px=0.0, metres_per_pixel=1.0):
        self.origin_px = origin_px
        self.metres_per_pixel = metres_per_pixel
        self.requests = []

    def elevation_at(self, global_px_x):
        return (global_px_x - self.origin_px) * self.metres_per_pixel

    def fetch(self, zoom, x, y):
        self.requests.append((zoom, x, y))
        columns = (x * geo.TILE_PIXELS
                   + np.arange(geo.TILE_PIXELS, dtype=np.float64))
        grid = np.tile(self.elevation_at(columns), (geo.TILE_PIXELS, 1))
        return encode_terrarium(grid)


def mosaic_origin_px(req, zoom):
    """Global pixel x of the west edge of the mosaic for ``req``."""
    lon, lat = geo.grid_lonlat(req)
    tx, _ = geo.lonlat_to_tile(lon, lat, zoom)
    return math.floor(float(np.min(tx))) * geo.TILE_PIXELS


def request(**kwargs):
    params = dict(center_lat=52.3676, center_lon=4.9041, tiles_x=1, tiles_y=1)
    params.update(kwargs)
    return geo.GeoImportRequest(**params)


# --- tile maths -----------------------------------------------------------


@pytest.mark.parametrize("lon,lat", [
    (0.0, 0.0),
    (4.9041, 52.3676),
    (-74.006, 40.7128),
    (139.6917, 35.6895),
    (-58.3816, -34.6037),
    (179.9, -45.0),
])
@pytest.mark.parametrize("zoom", [0, 5, 12, 15])
def test_tile_roundtrip(lon, lat, zoom):
    x, y = geo.lonlat_to_tile(lon, lat, zoom)
    back_lon, back_lat = geo.tile_to_lonlat(x, y, zoom)
    assert back_lon == pytest.approx(lon, abs=1e-9)
    assert back_lat == pytest.approx(lat, abs=1e-9)


def test_tile_origin_is_northwest():
    """Tile (0, 0) is the north-west corner of the world."""
    lon, lat = geo.tile_to_lonlat(0, 0, 0)
    assert lon == pytest.approx(-180.0)
    assert lat == pytest.approx(geo.MERCATOR_MAX_LAT, abs=1e-6)


def test_tile_y_increases_southward():
    _, north_y = geo.lonlat_to_tile(0.0, 60.0, 8)
    _, south_y = geo.lonlat_to_tile(0.0, -60.0, 8)
    assert north_y < south_y


def test_latitude_is_clamped_to_mercator_range():
    _, y = geo.lonlat_to_tile(0.0, 89.9, 4)
    _, y_limit = geo.lonlat_to_tile(0.0, geo.MERCATOR_MAX_LAT, 4)
    assert y == pytest.approx(y_limit)


def test_resolution_halves_each_zoom():
    at10 = geo.tile_resolution_m(10, 0.0)
    at11 = geo.tile_resolution_m(11, 0.0)
    assert at11 == pytest.approx(at10 / 2.0)


def test_equator_resolution_is_about_156km_per_tile():
    # The textbook figure: ~156.5 km per pixel at zoom 0.
    assert geo.tile_resolution_m(0, 0.0) == pytest.approx(156543.0, rel=1e-4)


@pytest.mark.parametrize("target,lat", [
    (16.0, 0.0), (16.0, 52.0), (32.0, 52.0), (4.0, 10.0), (64.0, -33.0),
])
def test_zoom_for_resolution_is_fine_enough(target, lat):
    zoom = geo.zoom_for_resolution(target, lat, max_zoom=20)
    assert geo.tile_resolution_m(zoom, lat) <= target
    # ...and it is the *smallest* such zoom, so we never over-download.
    if zoom > 0:
        assert geo.tile_resolution_m(zoom - 1, lat) > target


def test_zoom_is_capped():
    assert geo.zoom_for_resolution(0.01, 0.0, max_zoom=14) == 14


def test_zoom_rejects_nonsense_spacing():
    with pytest.raises(geo.GeoImportError):
        geo.zoom_for_resolution(0.0, 0.0)


# --- local metric frame ---------------------------------------------------

def test_metres_per_degree_matches_known_values():
    # Standard WGS84 figures.
    assert geo.metres_per_degree_lat(0.0) == pytest.approx(110574, abs=30)
    assert geo.metres_per_degree_lat(90.0) == pytest.approx(111694, abs=30)
    assert float(geo.metres_per_degree_lon(0.0)) == pytest.approx(111320, abs=30)
    assert float(geo.metres_per_degree_lon(60.0)) == pytest.approx(55800, abs=60)


def test_local_offsets_move_the_right_way():
    lon, lat = geo.local_offsets_to_lonlat(52.0, 5.0, east_m=1000.0, north_m=2000.0)
    assert lat > 52.0
    assert lon > 5.0
    lon, lat = geo.local_offsets_to_lonlat(52.0, 5.0, east_m=-1000.0, north_m=-2000.0)
    assert lat < 52.0
    assert lon < 5.0


def test_local_offsets_are_metrically_square():
    """A square in local metres stays square on the ground.

    This is the bug that sampling Web Mercator directly would introduce.
    """
    center_lat, center_lon = 60.0, 10.0
    side = 10000.0
    lon, lat = geo.local_offsets_to_lonlat(
        center_lat, center_lon,
        east_m=np.array([0.0, side]), north_m=np.array([0.0, 0.0]))
    east_span_m = (lon[1] - lon[0]) * float(geo.metres_per_degree_lon(center_lat))
    lon2, lat2 = geo.local_offsets_to_lonlat(
        center_lat, center_lon,
        east_m=np.array([0.0, 0.0]), north_m=np.array([0.0, side]))
    north_span_m = (lat2[1] - lat2[0]) * geo.metres_per_degree_lat(center_lat)
    assert east_span_m == pytest.approx(north_span_m, rel=1e-3)
    assert east_span_m == pytest.approx(side, rel=1e-3)


def test_longitude_scaling_follows_each_row():
    """East-west spacing is computed per row, not once at the centre."""
    req = request(tiles_x=4, tiles_y=4, metres_per_cell=64.0, center_lat=60.0)
    lon, lat = geo.grid_lonlat(req)
    top_span = lon[0, -1] - lon[0, 0]
    bottom_span = lon[-1, -1] - lon[-1, 0]
    # Closer to the pole, the same ground distance spans more longitude.
    assert top_span > bottom_span


# --- terrarium ------------------------------------------------------------


@pytest.mark.parametrize("elevation", [-500.0, -1.0, 0.0, 1.0, 250.5, 3000.0, 8000.0])
def test_terrarium_roundtrip(elevation):
    data = constant_tile(elevation)
    with Image.open(io.BytesIO(data)) as img:
        decoded = geo.decode_terrarium(np.asarray(img.convert("RGB")))
    assert decoded[0, 0] == pytest.approx(elevation, abs=1.0 / 256)


def test_terrarium_known_encoding():
    """Sea level is the documented (128, 0, 0) midpoint."""
    rgb = np.array([[[128, 0, 0]]], dtype=np.uint8)
    assert geo.decode_terrarium(rgb)[0, 0] == pytest.approx(0.0)


def test_decode_rejects_non_rgb():
    with pytest.raises(geo.GeoImportError):
        geo.decode_terrarium(np.zeros((4, 4), dtype=np.uint8))


# --- grid geometry --------------------------------------------------------


@pytest.mark.parametrize("tiles_x,tiles_y", [(1, 1), (2, 2), (4, 4), (3, 5)])
def test_grid_shape_is_cells_plus_one(tiles_x, tiles_y):
    req = request(tiles_x=tiles_x, tiles_y=tiles_y)
    rows, cols = req.grid_shape
    assert cols == tiles_x * 64 + 1
    assert rows == tiles_y * 64 + 1


def test_grid_row_zero_is_north():
    req = request(tiles_x=2, tiles_y=2)
    lon, lat = geo.grid_lonlat(req)
    assert lat[0, 0] > lat[-1, 0]
    assert lon[0, 0] < lon[0, -1]


def test_grid_is_centred_on_the_request():
    req = request(tiles_x=2, tiles_y=2)
    lon, lat = geo.grid_lonlat(req)
    rows, cols = req.grid_shape
    assert lat[rows // 2, cols // 2] == pytest.approx(req.center_lat, abs=1e-9)
    assert lon[rows // 2, cols // 2] == pytest.approx(req.center_lon, abs=1e-9)


def test_footprint_matches_metres_per_cell():
    req = request(tiles_x=4, tiles_y=2, metres_per_cell=16.0)
    assert req.tiles_x * 64 * req.metres_per_cell == pytest.approx(4096.0)
    lon, lat = geo.grid_lonlat(req)
    north_span = (lat[0, 0] - lat[-1, 0]) * geo.metres_per_degree_lat(req.center_lat)
    assert north_span == pytest.approx(2 * 64 * 16.0, rel=1e-3)


def test_rotation_pivots_about_the_centre():
    rows, cols = request(tiles_x=2, tiles_y=2).grid_shape
    straight = geo.grid_lonlat(request(tiles_x=2, tiles_y=2))
    turned = geo.grid_lonlat(request(tiles_x=2, tiles_y=2, rotation_deg=90.0))
    assert turned[0][rows // 2, cols // 2] == pytest.approx(straight[0][rows // 2, cols // 2])
    assert turned[1][rows // 2, cols // 2] == pytest.approx(straight[1][rows // 2, cols // 2])


def test_quarter_turn_maps_northwest_onto_northeast():
    """On a square region a 90 degree turn takes one corner to the next."""
    straight = geo.grid_lonlat(request(tiles_x=2, tiles_y=2))
    turned = geo.grid_lonlat(request(tiles_x=2, tiles_y=2, rotation_deg=90.0))
    assert turned[0][0, 0] == pytest.approx(straight[0][0, -1])
    assert turned[1][0, 0] == pytest.approx(straight[1][0, -1])


def test_rotation_actually_moves_the_corners():
    straight = geo.grid_lonlat(request(tiles_x=2, tiles_y=2))
    turned = geo.grid_lonlat(request(tiles_x=2, tiles_y=2, rotation_deg=30.0))
    moved = (turned[0][0, 0] != pytest.approx(straight[0][0, 0])
             or turned[1][0, 0] != pytest.approx(straight[1][0, 0]))
    assert moved


# --- validation -----------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"center_lat": 91.0},
    {"center_lat": -91.0},
    {"center_lon": 181.0},
    {"center_lat": 88.0},          # inside lat range, outside Mercator
    {"tiles_x": 0},
    {"tiles_y": -1},
    {"metres_per_cell": 0.0},
    {"vertical_scale": 0.0},
])
def test_invalid_requests_are_rejected(kwargs):
    with pytest.raises(geo.GeoImportError):
        request(**kwargs).validate()


def test_valid_request_passes():
    request().validate()


# --- sampling -------------------------------------------------------------


def test_constant_terrain_samples_flat():
    fetcher = ConstantFetcher(123.0)
    elevation, zoom, fetched, missing = geo.sample_elevation(request(), fetcher)
    assert elevation.shape == request().grid_shape
    assert missing == 0
    assert fetched > 0
    assert np.allclose(elevation, 123.0, atol=1.0 / 128)


def test_sampling_reproduces_a_known_ramp():
    """Sampled values match the ramp evaluated at the same coordinates.

    This pins the bilinear interpolation *and* the half-pixel centre
    convention: getting the latter wrong biases every sample by half a
    source pixel, which at zoom 13 is about ten metres on the ground.
    """
    req = request(tiles_x=1, tiles_y=1, metres_per_cell=16.0)
    zoom = geo.zoom_for_resolution(req.metres_per_cell, req.center_lat,
                                   max_zoom=req.max_zoom)
    fetcher = RampFetcher(origin_px=mosaic_origin_px(req, zoom),
                          metres_per_pixel=1.0)
    elevation, used_zoom, _, _ = geo.sample_elevation(req, fetcher)
    assert used_zoom == zoom

    lon, lat = geo.grid_lonlat(req)
    tx, _ = geo.lonlat_to_tile(lon, lat, zoom)
    # Pixel centres sit at +0.5, so a sample at continuous coordinate p
    # reads the ramp at p - 0.5.
    expected = fetcher.elevation_at(tx * geo.TILE_PIXELS - 0.5)
    assert np.allclose(elevation, expected, atol=0.01)


def test_ramp_increases_eastward():
    req = request()
    zoom = geo.zoom_for_resolution(req.metres_per_cell, req.center_lat,
                                   max_zoom=req.max_zoom)
    fetcher = RampFetcher(origin_px=mosaic_origin_px(req, zoom))
    elevation, _, _, _ = geo.sample_elevation(req, fetcher)
    assert (np.diff(elevation, axis=1) > 0).all()


def test_missing_tiles_are_treated_as_sea():
    class PartialFetcher:
        def __init__(self):
            self.data = constant_tile(100.0)
            self.seen = []

        def fetch(self, zoom, x, y):
            self.seen.append((zoom, x, y))
            return self.data if len(self.seen) == 1 else None

    req = request(tiles_x=4, tiles_y=4, metres_per_cell=16.0)
    elevation, _, fetched, missing = geo.sample_elevation(req, PartialFetcher())
    assert fetched == 1
    assert missing > 0
    assert float(np.min(elevation)) == pytest.approx(0.0, abs=1e-3)


def test_no_data_at_all_is_an_error():
    with pytest.raises(geo.GeoImportError, match="No elevation data"):
        geo.sample_elevation(request(), geo.DictTileFetcher())


def test_oversized_mosaic_is_refused():
    # A huge region at fine resolution would need an unreasonable download.
    req = request(tiles_x=64, tiles_y=64, metres_per_cell=1.0, max_zoom=20)
    with pytest.raises(geo.GeoImportError, match="elevation tiles"):
        geo.sample_elevation(req, ConstantFetcher(0.0))


def test_explicit_zoom_is_honoured():
    fetcher = ConstantFetcher(10.0)
    _, zoom, _, _ = geo.sample_elevation(request(zoom=9), fetcher)
    assert zoom == 9
    assert all(z == 9 for z, _, _ in fetcher.requests)


def test_zoom_adapts_to_scale_so_downloads_stay_bounded():
    fine = ConstantFetcher(0.0)
    coarse = ConstantFetcher(0.0)
    geo.sample_elevation(request(tiles_x=4, tiles_y=4, metres_per_cell=16.0), fine)
    geo.sample_elevation(request(tiles_x=4, tiles_y=4, metres_per_cell=128.0), coarse)
    # Eight times the ground area, but a comparable number of tiles.
    assert len(coarse.requests) <= len(fine.requests) * 2


def test_progress_is_reported():
    seen = []
    geo.sample_elevation(request(), ConstantFetcher(0.0),
                         progress=lambda done, total, msg: seen.append((done, total)))
    assert seen
    assert seen[-1][0] == seen[-1][1]


# --- height mapping -------------------------------------------------------


def test_sea_level_maps_to_250m():
    heights, clamped = geo.elevation_to_height_dm(np.array([[0.0]]))
    assert heights[0, 0] == 2500
    assert clamped == 0


def test_elevation_maps_linearly():
    heights, _ = geo.elevation_to_height_dm(np.array([[0.0, 100.0, 500.0]]))
    assert list(heights[0]) == [2500, 3500, 7500]


def test_vertical_scale_exaggerates():
    heights, _ = geo.elevation_to_height_dm(np.array([[100.0]]), vertical_scale=2.0)
    assert heights[0, 0] == 4500  # 250 + 100*2 = 450 m


def test_vertical_scale_can_compress():
    heights, _ = geo.elevation_to_height_dm(np.array([[1000.0]]), vertical_scale=0.5)
    assert heights[0, 0] == 7500  # 250 + 500 = 750 m


def test_ocean_is_flattened_to_a_shelf_by_default():
    heights, _ = geo.elevation_to_height_dm(
        np.array([[-4000.0, -5.0, 0.0]]), ocean_depth_m=20.0)
    assert heights[0, 0] == heights[0, 1] == 2300  # 230 m, a shallow shelf
    assert heights[0, 2] == 2500


def test_bathymetry_can_be_kept():
    heights, _ = geo.elevation_to_height_dm(
        np.array([[-100.0]]), keep_bathymetry=True)
    assert heights[0, 0] == 1500  # 250 - 100 = 150 m


def test_sea_reference_shifts_the_shoreline():
    """An inland basin can be imported with its own water datum."""
    heights, _ = geo.elevation_to_height_dm(
        np.array([[400.0]]), sea_reference_m=400.0)
    assert heights[0, 0] == 2500


def test_heights_are_clamped_and_counted():
    heights, clamped = geo.elevation_to_height_dm(
        np.array([[8848.0]]), vertical_scale=1.0)
    assert clamped == 1
    assert heights[0, 0] == 65535


def test_result_dtype_is_uint16():
    heights, _ = geo.elevation_to_height_dm(np.zeros((5, 5)))
    assert heights.dtype == np.uint16


def test_suggested_scale_fits_everest():
    scale = geo.suggested_vertical_scale(8848.0)
    heights, clamped = geo.elevation_to_height_dm(
        np.array([[8848.0]]), vertical_scale=scale)
    assert clamped == 0
    assert scale < 1.0


def test_suggested_scale_leaves_normal_terrain_alone():
    assert geo.suggested_vertical_scale(1200.0) == 1.0


# --- vertical scale -------------------------------------------------------


@pytest.mark.parametrize("metres,expected", [
    (16.0, 1.0), (32.0, 0.5), (48.0, 1.0 / 3.0), (8.0, 2.0), (64.0, 0.25),
])
def test_isotropic_scale_inverts_the_horizontal_squeeze(metres, expected):
    assert geo.isotropic_vertical_scale(metres) == pytest.approx(expected)


def test_isotropic_scale_rejects_nonsense():
    with pytest.raises(geo.GeoImportError):
        geo.isotropic_vertical_scale(0.0)


def test_match_mode_keeps_slopes_realistic():
    """The headline reason the mode exists.

    Importing at 48 m per cell squeezes the ground threefold. Left at true
    elevation the slopes come out three times too steep; matching the
    horizontal scale brings them back to what the ground actually does.
    """
    elevation = np.zeros((129, 129), dtype=np.float32)
    elevation[:] = np.linspace(0, 480, 129)  # a real 10% grade over 4.8 km

    fine = geo.GeoImportRequest(center_lat=0.0, center_lon=0.0, tiles_x=2,
                                tiles_y=2, metres_per_cell=48.0,
                                vertical_mode="match")
    true = geo.GeoImportRequest(center_lat=0.0, center_lon=0.0, tiles_x=2,
                                tiles_y=2, metres_per_cell=48.0,
                                vertical_mode="true")

    matched, _ = geo.elevation_to_height_dm(
        elevation, vertical_scale=fine.effective_vertical_scale())
    untouched, _ = geo.elevation_to_height_dm(
        elevation, vertical_scale=true.effective_vertical_scale())

    steep = geo.slope_statistics(untouched)["max_grade"]
    realistic = geo.slope_statistics(matched)["max_grade"]
    # Not exactly threefold: heights are stored in whole decimetres, so each
    # rounds slightly differently. The factor is what matters.
    assert steep / realistic == pytest.approx(3.0, rel=0.05)


@pytest.mark.parametrize("mode,metres,expected", [
    ("match", 48.0, 1.0 / 3.0),
    ("true", 48.0, 1.0),
    ("manual", 48.0, 2.5),
])
def test_effective_vertical_scale(mode, metres, expected):
    req = request(metres_per_cell=metres, vertical_mode=mode, vertical_scale=2.5)
    assert req.effective_vertical_scale() == pytest.approx(expected)


def test_unknown_vertical_mode_is_rejected():
    with pytest.raises(geo.GeoImportError):
        request(vertical_mode="sideways").validate()


def test_pipeline_applies_the_resolved_scale():
    req = request(tiles_x=1, tiles_y=1, metres_per_cell=32.0,
                  vertical_mode="match")
    result = geo.build_region_grid(req, ConstantFetcher(100.0))
    # 250 m sea level + 100 m halved = 300 m
    assert result.height_dm[0, 0] == 3000
    assert result.georeference.vertical_scale == pytest.approx(0.5)


# --- water datum ----------------------------------------------------------


def test_sea_datum_is_real_sea_level():
    req = request(water_datum_mode="sea", sea_reference_m=123.0)
    assert req.effective_water_datum(np.array([[500.0]])) == 0.0


def test_manual_datum_is_used_as_given():
    req = request(water_datum_mode="manual", sea_reference_m=559.0)
    assert req.effective_water_datum(np.array([[600.0]])) == 559.0


def test_lowest_datum_sits_under_the_lowest_ground():
    """So an inland region imports as dry land rather than a lake."""
    req = request(water_datum_mode="lowest")
    elevation = np.array([[553.0, 700.0], [600.0, 4109.0]])
    assert req.effective_water_datum(elevation) < 553.0


def test_lowest_datum_falls_back_before_sampling():
    req = request(water_datum_mode="lowest", sea_reference_m=42.0)
    assert req.effective_water_datum(None) == 42.0


def test_unknown_datum_mode_is_rejected():
    with pytest.raises(geo.GeoImportError):
        request(water_datum_mode="puddle").validate()


def test_alpine_valley_floats_above_the_shoreline_by_default():
    """The Interlaken problem: real sea level puts the whole valley on high
    ground, so its lakes come out as land."""
    result = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1), ConstantFetcher(560.0))
    assert result.water_fraction == 0.0
    assert result.height_dm.min() > 2500  # everything above SC4's shoreline


def test_datum_brings_an_alpine_lake_back_to_the_shoreline():
    result = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1, water_datum_mode="manual",
                sea_reference_m=560.0),
        ConstantFetcher(560.0))
    assert result.height_dm.max() == 2500  # exactly at sea level
    assert result.georeference.sea_reference_m == 560.0


def test_lowest_datum_keeps_land_below_sea_level_dry():
    """The Amsterdam problem: polders sit below real sea level but are land."""
    flooded = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1), ConstantFetcher(-5.0))
    assert flooded.water_fraction == 1.0

    dry = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1, water_datum_mode="lowest"),
        ConstantFetcher(-5.0))
    assert dry.water_fraction == 0.0
    assert dry.height_dm.min() >= 2500


def test_water_fraction_counts_submerged_vertices():
    result = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1), ConstantFetcher(-30.0))
    assert result.water_fraction == pytest.approx(1.0)


def test_datum_is_recorded_on_the_georeference():
    result = geo.build_region_grid(
        request(tiles_x=1, tiles_y=1, water_datum_mode="lowest"),
        ConstantFetcher(400.0))
    assert result.georeference.sea_reference_m == pytest.approx(399.0)
    restored = geo.GeoReference(**result.georeference.to_dict())
    assert restored == result.georeference


def test_summary_reports_the_shoreline_and_water():
    result = geo.build_region_grid(request(tiles_x=1, tiles_y=1),
                                   ConstantFetcher(-5.0))
    text = result.summary()
    assert "shoreline at" in text
    assert "under water" in text


# --- slope statistics -----------------------------------------------------


def test_flat_ground_has_no_slope():
    flat = np.full((10, 10), 2500, dtype=np.uint16)
    stats = geo.slope_statistics(flat)
    assert stats["max_grade"] == 0.0
    assert stats["steep_fraction"] == 0.0


def test_slope_is_measured_over_the_16m_cell():
    """A 1.6 m rise across one cell is a 10% grade, whatever the import scale."""
    ramp = np.arange(0, 10, dtype=np.float64) * 16.0 + 2500  # 1.6 m per cell
    grid = np.tile(ramp, (4, 1)).astype(np.uint16)
    stats = geo.slope_statistics(grid)
    assert stats["max_grade"] == pytest.approx(0.10)
    assert stats["p95_grade"] == pytest.approx(0.10)
    assert stats["steep_fraction"] == 0.0  # 10% is under the 15% warning line


def test_steep_fraction_counts_edges_over_the_threshold():
    grid = np.full((4, 4), 2500, dtype=np.uint16)
    grid[:, 2:] = 2500 + 400  # a 40 m step = 250% grade
    stats = geo.slope_statistics(grid)
    assert stats["max_grade"] > geo.STEEP_GRADE
    assert 0.0 < stats["steep_fraction"] < 1.0


def test_slope_statistics_survive_a_single_row():
    stats = geo.slope_statistics(np.full((1, 1), 2500, dtype=np.uint16))
    assert stats["max_grade"] == 0.0


# --- despiking ------------------------------------------------------------


def test_despike_removes_an_isolated_artifact():
    """Global DEM mosaics carry junk pixels; one must not spike the terrain."""
    elevation = np.full((32, 32), 50.0, dtype=np.float32)
    elevation[16, 16] = 6431.0  # the kind of value the real HK tile holds
    cleaned, count = geo.despike_elevation(elevation)
    assert count == 1
    assert cleaned[16, 16] == pytest.approx(50.0)
    assert cleaned.max() == pytest.approx(50.0)


def test_despike_leaves_real_terrain_alone():
    """A steep but genuine slope must survive untouched."""
    ramp = np.tile(np.linspace(0, 1500, 64), (64, 1)).astype(np.float32)
    cleaned, count = geo.despike_elevation(ramp)
    assert count == 0
    assert np.allclose(cleaned, ramp)


def test_despike_preserves_a_cliff():
    elevation = np.full((32, 32), 10.0, dtype=np.float32)
    elevation[:, 16:] = 150.0  # a 140 m cliff, under the threshold
    cleaned, count = geo.despike_elevation(elevation)
    assert count == 0
    assert cleaned[0, 20] == pytest.approx(150.0)


def test_despike_can_be_switched_off():
    elevation = np.full((8, 8), 10.0, dtype=np.float32)
    elevation[4, 4] = 9000.0
    cleaned, count = geo.despike_elevation(elevation, threshold_m=0)
    assert count == 0
    assert cleaned[4, 4] == pytest.approx(9000.0)


def test_despike_ignores_tiny_grids():
    tiny = np.array([[1.0, 2.0], [3.0, 9000.0]], dtype=np.float32)
    cleaned, count = geo.despike_elevation(tiny)
    assert count == 0


def test_pipeline_reports_despiked_vertices():
    class SpikyFetcher(ConstantFetcher):
        def __init__(self):
            super().__init__(0.0)
            grid = np.full((geo.TILE_PIXELS, geo.TILE_PIXELS), 20.0)
            grid[128, 128] = 6000.0
            self._data = encode_terrarium(grid)

    result = geo.build_region_grid(request(), SpikyFetcher())
    assert result.despiked_vertices >= 0
    assert result.max_elevation_m < 1000.0


# --- the whole pipeline ---------------------------------------------------


def test_build_region_grid_produces_a_region_sized_array():
    req = request(tiles_x=2, tiles_y=2, metres_per_cell=16.0)
    result = geo.build_region_grid(req, ConstantFetcher(300.0))
    assert result.height_dm.shape == req.grid_shape
    assert result.height_dm.dtype == np.uint16
    assert result.height_dm[0, 0] == 5500  # 250 + 300 m
    assert result.georeference.width_m == pytest.approx(2048.0)
    assert result.max_elevation_m == pytest.approx(300.0, abs=0.01)
    assert "Centre" in result.summary()


def test_georeference_survives_a_roundtrip_through_a_dict():
    result = geo.build_region_grid(request(), ConstantFetcher(0.0))
    data = result.georeference.to_dict()
    restored = geo.GeoReference(**data)
    assert restored == result.georeference


def test_georeference_records_the_scale():
    result = geo.build_region_grid(
        request(tiles_x=4, tiles_y=2, metres_per_cell=32.0), ConstantFetcher(0.0))
    geo_ref = result.georeference
    assert geo_ref.metres_per_cell == 32.0
    assert geo_ref.width_m == pytest.approx(4 * 64 * 32.0)
    assert geo_ref.height_m == pytest.approx(2 * 64 * 32.0)


def test_pipeline_records_the_actual_elevation_source():
    fetcher = ConstantFetcher(0.0)
    fetcher.url_template = "https://terrain.example/{z}/{x}/{y}.png"
    fetcher.attribution = "Example Terrain"
    result = geo.build_region_grid(request(), fetcher)
    assert result.georeference.source == fetcher.url_template
    assert result.attribution == "Example Terrain"


def test_summary_mentions_clamping():
    result = geo.build_region_grid(request(), ConstantFetcher(20000.0))
    assert result.clamped_vertices > 0
    assert "clamped" in result.summary()


# --- city tile layout -----------------------------------------------------


@pytest.mark.parametrize("size,preferred", [
    ((4, 4), 4), ((8, 8), 4), ((5, 5), 4), ((3, 7), 2), ((6, 6), 1), ((1, 1), 4),
])
def test_layout_covers_the_whole_footprint(size, preferred):
    image = geo.build_config_image(size, preferred)
    pixels = np.asarray(image)
    void = np.all(pixels == np.array(geo.CONFIG_VOID, dtype=np.uint8), axis=-1)
    assert not void.any(), "layout left a hole"


def test_layout_uses_requested_city_size():
    counts = geo.describe_layout((8, 8), preferred=4)
    assert counts[4] == 4
    assert counts[2] == 0 and counts[1] == 0


def test_layout_falls_back_for_leftovers():
    # 5x5 fits one large city; the L-shaped remainder needs smaller ones.
    counts = geo.describe_layout((5, 5), preferred=4)
    assert counts[4] == 1
    assert counts[2] + counts[1] > 0
    assert counts[4] * 16 + counts[2] * 4 + counts[1] == 25


def test_layout_honours_a_smaller_preference():
    counts = geo.describe_layout((8, 8), preferred=1)
    assert counts[1] == 64
    assert counts[4] == 0 and counts[2] == 0

    counts = geo.describe_layout((8, 8), preferred=2)
    assert counts[2] == 16
    assert counts[4] == 0


@pytest.mark.parametrize("size,preferred", [
    ((6, 4), 4), ((7, 3), 2), ((9, 9), 4), ((2, 11), 4),
])
def test_layout_area_always_adds_up(size, preferred):
    counts = geo.describe_layout(size, preferred)
    covered = counts[4] * 16 + counts[2] * 4 + counts[1]
    assert covered == size[0] * size[1]


def test_layout_colours_match_the_config_convention():
    pixels = np.asarray(geo.build_config_image((4, 4), 4))
    assert tuple(pixels[0, 0]) == geo.CONFIG_COLORS[4]
    pixels = np.asarray(geo.build_config_image((1, 1), 1))
    assert tuple(pixels[0, 0]) == geo.CONFIG_COLORS[1]


def verify_like_worktheconfig(image):
    """Re-implement ``region.WorkTheconfig``'s verification.

    That function walks the config in reading order and, on the first
    unclaimed blue or green pixel, asserts the whole 4x4 or 2x2 block from
    there is the same colour.  A layout that violates this makes the real
    importer raise, so mirroring the check here catches a bad layout without
    needing wx to import ``region``.

    Returns the city sizes found, in the order they were claimed.
    """
    pixels = np.asarray(image)
    height, width = pixels.shape[:2]
    claimed = np.zeros((height, width), dtype=bool)
    found = []

    def colour_of(x, y):
        red, green, blue = (int(v) for v in pixels[y, x][:3])
        if red > green and red > blue and red > 250:
            return 1
        if green > red and green > blue and green > 250:
            return 2
        if blue > red and blue > green and blue > 250:
            return 4
        return None

    for y in range(height):
        for x in range(width):
            if claimed[y, x]:
                continue
            size = colour_of(x, y)
            if size is None:
                continue
            assert x + size <= width and y + size <= height, \
                "city at %d,%d of size %d runs off the config" % (x, y, size)
            for dy in range(size):
                for dx in range(size):
                    assert colour_of(x + dx, y + dy) == size, \
                        "block at %d,%d is not uniformly size %d" % (x, y, size)
                    assert not claimed[y + dy, x + dx], "cities overlap"
                    claimed[y + dy, x + dx] = True
            found.append(size)
    return found


@pytest.mark.parametrize("size,preferred", [
    ((7, 5), 4), ((4, 4), 4), ((5, 5), 4), ((9, 3), 4), ((6, 6), 2),
    ((11, 7), 4), ((1, 1), 1), ((2, 3), 2),
])
def test_layout_is_readable_by_the_region_parser(size, preferred):
    found = verify_like_worktheconfig(geo.build_config_image(size, preferred))
    assert sum(s * s for s in found) == size[0] * size[1]


def test_verifier_rejects_a_broken_layout():
    """The verifier above must actually be able to fail."""
    broken = Image.new("RGB", (4, 4), geo.CONFIG_COLORS[4])
    broken.putpixel((3, 3), geo.CONFIG_COLORS[1])
    with pytest.raises(AssertionError):
        verify_like_worktheconfig(broken)


@pytest.mark.parametrize("size", [(0, 4), (4, 0), (-1, 3)])
def test_layout_rejects_empty_footprints(size):
    with pytest.raises(geo.GeoImportError):
        geo.build_config_image(size, 4)


def test_layout_rejects_unknown_city_size():
    with pytest.raises(geo.GeoImportError):
        geo.build_config_image((4, 4), 3)


# --- basemap underlay -----------------------------------------------------


class ImageryFetcher:
    """Serves a flat colour, or a per-tile colour keyed by tile x."""

    def __init__(self, color=(10, 120, 200), per_tile=False):
        self.color = color
        self.per_tile = per_tile
        self.requests = []

    def fetch(self, zoom, x, y):
        self.requests.append((zoom, x, y))
        color = (x % 256, self.color[1], self.color[2]) if self.per_tile else self.color
        rgb = np.zeros((geo.TILE_PIXELS, geo.TILE_PIXELS, 3), dtype=np.uint8)
        rgb[:, :] = color
        buffer = io.BytesIO()
        Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def test_basemap_matches_the_terrain_grid_exactly():
    """The underlay must line up pixel-for-pixel with the height grid."""
    req = request(tiles_x=2, tiles_y=2)
    rgb, _, _, _ = geo.sample_basemap(req, ImageryFetcher())
    elevation, _, _, _ = geo.sample_elevation(req, ConstantFetcher(0.0))
    assert rgb.shape[:2] == elevation.shape == req.grid_shape
    assert rgb.shape[2] == 3
    assert rgb.dtype == np.uint8


def test_basemap_preserves_colour():
    rgb, _, _, _ = geo.sample_basemap(request(), ImageryFetcher(color=(10, 120, 200)))
    assert (rgb[..., 0] == 10).all()
    assert (rgb[..., 1] == 120).all()
    assert (rgb[..., 2] == 200).all()


def test_basemap_and_elevation_use_the_same_zoom():
    req = request(tiles_x=2, tiles_y=2)
    _, map_zoom, _, _ = geo.sample_basemap(req, ImageryFetcher())
    _, dem_zoom, _, _ = geo.sample_elevation(req, ConstantFetcher(0.0))
    assert map_zoom == dem_zoom


def test_basemap_requests_the_same_tiles_as_elevation():
    """Same grid, same projection, so the two sources agree on coverage."""
    req = request(tiles_x=2, tiles_y=2)
    imagery = ImageryFetcher()
    elevation = ConstantFetcher(0.0)
    geo.sample_basemap(req, imagery)
    geo.sample_elevation(req, elevation)
    assert imagery.requests == elevation.requests


def test_basemap_varies_across_tiles():
    req = request(tiles_x=4, tiles_y=4, metres_per_cell=64.0)
    rgb, _, _, _ = geo.sample_basemap(req, ImageryFetcher(per_tile=True))
    assert len(np.unique(rgb[..., 0])) > 1


def test_basemap_missing_tiles_are_white():
    class OneTileFetcher(ImageryFetcher):
        def fetch(self, zoom, x, y):
            data = super().fetch(zoom, x, y)
            return data if len(self.requests) == 1 else None

    req = request(tiles_x=4, tiles_y=4, metres_per_cell=16.0)
    rgb, _, fetched, missing = geo.sample_basemap(req, OneTileFetcher())
    assert fetched == 1 and missing > 0
    assert (rgb == 255).any()


def test_basemap_with_no_data_is_an_error():
    with pytest.raises(geo.GeoImportError, match="No map data"):
        geo.sample_basemap(request(), geo.DictTileFetcher())


def test_missing_provider_response_explains_the_bad_url():
    fetcher = geo.DictTileFetcher()
    fetcher.last_missing = (404, "https://tiles.example/12/3/4.png")
    with pytest.raises(geo.GeoImportError, match=(
            r"HTTP 404.*tiles\.example.*check the tile URL")):
        geo.sample_basemap(request(), fetcher)


def test_no_basemap_is_configured_by_default():
    """Map tile providers each have their own terms, so nothing is presumed."""
    assert geo.BASEMAP_PRESETS == {}


def test_footprint_preview_uses_a_fixed_small_canvas():
    image, zoom, fetched, missing = geo.build_footprint_preview(
        request(tiles_x=8, tiles_y=6), ImageryFetcher(), size=(320, 240))
    pixels = np.asarray(image)
    assert image.size == (320, 240)
    assert zoom >= 0 and fetched > 0 and missing == 0
    assert np.any(np.all(pixels == (20, 55, 255), axis=-1))


def test_footprint_preview_makes_rotation_visible():
    straight, _, _, _ = geo.build_footprint_preview(
        request(tiles_x=8, tiles_y=4), ImageryFetcher(), size=(320, 240))
    rotated, _, _, _ = geo.build_footprint_preview(
        request(tiles_x=8, tiles_y=4, rotation_deg=35),
        ImageryFetcher(), size=(320, 240))
    assert not np.array_equal(np.asarray(straight), np.asarray(rotated))


def test_footprint_preview_can_fall_back_to_elevation():
    image, _, fetched, _ = geo.build_footprint_preview(
        request(), ConstantFetcher(120.0), imagery=False, size=(160, 120))
    assert image.mode == "RGB"
    assert image.size == (160, 120)
    assert fetched > 0


# --- locating -------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("52.3676, 4.9041", (52.3676, 4.9041)),
    ("52.3676,4.9041", (52.3676, 4.9041)),
    ("52.3676 4.9041", (52.3676, 4.9041)),
    ("-33.8688, 151.2093", (-33.8688, 151.2093)),
    ("  0, 0  ", (0.0, 0.0)),
    ("geo:52.3676,4.9041", (52.3676, 4.9041)),
    ("https://www.openstreetmap.org/#map=14/52.3676/4.9041",
     (52.3676, 4.9041)),
    ("https://www.google.com/maps/@52.3676,4.9041,14z", (52.3676, 4.9041)),
    ("https://www.google.com/maps/place/Amsterdam/@52.3676,4.9041,12z/data=x",
     (52.3676, 4.9041)),
    ("https://www.openstreetmap.org/?mlat=52.3676&mlon=4.9041", (52.3676, 4.9041)),
])
def test_parse_location(text, expected):
    result = geo.parse_location(text)
    assert result is not None
    assert result[0] == pytest.approx(expected[0])
    assert result[1] == pytest.approx(expected[1])


def test_parse_location_handles_dms():
    result = geo.parse_location("52°22'03.4\"N 4°54'14.8\"E")
    assert result is not None
    assert result[0] == pytest.approx(52.3676, abs=1e-3)
    assert result[1] == pytest.approx(4.9041, abs=1e-3)


def test_parse_location_handles_southern_and_western_dms():
    result = geo.parse_location("33°52'07\"S 151°12'33\"W")
    assert result is not None
    assert result[0] < 0
    assert result[1] < 0


@pytest.mark.parametrize("text", [
    "", None, "not a place", "999, 999", "1000.0, 2000.0",
])
def test_parse_location_rejects_nonsense(text):
    assert geo.parse_location(text) is None


def test_geocode_parses_results():
    payload = (b'[{"display_name": "Amsterdam, Netherlands", '
               b'"lat": "52.3727598", "lon": "4.8936041"}]')
    places = geo.geocode("Amsterdam", opener=lambda url: payload)
    assert len(places) == 1
    assert places[0].name.startswith("Amsterdam")
    assert places[0].lat == pytest.approx(52.3727598)


def test_geocode_skips_malformed_entries():
    payload = b'[{"display_name": "x"}, {"lat": "1.0", "lon": "2.0"}]'
    places = geo.geocode("x", opener=lambda url: payload)
    assert len(places) == 1
    assert places[0].lat == pytest.approx(1.0)


def test_geocode_ignores_blank_queries():
    assert geo.geocode("   ", opener=lambda url: b"[]") == []


def test_geocode_rejects_bad_json():
    with pytest.raises(geo.GeoImportError):
        geo.geocode("x", opener=lambda url: b"<html>nope</html>")


def test_geocode_sends_the_query():
    seen = {}

    def opener(url):
        seen["url"] = url
        return b"[]"

    geo.geocode("Gent, Belgium", opener=opener)
    assert "Gent" in seen["url"]
    assert "format=jsonv2" in seen["url"]


def test_geocode_labels_different_administrative_extents():
    payload = b'''[
      {"display_name":"Gent, Belgium","lat":"51.05","lon":"3.72",
       "category":"boundary","type":"administrative","addresstype":"city",
       "osm_type":"relation","importance":0.69,
       "boundingbox":["50.98","51.19","3.58","3.85"]},
      {"display_name":"Gent, Belgium","lat":"51.06","lon":"3.64",
       "category":"boundary","type":"administrative","addresstype":"county",
       "osm_type":"relation","importance":0.47,
       "boundingbox":["50.89","51.22","3.33","3.92"]}
    ]'''
    places = geo.geocode("Gent, Belgium", opener=lambda url: payload)
    assert len(places) == 2
    assert places[0].label.startswith("[City Boundary]")
    assert places[1].label.startswith("[County Boundary]")
    assert "approximately" in places[0].details()


def test_geocode_collapses_same_name_and_type():
    payload = b'''[
      {"display_name":"Example","lat":"1","lon":"2",
       "addresstype":"city","importance":0.3},
      {"display_name":"Example","lat":"1.1","lon":"2.1",
       "addresstype":"city","importance":0.8}
    ]'''
    places = geo.geocode("Example", opener=lambda url: payload)
    assert len(places) == 1
    assert places[0].lat == pytest.approx(1.1)


# --- fetcher plumbing -----------------------------------------------------


def test_dict_fetcher_records_requests():
    fetcher = geo.DictTileFetcher({(3, 1, 2): b"x"})
    assert fetcher.fetch(3, 1, 2) == b"x"
    assert fetcher.fetch(3, 9, 9) is None
    assert fetcher.requests == [(3, 1, 2), (3, 9, 9)]


def test_http_fetcher_uses_the_cache(tmp_path):
    fetcher = geo.HttpTileFetcher(cache_dir=str(tmp_path))
    cached = Path(fetcher._cache_path(7, 3, 5))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached-tile")

    # No network is configured in the test environment; a cache hit must not
    # attempt one.
    assert fetcher.fetch(7, 3, 5) == b"cached-tile"


def test_http_cache_is_namespaced_by_provider(tmp_path):
    first = geo.HttpTileFetcher(
        url_template="https://first.example/{z}/{x}/{y}.png",
        cache_dir=str(tmp_path))
    second = geo.HttpTileFetcher(
        url_template="https://second.example/{z}/{x}/{y}.png",
        cache_dir=str(tmp_path))
    assert first._cache_path(7, 3, 5) != second._cache_path(7, 3, 5)


def test_http_fetcher_builds_the_expected_url():
    fetcher = geo.HttpTileFetcher(url_template="https://example/{z}/{x}/{y}.png")
    assert fetcher.url_template.format(z=1, x=2, y=3) == "https://example/1/2/3.png"


def test_http_fetcher_expands_common_tile_placeholders():
    fetcher = geo.HttpTileFetcher(
        url_template="https://{s}.google.com/vt/lyrs={l}&x={x}&y={y}&z={z}")
    assert fetcher._tile_url(7, 1, 2) == (
        "https://mt3.google.com/vt/lyrs=m&x=1&y=2&z=7")


def test_http_fetcher_expands_generic_subdomains():
    fetcher = geo.HttpTileFetcher(
        url_template="https://{s}.tiles.example/{z}/{x}/{y}.png")
    assert fetcher._tile_url(7, 1, 2) == (
        "https://a.tiles.example/7/1/2.png")


def test_http_fetcher_explains_unknown_placeholders():
    fetcher = geo.HttpTileFetcher(
        url_template="https://tiles.example/{zoom}/{x}/{y}.png")
    with pytest.raises(geo.GeoImportError, match=r"unsupported placeholder \{zoom\}"):
        fetcher._tile_url(7, 1, 2)
