"""Unit tests for `earthlens.catrare.catalog`."""

from __future__ import annotations

import pytest

from earthlens.catrare import CATALOG_PATH, Catalog, CatRaReDataset
from earthlens.catrare.catalog import clear_catalog_cache

pytestmark = pytest.mark.catrare


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Reset the module-level parse cache so tmp-file rewrites work."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


def test_bundled_catalog_loads_both_thresholds():
    """`Catalog()` loads the `t5` and `w3` threshold rows."""
    cat = Catalog()
    assert sorted(cat.datasets) == ["t5", "w3"]
    assert isinstance(cat.get("t5"), CatRaReDataset)


def test_threshold_codes():
    """The rows carry the uppercase threshold codes used in file names."""
    assert Catalog().get("t5").threshold == "T5"
    assert Catalog().get("w3").threshold == "W3"


def test_shared_metadata_present():
    """Version, CRS, licence, and event columns are loaded."""
    cat = Catalog()
    assert cat.version == "v2026.01"
    assert cat.version_tag == "v2026_01"
    assert cat.source_crs.startswith("+proj=stere")
    assert cat.license == "CC-BY-4.0"
    assert "Event_ID" in cat.event_columns
    assert cat.date_columns == {"start": "Date_START", "end": "Date_END"}


def test_download_url_composed():
    """`download_url` composes the versioned `.gdb.zip` path."""
    url = Catalog().download_url("t5")
    assert url.endswith(
        "CatRaRE_v2026.01/data/CatRaRE_2001_2025_T5_Eta_v2026_01.gdb.zip"
    )


def test_layer_name_composed():
    """`layer_name` composes the `<stem>_<layer>_<tag>` FileGDB layer name."""
    cat = Catalog()
    assert (
        cat.layer_name("t5", "zones") == "CatRaRE_2001_2025_T5_Eta_EventZones_v2026_01"
    )
    assert (
        cat.layer_name("w3", "points")
        == "CatRaRE_2001_2025_W3_Eta_RRmaxPoints_v2026_01"
    )


def test_layer_name_unknown_geometry_raises():
    """An unknown geometry kind raises a listing `ValueError`."""
    cat = Catalog()
    with pytest.raises(ValueError, match="is not a CatRaRE geometry kind"):
        cat.layer_name("t5", "lines")


def test_unknown_threshold_raises_with_hint():
    """An unknown threshold key raises a listing `ValueError`."""
    cat = Catalog()
    with pytest.raises(ValueError, match="CatRaRE catalog"):
        cat.get("t9")


def test_get_catalog_returns_datasets_map():
    """`get_catalog()` returns the same object as `datasets`."""
    cat = Catalog()
    assert cat.get_catalog() is cat.datasets


def test_empty_datasets_block_rejected(tmp_path, monkeypatch):
    """A catalog file with no `datasets:` block fails loud."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("license: CC-BY-4.0\n")
    monkeypatch.setattr("earthlens.catrare.catalog.CATALOG_PATH", bad)
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(bad)


def test_invalid_row_rejected(tmp_path, monkeypatch):
    """A row missing the required `threshold` field fails validation."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("datasets:\n  t5:\n    description: x\n")
    monkeypatch.setattr("earthlens.catrare.catalog.CATALOG_PATH", bad)
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)


def test_catalog_path_points_at_bundled_yaml():
    """`CATALOG_PATH` is the shipped `catrare_data_catalog.yaml`."""
    assert CATALOG_PATH.name == "catrare_data_catalog.yaml"
    assert CATALOG_PATH.is_file()
