"""The georeference record carried inside city saves.

These write real .sc4 files from the committed templates and read them back
with the same DBPF reader the application uses, so the header bookkeeping
that adding an index entry requires is genuinely exercised.
"""

import json
import os
import struct

import numpy as np
import pytest

from sc4mapper import geo
from sc4mapper import region


def make_georeference(**kwargs):
    params = dict(center_lat=37.7955, center_lon=-122.4470, tiles_x=4,
                  tiles_y=4, metres_per_cell=16.0, rotation_deg=0.0,
                  sea_level_m=250.0, vertical_scale=1.0, sea_reference_m=0.0,
                  zoom=13)
    params.update(kwargs)
    return geo.GeoReference(**params)


# --- the record itself ----------------------------------------------------


def test_record_round_trips():
    payload = geo.build_georef_record(make_georeference(), offset_x=64,
                                      offset_z=128, tile_size=2)
    record = geo.parse_georef_record(payload)
    assert record is not None
    assert record["format"] == "sc4mapper.georef"
    assert record["version"] == geo.GEOREF_VERSION
    assert record["tile"]["offset_x"] == 64
    assert record["tile"]["offset_z"] == 128
    assert record["tile"]["size"] == 2
    assert record["tile"]["cells"] == 128
    assert record["frame"]["center_lat"] == pytest.approx(37.7955)
    assert record["frame"]["metres_per_cell"] == pytest.approx(16.0)
    assert record["heights"]["sea_level_m"] == pytest.approx(250.0)


def test_record_is_readable_text():
    """A modder opening a save in a hex editor should be able to read it."""
    payload = geo.build_georef_record(make_georeference())
    text = payload.decode("utf-8")
    assert "sc4mapper.georef" in text
    assert json.loads(text)["frame"]["center_lon"] == pytest.approx(-122.4470)


def test_record_carries_optional_names():
    payload = geo.build_georef_record(make_georeference(),
                                      region_name="San Francisco",
                                      import_id="abc-123")
    record = geo.parse_georef_record(payload)
    assert record["region"] == "San Francisco"
    assert record["import_id"] == "abc-123"


def test_record_omits_empty_optionals():
    record = geo.parse_georef_record(geo.build_georef_record(make_georeference()))
    assert "region" not in record
    assert "import_id" not in record


@pytest.mark.parametrize("payload", [
    b"", None, b"not json", b"{}", b'{"format": "something.else"}',
    b'\x00\x01\x02\x03', b'[1, 2, 3]', "not bytes".encode("utf-16"),
])
def test_parser_rejects_anything_else(payload):
    assert geo.parse_georef_record(payload) is None


def test_parser_refuses_a_future_version():
    payload = geo.build_georef_record(make_georeference())
    record = json.loads(payload.decode("utf-8"))
    record["version"] = geo.GEOREF_VERSION + 1
    assert geo.parse_georef_record(json.dumps(record).encode("utf-8")) is None


def test_parser_survives_a_junk_version():
    payload = geo.build_georef_record(make_georeference())
    record = json.loads(payload.decode("utf-8"))
    record["version"] = "banana"
    assert geo.parse_georef_record(json.dumps(record).encode("utf-8")) is None


def test_record_is_small():
    """It rides inside every city save, so it should stay negligible."""
    payload = geo.build_georef_record(make_georeference(),
                                      region_name="Somewhere")
    assert len(payload) < 2048


# --- the reference implementation ----------------------------------------


def test_cell_lookup_matches_the_sampling_grid():
    """The record must reproduce the coordinates the import actually used."""
    request = geo.GeoImportRequest(center_lat=37.7955, center_lon=-122.4470,
                                   tiles_x=2, tiles_y=2, metres_per_cell=16.0)
    lon, lat = geo.grid_lonlat(request)

    georeference = make_georeference(tiles_x=2, tiles_y=2)
    # A city at the south-east quadrant of the region.
    payload = geo.build_georef_record(georeference, offset_x=64, offset_z=64,
                                      tile_size=1)
    record = geo.parse_georef_record(payload)

    for cell_x, cell_z in [(0, 0), (10, 3), (64, 64), (32, 17)]:
        got_lon, got_lat = geo.georef_cell_to_lonlat(record, cell_x, cell_z)
        assert got_lon == pytest.approx(lon[64 + cell_z, 64 + cell_x], abs=1e-9)
        assert got_lat == pytest.approx(lat[64 + cell_z, 64 + cell_x], abs=1e-9)


def test_cell_lookup_honours_rotation():
    request = geo.GeoImportRequest(center_lat=46.686, center_lon=7.863,
                                   tiles_x=1, tiles_y=1, metres_per_cell=32.0,
                                   rotation_deg=30.0)
    lon, lat = geo.grid_lonlat(request)
    georeference = make_georeference(center_lat=46.686, center_lon=7.863,
                                     tiles_x=1, tiles_y=1,
                                     metres_per_cell=32.0, rotation_deg=30.0)
    record = geo.parse_georef_record(geo.build_georef_record(georeference))
    got_lon, got_lat = geo.georef_cell_to_lonlat(record, 20, 40)
    assert got_lon == pytest.approx(lon[40, 20], abs=1e-9)
    assert got_lat == pytest.approx(lat[40, 20], abs=1e-9)


def test_cell_lookup_accepts_arrays():
    record = geo.parse_georef_record(geo.build_georef_record(make_georeference()))
    lon, lat = geo.georef_cell_to_lonlat(record, np.array([0, 10]),
                                         np.array([0, 10]))
    assert lon.shape == (2,)
    assert lat[0] > lat[1]  # moving south lowers the latitude


# --- inside a real save file ---------------------------------------------


def build_region(tmp_path, tiles=1, with_georef=True):
    """Create and save a tiny region, returning its folder."""
    config = geo.build_config_image((tiles, tiles), 1)
    new_region = region.SC4Region(None, 250.0, None, config)
    new_region.show(None)
    new_region.height = np.full(new_region.shape, 2600, dtype=np.uint16)
    if with_georef:
        new_region.georeference = make_georeference(tiles_x=tiles, tiles_y=tiles)

    folder = str(tmp_path / "region")
    os.makedirs(folder, exist_ok=True)
    new_region.folder = folder

    class Progress:
        def Update(self, *args):
            pass

    minX, minY, maxX, maxY, sizeX, sizeY, cropped = new_region.CropConfig()
    subRgn = [minX * 64, minY * 64, maxX * 64 + 1, maxY * 64 + 1]
    assert new_region.Save(Progress(), minX, minY, subRgn)
    return folder


def read_entries(path):
    """Read a DBPF index the way the application does."""
    sc4 = region.SC4File(path)
    sc4.ReadHeader()
    sc4.ReadEntries()
    return sc4


def test_saved_city_carries_the_record(tmp_path):
    folder = build_region(tmp_path)
    saves = [f for f in os.listdir(folder) if f.endswith(".sc4")]
    assert saves

    with open(os.path.join(folder, saves[0]), "rb") as fh:
        blob = fh.read()
    raw = struct.unpack("<4s17I24s", blob[:96])
    count, index_pos, index_len = raw[9], raw[10], raw[11]

    # The header must agree with the index it describes.
    assert index_len == count * 20

    found = None
    index = blob[index_pos:index_pos + index_len]
    for i in range(count):
        t, g, inst, loc, size = struct.unpack("<3I2i", index[i * 20:i * 20 + 20])
        if (t, g, inst) == geo.GEOREF_TGI:
            found = blob[loc:loc + size]
    assert found is not None, "no georeference entry in the save"

    record = geo.parse_georef_record(found)
    assert record is not None
    assert record["frame"]["center_lat"] == pytest.approx(37.7955)
    assert record["tile"]["size"] == 1


def test_adding_the_record_leaves_the_save_readable(tmp_path):
    """The terrain must still parse, which is what proves the index maths."""
    folder = build_region(tmp_path)
    saves = sorted(f for f in os.listdir(folder) if f.endswith(".sc4"))
    sc4 = read_entries(os.path.join(folder, saves[0]))

    assert hasattr(sc4, "heightMapEntry")
    heights = np.frombuffer(sc4.heightMapEntry.content[2:], np.float32)
    assert heights.size == sc4.xSize * sc4.ySize
    assert np.isfinite(heights).all()
    assert heights[0] == pytest.approx(260.0)  # 2600 dm


def test_region_without_a_georeference_is_unchanged(tmp_path):
    """Bitmap imports have no georeference and must save exactly as before."""
    folder = build_region(tmp_path, with_georef=False)
    saves = sorted(f for f in os.listdir(folder) if f.endswith(".sc4"))
    with open(os.path.join(folder, saves[0]), "rb") as fh:
        blob = fh.read()
    raw = struct.unpack("<4s17I24s", blob[:96])
    count, index_pos, index_len = raw[9], raw[10], raw[11]
    index = blob[index_pos:index_pos + index_len]
    tgis = [struct.unpack("<3I2i", index[i * 20:i * 20 + 20])[:3]
            for i in range(count)]
    assert geo.GEOREF_TGI not in tgis
    # ...and it still reads.
    sc4 = read_entries(os.path.join(folder, saves[0]))
    assert hasattr(sc4, "heightMapEntry")


def test_record_offsets_locate_each_city_in_the_region(tmp_path):
    """A 2x2 region of small cities: each must record its own corner."""
    folder = build_region(tmp_path, tiles=2)
    saves = sorted(f for f in os.listdir(folder) if f.endswith(".sc4"))
    assert len(saves) == 4

    offsets = set()
    for name in saves:
        with open(os.path.join(folder, name), "rb") as fh:
            blob = fh.read()
        raw = struct.unpack("<4s17I24s", blob[:96])
        count, index_pos, index_len = raw[9], raw[10], raw[11]
        index = blob[index_pos:index_pos + index_len]
        for i in range(count):
            t, g, inst, loc, size = struct.unpack("<3I2i",
                                                  index[i * 20:i * 20 + 20])
            if (t, g, inst) == geo.GEOREF_TGI:
                record = geo.parse_georef_record(blob[loc:loc + size])
                offsets.add((record["tile"]["offset_x"],
                             record["tile"]["offset_z"]))
    assert offsets == {(0, 0), (64, 0), (0, 64), (64, 64)}


def test_saving_twice_does_not_duplicate_the_entry(tmp_path):
    """Re-saving must replace the record, not stack another one."""
    folder = build_region(tmp_path)
    saves = sorted(f for f in os.listdir(folder) if f.endswith(".sc4"))
    target = os.path.join(folder, saves[0])

    save = region.SaveFile(target)
    payload = geo.build_georef_record(make_georeference())
    before = save.indexRecordEntryCount
    save.AddOrReplaceEntry(geo.GEOREF_TGI, payload)
    assert save.indexRecordEntryCount == before  # already present, replaced
    save.AddOrReplaceEntry(geo.GEOREF_TGI, payload + b" ")
    assert save.indexRecordEntryCount == before


# --- the inverse: what a plugin actually needs ---------------------------


def test_lonlat_to_cell_inverts_cell_to_lonlat():
    """Round trip: a cell, out to the world, and back to the same cell."""
    record = geo.parse_georef_record(
        geo.build_georef_record(make_georeference(tiles_x=4, tiles_y=4),
                                offset_x=128, offset_z=64, tile_size=2))
    for cell_x, cell_z in [(0, 0), (1, 1), (63, 127), (128, 128), (37, 91)]:
        lon, lat = geo.georef_cell_to_lonlat(record, cell_x, cell_z)
        back_x, back_z = geo.georef_lonlat_to_cell(record, lon, lat)
        assert back_x == pytest.approx(cell_x, abs=1e-6)
        assert back_z == pytest.approx(cell_z, abs=1e-6)


@pytest.mark.parametrize("rotation", [0.0, 15.0, 90.0, -37.5, 180.0])
def test_round_trip_holds_under_rotation(rotation):
    record = geo.parse_georef_record(
        geo.build_georef_record(
            make_georeference(rotation_deg=rotation, tiles_x=2, tiles_y=2),
            offset_x=64, offset_z=0, tile_size=1))
    lon, lat = geo.georef_cell_to_lonlat(record, 20, 44)
    back_x, back_z = geo.georef_lonlat_to_cell(record, lon, lat)
    assert back_x == pytest.approx(20, abs=1e-6)
    assert back_z == pytest.approx(44, abs=1e-6)


def test_round_trip_holds_at_high_latitude():
    """Where the per-row longitude scaling matters most."""
    record = geo.parse_georef_record(
        geo.build_georef_record(
            make_georeference(center_lat=69.65, center_lon=18.96,
                              tiles_x=8, tiles_y=8, metres_per_cell=48.0)))
    for cell_x, cell_z in [(0, 0), (511, 511), (100, 400)]:
        lon, lat = geo.georef_cell_to_lonlat(record, cell_x, cell_z)
        back_x, back_z = geo.georef_lonlat_to_cell(record, lon, lat)
        assert back_x == pytest.approx(cell_x, abs=1e-4)
        assert back_z == pytest.approx(cell_z, abs=1e-4)


def test_points_outside_the_tile_report_outside():
    record = geo.parse_georef_record(
        geo.build_georef_record(make_georeference(tiles_x=4, tiles_y=4),
                                offset_x=128, offset_z=128, tile_size=1))
    # A point one tile north-west of this city.
    lon, lat = geo.georef_cell_to_lonlat(record, -64, -64)
    cell_x, cell_z = geo.georef_lonlat_to_cell(record, lon, lat)
    assert cell_x < 0 and cell_z < 0


def test_inverse_accepts_arrays():
    record = geo.parse_georef_record(geo.build_georef_record(make_georeference()))
    lon, lat = geo.georef_cell_to_lonlat(record, np.array([0, 5, 10]),
                                         np.array([0, 5, 10]))
    cell_x, cell_z = geo.georef_lonlat_to_cell(record, lon, lat)
    assert np.allclose(cell_x, [0, 5, 10], atol=1e-6)
    assert np.allclose(cell_z, [0, 5, 10], atol=1e-6)


# --- self-describing projection ------------------------------------------


def test_record_publishes_its_projection():
    """A reader must not have to guess at the earth model."""
    record = geo.parse_georef_record(geo.build_georef_record(make_georeference()))
    projection = record["projection"]
    assert projection["model"] == geo.PROJECTION_MODEL
    assert projection["crs"] == "EPSG:4326"
    assert projection["metres_per_degree_lat_coeffs"] == list(
        geo.METRES_PER_DEGREE_LAT_COEFFS)
    assert projection["metres_per_degree_lon_coeffs"] == list(
        geo.METRES_PER_DEGREE_LON_COEFFS)


def test_published_coefficients_are_the_ones_actually_used():
    """The record cannot drift from the code that produced it."""
    import math as _math
    record = geo.parse_georef_record(geo.build_georef_record(make_georeference()))
    a, b, c, d = record["projection"]["metres_per_degree_lat_coeffs"]
    for lat in (0.0, 23.5, 52.0, -33.9):
        phi = _math.radians(lat)
        expected = (a + b * _math.cos(2 * phi) + c * _math.cos(4 * phi)
                    + d * _math.cos(6 * phi))
        assert geo.metres_per_degree_lat(lat) == pytest.approx(expected)

    a, b, c = record["projection"]["metres_per_degree_lon_coeffs"]
    for lat in (0.0, 23.5, 52.0, -33.9):
        phi = _math.radians(lat)
        expected = (a * _math.cos(phi) + b * _math.cos(3 * phi)
                    + c * _math.cos(5 * phi))
        assert float(geo.metres_per_degree_lon(lat)) == pytest.approx(expected)


def test_record_states_how_water_was_handled():
    """Heights below the shoreline are not invertible; say so."""
    record = geo.parse_georef_record(
        geo.build_georef_record(make_georeference(), ocean_depth_m=35.0,
                                keep_bathymetry=False))
    assert record["heights"]["ocean_depth_m"] == pytest.approx(35.0)
    assert record["heights"]["keep_bathymetry"] is False

    record = geo.parse_georef_record(
        geo.build_georef_record(make_georeference(), keep_bathymetry=True))
    assert record["heights"]["keep_bathymetry"] is True


def test_height_mapping_is_invertible_above_the_shoreline():
    """Given an in-game height, a reader can recover the real elevation."""
    georeference = make_georeference(vertical_scale=0.5, sea_reference_m=560.0)
    record = geo.parse_georef_record(geo.build_georef_record(georeference))
    heights = record["heights"]

    real = 812.0
    in_game = (heights["sea_level_m"]
               + (real - heights["sea_reference_m"]) * heights["vertical_scale"])
    recovered = (heights["sea_reference_m"]
                 + (in_game - heights["sea_level_m"]) / heights["vertical_scale"])
    assert recovered == pytest.approx(real)


def test_saved_city_records_region_name_and_import_id(tmp_path):
    """Both were plumbed but never set; a save must actually carry them."""
    config = geo.build_config_image((1, 1), 1)
    new_region = region.SC4Region(None, 250.0, None, config)
    new_region.show(None)
    new_region.height = np.full(new_region.shape, 2600, dtype=np.uint16)
    new_region.georeference = make_georeference(tiles_x=1, tiles_y=1)
    new_region.regionName = "San Francisco"
    new_region.importId = "deadbeef"
    folder = str(tmp_path / "named")
    os.makedirs(folder, exist_ok=True)
    new_region.folder = folder

    class Progress:
        def Update(self, *args):
            pass

    minX, minY, maxX, maxY, sx, sy, cropped = new_region.CropConfig()
    assert new_region.Save(Progress(), minX, minY,
                           [minX * 64, minY * 64, maxX * 64 + 1, maxY * 64 + 1])

    name = sorted(f for f in os.listdir(folder) if f.endswith(".sc4"))[0]
    with open(os.path.join(folder, name), "rb") as fh:
        blob = fh.read()
    raw = struct.unpack("<4s17I24s", blob[:96])
    count, index_pos, index_len = raw[9], raw[10], raw[11]
    index = blob[index_pos:index_pos + index_len]
    record = None
    for i in range(count):
        t, g, inst, loc, size = struct.unpack("<3I2i", index[i * 20:i * 20 + 20])
        if (t, g, inst) == geo.GEOREF_TGI:
            record = geo.parse_georef_record(blob[loc:loc + size])
    assert record is not None
    assert record["region"] == "San Francisco"
    assert record["import_id"] == "deadbeef"
