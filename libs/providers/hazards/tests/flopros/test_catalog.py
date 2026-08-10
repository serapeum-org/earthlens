"""Unit tests for `earthlens.flopros.catalog`."""

from __future__ import annotations

import pytest

from earthlens.flopros import CATALOG_PATH, Catalog, FloprosDataset
from earthlens.flopros.catalog import clear_catalog_cache

pytestmark = pytest.mark.flopros


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Reset the module-level parse cache so tmp-file rewrites work."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


def test_bundled_catalog_loads_the_flopros_row():
    """`Catalog()` loads the single shipped `flopros` dataset row."""
    cat = Catalog()
    assert list(cat.datasets) == ["flopros"]
    assert isinstance(cat.get("flopros"), FloprosDataset)


def test_row_fields_match_the_shipped_shapefile():
    """The row carries the supplement URL, stem, identity columns, and layers."""
    row = Catalog().get("flopros")
    assert row.url.endswith("nhess-16-1049-2016-supplement.zip")
    assert row.shapefile_stem == "FLOPROS_shp_V1"
    assert row.crs == "EPSG:4326"
    assert row.identity_columns == ["name", "geonunit", "type_en"]
    assert row.layers["merged_riverine"] == "MerL_Riv"
    assert row.layers["modelled_riverine"] == "ModL_Riv"


def test_license_and_attribution_are_present():
    """The catalog carries the CC-BY-3.0 licence and a citation string."""
    cat = Catalog()
    assert cat.license == "CC-BY-3.0"
    assert "Scussolini" in cat.attribution


def test_get_default_is_flopros():
    """`get()` with no argument resolves the single `flopros` row."""
    assert Catalog().get().shapefile_stem == "FLOPROS_shp_V1"


def test_unknown_dataset_raises_with_hint():
    """An unknown dataset name raises a listing `ValueError`."""
    cat = Catalog()
    with pytest.raises(ValueError, match="FLOPROS catalog"):
        cat.get("floprosx")


def test_empty_datasets_block_rejected(tmp_path, monkeypatch):
    """A catalog file with no `datasets:` block fails loud."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("license: CC-BY-3.0\n")
    monkeypatch.setattr("earthlens.flopros.catalog.CATALOG_PATH", bad)
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(bad)


def test_catalog_path_points_at_bundled_yaml():
    """`CATALOG_PATH` is the shipped `flopros_data_catalog.yaml`."""
    assert CATALOG_PATH.name == "flopros_data_catalog.yaml"
    assert CATALOG_PATH.is_file()


def test_get_catalog_returns_the_datasets_map():
    """`get_catalog()` returns the same object as `datasets`."""
    cat = Catalog()
    assert cat.get_catalog() is cat.datasets


def test_invalid_row_rejected(tmp_path, monkeypatch):
    """A row missing a required field fails validation with a listing error."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("datasets:\n  flopros:\n    shapefile_stem: X\n")  # no `url`
    monkeypatch.setattr("earthlens.flopros.catalog.CATALOG_PATH", bad)
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)
