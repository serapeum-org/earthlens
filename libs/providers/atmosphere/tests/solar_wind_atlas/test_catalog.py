"""Unit tests for the solar_wind_atlas catalog loader and Layer rows."""

from __future__ import annotations

import pytest

from earthlens.solar_wind_atlas import Catalog, Layer

pytestmark = pytest.mark.solar_wind_atlas


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """The bundled Solar & Wind Atlas catalog."""
    return Catalog()


def test_solar_layer_resolves_to_gsa_download_zip(catalog: Catalog) -> None:
    """A solar layer carries atlas gsa and the download_zip transport."""
    ghi = catalog.get("ghi")
    assert ghi.atlas == "gsa"
    assert ghi.transport == "download_zip"
    assert ghi.url.endswith("_GEOTIFF.zip")


def test_wind_layer_resolves_to_gwa_vsicurl(catalog: Catalog) -> None:
    """A wind layer carries atlas gwa and the vsicurl transport."""
    wind = catalog.get("wind_100m")
    assert wind.atlas == "gwa"
    assert wind.transport == "vsicurl"
    assert "figshare" in wind.url


def test_unknown_layer_raises_did_you_mean(catalog: Catalog) -> None:
    """An unknown id raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'ghi'"):
        catalog.get("gho")


def test_every_row_is_complete(catalog: Catalog) -> None:
    """Every catalog row has an atlas, transport, url, units, and long_name."""
    for layer in catalog.datasets.values():
        assert isinstance(layer, Layer)
        assert layer.atlas in ("gsa", "gwa")
        assert layer.transport in ("vsicurl", "download_zip")
        assert layer.url.startswith("https://")
        assert layer.units
        assert layer.long_name
        assert layer.license_note


def test_transport_matches_atlas(catalog: Catalog) -> None:
    """Solar rows use download_zip and wind rows use vsicurl."""
    for layer in catalog.datasets.values():
        expected = "download_zip" if layer.atlas == "gsa" else "vsicurl"
        assert layer.transport == expected


def test_available_lists_all_curated_ids(catalog: Catalog) -> None:
    """available() returns the sorted curated ids and matches the index."""
    assert catalog.available() == sorted(catalog.datasets)
    assert set(catalog.available_datasets) == set(catalog.datasets)


def test_catalog_covers_the_six_solar_layers(catalog: Catalog) -> None:
    """All six Global Solar Atlas variables are curated."""
    solar = {k for k, v in catalog.datasets.items() if v.atlas == "gsa"}
    assert solar == {"ghi", "dni", "dif", "gti", "pvout", "opta"}


_ROW = (
    "  x:\n"
    "    atlas: gsa\n"
    "    transport: download_zip\n"
    "    url: https://example/x.zip\n"
    "    units: kWh/m2/day\n"
    "    long_name: example\n"
)


def test_load_from_single_file(tmp_path) -> None:
    """The loader accepts a single YAML file (the test back-compat path)."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    one = tmp_path / "one.yaml"
    one.write_text(f"available_datasets: [x]\ndatasets:\n{_ROW}", encoding="utf-8")
    cat = Catalog.load(one)
    assert cat.get("x").atlas == "gsa"


def test_duplicate_layer_across_files_raises(tmp_path) -> None:
    """A layer declared in two files fails the merge."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    (tmp_path / "a.yaml").write_text(f"datasets:\n{_ROW}", encoding="utf-8")
    (tmp_path / "b.yaml").write_text(f"datasets:\n{_ROW}", encoding="utf-8")
    with pytest.raises(ValueError, match="declared in two catalog files"):
        Catalog.load(tmp_path)


def test_missing_datasets_block_raises(tmp_path) -> None:
    """A catalog with no datasets: block is rejected."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    (tmp_path / "empty.yaml").write_text("available_datasets: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(tmp_path)


def test_invalid_row_raises(tmp_path) -> None:
    """A row missing a required field fails pydantic validation."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    bad = "  y:\n    atlas: gsa\n    transport: download_zip\n"  # no url
    (tmp_path / "bad.yaml").write_text(f"datasets:\n{bad}", encoding="utf-8")
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(tmp_path)


def test_curated_missing_from_index_raises(tmp_path) -> None:
    """A curated id absent from available_datasets: is rejected."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    (tmp_path / "c.yaml").write_text(
        f"available_datasets: [other]\ndatasets:\n{_ROW}", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing from"):
        Catalog.load(tmp_path)


def test_missing_path_raises(tmp_path) -> None:
    """A non-existent catalog path is rejected."""
    from earthlens.solar_wind_atlas import catalog as catmod

    catmod.clear_catalog_cache()
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(tmp_path / "nope")


def test_clear_catalog_cache_is_callable() -> None:
    """clear_catalog_cache empties the parse cache without error."""
    from earthlens.solar_wind_atlas import catalog as catmod

    Catalog()
    catmod.clear_catalog_cache()
    assert catmod._CATALOG_CACHE == {}
