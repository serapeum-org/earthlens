"""Unit tests for the FABDEM catalog loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.fabdem.catalog import (
    CATALOG_PATH,
    Catalog,
    Dataset,
    _parse_catalog,
    clear_catalog_cache,
)

pytestmark = pytest.mark.fabdem


class TestCatalog:
    """Tests for the bundled FABDEM catalog."""

    def test_loads_single_product(self):
        """The bundled catalog exposes the fabdem product."""
        cat = Catalog()
        assert list(cat.datasets) == ["fabdem"]
        assert cat.available_datasets == ["fabdem"]

    def test_license_and_attribution(self):
        """The catalog carries the non-commercial licence + attribution."""
        cat = Catalog()
        assert cat.license_id == "CC-BY-NC-SA-4.0"
        assert "Hawker" in cat.attribution
        assert "Fathom" in cat.commercial_contact

    def test_row_fields(self):
        """The fabdem row carries the elevation band and version."""
        row = Catalog().get("fabdem")
        assert isinstance(row, Dataset)
        assert (row.band, row.version, row.units) == ("elevation", "V1-2", "m")
        assert row.spatial_resolution == 30

    def test_unknown_key_raises_did_you_mean(self):
        """An unknown key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="fabdem"):
            Catalog().get("fabdm")

    def test_load_classmethod_matches_default(self):
        """Catalog.load() with no path reads the bundled catalog."""
        assert list(Catalog.load().datasets) == ["fabdem"]


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
        bad.write_text("datasets:\n  fabdem:\n    bogus: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            _parse_catalog([bad])

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same map as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_clear_cache_is_callable(self):
        """clear_catalog_cache empties the parse cache without error."""
        Catalog()
        clear_catalog_cache()
        assert list(Catalog().datasets) == ["fabdem"]

    def test_catalog_path_points_at_yaml(self):
        """CATALOG_PATH is the bundled fabdem YAML."""
        assert CATALOG_PATH.name == "fabdem_data_catalog.yaml"
        assert CATALOG_PATH.exists()
