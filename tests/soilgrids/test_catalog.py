"""Unit tests for the soilgrids sharded catalog loader and Property rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.soilgrids import Catalog, Property
from earthlens.soilgrids.catalog import (
    CATALOG_PATH,
    _load_catalog_data,
    clear_catalog_cache,
)

pytestmark = pytest.mark.soilgrids

EXPECTED_PROPERTIES = {
    "clay", "sand", "silt", "cfvo", "phh2o", "cec",
    "nitrogen", "soc", "ocd", "ocs", "bdod",
}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """The bundled SoilGrids catalog."""
    return Catalog()


def _write_single_catalog(path: Path, body: str) -> Path:
    """Write a one-file catalog YAML to path and return it."""
    path.write_text(body, encoding="utf-8")
    return path


def test_parameters_lists_all_curated_properties(catalog: Catalog) -> None:
    """parameters() returns the sorted curated property ids."""
    assert catalog.parameters() == sorted(EXPECTED_PROPERTIES)


def test_available_datasets_matches_curated_keys(catalog: Catalog) -> None:
    """Every curated property id appears in the _index.yaml available list."""
    assert set(catalog.available_datasets) == EXPECTED_PROPERTIES
    assert set(catalog.datasets) == EXPECTED_PROPERTIES


def test_phh2o_row_carries_scaled_integer_units(catalog: Catalog) -> None:
    """phh2o records the pH unit and the *10 scale factor."""
    row = catalog.get("phh2o")
    assert row.unit == "pH"
    assert row.scale_factor == 10.0
    assert row.endpoint.endswith("phh2o.map")


def test_ocs_row_has_single_depth(catalog: Catalog) -> None:
    """The ocs carbon-stock property publishes a single 0-30cm depth."""
    assert catalog.get("ocs").depths == ["0-30cm"]


def test_every_row_is_complete(catalog: Catalog) -> None:
    """Every property row has an endpoint plus non-empty depths and quantiles."""
    for row in catalog.datasets.values():
        assert isinstance(row, Property)
        assert row.endpoint.startswith("https://maps.isric.org/")
        assert row.depths
        assert row.quantiles
        assert "mean" in row.quantiles
        assert row.license_note


def test_standard_properties_have_six_depths(catalog: Catalog) -> None:
    """Every property except ocs publishes the six standard depths."""
    for pid, row in catalog.datasets.items():
        if pid == "ocs":
            continue
        assert len(row.depths) == 6


def test_get_unknown_property_did_you_mean(catalog: Catalog) -> None:
    """An unknown property id raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'clay'"):
        catalog.get("clayy")


def test_get_catalog_returns_the_property_map(catalog: Catalog) -> None:
    """get_catalog() returns the same map exposed as datasets."""
    assert catalog.get_catalog() is catalog.datasets


def test_default_catalog_path_is_the_bundled_directory() -> None:
    """CATALOG_PATH points at the bundled per-group catalog directory."""
    assert CATALOG_PATH.name == "catalog"
    assert CATALOG_PATH.is_dir()


def test_single_file_catalog_loads(tmp_path: Path) -> None:
    """A monolithic single-file catalog loads (the test back-compat path)."""
    yaml_file = _write_single_catalog(
        tmp_path / "mono.yaml",
        "available_datasets: [clay]\n"
        "datasets:\n"
        "  clay:\n"
        '    endpoint: "https://maps.isric.org/mapserv?map=/map/clay.map"\n'
        "    depths: ['0-5cm']\n"
        "    quantiles: ['mean']\n",
    )
    cat = Catalog.load(catalog_path=yaml_file)
    assert cat.parameters() == ["clay"]
    assert cat.get("clay").depths == ["0-5cm"]


def test_duplicate_property_across_files_raises(tmp_path: Path) -> None:
    """A property declared in two catalog files raises ValueError."""
    common = (
        "datasets:\n"
        "  clay:\n"
        '    endpoint: "https://maps.isric.org/mapserv?map=/map/clay.map"\n'
        "    depths: ['0-5cm']\n"
        "    quantiles: ['mean']\n"
    )
    (tmp_path / "a.yaml").write_text("available_datasets: [clay]\n" + common, "utf-8")
    (tmp_path / "b.yaml").write_text(common, "utf-8")
    clear_catalog_cache()
    with pytest.raises(ValueError, match="declared in two catalog files"):
        Catalog.load(catalog_path=tmp_path)


def test_curated_id_missing_from_index_raises(tmp_path: Path) -> None:
    """A property present in datasets but absent from the index raises."""
    yaml_file = _write_single_catalog(
        tmp_path / "mono.yaml",
        "available_datasets: [sand]\n"
        "datasets:\n"
        "  clay:\n"
        '    endpoint: "https://maps.isric.org/mapserv?map=/map/clay.map"\n'
        "    depths: ['0-5cm']\n"
        "    quantiles: ['mean']\n",
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="missing from"):
        Catalog.load(catalog_path=yaml_file)


def test_empty_datasets_block_raises(tmp_path: Path) -> None:
    """A catalog with no datasets block raises ValueError."""
    yaml_file = _write_single_catalog(tmp_path / "empty.yaml", "available_datasets: []\n")
    clear_catalog_cache()
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(catalog_path=yaml_file)


def test_invalid_row_raises_validation_error(tmp_path: Path) -> None:
    """A row missing the required endpoint raises a validation ValueError."""
    yaml_file = _write_single_catalog(
        tmp_path / "bad.yaml",
        "available_datasets: [clay]\n"
        "datasets:\n"
        "  clay:\n"
        "    depths: ['0-5cm']\n"
        "    quantiles: ['mean']\n",
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(catalog_path=yaml_file)


def test_missing_catalog_path_raises(tmp_path: Path) -> None:
    """A non-existent catalog path raises ValueError."""
    clear_catalog_cache()
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(catalog_path=tmp_path / "nope")


def test_load_is_cached_by_path_and_mtime() -> None:
    """A second load of the bundled catalog reuses the cached parse result."""
    clear_catalog_cache()
    first = _load_catalog_data(CATALOG_PATH)
    second = _load_catalog_data(CATALOG_PATH)
    assert first is second


def test_clear_cache_forces_reparse() -> None:
    """clear_catalog_cache() drops the entry so the next load re-parses."""
    first = _load_catalog_data(CATALOG_PATH)
    clear_catalog_cache()
    second = _load_catalog_data(CATALOG_PATH)
    assert first is not second
    assert first[1].keys() == second[1].keys()
