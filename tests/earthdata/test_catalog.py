"""Unit tests for the Earthdata catalog loader."""

from __future__ import annotations

import pytest

from earthlens.earthdata import Band, Catalog, EarthdataDAAC, EarthdataDataset
from earthlens.earthdata.catalog import clear_catalog_cache

pytestmark = [pytest.mark.earthdata, pytest.mark.unit]

_PROVIDERS = """
daacs:
  GES_DISC:
    daac: GES DISC
    cloud_region: us-west-2
  POCLOUD:
    daac: PO.DAAC
    cloud_region: us-west-2
"""

_DAAC_A = """
datasets:
  RASTER_DS:
    short_name: RASTER
    version: "07"
    daac: GES DISC
    provider: GES_DISC
    output_kind: raster
    format: hdf5
    cloud_hosted: true
    bands:
      precip:
        long_name: Precipitation
        units: mm/hr
"""

_DAAC_B = """
datasets:
  VECTOR_DS:
    short_name: VECTOR
    daac: PO.DAAC
    provider: POCLOUD
    output_kind: vector
    format: hdf5
"""


def _write_catalog(tmp_path, providers=_PROVIDERS, files=(_DAAC_A, _DAAC_B), index=None):
    """Write a providers.yaml + per-DAAC catalog dir under tmp_path."""
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    for i, body in enumerate(files):
        (cat_dir / f"daac_{i}.yaml").write_text(body)
    if index is not None:
        (cat_dir / "_index.yaml").write_text(index)
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(providers)
    return cat_dir, providers_path


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts from a clean parse cache."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestCatalogLoad:
    """Loading, merging, and validating the bundled + temp catalogs."""

    def test_bundled_catalog_loads(self):
        """The shipped catalog loads and covers all nine DAACs."""
        cat = Catalog()
        assert len(cat.datasets) >= 9
        assert len({ds.daac for ds in cat.datasets.values()}) == 9

    def test_bundled_output_kinds(self):
        """The shipped catalog carries both raster and vector rows."""
        cat = Catalog()
        kinds = {ds.output_kind for ds in cat.datasets.values()}
        assert {"raster", "vector"} <= kinds

    def test_multi_file_merge(self, tmp_path):
        """Two per-DAAC files merge into one datasets map."""
        cat_dir, providers = _write_catalog(tmp_path)
        cat = Catalog.load(catalog_path=cat_dir, providers_path=providers)
        assert set(cat.datasets) == {"RASTER_DS", "VECTOR_DS"}

    def test_output_kind_per_row(self, tmp_path):
        """Each row keeps its own output_kind."""
        cat_dir, providers = _write_catalog(tmp_path)
        cat = Catalog.load(catalog_path=cat_dir, providers_path=providers)
        assert cat.get_dataset("RASTER_DS").output_kind == "raster"
        assert cat.get_dataset("VECTOR_DS").output_kind == "vector"

    def test_duplicate_key_across_files_rejected(self, tmp_path):
        """A dataset key declared in two files fails loud."""
        cat_dir, providers = _write_catalog(tmp_path, files=(_DAAC_A, _DAAC_A))
        with pytest.raises(ValueError, match="declared in two"):
            Catalog.load(catalog_path=cat_dir, providers_path=providers)

    def test_unknown_provider_rejected(self, tmp_path):
        """A row naming a provider absent from providers.yaml fails loud."""
        bad = _DAAC_A.replace("provider: GES_DISC", "provider: NOPE")
        cat_dir, providers = _write_catalog(tmp_path, files=(bad,))
        with pytest.raises(ValueError, match="not in providers.yaml"):
            Catalog.load(catalog_path=cat_dir, providers_path=providers)

    def test_empty_datasets_rejected(self, tmp_path):
        """A catalog with no datasets fails loud."""
        cat_dir, providers = _write_catalog(tmp_path, files=("datasets: {}\n",))
        with pytest.raises(ValueError, match="empty 'datasets:'"):
            Catalog.load(catalog_path=cat_dir, providers_path=providers)

    def test_missing_daacs_block_rejected(self, tmp_path):
        """A providers file with no daacs block fails loud."""
        cat_dir, providers = _write_catalog(tmp_path, providers="other: {}\n")
        with pytest.raises(ValueError, match="empty 'daacs:'"):
            Catalog.load(catalog_path=cat_dir, providers_path=providers)

    def test_extra_field_forbidden(self, tmp_path):
        """An unknown field on a dataset row is rejected."""
        bad = _DAAC_A + "    bogus_field: 1\n"
        cat_dir, providers = _write_catalog(tmp_path, files=(bad,))
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(catalog_path=cat_dir, providers_path=providers)

    def test_single_file_catalog(self, tmp_path):
        """A single YAML file (not a dir) loads as the whole catalog."""
        _, providers = _write_catalog(tmp_path)
        single = tmp_path / "one.yaml"
        single.write_text(_DAAC_A)
        cat = Catalog.load(catalog_path=single, providers_path=providers)
        assert set(cat.datasets) == {"RASTER_DS"}

    def test_missing_catalog_path_rejected(self, tmp_path):
        """A nonexistent catalog path fails loud."""
        _, providers = _write_catalog(tmp_path)
        with pytest.raises(ValueError, match="does not exist"):
            Catalog.load(catalog_path=tmp_path / "nope", providers_path=providers)


class TestCatalogLookup:
    """get_dataset / resolve / get_daac behaviour."""

    def test_get_dataset_didyoumean(self):
        """An unknown key surfaces a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean"):
            Catalog().get_dataset("GPM_3IMERGHHL")

    def test_resolve_with_matching_daac(self):
        """resolve() with a matching daac returns the row."""
        ds = Catalog().resolve("GPM_3IMERGHHL_07", daac="GES_DISC")
        assert ds.short_name == "GPM_3IMERGHHL"

    def test_resolve_with_wrong_daac_rejected(self):
        """resolve() with a non-matching daac raises KeyError."""
        with pytest.raises(KeyError, match="not the requested"):
            Catalog().resolve("GEDI04_A_002", daac="GES DISC")

    def test_get_daac(self):
        """get_daac returns the registry entry for a provider code."""
        daac = Catalog().get_daac("POCLOUD")
        assert isinstance(daac, EarthdataDAAC)
        assert daac.daac == "PO.DAAC"

    def test_get_daac_didyoumean(self):
        """An unknown provider code surfaces a did-you-mean hint."""
        with pytest.raises(KeyError, match="Did you mean"):
            Catalog().get_daac("POCLOD")

    def test_contains_and_iter(self):
        """The catalog supports `in` and iteration over keys."""
        cat = Catalog()
        assert "GPM_3IMERGHHL_07" in cat
        assert "GPM_3IMERGHHL_07" in set(cat)


class TestModels:
    """The leaf pydantic models."""

    def test_band_defaults(self):
        """A band defaults to empty strings."""
        assert Band().long_name == "" and Band().units == ""

    def test_dataset_defaults(self):
        """A dataset row defaults to raster / not-cloud-hosted."""
        ds = EarthdataDataset(short_name="X")
        assert ds.output_kind == "raster" and ds.cloud_hosted is False

    def test_dataset_frozen(self):
        """Dataset rows are immutable."""
        ds = EarthdataDataset(short_name="X")
        with pytest.raises(Exception):
            ds.short_name = "Y"
