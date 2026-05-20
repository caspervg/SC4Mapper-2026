# SC4Mapper-2026

SC4Mapper is a SimCity 4 region import/export tool.

This repository is a modernization of the SC4Mapper source code by
Wouanagaine and JoeST from 2012:

https://github.com/wouanagaine/SC4Mapper-2013

The goal is to keep the original tool usable on current Python and current
Windows systems, with less build and install friction.

## What Changed

- Ported the app to Python 3 and wxPython 4.
- Replaced the old native extension modules with Python/NumPy code:
  - `qfs.py` replaces the old `QFS` compression extension.
  - `terrain.py` replaces the old `tools3D` terrain rendering extension.
- Moved the code into a normal `src/sc4mapper/` package layout.
- Added tests for QFS round-trips, city save fixtures, terrain rendering, and
  DBPF save round-trips.
- Replaced the old installer/batch packaging flow with a PyInstaller build.

## Running From Source

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv venv
uv pip install -e .
uv run sc4mapper
```

The editable settings file is:

```text
config/SC4Mapper.ini
```

It contains the default import/export/save folders and the terrain colour
palette. Paths may use `{documents}`, `{home}`, and `{config}` placeholders.

## Tests

```sh
uv pip install --group dev
uv run pytest
```

## Building

The supported end-user build is a portable Windows folder made with
PyInstaller. Users do not need Python, a compiler, UPX, or an installer.

```sh
uv run --group build pyinstaller --clean --noconfirm SC4Mapper.spec
Copy-Item -Recurse -Force config dist/SC4Mapper/config
```

Zip and distribute the whole `dist/SC4Mapper` folder.

## Releases

GitHub Actions builds the Windows zip on pushes and pull requests.

Pushing a tag matching the historical version format creates a GitHub Release.
Use `vYYYY.Nsuffix`, for example `v2026.1a`. Windows is the required release
artifact. macOS and Linux builds are attempted on native runners and attached
when they succeed, but they should be treated as experimental until the app
behavior and wxPython packaging have been tested on those platforms.

### macOS

The app is distributed as a signed `.app` bundle (but without Apple Developer certificate).
After unzipping, remove the quarantine flag once before launching:

```sh
xattr -rd com.apple.quarantine SC4Mapper.app
```

## Repository Layout

- `src/sc4mapper/` - application code
- `src/sc4mapper/assets/` - bundled city templates and splash image
- `config/SC4Mapper.ini` - user-visible defaults and terrain palette
- `scripts/run_sc4mapper.py` - PyInstaller entry point
- `tests/` - regression tests

## License

See `license.txt`.
