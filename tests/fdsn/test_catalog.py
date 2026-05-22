"""Unit tests for `earthlens.fdsn.catalog` (provider dispatch table)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.fdsn import Catalog, Provider
from earthlens.fdsn.catalog import CATALOG_PATH


@pytest.mark.fdsn
class TestProvider:
    """The `Provider` row model and its defaults."""

    def test_minimal_construction(self):
        """A provider needs only an `fdsn_id`; the rest default."""
        p = Provider(fdsn_id="USGS")
        assert p.fdsn_id == "USGS"
        assert p.needs_token is False
        assert p.default_min_magnitude == 4.5

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            Provider(fdsn_id="USGS", bogus="x")


@pytest.mark.fdsn
class TestCatalogLoad:
    """`Catalog` loads and validates the bundled provider YAML."""

    def test_bundled_catalog_path_exists(self):
        """The shipped catalog YAML is present as package data."""
        assert CATALOG_PATH.is_file(), f"missing bundled catalog at {CATALOG_PATH}"

    def test_known_providers(self):
        """The bundled catalog lists all six curated FDSN networks."""
        assert sorted(Catalog().providers) == [
            "EARTHSCOPE",
            "EMSC",
            "GEONET",
            "INGV",
            "ISC",
            "USGS",
        ]

    def test_usgs_row_fields(self):
        """The USGS row resolves to the obspy URL_MAPPINGS key."""
        usgs = Catalog().get_provider("USGS")
        assert usgs.fdsn_id == "USGS"
        assert usgs.needs_token is False

    @pytest.mark.parametrize(
        "name, fdsn_id, min_mag",
        [
            ("ISC", "ISC", 4.5),
            ("GEONET", "GEONET", 3.0),
        ],
    )
    def test_added_provider_rows(self, name: str, fdsn_id: str, min_mag: float):
        """The ISC and GeoNet rows resolve with their expected fields."""
        provider = Catalog().get_provider(name)
        assert provider.fdsn_id == fdsn_id
        assert provider.needs_token is False
        assert provider.default_min_magnitude == min_mag

    def test_get_catalog_returns_datasets(self):
        """`get_catalog` returns the framework `datasets` map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_providers_aliases_datasets(self):
        """`providers` and `datasets` expose the same network rows."""
        cat = Catalog()
        assert sorted(cat.providers) == sorted(cat.datasets)
        assert cat.providers == cat.datasets

    def test_dict_surface_like_other_backends(self):
        """The inherited len/contains/getitem/iter surface works (not empty)."""
        cat = Catalog()
        assert len(cat) == 6, f"expected 6 networks, got {len(cat)}"
        assert "USGS" in cat
        assert cat["USGS"].fdsn_id == "USGS"
        assert set(iter(cat)) == {"EARTHSCOPE", "EMSC", "GEONET", "INGV", "ISC", "USGS"}

    def test_get_dataset_alias(self):
        """`get_dataset` resolves a network too (alias of get_provider)."""
        assert Catalog().get_dataset("EMSC").fdsn_id == "EMSC"

    def test_get_provider_unknown_raises_with_hint(self):
        """An unknown but close name raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'USGS'"):
            Catalog().get_provider("USG")

    def test_get_provider_unknown_lists_known(self):
        """The error lists the known providers."""
        with pytest.raises(ValueError, match="not a registered provider"):
            Catalog().get_provider("nope")


@pytest.mark.fdsn
class TestCatalogLoadErrors:
    """`Catalog.load` fails loudly on a malformed catalog file."""

    def test_missing_providers_block(self, tmp_path: Path):
        """A YAML with no `providers:` block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'providers:' block"):
            Catalog.load(bad)

    def test_invalid_row(self, tmp_path: Path):
        """A row missing the required `fdsn_id` raises ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("providers:\n  X:\n    title: no id\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)

    def test_explicit_providers_skip_disk(self):
        """Passing `providers=` skips the disk read (no auto-load)."""
        cat = Catalog(providers={"X": Provider(fdsn_id="USGS")})
        assert sorted(cat.providers) == ["X"]
