"""Settings for geographic data providers."""

from sc4mapper import settings


def test_default_geo_providers_are_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", lambda: tmp_path)
    configured = settings.load()

    assert configured.elevation_url
    assert configured.elevation_attribution
    assert configured.overpass_endpoints() == [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]


def test_geo_provider_edits_survive_save(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", lambda: tmp_path)
    configured = settings.load()
    configured.elevation_url = "https://terrain.example/{z}/{x}/{y}.png"
    configured.elevation_attribution = "Example terrain"
    configured.basemap_url = "https://map.example/{z}/{x}/{y}.png"
    configured.basemap_attribution = "Example map"
    configured.overpass_urls = (
        "https://one.example/api/interpreter\n"
        "https://two.example/api/interpreter")
    configured.save()

    restored = settings.load()
    assert restored.elevation_url == configured.elevation_url
    assert restored.elevation_attribution == "Example terrain"
    assert restored.basemap_url == configured.basemap_url
    assert restored.basemap_attribution == "Example map"
    assert restored.overpass_endpoints() == [
        "https://one.example/api/interpreter",
        "https://two.example/api/interpreter",
    ]


def test_overpass_endpoints_accept_commas_and_semicolons(tmp_path):
    configured = settings.AppSettings(
        config_dir=tmp_path,
        config_file=tmp_path / "SC4Mapper.ini",
        import_dir="", region_dir="", export_dir="", image_save_dir="",
        overpass_urls="https://one.example, https://two.example;https://three.example")
    assert configured.overpass_endpoints() == [
        "https://one.example",
        "https://two.example",
        "https://three.example",
    ]
