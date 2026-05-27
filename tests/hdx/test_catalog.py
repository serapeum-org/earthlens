"""Unit tests for the HDX catalog loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from earthlens.hdx import Catalog, HdxDataset
from earthlens.hdx import catalog as catalog_mod
from earthlens.hdx.catalog import (
    _load_catalog_data,
    _yaml_files_for,
    clear_catalog_cache,
)

pytestmark = pytest.mark.hdx


@pytest.fixture
def temp_catalog_dir(tmp_path: Path) -> Path:
    """Write a tiny two-file catalog directory and return its path."""
    (tmp_path / "a.yaml").write_text(
        textwrap.dedent(
            """
            datasets:
              ds-one:
                hdx_id: real-one
                org: org-a
                title: One
                themes: [population]
                formats: [CSV]
                resource_filter: CSV
                output_kinds: [tabular]
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "_index.yaml").write_text(
        "available_datasets: [real-one, real-two]\n", encoding="utf-8"
    )
    return tmp_path


class TestHdxDataset:
    """Tests for the HdxDataset model."""

    def test_defaults(self):
        """Only hdx_id is required; the rest default to empty."""
        row = HdxDataset(hdx_id="x")
        assert row.hdx_id == "x"
        assert row.org == "" and row.themes == [] and row.resource_filter == ""

    def test_frozen(self):
        """Rows are immutable."""
        row = HdxDataset(hdx_id="x")
        with pytest.raises(Exception):
            row.hdx_id = "y"

    def test_extra_forbidden(self):
        """An unknown field is rejected."""
        with pytest.raises(Exception):
            HdxDataset(hdx_id="x", bogus=1)

    def test_invalid_output_kind_rejected(self):
        """output_kinds is constrained to the known literals."""
        with pytest.raises(Exception):
            HdxDataset(hdx_id="x", output_kinds=["spreadsheet"])


class TestCatalog:
    """Tests for the bundled-catalog loader and lookups."""

    def test_bundled_loads_many(self):
        """The shipped catalog carries a sizeable curated set."""
        catalog = Catalog()
        assert len(catalog) >= 40

    def test_resolve_known_key(self):
        """A curated key resolves to its HDX id."""
        assert (
            Catalog().resolve("kontur-population").hdx_id == "kontur-population-dataset"
        )

    def test_get_dataset_unknown_did_you_mean(self):
        """An unknown key raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean"):
            Catalog().get_dataset("kontur-populaton")

    def test_contains_and_getitem(self):
        """Membership and dict-style access agree."""
        catalog = Catalog()
        assert "kontur-population" in catalog
        assert catalog["kontur-population"].org == "kontur"

    def test_getitem_missing_raises_keyerror(self):
        """`cat[missing]` raises KeyError."""
        with pytest.raises(KeyError):
            Catalog()["does-not-exist"]

    def test_all_three_output_kinds_present(self):
        """The curated set spans raster, vector and tabular (mixed backend)."""
        kinds = {k for row in Catalog().datasets.values() for k in row.output_kinds}
        assert {"raster", "vector", "tabular"} <= kinds

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same map as the datasets field."""
        catalog = Catalog()
        assert catalog.get_catalog() is catalog.datasets

    def test_load_from_temp_dir(self, temp_catalog_dir: Path):
        """Loading a custom directory merges datasets and the index."""
        clear_catalog_cache()
        catalog = Catalog.load(catalog_path=temp_catalog_dir)
        assert catalog.resolve("ds-one").hdx_id == "real-one"
        assert catalog.available_datasets == ["real-one", "real-two"]

    def test_load_from_single_file(self, temp_catalog_dir: Path):
        """A single YAML file is also a valid catalog path."""
        clear_catalog_cache()
        catalog = Catalog.load(catalog_path=temp_catalog_dir / "a.yaml")
        assert "ds-one" in catalog


class TestLoaderHelpers:
    """Tests for the module-level loader helpers."""

    def test_yaml_files_for_missing_path_raises(self, tmp_path: Path):
        """A non-existent catalog path raises ValueError."""
        with pytest.raises(ValueError, match="does not exist"):
            _yaml_files_for(tmp_path / "nope")

    def test_empty_datasets_block_raises(self, tmp_path: Path):
        """A catalog with no datasets is rejected."""
        (tmp_path / "x.yaml").write_text("available_datasets: []\n", encoding="utf-8")
        clear_catalog_cache()
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            _load_catalog_data(tmp_path)

    def test_duplicate_key_across_files_raises(self, tmp_path: Path):
        """A dataset key declared in two files is rejected."""
        body = "datasets:\n  dup:\n    hdx_id: a\n"
        (tmp_path / "one.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "two.yaml").write_text(body, encoding="utf-8")
        clear_catalog_cache()
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_invalid_row_raises_with_origin(self, tmp_path: Path):
        """A row failing validation names its origin file."""
        (tmp_path / "bad.yaml").write_text(
            "datasets:\n  b:\n    hdx_id: a\n    bogus: 1\n", encoding="utf-8"
        )
        clear_catalog_cache()
        with pytest.raises(ValueError, match="failed validation"):
            _load_catalog_data(tmp_path)

    def test_cache_returns_same_object(self, temp_catalog_dir: Path):
        """A second load of an unchanged path hits the parse cache."""
        clear_catalog_cache()
        first = _load_catalog_data(temp_catalog_dir)
        second = _load_catalog_data(temp_catalog_dir)
        assert first is second

    def test_clear_cache_forces_reparse(self, temp_catalog_dir: Path):
        """Clearing the cache yields a fresh parse."""
        clear_catalog_cache()
        first = _load_catalog_data(temp_catalog_dir)
        clear_catalog_cache()
        second = _load_catalog_data(temp_catalog_dir)
        assert first is not second

    def test_catalog_path_is_directory(self):
        """The bundled CATALOG_PATH points at the per-theme directory."""
        assert catalog_mod.CATALOG_PATH.is_dir()

    def test_load_available_reads_json(self, tmp_path: Path):
        """The JSON `_available.json` index is read and cached."""
        import json

        (tmp_path / "_available.json").write_text(
            json.dumps({"available_datasets": ["a", "b"]}), encoding="utf-8"
        )
        clear_catalog_cache()
        names = catalog_mod._load_available(tmp_path / "_available.json")
        assert names == ["a", "b"]

    def test_load_available_absent_returns_empty(self, tmp_path: Path):
        """A missing JSON index yields an empty list (no error)."""
        clear_catalog_cache()
        assert catalog_mod._load_available(tmp_path / "_available.json") == []


class TestBundledAvailableIndex:
    """Tests for the bundled JSON available-datasets index."""

    def test_index_is_large_and_covers_curated(self):
        """The bundled index spans the whole HDX catalogue and covers curated."""
        catalog = Catalog()
        index = set(catalog.available_datasets)
        assert len(index) >= 20000
        curated = {row.hdx_id for row in catalog.datasets.values()}
        assert curated <= index

    def test_long_tail_id_resolves_to_thin_row(self):
        """Any id in the full index resolves to a thin HdxDataset (cached set)."""
        catalog = Catalog()
        first, second = catalog.available_datasets[0], catalog.available_datasets[-1]
        assert catalog.get_dataset(first).hdx_id == first
        # second resolve reuses the cached membership set
        assert catalog.get_dataset(second).hdx_id == second

    def test_long_tail_not_a_member(self):
        """The long tail resolves but is not reported by `in` (curated only)."""
        catalog = Catalog()
        some_id = catalog.available_datasets[0]
        assert some_id not in catalog or some_id in catalog.datasets

    def test_unknown_id_raises_with_hint(self):
        """An id in neither curated nor the index raises with a hint."""
        with pytest.raises(ValueError, match="available"):
            Catalog().get_dataset("definitely-not-an-hdx-dataset-zzz")

    def test_catalog_loads_without_yaml_index(self):
        """The curated YAML glob no longer includes a `_index.yaml`."""
        files = catalog_mod._yaml_files_for(catalog_mod.CATALOG_PATH)
        assert not any(f.name == "_index.yaml" for f in files)
