"""Unit tests for the bathymetry DEM catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.bathymetry import Catalog, Dataset
from earthlens.bathymetry.catalog import Transport, clear_catalog_cache

pytestmark = pytest.mark.bathymetry

_ROW = "    endpoint: https://x/erddap\n    dataset_id: A\n    variable: z\n"

VALID_TRANSPORTS = set(Transport.__args__)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """A loaded bathymetry catalog."""
    return Catalog()


def test_catalog_loads_curated_dems(catalog: Catalog):
    """The shipped catalog carries the GEBCO, ETOPO, and EMODnet rows."""
    assert sorted(catalog.datasets) == [
        "emodnet",
        "emodnet_2016",
        "emodnet_2018",
        "emodnet_2020",
        "emodnet_2022",
        "etopo1_bedrock",
        "etopo1_ice",
        "gebco_2020",
    ]


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


def test_emodnet_row_fields(catalog: Catalog):
    """The EMODnet row carries the live-pinned WCS transport fields."""
    row = catalog.get("emodnet")
    assert row.transport == "wcs"
    assert row.endpoint == "https://ows.emodnet-bathymetry.eu/wcs"
    assert row.dataset_id == "emodnet:mean"
    assert row.wcs_version == "1.0.0"
    assert row.crs == "EPSG:4326"
    assert row.native_bbox == (-70.5, 11.0, 43.0, 90.0)
    assert row.variable == "elevation"


def test_emodnet_licence_recorded(catalog: Catalog):
    """The EMODnet row records the required attribution / DOI licence note."""
    note = catalog.get("emodnet").license_note
    assert "EMODnet" in note
    assert "doi:10.12770" in note


@pytest.mark.parametrize(
    "dataset_id, coverage",
    [
        ("emodnet_2016", "emodnet:mean_2016"),
        ("emodnet_2018", "emodnet:mean_2018"),
        ("emodnet_2020", "emodnet:mean_2020"),
        ("emodnet_2022", "emodnet:mean_2022"),
    ],
)
def test_emodnet_release_variants(catalog: Catalog, dataset_id: str, coverage: str):
    """Each year-stamped release resolves to its own colon coverage id."""
    row = catalog.get(dataset_id)
    assert row.transport == "wcs"
    assert row.dataset_id == coverage
    assert row.wcs_version == "1.0.0"


def test_wcs_row_missing_version_rejected():
    """A wcs row without wcs_version fails validation."""
    with pytest.raises(ValidationError, match="wcs_version"):
        Dataset(
            transport="wcs",
            endpoint="https://x/wcs",
            dataset_id="c:mean",
            variable="elevation",
            native_bbox=(-1.0, -1.0, 1.0, 1.0),
        )


def test_wcs_row_missing_native_bbox_rejected():
    """A wcs row without native_bbox fails validation."""
    with pytest.raises(ValidationError, match="native_bbox"):
        Dataset(
            transport="wcs",
            endpoint="https://x/wcs",
            dataset_id="c:mean",
            variable="elevation",
            wcs_version="1.0.0",
        )


def test_wcs_row_blank_version_rejected():
    """A wcs row whose wcs_version is only whitespace fails validation."""
    with pytest.raises(ValidationError, match="wcs_version"):
        Dataset(
            transport="wcs",
            endpoint="https://x/wcs",
            dataset_id="c:mean",
            variable="elevation",
            wcs_version="  ",
            native_bbox=(-1.0, -1.0, 1.0, 1.0),
        )


def test_wcs_row_degenerate_bbox_rejected():
    """A wcs row with a zero-area native_bbox fails validation."""
    with pytest.raises(ValidationError, match="degenerate native_bbox"):
        Dataset(
            transport="wcs",
            endpoint="https://x/wcs",
            dataset_id="c:mean",
            variable="elevation",
            wcs_version="1.0.0",
            native_bbox=(0.0, 0.0, 0.0, 0.0),
        )


def test_griddap_row_needs_no_wcs_fields():
    """A griddap row builds without the WCS-only fields (they stay defaulted)."""
    row = Dataset(
        transport="erddap-griddap",
        endpoint="https://x/erddap",
        dataset_id="A",
        variable="z",
    )
    assert row.wcs_version == ""
    assert row.native_bbox is None


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
