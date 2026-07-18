"""Unit tests for the admin boundary catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.admin import Catalog, Dataset
from earthlens.admin.catalog import Provider, clear_catalog_cache

pytestmark = pytest.mark.admin

VALID_PROVIDERS = set(Provider.__args__)

_ROW = "    provider: cgaz\n    url_template: https://x/{level}.gpkg\n"

_EXPECTED_IDS = [
    "cgaz:adm0",
    "cgaz:adm1",
    "cgaz:adm2",
    "geoboundaries:adm0",
    "geoboundaries:adm1",
    "geoboundaries:adm2",
    "geoboundaries:adm3",
    "geoboundaries:adm4",
    "geoboundaries:adm5",
    "natural_earth:countries",
    "natural_earth:states",
    "tiger:county",
    "tiger:nation",
    "tiger:state",
    "tiger:tract",
]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """A loaded admin catalog."""
    return Catalog()


def test_catalog_loads_all_curated_rows(catalog: Catalog):
    """The shipped catalog carries every curated dataset across four providers."""
    assert sorted(catalog.datasets) == _EXPECTED_IDS


def test_available_datasets_matches_curated(catalog: Catalog):
    """Every curated id is listed in the _index.yaml availability index."""
    assert sorted(catalog.available_datasets) == sorted(catalog.datasets)


@pytest.mark.parametrize("dataset_id", _EXPECTED_IDS)
def test_every_row_has_required_fields(catalog: Catalog, dataset_id: str):
    """Each row carries a valid provider, a native_crs, a url_template, and its id."""
    row = catalog.get(dataset_id)
    assert row.provider in VALID_PROVIDERS
    assert row.native_crs
    assert row.url_template
    assert row.id == dataset_id


def test_geoboundaries_rows_require_country(catalog: Catalog):
    """Every geoBoundaries row requires the country selector and an ADM level."""
    for level in range(6):
        row = catalog.get(f"geoboundaries:adm{level}")
        assert row.required_selectors == ("country",)
        assert row.adm_level == f"ADM{level}"


def test_cgaz_rows_are_selectorless_and_undefined_crs(catalog: Catalog):
    """CGAZ rows need no selector and carry the unlabelled-CRS marker."""
    row = catalog.get("cgaz:adm1")
    assert row.required_selectors == ()
    assert row.native_crs == "undefined"
    assert row.adm_level == "ADM1"


def test_natural_earth_rows_carry_layer_and_default_scale(catalog: Catalog):
    """Natural Earth rows carry a layer fragment and a default scale."""
    row = catalog.get("natural_earth:countries")
    assert row.layer == "admin_0_countries"
    assert row.default_scale == "110m"


def test_tiger_rows_carry_year_resolution_entity(catalog: Catalog):
    """TIGER rows carry an entity layer, a resolution, a default year, and NAD83."""
    row = catalog.get("tiger:county")
    assert row.layer == "county"
    assert row.resolution == "500k"
    assert row.default_year == 2023
    assert row.native_crs == "EPSG:4269"


def test_tiger_tract_is_per_state(catalog: Catalog):
    """The TIGER tract row is per-state and requires the state selector."""
    row = catalog.get("tiger:tract")
    assert row.per_state is True
    assert row.required_selectors == ("state",)


def test_unknown_id_raises_did_you_mean(catalog: Catalog):
    """An unknown id raises a ValueError naming a close match."""
    with pytest.raises(ValueError, match="geoboundaries:adm1"):
        catalog.get("geoboundaries:adm1x")


def test_get_returns_frozen_dataset(catalog: Catalog):
    """get() returns a frozen Dataset row that rejects mutation."""
    row = catalog.get("tiger:state")
    assert isinstance(row, Dataset)
    with pytest.raises(ValidationError):
        row.provider = "cgaz"


def test_dataset_requires_provider_and_url_template():
    """A Dataset row without provider / url_template fails to build."""
    with pytest.raises(ValidationError):
        Dataset(adm_level="ADM0")


def test_dataset_rejects_unknown_provider():
    """A Dataset row with an out-of-set provider is rejected."""
    with pytest.raises(ValidationError):
        Dataset(provider="gadm", url_template="https://x")


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
        "available_datasets: [a]\ndatasets:\n  a:\n    adm_level: ADM0\n"
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(catalog_path=folder)


def test_parse_cache_reuses_loaded_data():
    """Two Catalog() builds share the mtime-keyed parse cache."""
    clear_catalog_cache()
    first = Catalog()
    second = Catalog()
    assert sorted(first.datasets) == sorted(second.datasets)
