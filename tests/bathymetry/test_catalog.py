"""Unit tests for the bathymetry DEM catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.bathymetry import Catalog, Dataset
from earthlens.bathymetry.catalog import Transport, clear_catalog_cache

pytestmark = pytest.mark.bathymetry

_ROW = "    endpoint: https://x/erddap\n" "    dataset_id: A\n" "    variable: z\n"

VALID_TRANSPORTS = set(Transport.__args__)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """A loaded bathymetry catalog."""
    return Catalog()


def test_catalog_loads_three_dems(catalog: Catalog):
    """The shipped catalog carries the three curated DEM rows."""
    assert sorted(catalog.datasets) == ["etopo1_bedrock", "etopo1_ice", "gebco_2020"]


def test_available_datasets_matches_curated(catalog: Catalog):
    """Every curated id is listed in the _index.yaml availability index."""
    assert sorted(catalog.available_datasets) == sorted(catalog.datasets)


def test_etopo_ships_two_variant_rows(catalog: Catalog):
    """ETOPO ships exactly the ice and bedrock variant rows."""
    etopo = [d for d in catalog.datasets if d.startswith("etopo1_")]
    assert sorted(etopo) == ["etopo1_bedrock", "etopo1_ice"]


def test_gebco_row_fields(catalog: Catalog):
    """The GEBCO row carries the live-pinned endpoint, id, and band."""
    row = catalog.get("gebco_2020")
    assert row.transport == "erddap-griddap"
    assert row.dataset_id == "GEBCO_2020"
    assert row.variable == "elevation"
    assert row.lon_convention == "-180..180"


def test_etopo_rows_use_z_band(catalog: Catalog):
    """Both ETOPO variants expose the single `z` elevation band."""
    assert catalog.get("etopo1_ice").variable == "z"
    assert catalog.get("etopo1_bedrock").variable == "z"


@pytest.mark.parametrize("dataset_id", ["gebco_2020", "etopo1_ice", "etopo1_bedrock"])
def test_every_row_has_required_fields(catalog: Catalog, dataset_id: str):
    """Each row has a non-empty endpoint, dataset_id, variable, valid transport."""
    row = catalog.get(dataset_id)
    assert row.endpoint
    assert row.dataset_id
    assert row.variable
    assert row.transport in VALID_TRANSPORTS
    assert row.id == dataset_id


def test_unknown_id_raises_did_you_mean(catalog: Catalog):
    """An unknown id raises a ValueError naming a close match."""
    with pytest.raises(ValueError, match="gebco_2020"):
        catalog.get("gebco2020")


def test_get_returns_frozen_dataset(catalog: Catalog):
    """get() returns a frozen Dataset row that rejects mutation."""
    row = catalog.get("etopo1_ice")
    assert isinstance(row, Dataset)
    with pytest.raises(ValidationError):
        row.variable = "other"


def test_dataset_requires_core_fields():
    """A Dataset row without endpoint / dataset_id / variable fails to build."""
    with pytest.raises(ValidationError):
        Dataset(endpoint="https://x/erddap")


def test_load_from_single_file(tmp_path: Path):
    """The loader accepts a single YAML file, not only a directory."""
    one = tmp_path / "one.yaml"
    one.write_text("available_datasets: [a]\ndatasets:\n  a:\n" + _ROW)
    clear_catalog_cache()
    cat = Catalog.load(catalog_path=one)
    assert "a" in cat.datasets


def test_missing_catalog_path_raises(tmp_path: Path):
    """A non-existent catalog path raises a clear ValueError."""
    clear_catalog_cache()
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(catalog_path=tmp_path / "nope.yaml")


def test_empty_datasets_block_raises(tmp_path: Path):
    """A catalog with no datasets: block raises."""
    folder = tmp_path / "cat"
    folder.mkdir()
    (folder / "x.yaml").write_text("available_datasets: []\n")
    clear_catalog_cache()
    with pytest.raises(ValueError, match="datasets"):
        Catalog.load(catalog_path=folder)


def test_duplicate_id_across_files_raises(tmp_path: Path):
    """The same id declared in two files is rejected."""
    folder = tmp_path / "cat"
    folder.mkdir()
    (folder / "a.yaml").write_text("datasets:\n  a:\n" + _ROW)
    (folder / "b.yaml").write_text("datasets:\n  a:\n" + _ROW)
    clear_catalog_cache()
    with pytest.raises(ValueError, match="two catalog files"):
        Catalog.load(catalog_path=folder)


def test_curated_id_missing_from_index_raises(tmp_path: Path):
    """A curated id absent from available_datasets: is rejected."""
    folder = tmp_path / "cat"
    folder.mkdir()
    (folder / "_index.yaml").write_text("available_datasets: [a]\n")
    (folder / "rows.yaml").write_text("datasets:\n  b:\n" + _ROW)
    clear_catalog_cache()
    with pytest.raises(ValueError, match="missing from"):
        Catalog.load(catalog_path=folder)


def test_invalid_row_raises_value_error(tmp_path: Path):
    """A row missing required fields surfaces as a wrapped ValueError."""
    folder = tmp_path / "cat"
    folder.mkdir()
    (folder / "rows.yaml").write_text(
        "available_datasets: [a]\ndatasets:\n  a:\n    endpoint: https://x/erddap\n"
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(catalog_path=folder)
