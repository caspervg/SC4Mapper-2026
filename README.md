# SC4Mapper-2026

SC4Mapper is a SimCity 4 region import/export tool. This repository is a
modernization of the original [SC4Mapper-2013](https://github.com/wouanagaine/SC4Mapper-2013)
by Wouanagaine and JoeST, updated to run on current Python and wxPython with
less build and install friction.

> This modernization was carried out primarily using OpenAI Codex and
> Claude Code, supervised and verified by a human maintainer.

## What Changed

- Ported to Python 3 and wxPython 4.
- Replaced native extension modules with pure Python/NumPy:
  - `qfs.py` replaces the old `QFS` compression extension.
  - `terrain.py` replaces the old `tools3D` terrain rendering extension.
- Reorganized into a standard `src/sc4mapper/` package layout.
- Added a test suite covering QFS round-trips, city save fixtures, terrain
  rendering, and DBPF save round-trips.
- Replaced the old installer/batch packaging flow with a PyInstaller build.

## Real-World Locations

Besides importing a heightmap, SC4Mapper can build a region directly from
real-world elevation data. **Create Region -> Real-world location** asks for
a place and the shape of the region to cut out of it:

![The location dialog](doc/geo/location-dialog.jpg)

- **Where** takes a place name, a coordinate pair, or a pasted OpenStreetMap
  or Google Maps link. Place-name lookup uses OpenStreetMap's Nominatim
  service, one search per button press.
- **Metres per cell** is the scale. SimCity 4 cells are 16 m, so 16 is true
  scale; larger values squeeze more real ground into the same region. The
  dialog shows the resulting footprint in kilometres as you type.
- **Start layout with** picks the city size to pack the region with. Whatever
  does not fit is filled with smaller cities.
- **Vertical exaggeration** scales relief. Real slopes at true scale are
  often too steep to build on, so mountainous areas usually want a value
  below 1.
- Sea level is SimCity 4's 250 m datum. By default everything below the
  real shoreline is flattened to a shallow shelf, because scaled ocean
  bathymetry would otherwise bottom out as a pit.

Elevation comes from the [Mapzen/AWS terrain
tiles](https://registry.opendata.aws/terrain-tiles/): global coverage, open
data, no API key. Tiles are cached on disk, so re-importing an area is
offline and instant.

### Laying Out City Tiles

The import produces a starting layout you then reshape with **Edit
Config.bmp** -- paint small, medium or large cities over the imported
terrain, or erase tiles to leave holes:

![Editing the city layout over imported terrain](doc/geo/region-layout.jpg)

Blue outlines are large cities, green medium, red small; hatched tiles are
holes. Enabling `basemap_url` (see below) draws a real map underneath the
terrain so you can see what you are turning into city tiles.

### Map Underlay

`config/SC4Mapper.ini` has a `[geo]` section:

```ini
[geo]
elevation_url = https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
basemap_url =
basemap_opacity = 0.55
```

`basemap_url` is empty by default and the underlay is switched off. Unlike
the elevation source, general map and imagery providers each set their own
terms -- OpenStreetMap's tile policy, for instance, forbids distributed
applications from drawing on their servers -- so choosing a provider, and
supplying any API key it needs, is left to you. Any XYZ tile URL works;
note that some providers order the path `{z}/{y}/{x}`.

## Running From Source

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync
uv run sc4mapper
```

The editable settings file is `config/SC4Mapper.ini`. It contains the default
import/export/save folders and the terrain colour palette. Paths may use
`{documents}`, `{home}`, and `{config}` placeholders.

## Tests

```sh
uv sync --group dev
uv run pytest
```

## Building

```sh
uv sync --group build
uv run pyinstaller --clean --noconfirm SC4Mapper.spec
```

**Windows** — copy the config folder alongside the executable, then zip:

```powershell
Copy-Item -Recurse -Force config dist/SC4Mapper/config
Compress-Archive -Path dist/SC4Mapper -DestinationPath SC4Mapper-windows.zip
```

**macOS** — copy config into the app bundle, then archive:

```sh
cp -R config dist/SC4Mapper.app/Contents/MacOS/config
ditto -c -k --sequesterRsrc --keepParent dist/SC4Mapper.app SC4Mapper-macos.zip
```

**Linux** — copy config alongside the executable, then archive:

```sh
cp -R config dist/SC4Mapper/config
tar -czf SC4Mapper-linux.tar.gz -C dist SC4Mapper
```

## Releases

Pushing a tag matching `vYYYY.Nsuffix` (for example `v2026.1a`) triggers a
GitHub Actions release build. Windows is the required artifact. macOS and Linux
builds are experimental and attached when they succeed.

### macOS

The app bundle is ad-hoc signed but does not carry an Apple Developer
certificate. Remove the quarantine flag once after unzipping:

```sh
xattr -rd com.apple.quarantine SC4Mapper.app
```

## Repository Layout

- `src/sc4mapper/` — application source
- `src/sc4mapper/assets/` — bundled city templates and splash image
- `config/SC4Mapper.ini` — user-editable defaults and terrain palette
- `scripts/run_sc4mapper.py` — PyInstaller entry point
- `tests/` — regression tests

## License

See `license.txt`.