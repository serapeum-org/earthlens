"""Unit tests for `earthlens.s3.catalog`."""

from __future__ import annotations

import pytest
from earthlens.s3.catalog import Catalog, Dataset, Variable, clear_catalog_cache

pytestmark = [pytest.mark.s3]


@pytest.fixture
def catalog():
    """A freshly-loaded registry catalog."""
    clear_catalog_cache()
    return Catalog()


def test_lists_the_seed_datasets(catalog):
    """All curated datasets load, sorted by name."""
    assert catalog.dataset_names() == [
        "copernicus-dem",
        "era5",
        "esa-worldcover",
        "goes",
        "naip-source",
        "sentinel-2-l2a",
        "usgs-landsat",
    ]


@pytest.mark.parametrize(
    "name,region", [("usgs-landsat", "us-west-2"), ("naip-source", "us-east-1")]
)
def test_requester_pays_datasets_carry_region(catalog, name, region):
    """The requester-pays datasets declare requester_pays + their region."""
    ds = catalog.resolve(name)
    assert ds.requester_pays is True and ds.region == region


def test_public_datasets_are_not_requester_pays(catalog):
    """The public datasets default to requester_pays=False."""
    assert catalog.resolve("era5").requester_pays is False


def test_era5_resolves_to_the_ncar_bucket(catalog):
    """ERA5 points at the live NCAR mirror, not the dead Planet OS bucket."""
    era5 = catalog.resolve("era5")
    assert era5.bucket == "nsf-ncar-era5" and era5.format == "netcdf"


@pytest.mark.parametrize("key", ["t2m", "2m_temperature", "128_167_2t"])
def test_variable_resolves_by_friendly_alias_and_raw_token(catalog, key):
    """A friendly name, an alias, and the raw token map to one native token."""
    assert catalog.resolve("era5").resolve_variable(key).native == "128_167_2t"


def test_default_variables_used_when_none_requested(catalog):
    """resolve_variables falls back to the dataset defaults."""
    natives = [
        v.native for v in catalog.resolve("sentinel-2-l2a").resolve_variables(None)
    ]
    assert natives == ["B04", "B03", "B02"]


def test_resolve_variables_dedupes_by_native(catalog):
    """A variable requested twice is resolved once."""
    s2 = catalog.resolve("sentinel-2-l2a")
    assert len(s2.resolve_variables(["red", "B04", "red"])) == 1


def test_inline_spec_is_a_passthrough_dataset(catalog):
    """An inline dict resolves to a Dataset (the passthrough path)."""
    ds = catalog.resolve(
        {"bucket": "my-bucket", "format": "cog", "layout": "deterministic_tiles"}
    )
    assert isinstance(ds, Dataset) and ds.bucket == "my-bucket"


def test_already_built_dataset_passes_through(catalog):
    """Resolving a Dataset returns it unchanged."""
    ds = catalog.resolve("goes")
    assert catalog.resolve(ds) is ds


def test_unknown_dataset_raises_did_you_mean(catalog):
    """An unknown name lists the registry and suggests the closest."""
    with pytest.raises(ValueError, match=r"Did you mean 'era5'"):
        catalog.resolve("era-5")


def test_inline_spec_rejects_unknown_fields(catalog):
    """extra=forbid catches a typo'd passthrough field."""
    with pytest.raises(ValueError):
        catalog.resolve(
            {"bucket": "b", "format": "cog", "layout": "deterministic_tiles", "oops": 1}
        )


def test_unknown_variable_raises_did_you_mean(catalog):
    """An unknown variable suggests the closest known one."""
    with pytest.raises(ValueError, match=r"is not a variable"):
        catalog.resolve("era5").resolve_variable("zzz")


def test_catalog_cache_returns_equivalent_data():
    """A second load (cache hit) yields the same dataset set."""
    clear_catalog_cache()
    first = Catalog().dataset_names()
    assert Catalog().dataset_names() == first


def test_variable_requires_a_native_token():
    """A Variable without a native token fails validation."""
    with pytest.raises(Exception):
        Variable()


def test_load_empty_datasets_block_raises(tmp_path):
    """A YAML with no datasets block is rejected."""
    clear_catalog_cache()
    bad = tmp_path / "empty.yaml"
    bad.write_text("available_datasets: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(bad)


def test_load_malformed_row_raises(tmp_path):
    """A dataset row that fails validation names the offending dataset."""
    clear_catalog_cache()
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "datasets:\n  oops:\n    bucket: b\n    format: zzz\n    layout: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="oops"):
        Catalog.load(bad)


def test_load_missing_file_raises(tmp_path):
    """Loading a non-existent catalog path raises rather than silently empty."""
    clear_catalog_cache()
    with pytest.raises(Exception):
        Catalog.load(tmp_path / "nope.yaml")
