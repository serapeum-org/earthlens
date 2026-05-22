"""Unit tests for `earthlens.openaq.catalog` (parameter dispatch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.openaq import Catalog, Parameter
from earthlens.openaq.catalog import CATALOG_PATH


@pytest.mark.openaq
class TestParameter:
    """The Parameter row model and its defaults."""

    def test_minimal_construction(self):
        """A parameter needs id/name/units; display_name and group default."""
        p = Parameter(id=2, name="pm25", units="µg/m³")
        assert p.id == 2
        assert p.display_name == ""
        assert p.group == "other"

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            Parameter(id=2, name="pm25", units="µg/m³", bogus="x")


@pytest.mark.openaq
class TestCatalogLoad:
    """Catalog loads and validates the bundled parameter YAML."""

    def test_bundled_catalog_path_exists(self):
        """The shipped catalog YAML is present as package data."""
        assert CATALOG_PATH.is_file(), f"missing bundled catalog at {CATALOG_PATH}"

    def test_known_parameters(self):
        """The bundled catalog lists the curated parameter set."""
        assert sorted(Catalog().parameters) == [
            "bc", "co", "no", "no2", "o3", "pm10",
            "pm25", "pressure", "relativehumidity", "so2", "temperature",
        ]

    def test_pm25_id(self):
        """pm25 resolves to OpenAQ parameters_id 2."""
        assert Catalog().get_parameter("pm25").id == 2

    def test_ids_for_preserves_order(self):
        """ids_for returns ids in the requested name order."""
        assert Catalog().ids_for(["pm25", "no2"]) == [2, 15]

    def test_get_catalog_returns_parameters(self):
        """get_catalog returns the same parameter map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.parameters

    def test_get_parameter_unknown_raises_with_hint(self):
        """A close typo raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'pm25'"):
            Catalog().get_parameter("pm2.5")

    def test_get_parameter_unknown_lists_known(self):
        """An unknown name error names the catalog kind."""
        with pytest.raises(ValueError, match="OpenAQ parameter catalog"):
            Catalog().get_parameter("zzz")

    def test_ids_for_unknown_raises(self):
        """ids_for propagates the unknown-name error."""
        with pytest.raises(ValueError):
            Catalog().ids_for(["pm25", "nope"])


@pytest.mark.openaq
class TestCatalogLoadErrors:
    """Catalog.load fails loudly on a malformed catalog file."""

    def test_missing_parameters_block(self, tmp_path: Path):
        """A YAML with no parameters: block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'parameters:' block"):
            Catalog.load(bad)

    def test_invalid_row(self, tmp_path: Path):
        """A row missing required fields raises ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("parameters:\n  x:\n    id: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)

    def test_explicit_parameters_skip_disk(self):
        """Passing parameters= skips the disk read (no auto-load)."""
        cat = Catalog(parameters={"x": Parameter(id=1, name="x", units="u")})
        assert sorted(cat.parameters) == ["x"]
