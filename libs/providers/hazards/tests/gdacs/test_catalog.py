"""Unit tests for `earthlens.gdacs.catalog` (hazard-type dispatch table)."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.gdacs.catalog import CATALOG_PATH

from earthlens.gdacs import Catalog, HazardType

_EXPECTED_CODES = ["DR", "EQ", "FL", "TC", "VO", "WF"]


@pytest.mark.gdacs
class TestHazardType:
    """The `HazardType` row model and its defaults."""

    def test_minimal_construction(self):
        """A hazard type needs only a `name`; description defaults empty."""
        h = HazardType(name="Earthquake")
        assert h.name == "Earthquake"
        assert h.description == ""

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            HazardType(name="Earthquake", bogus="x")


@pytest.mark.gdacs
class TestCatalogLoad:
    """`Catalog` loads and validates the bundled hazard YAML."""

    def test_bundled_catalog_path_exists(self):
        """The shipped catalog YAML is present as package data."""
        assert CATALOG_PATH.is_file(), f"missing bundled catalog at {CATALOG_PATH}"

    def test_codes(self):
        """The bundled catalog lists all six GDACS hazard codes."""
        assert Catalog().codes() == _EXPECTED_CODES

    def test_get_hazard_fields(self):
        """The EQ row resolves to its display name."""
        eq = Catalog().get_hazard("EQ")
        assert eq.name == "Earthquake"

    def test_contains_and_len(self):
        """The inherited dict-like surface works over `datasets`."""
        cat = Catalog()
        assert "EQ" in cat
        assert len(cat) == 6

    def test_getitem(self):
        """`cat['TC']` resolves via the inherited mapping protocol."""
        assert Catalog()["TC"].name == "Tropical cyclone"

    def test_get_catalog_returns_datasets(self):
        """`get_catalog` returns the same hazard map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_get_hazard_unknown_raises_with_hint(self):
        """An unknown but close code raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'EQ'"):
            Catalog().get_hazard("EQK")

    def test_get_hazard_unknown_lists_known(self):
        """The error names the GDACS hazard catalog and known codes."""
        with pytest.raises(ValueError, match="GDACS hazard catalog"):
            Catalog().get_hazard("nope")


@pytest.mark.gdacs
class TestCatalogLoadErrors:
    """`Catalog.load` fails loudly on a malformed catalog file."""

    def test_missing_hazard_types_block(self, tmp_path: Path):
        """A YAML with no `hazard_types:` block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'hazard_types:' block"):
            Catalog.load(bad)

    def test_invalid_row(self, tmp_path: Path):
        """A row with an unknown field raises ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "hazard_types:\n  EQ:\n    name: Earthquake\n    bogus: x\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)

    def test_explicit_datasets_skip_disk(self):
        """Passing `datasets=` skips the disk read (no auto-load)."""
        cat = Catalog(datasets={"EQ": HazardType(name="Earthquake")})
        assert cat.codes() == ["EQ"]
