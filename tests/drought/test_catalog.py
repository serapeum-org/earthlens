"""Catalog loader / row validation tests for `earthlens.drought`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.drought import Catalog, Dataset
from earthlens.drought import catalog as catalog_module


def test_catalog_loads_curated_rows():
    """Bundled catalog parses and the index covers every curated id."""
    cat = Catalog()
    assert len(cat.datasets) >= 25
    assert set(cat.datasets) <= set(cat.available_datasets)


def test_usdm_row_is_vector_geojson():
    """USDM is the only vector row and routes through usdm-geojson."""
    ds = Catalog().get("usdm")
    assert ds.output_kind == "vector"
    assert ds.transport == "usdm-geojson"
    assert ds.cadence == "weekly"
    assert "{ymd}" in ds.endpoint
    assert ds.coverage is None
    assert ds.native_crs == "EPSG:4326"


def test_edo_row_is_raster_wcs():
    """An EDO indicator row carries a WCS coverage id and routes edo-wcs."""
    ds = Catalog().get("edo-spaST")
    assert ds.output_kind == "raster"
    assert ds.transport == "edo-wcs"
    assert ds.coverage == "spaST"
    assert ds.endpoint.endswith("map=DO_WCS")


def test_gdo_row_uses_global_map_switcher():
    """GDO rows hit the same MapServer with the global map switcher."""
    ds = Catalog().get("gdo-spaST")
    assert ds.output_kind == "raster"
    assert ds.transport == "edo-wcs"
    assert ds.endpoint.endswith("map=GDO_WCS")


def test_speibase_row_is_raster_netcdf():
    """SPEIbase rows route through netcdf-url and ship a literal URL."""
    ds = Catalog().get("speibase-12")
    assert ds.output_kind == "raster"
    assert ds.transport == "netcdf-url"
    assert ds.cadence == "monthly"
    assert ds.endpoint.endswith("spei12.nc")
    assert ds.coverage is None


def test_unknown_id_raises_did_you_mean():
    """Misspelled id surfaces a sorted Known datasets list."""
    cat = Catalog()
    with pytest.raises(ValueError) as excinfo:
        cat.get("usdmm")
    msg = str(excinfo.value)
    assert "drought catalog" in msg
    assert "Known datasets" in msg
    assert "Did you mean 'usdm'" in msg


def test_dataset_row_rejects_unknown_transport():
    """Unknown transport in the YAML is a validation error."""
    with pytest.raises(ValueError):
        Dataset(
            id="bogus",
            source="x",
            transport="bogus-tx",  # type: ignore[arg-type]
            endpoint="https://example.com",
            output_kind="raster",
            cadence="monthly",
        )


def test_dataset_row_rejects_unknown_output_kind():
    """Unknown output_kind in the YAML is a validation error."""
    with pytest.raises(ValueError):
        Dataset(
            id="bogus",
            source="x",
            transport="netcdf-url",
            endpoint="https://example.com",
            output_kind="tabular",  # type: ignore[arg-type]
            cadence="monthly",
        )


def test_catalog_path_redirect_to_single_file(tmp_path, monkeypatch):
    """`CATALOG_PATH` can redirect to a single YAML for tests."""
    yaml_text = """
available_datasets:
  - mini-usdm
datasets:
  mini-usdm:
    source: "test"
    transport: usdm-geojson
    endpoint: "https://example.com/{ymd}.json"
    output_kind: vector
    cadence: weekly
    native_crs: "EPSG:4326"
"""
    p = tmp_path / "mini.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", p)
    catalog_module.clear_catalog_cache()
    cat = Catalog()
    assert list(cat.datasets) == ["mini-usdm"]


def test_clear_catalog_cache_drops_entries(tmp_path, monkeypatch):
    """`clear_catalog_cache` empties the loader cache."""
    catalog_module.clear_catalog_cache()
    Catalog()  # populates the cache
    assert catalog_module._CATALOG_CACHE
    catalog_module.clear_catalog_cache()
    assert catalog_module._CATALOG_CACHE == {}


def test_catalog_dir_with_missing_index_in_available_raises(
    tmp_path, monkeypatch
):
    """A curated id missing from `available_datasets:` is an error."""
    (tmp_path / "a.yaml").write_text(
        """
available_datasets:
  - other-id
datasets:
  not-listed:
    source: "test"
    transport: usdm-geojson
    endpoint: "https://example.com/{ymd}.json"
    output_kind: vector
    cadence: weekly
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", tmp_path)
    catalog_module.clear_catalog_cache()
    with pytest.raises(ValueError, match="absent from `available_datasets:`"):
        Catalog()


def test_catalog_path_missing_raises():
    """A non-existent catalog path raises a clear ValueError."""
    with pytest.raises(ValueError, match="does not exist"):
        catalog_module._yaml_files_for(Path("does/not/exist"))
