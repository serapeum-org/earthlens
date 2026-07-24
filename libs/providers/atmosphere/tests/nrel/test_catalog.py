"""Unit tests for the NREL product catalog (`earthlens.nrel.catalog`)."""

from __future__ import annotations

import pytest

from earthlens.nrel import Catalog, Product
from earthlens.nrel.catalog import CATALOG_PATH, _load_catalog_data, clear_catalog_cache

pytestmark = pytest.mark.nrel


class TestLoad:
    """Tests for loading the bundled catalog."""

    def test_available_lists_three_products(self):
        """The shipped catalog exposes the three curated products, sorted."""
        assert Catalog().available() == ["nsrdb-psm3", "nsrdb-tmy", "wtk"]

    def test_products_property_aliases_datasets(self):
        """The `products` property returns the same mapping as `datasets`."""
        cat = Catalog()
        assert cat.products is cat.datasets

    def test_get_catalog_returns_mapping(self):
        """`get_catalog` returns the product map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets


class TestRows:
    """Tests for the per-product row contents pinned from the live API."""

    def test_nsrdb_psm3_row(self):
        """The hourly NSRDB row points at the GOES Aggregated v4 endpoint."""
        row = Catalog().get("nsrdb-psm3")
        assert row.source == "nsrdb"
        assert row.endpoint.endswith("nsrdb-GOES-aggregated-v4-0-0-download.csv")
        assert row.names_kind == "year"
        assert row.meta_rows == 2
        assert "ghi" in row.default_attributes

    def test_tmy_row_uses_tmy_names_kind(self):
        """The TMY row is flagged names_kind='tmy'."""
        assert Catalog().get("nsrdb-tmy").names_kind == "tmy"

    def test_wtk_row_has_single_metadata_row(self):
        """The WTK row records its single CSV metadata row."""
        row = Catalog().get("wtk")
        assert row.source == "wtk"
        assert row.meta_rows == 1
        assert row.endpoint.endswith("wtk-download.csv")


class TestErrors:
    """Tests for catalog lookup and validation errors."""

    def test_unknown_close_id_suggests(self):
        """A close miss raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'wtk'"):
            Catalog().get("wkt")

    def test_unknown_id_lists_known(self):
        """An unknown id lists the known products."""
        with pytest.raises(ValueError, match="not in the NREL product catalog"):
            Catalog().get("totally-unknown")

    def test_products_alias_construction(self):
        """Constructing with products= populates the datasets mapping."""
        row = Product(source="nsrdb", endpoint="/x.csv", names_kind="year")
        cat = Catalog(products={"only": row})
        assert cat.get("only") is row

    def test_empty_block_raises(self, tmp_path):
        """A catalog file with no products block raises ValueError."""
        path = tmp_path / "empty.yaml"
        path.write_text("products:\n", encoding="utf-8")
        clear_catalog_cache()
        with pytest.raises(ValueError, match="empty 'products:' block"):
            _load_catalog_data(path)

    def test_missing_file_raises(self, tmp_path):
        """Loading a non-existent catalog path raises rather than caching."""
        clear_catalog_cache()
        with pytest.raises((FileNotFoundError, ValueError)):
            _load_catalog_data(tmp_path / "does-not-exist.yaml")

    def test_bad_row_raises(self, tmp_path):
        """A malformed product row raises a validation ValueError."""
        path = tmp_path / "bad.yaml"
        path.write_text("products:\n  x:\n    endpoint: /x.csv\n", encoding="utf-8")
        clear_catalog_cache()
        with pytest.raises(ValueError, match="failed validation"):
            _load_catalog_data(path)


class TestCache:
    """Tests for the parse cache."""

    def test_cache_is_used_then_cleared(self):
        """A second load hits the cache; clearing forces a re-parse."""
        clear_catalog_cache()
        first = _load_catalog_data(CATALOG_PATH)
        second = _load_catalog_data(CATALOG_PATH)
        assert first is second
        clear_catalog_cache()
        assert _load_catalog_data(CATALOG_PATH) is not first
