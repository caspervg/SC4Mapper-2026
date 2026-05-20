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