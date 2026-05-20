"""Helpers for accessing packaged runtime assets."""

from contextlib import contextmanager
from importlib.resources import as_file, files


@contextmanager
def asset_path(name):
    """Yield a filesystem path for a bundled asset."""
    asset = files("sc4mapper").joinpath("assets", name)
    with as_file(asset) as path:
        yield path
