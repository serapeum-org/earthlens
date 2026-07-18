"""Unit tests for the DEM catalog loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from earthlens.dem import CATALOG_PATH, Catalog, DEMDataset, clear_catalog_cache

pytestmark = [pytest.mark.dem, pytest.mark.unit]


class TestBundledCatalog:
    """The shipped `dem_data_catalog.yaml` parses and exposes both DEMs."""

    def test_catalog_path_exists(self):
        """The bundled YAML ships with the package."""
        assert CATALOG_PATH.is_file()

    def test_datasets_present(self):
        """Both curated datasets are visible under `datasets`."""
        cat = Catalog()
        assert sorted(cat.datasets) == ["cop-dem-glo-30", "cop-dem-glo-90"]

    def test_available_index_matches_datasets(self):
        """`available_datasets` mirrors the curated keys, sorted."""
        cat = Catalog()
        assert cat.available_datasets == sorted(cat.datasets)

    def test_glo30_row_pins_bucket_and_token(self):
        """GLO-30 resolves to the 30-m bucket and the `10` token."""
        row = Catalog().get_dataset("cop-dem-glo-30")
        assert row.bucket == "copernicus-dem-30m"
        assert row.resolution_token == "10"
        assert row.region == "eu-central-1"
        assert row.native_resolution_m == 30

    def test_glo90_row_pins_bucket_and_token(self):
        """GLO-90 resolves to the 90-m bucket and the `30` token."""
        row = Catalog().get_dataset("cop-dem-glo-90")
        assert row.bucket == "copernicus-dem-90m"
        assert row.resolution_token == "30"


class TestDidYouMean:
    """A close-but-wrong key surfaces a helpful hint."""

    def test_hint_on_typo(self):
        """A near miss reports the closest curated key."""
        with pytest.raises(ValueError, match="Did you mean 'cop-dem-glo-30'"):
            Catalog().get_dataset("cop-dem-glo-3")


class TestLoaderErrors:
    """The loader rejects an empty or duplicate-key catalog."""

    def test_empty_yaml_raises(self, tmp_path: Path):
        """A YAML with no `datasets:` block fails fast."""
        clear_catalog_cache()
        empty = tmp_path / "empty.yaml"
        empty.write_text("datasets: {}\n")
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            Catalog.load(empty)

    def test_duplicate_keys_rejected(self, tmp_path: Path):
        """A duplicate dataset key is rejected by the strict loader."""
        clear_catalog_cache()
        dup = tmp_path / "dup.yaml"
        dup.write_text(
            textwrap.dedent(
                """
                datasets:
                  cop-dem-glo-30:
                    bucket: copernicus-dem-30m
                    resolution_token: '10'
                  cop-dem-glo-30:
                    bucket: copernicus-dem-30m
                    resolution_token: '10'
                """
            )
        )
        with pytest.raises(ValueError):
            Catalog.load(dup)

    def test_missing_bucket_raises(self, tmp_path: Path):
        """A dataset row missing the required `bucket` field fails validation."""
        clear_catalog_cache()
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            textwrap.dedent(
                """
                datasets:
                  broken:
                    resolution_token: '10'
                """
            )
        )
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)


class TestParseCache:
    """The `(path, mtime)` cache keeps repeat loads cheap."""

    def test_cache_hit_returns_same_object(self, tmp_path: Path):
        """A second `Catalog.load(path)` reuses the cached instance."""
        clear_catalog_cache()
        path = tmp_path / "cat.yaml"
        path.write_text(
            textwrap.dedent(
                """
                datasets:
                  cop-dem-glo-30:
                    bucket: copernicus-dem-30m
                    resolution_token: '10'
                """
            )
        )
        first = Catalog.load(path)
        second = Catalog.load(path)
        assert first is second

    def test_missing_file_reported_by_pydantic(self, tmp_path: Path):
        """`load(nonexistent_path)` reports the missing / empty catalog."""
        clear_catalog_cache()
        ghost = tmp_path / "does-not-exist.yaml"
        with pytest.raises(FileNotFoundError):
            Catalog.load(ghost)

    def test_get_catalog_returns_datasets_map(self):
        """`get_catalog()` satisfies the `AbstractCatalog` contract."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_clear_cache_forces_reload(self, tmp_path: Path):
        """After `clear_catalog_cache`, a fresh instance is loaded."""
        path = tmp_path / "cat.yaml"
        path.write_text(
            textwrap.dedent(
                """
                datasets:
                  cop-dem-glo-30:
                    bucket: copernicus-dem-30m
                    resolution_token: '10'
                """
            )
        )
        first = Catalog.load(path)
        clear_catalog_cache()
        second = Catalog.load(path)
        assert first is not second
        assert isinstance(second.get_dataset("cop-dem-glo-30"), DEMDataset)
