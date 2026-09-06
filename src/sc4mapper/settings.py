"""Application settings stored in a user-visible INI file."""

import configparser
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "SC4Mapper.ini"

DEFAULT_CONFIG = """[paths]
# Relative paths are resolved from this config folder.
# Supported placeholders: {documents}, {home}, {config}
import_dir = {documents}
region_dir = {documents}/SimCity 4/Regions
export_dir = {documents}
image_save_dir = {documents}/Pictures
# Cache for downloaded elevation and map tiles. Tiles never change, so this
# only ever grows; delete it freely to reclaim space.
tile_cache_dir = {config}/tilecache

[geo]
# Elevation source for "Real-world location" imports. The default is the
# Mapzen/AWS terrain tile set: global, open, and no API key required.
elevation_url = https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png

# Optional raster map drawn underneath the region so you can see what you are
# turning into city tiles. Empty by default: every map and imagery provider
# sets its own terms, and OpenStreetMap's tile policy in particular forbids
# distributed applications from using their servers, so picking one (and
# supplying any API key) has to be your call. Any XYZ tile URL works, e.g.
#   basemap_url = https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}
# Note that some providers order the path {z}/{y}/{x} rather than {z}/{x}/{y}.
basemap_url =

# How strongly the map underlay shows through the terrain colours, 0.0 - 1.0.
basemap_opacity = 0.55

[background]
color = 5c687e

[water]
0 = 94b0bb
200 = 353a65
6000 = 353a65

[land]
1 = ddd2ac
3 = e9e7cf
4 = 0d3d18
50 = 1e4c0a
100 = 456235
250 = 487542
450 = 376540
600 = 287021
900 = 3a7026
1000 = 4a7a37
1220 = 728551
1450 = 919168
1900 = a19b7d
2050 = FFFFFF
6000 = FFFFFF
"""


@dataclass
class AppSettings:
    config_dir: Path
    config_file: Path
    import_dir: str
    region_dir: str
    export_dir: str
    image_save_dir: str
    tile_cache_dir: str = ""
    elevation_url: str = ""
    basemap_url: str = ""
    basemap_opacity: float = 0.55

    def save(self):
        parser = configparser.ConfigParser()
        parser.read(self.config_file, encoding="utf-8")
        if not parser.has_section("paths"):
            parser.add_section("paths")
        parser["paths"] = {
            "import_dir": self.import_dir,
            "region_dir": self.region_dir,
            "export_dir": self.export_dir,
            "image_save_dir": self.image_save_dir,
            "tile_cache_dir": self.tile_cache_dir,
        }
        if not parser.has_section("geo"):
            parser.add_section("geo")
        parser["geo"] = {
            "elevation_url": self.elevation_url,
            "basemap_url": self.basemap_url,
            "basemap_opacity": str(self.basemap_opacity),
        }
        with open(self.config_file, "w", encoding="utf-8") as fh:
            parser.write(fh)


def app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def config_dir():
    return app_root() / "config"


def _documents_dir():
    if sys.platform == "win32":
        return Path.home() / "Documents"
    if sys.platform == "darwin":
        return Path.home() / "Documents"
    xdg_documents = os.environ.get("XDG_DOCUMENTS_DIR")
    if xdg_documents:
        return Path(xdg_documents)
    return Path.home() / "Documents"


def _expand_placeholders(value, base_dir):
    replacements = {
        "config": str(base_dir),
        "documents": str(_documents_dir()),
        "home": str(Path.home()),
    }
    for key, replacement in replacements.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def _resolve_path(value, base_dir):
    value = _expand_placeholders(value, base_dir)
    value = os.path.expandvars(os.path.expanduser(value.strip()))
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return str(path)


def _ensure_files(path):
    path.mkdir(parents=True, exist_ok=True)
    config_file = path / CONFIG_FILE
    if not config_file.exists():
        config_file.write_text(DEFAULT_CONFIG, encoding="utf-8")


def load(default_region_dir=None):
    path = config_dir()
    _ensure_files(path)

    parser = configparser.ConfigParser()
    parser.read(path / CONFIG_FILE, encoding="utf-8")
    paths = parser["paths"] if parser.has_section("paths") else {}
    default_region_dir = default_region_dir or ""

    def item(name, fallback=""):
        return _resolve_path(paths.get(name, fallback), path)

    region_dir = item("region_dir", default_region_dir)
    import_dir = item("import_dir")
    export_dir = item("export_dir")
    image_save_dir = item("image_save_dir")
    tile_cache_dir = item("tile_cache_dir", "{config}/tilecache")

    geo = parser["geo"] if parser.has_section("geo") else {}
    try:
        basemap_opacity = float(geo.get("basemap_opacity", "0.55"))
    except (TypeError, ValueError):
        basemap_opacity = 0.55

    return AppSettings(
        config_dir=path,
        config_file=path / CONFIG_FILE,
        import_dir=import_dir,
        region_dir=region_dir,
        export_dir=export_dir,
        image_save_dir=image_save_dir,
        tile_cache_dir=tile_cache_dir,
        elevation_url=geo.get("elevation_url", "").strip(),
        basemap_url=geo.get("basemap_url", "").strip(),
        basemap_opacity=min(1.0, max(0.0, basemap_opacity)),
    )
