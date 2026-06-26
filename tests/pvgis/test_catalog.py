"""Unit tests for the PVGIS product catalog (`earthlens.pvgis.catalog`)."""

from __future__ import annotations

import pytest

from earthlens.pvgis import Catalog, Product
from earthlens.pvgis import catalog as catalog_mod

pytestmark = pytest.mark.pvgis


class TestProduct:
    """Tests for the `Product` row model."""

    def test_defaults(self):
        """A minimal Product fills empty default_params / columns."""
        p = Product(tool="seriescalc", endpoint="seriescalc")
        assert p.default_params == {}, p.default_params
        assert p.columns == [], p.columns
        assert p.description == "", p.description

    def test_frozen_and_extra_forbidden(self):
        """The model is frozen and rejects unknown fields."""
        with pytest.raises(Exception):
            Product(tool="x", endpoint="x", bogus=1)


class TestCatalog:
    """Tests for the `Catalog` loader."""

    def test_available_lists_shipped_products(self):
        """`available` returns the sorted shipped product ids."""
        assert Catalog().available() == ["seriescalc", "tmy"], Catalog().available()

    def test_get_resolves_product(self):
        """`get` resolves a product id to its row."""
        product = Catalog().get("seriescalc")
        assert product.tool == "seriescalc", product.tool
        assert product.endpoint == "seriescalc", product.endpoint
        assert "time" in product.columns, product.columns

    def test_get_unknown_did_you_mean(self):
        """An unknown but close id raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'seriescalc'"):
            Catalog().get("seriescal")

    def test_products_alias_is_datasets(self):
        """The `products` property aliases the base `datasets` map."""
        cat = Catalog()
        assert cat.products is cat.datasets, "products should alias datasets"

    def test_products_kwarg_alias(self):
        """Constructing with `products=` populates `datasets`."""
        row = Product(tool="seriescalc", endpoint="seriescalc", columns=["time"])
        cat = Catalog(products={"seriescalc": row})
        assert cat.get("seriescalc") is row, "products= should seed datasets"

    def test_get_catalog_returns_datasets(self):
        """`get_catalog` returns the same object as `datasets`."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets, "get_catalog should return datasets"

    def test_catalog_integrity(self):
        """Every shipped row carries a tool, endpoint, and non-empty columns."""
        for pid, row in Catalog().datasets.items():
            assert row.tool, f"{pid} has no tool"
            assert row.endpoint, f"{pid} has no endpoint"
            assert row.columns, f"{pid} has no columns"


class TestCatalogLoading:
    """Tests for the on-disk loader, cache, and error paths."""

    def test_cache_returns_same_object(self):
        """Loading the same path twice hits the parse cache."""
        catalog_mod.clear_catalog_cache()
        first = catalog_mod._load_catalog_data(catalog_mod.CATALOG_PATH)
        second = catalog_mod._load_catalog_data(catalog_mod.CATALOG_PATH)
        assert first is second, "second load should return the cached object"

    def test_clear_cache(self):
        """`clear_catalog_cache` empties the parse cache."""
        catalog_mod._load_catalog_data(catalog_mod.CATALOG_PATH)
        catalog_mod.clear_catalog_cache()
        assert catalog_mod._CATALOG_CACHE == {}, "cache should be empty"

    def test_missing_products_block_raises(self, tmp_path):
        """A YAML without a `products:` block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'products:' block"):
            catalog_mod._load_catalog_data(bad)

    def test_malformed_row_raises(self, tmp_path):
        """A row missing required fields raises a validation ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("products:\n  x:\n    columns: [time]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            catalog_mod._load_catalog_data(bad)

    def test_missing_file_raises(self, tmp_path):
        """Loading a non-existent catalog path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            catalog_mod._load_catalog_data(tmp_path / "nope.yaml")

    def test_load_from_explicit_path(self, tmp_path):
        """`Catalog.load` reads an explicit catalog path."""
        good = tmp_path / "cat.yaml"
        good.write_text(
            "products:\n  seriescalc:\n    tool: seriescalc\n"
            "    endpoint: seriescalc\n    columns: [time]\n",
            encoding="utf-8",
        )
        cat = Catalog.load(good)
        assert cat.get("seriescalc").tool == "seriescalc", cat.get("seriescalc")
