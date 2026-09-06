"""The OpenStreetMap water mask.

Overpass responses here are built to the documented ``out geom`` schema:
ways carry a ``geometry`` list of ``{lat, lon}``, and relation members carry
a ``role`` alongside their own geometry.
"""

import json
import os

import numpy as np
import pytest

from sc4mapper import geo


def request(**kwargs):
    params = dict(center_lat=52.0, center_lon=5.0, tiles_x=1, tiles_y=1)
    params.update(kwargs)
    return geo.GeoImportRequest(**params)


def square_around(request, half_cells):
    """A ring covering the middle ``2 * half_cells`` of a region."""
    rows, cols = request.grid_shape
    lon, lat = geo.grid_lonlat(request)
    mid_r, mid_c = rows // 2, cols // 2
    lo_r, hi_r = mid_r - half_cells, mid_r + half_cells
    lo_c, hi_c = mid_c - half_cells, mid_c + half_cells
    return [(lon[lo_r, lo_c], lat[lo_r, lo_c]),
            (lon[lo_r, hi_c], lat[lo_r, hi_c]),
            (lon[hi_r, hi_c], lat[hi_r, hi_c]),
            (lon[hi_r, lo_c], lat[hi_r, lo_c])]


def way(ring, tags=None):
    return {"type": "way", "id": 1, "tags": tags or {"natural": "water"},
            "geometry": [{"lat": p[1], "lon": p[0]} for p in ring]}


def relation(outer, inners=(), tags=None):
    members = [{"type": "way", "ref": 10, "role": "outer",
                "geometry": [{"lat": p[1], "lon": p[0]} for p in outer]}]
    for i, inner in enumerate(inners):
        members.append({"type": "way", "ref": 20 + i, "role": "inner",
                        "geometry": [{"lat": p[1], "lon": p[0]} for p in inner]})
    return {"type": "relation", "id": 2, "tags": tags or {"natural": "water"},
            "members": members}


# --- the query ------------------------------------------------------------


def test_query_uses_the_documented_bbox_order():
    """Overpass wants (south, west, north, east) -- latitude first."""
    query = geo.build_water_query((46.0, 7.0, 47.0, 8.0))
    assert "(46.000000,7.000000,47.000000,8.000000)" in query


def test_query_asks_for_json_and_geometry():
    query = geo.build_water_query((0, 0, 1, 1), timeout=45)
    assert query.startswith("[out:json][timeout:45];")
    assert query.rstrip().endswith("out geom;")


def test_query_covers_the_water_tags():
    query = geo.build_water_query((0, 0, 1, 1))
    for key, value in geo.WATER_AREA_TAGS:
        assert 'way["%s"="%s"]' % (key, value) in query
        assert 'relation["%s"="%s"]' % (key, value) in query


def test_query_is_a_union_block():
    query = geo.build_water_query((0, 0, 1, 1))
    assert "(\n" in query and ");" in query


def test_region_bbox_contains_the_whole_grid():
    req = request(tiles_x=2, tiles_y=2, metres_per_cell=32.0)
    south, west, north, east = geo.region_bbox(req)
    lon, lat = geo.grid_lonlat(req)
    assert south < lat.min() and north > lat.max()
    assert west < lon.min() and east > lon.max()


# --- parsing --------------------------------------------------------------


def test_parses_a_way():
    ring = [(5.0, 52.0), (5.1, 52.0), (5.1, 52.1)]
    outers, inners = geo.parse_overpass_water({"elements": [way(ring)]})
    assert len(outers) == 1 and not inners
    assert outers[0][0] == pytest.approx((5.0, 52.0))


def test_parses_a_multipolygon_with_a_hole():
    outer = [(5.0, 52.0), (5.2, 52.0), (5.2, 52.2), (5.0, 52.2)]
    inner = [(5.05, 52.05), (5.1, 52.05), (5.1, 52.1)]
    outers, inners = geo.parse_overpass_water(
        {"elements": [relation(outer, [inner])]})
    assert len(outers) == 1
    assert len(inners) == 1


def test_members_without_a_role_count_as_outer():
    outer = [(5.0, 52.0), (5.2, 52.0), (5.2, 52.2)]
    element = relation(outer)
    del element["members"][0]["role"]
    outers, inners = geo.parse_overpass_water({"elements": [element]})
    assert len(outers) == 1 and not inners


def test_degenerate_rings_are_dropped():
    outers, inners = geo.parse_overpass_water(
        {"elements": [way([(5.0, 52.0), (5.1, 52.0)])]})
    assert not outers and not inners


@pytest.mark.parametrize("data", [
    None, {}, {"elements": []}, {"elements": None}, {"elements": [None, 3]},
    {"elements": [{"type": "node", "lat": 1, "lon": 2}]},
    {"elements": [{"type": "way"}]},
    {"elements": [{"type": "relation", "members": None}]},
    "not a dict",
])
def test_parser_survives_junk(data):
    outers, inners = geo.parse_overpass_water(data)
    assert outers == [] and inners == []


# --- rasterising ----------------------------------------------------------


def test_rings_land_on_the_right_cells():
    req = request()
    mask = geo.rasterize_water(req, [square_around(req, 8)])
    rows, cols = req.grid_shape
    assert mask.shape == (rows, cols)
    assert mask[rows // 2, cols // 2]          # the middle is wet
    assert not mask[0, 0]                       # the corners are not
    assert not mask[-1, -1]


def test_holes_are_punched_back_out():
    req = request()
    rows, cols = req.grid_shape
    mask = geo.rasterize_water(req, [square_around(req, 16)],
                               [square_around(req, 4)])
    assert not mask[rows // 2, cols // 2]      # island in the middle
    assert mask[rows // 2, cols // 2 + 10]     # water around it


def test_empty_input_gives_dry_land():
    mask = geo.rasterize_water(request(), [])
    assert not mask.any()


def test_mask_follows_rotation():
    """The mask is projected through the same frame as the terrain."""
    straight = request(tiles_x=2, tiles_y=2)
    ring = square_around(straight, 20)
    turned = request(tiles_x=2, tiles_y=2, rotation_deg=45.0)
    a = geo.rasterize_water(straight, [ring])
    b = geo.rasterize_water(turned, [ring])
    assert a.any() and b.any()
    assert not np.array_equal(a, b)


# --- applying the mask ----------------------------------------------------


def make_heights(value_m, shape=(16, 16)):
    return np.full(shape, int(value_m * 10), dtype=np.uint16)


def test_masked_ground_is_pushed_under_the_waterline():
    heights = make_heights(300.0)
    mask = np.zeros(heights.shape, dtype=bool)
    mask[4:8, 4:8] = True
    out, wet, lifted = geo.apply_water_mask(heights, mask, water_depth_m=3.0)
    assert out[5, 5] == 2470          # 250 - 3 m
    assert out[0, 0] == 3000          # untouched land
    assert wet == 16


def test_unmasked_ground_is_lifted_clear_of_the_waterline():
    """The polder rule: dry land that really is below sea level."""
    heights = make_heights(245.0)     # 5 m under SC4's shoreline
    mask = np.zeros(heights.shape, dtype=bool)
    out, wet, lifted = geo.apply_water_mask(heights, mask)
    assert (out >= 2500).all()
    assert lifted == heights.size


def test_lifting_can_be_switched_off():
    heights = make_heights(245.0)
    mask = np.zeros(heights.shape, dtype=bool)
    out, wet, lifted = geo.apply_water_mask(heights, mask, lift_land=False)
    assert lifted == 0
    assert (out == 2450).all()


def test_deep_water_is_left_deep():
    """Ocean already below the bed should not be raised to it."""
    heights = make_heights(100.0)
    mask = np.ones(heights.shape, dtype=bool)
    out, _, _ = geo.apply_water_mask(heights, mask, water_depth_m=3.0)
    assert (out == 1000).all()


def test_mask_shape_must_match():
    with pytest.raises(geo.GeoImportError):
        geo.apply_water_mask(make_heights(300.0), np.zeros((4, 4), dtype=bool))


def test_result_stays_uint16():
    out, _, _ = geo.apply_water_mask(make_heights(300.0),
                                     np.zeros((16, 16), dtype=bool))
    assert out.dtype == np.uint16


# --- through the pipeline -------------------------------------------------


class ConstantFetcher:
    def __init__(self, elevation_m):
        import io
        from PIL import Image
        value = float(elevation_m) + 32768.0
        red = int(value // 256)
        green = int(value - red * 256)
        blue = int(round((value - red * 256 - green) * 256)) % 256
        rgb = np.zeros((geo.TILE_PIXELS, geo.TILE_PIXELS, 3), dtype=np.uint8)
        rgb[:, :] = (red, green, blue)
        buffer = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buffer, format="PNG")
        self._data = buffer.getvalue()

    def fetch(self, zoom, x, y):
        return self._data


def test_elevation_source_ignores_the_mask():
    req = request(water_source="elevation")
    mask = np.ones(req.grid_shape, dtype=bool)
    result = geo.build_region_grid(req, ConstantFetcher(100.0), water_mask=mask)
    assert result.water_cells == 0
    assert result.water_fraction == 0.0


def test_mask_source_overrides_elevation_entirely():
    """Amsterdam in miniature: ground below sea level that must stay dry."""
    req = request(water_source="mask")
    mask = np.zeros(req.grid_shape, dtype=bool)
    mask[10:20, 10:20] = True
    result = geo.build_region_grid(req, ConstantFetcher(-5.0), water_mask=mask)
    assert result.lifted_cells > 0
    # Only the mapped water is wet; the rest is dry despite being below zero.
    assert result.water_fraction == pytest.approx(100 / result.height_dm.size,
                                                  rel=0.05)


def test_both_source_keeps_the_sea_and_adds_mapped_water():
    """San Francisco in miniature: the ocean must survive the mask."""
    req = request(water_source="both")
    mask = np.zeros(req.grid_shape, dtype=bool)
    result = geo.build_region_grid(req, ConstantFetcher(-30.0), water_mask=mask)
    # Below the datum, so the sea is still there even with an empty mask.
    assert result.water_fraction == pytest.approx(1.0)


def test_mask_makes_a_lake_out_of_high_ground():
    """Interlaken in miniature: a lake 560 m up, with no datum tuning."""
    req = request(water_source="mask", water_datum_mode="lowest")
    mask = np.zeros(req.grid_shape, dtype=bool)
    mask[20:40, 20:40] = True
    result = geo.build_region_grid(req, ConstantFetcher(560.0), water_mask=mask)
    assert result.water_cells == 400
    assert result.water_fraction > 0
    assert result.height_dm[30, 30] < 2500      # lake surface is water
    assert result.height_dm[0, 0] >= 2500       # valley floor is land


def test_unknown_water_source_is_rejected():
    with pytest.raises(geo.GeoImportError):
        request(water_source="vibes").validate()


def test_summary_reports_the_mask():
    req = request(water_source="mask")
    mask = np.zeros(req.grid_shape, dtype=bool)
    mask[5:9, 5:9] = True
    result = geo.build_region_grid(req, ConstantFetcher(20.0), water_mask=mask)
    assert "Mapped water" in result.summary()


# --- the client and its cache --------------------------------------------


def test_client_caches_to_disk(tmp_path):
    calls = []

    def opener(url, query):
        calls.append(query)
        return json.dumps({"elements": []}).encode("utf-8")

    client = geo.OverpassClient(cache_dir=str(tmp_path), opener=opener)
    query = geo.build_water_query((0, 0, 1, 1))
    assert client.fetch(query) == {"elements": []}
    assert client.fetch(query) == {"elements": []}
    assert len(calls) == 1, "second call should have come from the cache"
    assert list(tmp_path.glob("*.json"))


def test_cache_keys_differ_per_query(tmp_path):
    calls = []

    def opener(url, query):
        calls.append(query)
        return b'{"elements": []}'

    client = geo.OverpassClient(cache_dir=str(tmp_path), opener=opener)
    client.fetch(geo.build_water_query((0, 0, 1, 1)))
    client.fetch(geo.build_water_query((2, 2, 3, 3)))
    assert len(calls) == 2


def test_truncated_cache_entry_is_refetched(tmp_path):
    calls = []

    def opener(url, query):
        calls.append(query)
        return b'{"elements": []}'

    client = geo.OverpassClient(cache_dir=str(tmp_path), opener=opener)
    query = geo.build_water_query((0, 0, 1, 1))
    client.fetch(query)
    for path in tmp_path.glob("*.json"):
        path.write_text("{ truncated")
    client.fetch(query)
    assert len(calls) == 2


def test_client_rejects_an_unreadable_reply(tmp_path):
    client = geo.OverpassClient(cache_dir=str(tmp_path),
                                opener=lambda url, q: b"<html>busy</html>")
    with pytest.raises(geo.GeoImportError):
        client.fetch(geo.build_water_query((0, 0, 1, 1)))


def test_fetch_water_mask_end_to_end(tmp_path):
    req = request()
    ring = square_around(req, 10)
    payload = json.dumps({"elements": [way(ring)]}).encode("utf-8")
    client = geo.OverpassClient(cache_dir=str(tmp_path),
                                opener=lambda url, q: payload)
    mask = geo.fetch_water_mask(req, client)
    rows, cols = req.grid_shape
    assert mask.shape == (rows, cols)
    assert mask[rows // 2, cols // 2]


def test_fetch_water_mask_reports_progress(tmp_path):
    seen = []
    client = geo.OverpassClient(cache_dir=str(tmp_path),
                                opener=lambda url, q: b'{"elements": []}')
    geo.fetch_water_mask(request(), client,
                         progress=lambda d, t, m: seen.append(m))
    assert seen


# --- keeping unrepresentable water out ------------------------------------


def test_labelling_separates_bodies():
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:5, 2:5] = True
    mask[10:18, 10:18] = True
    labels, count = geo.label_water_bodies(mask)
    assert count == 2
    assert labels[3, 3] != labels[12, 12]
    assert labels[0, 0] == 0


def test_labelling_joins_diagonally_separate_runs_only_when_touching():
    mask = np.zeros((10, 10), dtype=bool)
    mask[2, 2] = True
    mask[3, 3] = True  # diagonal neighbour: four-connected, so separate
    _, count = geo.label_water_bodies(mask)
    assert count == 2


def test_labelling_handles_an_empty_mask():
    labels, count = geo.label_water_bodies(np.zeros((8, 8), dtype=bool))
    assert count == 0 and not labels.any()


def test_small_bodies_are_dropped():
    """A creek two cells wide should not become water."""
    mask = np.zeros((40, 40), dtype=bool)
    mask[5, 5] = True                 # a speck
    mask[20:30, 20:30] = True         # a real lake
    heights = np.full((40, 40), 2500, dtype=np.uint16)
    kept_mask, kept, small, high = geo.filter_water_bodies(
        mask, heights, min_area_cells=64)
    assert kept == 1 and small == 1 and high == 0
    assert not kept_mask[5, 5]
    assert kept_mask[25, 25]


def test_bodies_far_above_the_waterline_are_dropped():
    """The canyon case: an alpine stream must not be carved to sea level."""
    mask = np.zeros((40, 40), dtype=bool)
    mask[10:25, 10:25] = True
    heights = np.full((40, 40), 8770, dtype=np.uint16)   # 877 m up
    kept_mask, kept, small, high = geo.filter_water_bodies(
        mask, heights, max_rise_m=30.0)
    assert kept == 0 and high == 1
    assert not kept_mask.any()


def test_bodies_near_the_waterline_are_kept():
    mask = np.zeros((40, 40), dtype=bool)
    mask[10:25, 10:25] = True
    heights = np.full((40, 40), 2600, dtype=np.uint16)   # 10 m up
    kept_mask, kept, small, high = geo.filter_water_bodies(
        mask, heights, max_rise_m=30.0)
    assert kept == 1 and high == 0
    assert kept_mask[15, 15]


def test_height_test_uses_the_median():
    """A few cells clipped off a bank must not disqualify a whole lake."""
    mask = np.zeros((40, 40), dtype=bool)
    mask[10:30, 10:30] = True
    heights = np.full((40, 40), 2510, dtype=np.uint16)
    heights[10, 10:14] = 60000        # a sliver of mountainside
    kept_mask, kept, small, high = geo.filter_water_bodies(
        mask, heights, max_rise_m=30.0)
    assert kept == 1


def test_filters_can_be_disabled():
    mask = np.zeros((20, 20), dtype=bool)
    mask[5, 5] = True
    heights = np.full((20, 20), 50000, dtype=np.uint16)
    kept_mask, kept, small, high = geo.filter_water_bodies(
        mask, heights, min_area_cells=0, max_rise_m=None)
    assert kept == 1 and kept_mask[5, 5]


def test_filter_rejects_a_mismatched_mask():
    with pytest.raises(geo.GeoImportError):
        geo.filter_water_bodies(np.zeros((4, 4), dtype=bool),
                                np.zeros((8, 8), dtype=np.uint16))


def test_pipeline_drops_a_high_creek_but_keeps_the_lake():
    """Both filters, through the real pipeline, on one region."""
    req = request(tiles_x=2, tiles_y=2, water_source="mask",
                  water_datum_mode="lowest", min_water_area_cells=64,
                  max_water_rise_m=30.0)
    rows, cols = req.grid_shape
    mask = np.zeros((rows, cols), dtype=bool)
    mask[20:40, 20:40] = True     # a lake at the sampled elevation
    mask[100, 100:104] = True     # a four-cell creek
    result = geo.build_region_grid(req, ConstantFetcher(560.0), water_mask=mask)
    assert result.water_bodies == 1
    assert result.dropped_water_bodies == 1
    assert result.height_dm[100, 101] >= 2500   # the creek stayed as terrain


def test_summary_mentions_skipped_bodies():
    req = request(tiles_x=1, tiles_y=1, water_source="mask")
    rows, cols = req.grid_shape
    mask = np.zeros((rows, cols), dtype=bool)
    mask[3, 3] = True
    result = geo.build_region_grid(req, ConstantFetcher(20.0), water_mask=mask)
    assert "skipped" in result.summary()
