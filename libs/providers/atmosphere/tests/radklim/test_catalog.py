"""Unit tests for the RADKLIM catalog loader (no network)."""

from __future__ import annotations

import pytest

from earthlens.radklim.catalog import (
    CATALOG_PATH,
    Catalog,
    RadklimProduct,
    clear_catalog_cache,
)

pytestmark = [pytest.mark.radklim, pytest.mark.unit]


class TestCatalog:
    """Tests for Catalog loading and lookups."""

    def test_products_are_the_four_datasets(self):
        """The bundled catalog lists the four RADKLIM / RADOLAN products."""
        assert Catalog().products() == [
            "radklim-rw",
            "radklim-yw",
            "radolan-rw",
            "radolan-yw",
        ]

    def test_license_and_grid_load(self):
        """The top-level licence and grid id are read off the YAML."""
        cat = Catalog()
        assert cat.license == "CC-BY-4.0/GeoNutzV"
        assert cat.grid["id"] == "radolan-polar-stereographic"

    def test_reproc_and_operational_rows(self):
        """A reproc row keeps its version; an operational row keeps its retention."""
        yw = Catalog().get_product("radklim-yw")
        assert (yw.stream, yw.code, yw.default_format, yw.version) == (
            "reproc",
            "YW",
            "nc",
            "2017_002",
        )
        op = Catalog().get_product("radolan-rw")
        assert (op.stream, op.code, op.default_format, op.retention_days) == (
            "operational",
            "rw",
            "hdf5",
            2,
        )

    def test_unknown_product_raises_did_you_mean(self):
        """An unknown product key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'radklim-yw'"):
            Catalog().get_product("radklim-y")

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same object as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_available_datasets_sorted(self):
        """available_datasets mirrors the sorted product keys."""
        assert Catalog().available_datasets == Catalog().products()

    def test_load_from_explicit_path(self):
        """load() accepts an explicit catalog path."""
        assert Catalog.load(CATALOG_PATH).products()[0] == "radklim-rw"

    def test_injected_datasets_skip_disk(self):
        """Passing datasets= builds a Catalog without reading disk."""
        row = RadklimProduct(
            product="x",
            stream="reproc",
            code="RW",
            step_minutes=60,
            default_format="nc",
        )
        cat = Catalog(datasets={"x": row})
        assert cat.products() == ["x"]


class TestParseErrors:
    """Tests for catalog parse-time validation."""

    def test_missing_products_block_raises(self, tmp_path):
        """A YAML with no products block is rejected."""
        clear_catalog_cache()
        bad = tmp_path / "bad.yaml"
        bad.write_text("license: X\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'products:' block"):
            Catalog.load(bad)

    def test_bad_row_raises_with_key(self, tmp_path):
        """A product row missing a required field names the offending key."""
        clear_catalog_cache()
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "products:\n  radklim-yw:\n    stream: reproc\n    code: YW\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="product 'radklim-yw' failed validation"):
            Catalog.load(bad)


class TestRadklimProduct:
    """Tests for the RadklimProduct row model."""

    def test_extra_field_forbidden(self):
        """An unexpected field is rejected (extra='forbid')."""
        with pytest.raises(ValueError):
            RadklimProduct(
                product="x",
                stream="reproc",
                code="RW",
                step_minutes=60,
                default_format="nc",
                bogus=1,
            )
