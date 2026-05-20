SC4Mapper-2013
==============

SimCity 4 region import/export tool.

Check [SC4Devotion](http://sc4devotion.com/forums/index.php?topic=15455.0)
for more information (registration may be required).

Modern rewrite
==============

This is a Python 3 / wxPython 4 modernisation of the original 2013 code base.
The two former C/C++ extension modules have been reimplemented in pure
Python / NumPy, so **no C compiler is required** and the project is now
installable on any platform with `pip` / `uv`:

- `qfs.py` replaces the `QFS` C extension (QFS / RefPack compression).
- `tools3d.py` replaces the `tools3D` C++ extension (terrain colouring and
  isometric thumbnail generation).

Requirements
============

- Python 3.10 or newer
- [NumPy](https://numpy.org) 1.26+
- [Pillow](https://python-pillow.org) 10+
- [wxPython](https://www.wxpython.org) 4.2+

Setup with [uv](https://docs.astral.sh/uv/)
===========================================

```sh
uv venv
uv pip install -e .
```

Running
=======

```sh
uv run sc4mapper
```

The user-editable settings and terrain colour palette live in
`config/SC4Mapper.ini`.

Repository layout
=================

- `src/sc4mapper/` contains the application code.
- `src/sc4mapper/assets/` contains bundled runtime templates and images.
- `config/SC4Mapper.ini` contains default folders and terrain colours.
- `scripts/run_sc4mapper.py` is the PyInstaller entry point.
- `tests/` contains the regression suite.

Tests
=====

A regression suite guards the reimplemented backend (QFS round-trips against
the committed `City - *.sc4` fixtures, the terrain/thumbnail renderers, and a
full DBPF save round-trip):

```sh
uv pip install --group dev
uv run pytest
```

Building a Windows executable
=============================

The supported end-user build is a portable PyInstaller onedir bundle.  It does
not require Python, a C compiler, an installer, or UPX on the target machine.

```sh
uv run --group build pyinstaller --clean --noconfirm SC4Mapper.spec
Copy-Item -Recurse -Force config dist/SC4Mapper/config
```

The executable is written to `dist/SC4Mapper/SC4Mapper.exe`.  Distribute the
whole `dist/SC4Mapper` folder as a zip file.  Include the `config/` folder
beside the executable so users can edit default paths and terrain colours.

GitHub Actions builds the same portable Windows zip on every push and pull
request.  Manual workflow runs also include experimental macOS and Linux
packaging jobs; those are allowed to fail until the cross-platform app behavior
and wxPython packaging story are verified.

Legacy packaging
================

The original installer script, Python 2 extension build scripts, debug spec,
and batch distribution script have been removed.  The former native `QFS` and
`tools3D` extensions are now maintained as `qfs.py` and `tools3d.py`.

Contributors
============

- Wouanagaine
- JoeST
