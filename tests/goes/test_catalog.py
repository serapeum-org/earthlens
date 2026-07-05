"""Unit tests for the GOES catalog loader and its pydantic rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.goes import Catalog, GOESChannel, GOESDomain, GOESProduct
from earthlens.goes.catalog import CATALOG_PATH, clear_catalog_cache

pytestmark = pytest.mark.goes


class TestCatalogLoad:
    """Tests for Catalog loading and the bundled data."""

    def test_bundled_catalog_loads(self, catalog):
        """The bundled catalog exposes curated products, domains and channels."""
        assert "abi-l2-mcmip" in catalog.products(), "mcmip product should be curated"
        assert set(catalog.domains) == {"C", "F", "M1", "M2"}, "four domains expected"
        assert len(catalog.channels) == 16, "16 ABI channels expected"

    def test_available_datasets_sorted(self, catalog):
        """available_datasets is the sorted product-key index."""
        assert catalog.available_datasets == sorted(catalog.datasets), "index sorted"

    def test_products_sorted(self, catalog):
        """products() returns the product keys sorted."""
        keys = catalog.products()
        assert keys == sorted(keys), "products() should be sorted"

    def test_dict_surface(self, catalog):
        """The catalog offers the inherited dict-like membership + len surface."""
        assert "abi-l1b-rad" in catalog, "membership via __contains__"
        assert len(catalog) == len(catalog.datasets), "len == dataset count"

    def test_parse_cache_reused(self):
        """A second load with an unchanged file returns the cached instance."""
        clear_catalog_cache()
        first = Catalog.load()
        second = Catalog.load()
        assert first is second, "the (path, mtime) cache should return the same object"

    def test_load_missing_products_block_raises(self, tmp_path):
        """A catalog file with no products block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("domains: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'products:' block"):
            Catalog.load(bad)

    def test_load_missing_file_raises(self, tmp_path):
        """Loading a nonexistent path raises rather than silently succeeding."""
        with pytest.raises(FileNotFoundError):
            Catalog.load(tmp_path / "does-not-exist.yaml")

    def test_load_invalid_product_row_raises(self, tmp_path):
        """A product row missing product_group fails validation with the key named."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("products:\n  x:\n    level: L2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="product 'x' failed validation"):
            Catalog.load(bad)

    def test_load_invalid_domain_row_raises(self, tmp_path):
        """A domain row missing prefix_suffix fails validation with the key named."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "products:\n  x:\n    product_group: G\n"
            "domains:\n  C:\n    name: CONUS\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="domain 'C' failed validation"):
            Catalog.load(bad)

    def test_load_invalid_channel_row_raises(self, tmp_path):
        """A channel row missing wavelength_um fails validation with the key named."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "products:\n  x:\n    product_group: G\n"
            "channels:\n  C01:\n    name: Blue\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="channel 'C01' failed validation"):
            Catalog.load(bad)

    def test_catalog_path_points_at_bundled_yaml(self):
        """CATALOG_PATH resolves to the shipped goes_data_catalog.yaml."""
        assert CATALOG_PATH.name == "goes_data_catalog.yaml", "bundled YAML name"
        assert CATALOG_PATH.exists(), "the bundled catalog must ship"

    def test_get_catalog_returns_datasets(self, catalog):
        """get_catalog() returns the datasets map (abstract-contract satisfier)."""
        assert catalog.get_catalog() is catalog.datasets, "same object as datasets"

    def test_injected_datasets_skip_disk_read(self):
        """Constructing with datasets= skips the disk load (used by tests)."""
        product = GOESProduct(product="p", product_group="ABI-L2-P", domains=["F"])
        cat = Catalog(datasets={"p": product})
        assert cat.products() == ["p"], "only the injected product is present"


class TestGetProduct:
    """Tests for Catalog.get_product."""

    def test_returns_row(self, catalog):
        """get_product returns the GOESProduct for a known key."""
        product = catalog.get_product("abi-l2-mcmip")
        assert product.product_group == "ABI-L2-MCMIP", "resolves the product group"
        assert product.band_split is False, "mcmip is a combined multi-band product"

    def test_band_split_product(self, catalog):
        """A band-split product carries the 16 channel tokens as bands."""
        product = catalog.get_product("abi-l1b-rad")
        assert product.band_split is True, "radiances are one file per channel"
        assert product.bands[:2] == ["C01", "C02"], "channel tokens listed as bands"

    def test_unknown_key_raises_did_you_mean(self, catalog):
        """An unknown product raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'abi-l2-mcmip'"):
            catalog.get_product("abi-l2-mcmi")


class TestGetDomain:
    """Tests for Catalog.get_domain."""

    def test_conus_domain(self, catalog):
        """The CONUS domain resolves to the C prefix suffix with no subsector."""
        domain = catalog.get_domain("C")
        assert domain.prefix_suffix == "C", "CONUS uses the C suffix"
        assert domain.subsector == "", "CONUS has no subsector token"

    def test_mesoscale_domains_share_m_prefix(self, catalog):
        """Both mesoscale domains use the shared M prefix with a subsector token."""
        assert catalog.get_domain("M1").prefix_suffix == "M", "M1 uses the M prefix"
        assert catalog.get_domain("M2").subsector == "M2", "M2 carries its subsector"

    def test_unknown_domain_raises(self, catalog):
        """An unknown domain raises ValueError listing the known domains."""
        with pytest.raises(ValueError, match="not a GOES domain"):
            catalog.get_domain("Z")


class TestBucketFor:
    """Tests for Catalog.bucket_for."""

    @pytest.mark.parametrize(
        "satellite, bucket",
        [
            ("east", "noaa-goes19"),
            ("west", "noaa-goes18"),
            ("19", "noaa-goes19"),
            ("18", "noaa-goes18"),
            ("16", "noaa-goes16"),
            ("EAST", "noaa-goes19"),
        ],
    )
    def test_resolves_role_and_number(self, catalog, satellite, bucket):
        """Roles and satellite numbers (case-insensitive) resolve to a bucket."""
        assert catalog.bucket_for(satellite) == bucket, f"{satellite} -> {bucket}"

    def test_unknown_satellite_raises(self, catalog):
        """An unknown satellite raises ValueError listing the known ones."""
        with pytest.raises(ValueError, match="not a known GOES satellite"):
            catalog.bucket_for("north")


class TestRows:
    """Tests for the frozen pydantic row models."""

    def test_channel_row(self, catalog):
        """A channel row carries its wavelength and name."""
        channel = catalog.channels["C02"]
        assert isinstance(channel, GOESChannel), "channels map to GOESChannel"
        assert channel.wavelength_um == 0.64, "C02 is the 0.64um red band"

    def test_domain_row_is_frozen(self):
        """A GOESDomain is frozen (immutable value object)."""
        domain = GOESDomain(prefix_suffix="C")
        with pytest.raises(Exception):
            domain.prefix_suffix = "F"

    def test_product_defaults(self):
        """A GOESProduct defaults level to L2 and default_domain to C."""
        product = GOESProduct(product="p", product_group="ABI-L2-P")
        assert product.level == "L2", "default level"
        assert product.default_domain == "C", "default domain"
