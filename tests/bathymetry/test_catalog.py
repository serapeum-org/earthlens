"""Unit tests for the bathymetry DEM catalog."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.bathymetry import Catalog, Dataset
from earthlens.bathymetry.catalog import Transport

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
