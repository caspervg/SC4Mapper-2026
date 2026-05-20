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

The original `Modules/` C sources are kept for historical reference only and
are no longer built or imported.

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
uv run python SC4Map.py
```

or, after `uv pip install -e .`, via the installed entry point:

```sh
uv run sc4mapper
```

Tests
=====

A regression suite guards the reimplemented backend (QFS round-trips against
the committed `City - *.sc4` fixtures, the terrain/thumbnail renderers, and a
full DBPF save round-trip):

```sh
uv pip install --group dev
uv run pytest
```

Contributors
============

- Wouanagaine
- JoeST
