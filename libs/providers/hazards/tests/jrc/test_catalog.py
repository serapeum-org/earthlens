"""Unit tests for the JRC-flood catalog loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.jrc.catalog import (
    CATALOG_PATH,
    Catalog,
    Dataset,
    _parse_catalog,
    clear_catalog_cache,
)

pytestmark = pytest.mark.jrc


class TestCatalog:
    """Tests for the bundled JRC-flood catalog."""

    def test_loads_single_product(self):
        """The bundled catalog exposes the efhm product."""
        cat = Catalog()
        assert list(cat.datasets) == ["efhm"]
        assert cat.available_datasets == ["efhm"]

    def test_permissive_license(self):
        """The EFHM is CC-BY-4.0 (permissive)."""
        assert Catalog().license_id == "CC-BY-4.0"

    def test_row_return_periods(self):
        """The efhm row carries the 9 published return periods + band."""
        row = Catalog().get("efhm")
        assert isinstance(row, Dataset)
        assert row.band == "water_depth"
        assert row.return_periods == [10, 20, 30, 40, 50, 75, 100, 200, 500]
        assert row.filename_template == "Europe_RP{rp}_filled_depth.tif"

    def test_unknown_key_raises(self):
        """An unknown key raises a did-you-mean error."""
        catalog = Catalog()
        with pytest.raises(ValueError, match="efhm"):
            catalog.get("efmh")

    def test_load_classmethod(self):
        """Catalog.load() reads the bundled catalog."""
        assert list(Catalog.load().datasets) == ["efhm"]


class TestParseCatalog:
    """Tests for the low-level YAML parser."""

    def test_missing_datasets_block_raises(self, tmp_path: Path):
        """A YAML without a datasets block raises a clear error."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("license: X\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'datasets:'"):
            _parse_catalog([empty])

    def test_invalid_row_raises(self, tmp_path: Path):
        """A row with an unknown field fails validation with a clear error."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("datasets:\n  efhm:\n    bogus: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            _parse_catalog([bad])

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same map as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_clear_cache_callable(self):
        """clear_catalog_cache empties the parse cache without error."""
        Catalog()
        clear_catalog_cache()
        assert list(Catalog().datasets) == ["efhm"]

    def test_catalog_path_points_at_yaml(self):
        """CATALOG_PATH is the bundled EFHM YAML."""
        assert CATALOG_PATH.name == "jrc_data_catalog.yaml"
        assert CATALOG_PATH.exists()
