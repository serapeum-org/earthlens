"""Tests for the AirNow pollutant catalog loader."""

from __future__ import annotations

import pytest

from earthlens.airnow import CATALOG_PATH, Catalog, Pollutant
from earthlens.airnow.catalog import clear_catalog_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the parse cache around each test so rewrites take effect."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.airnow
class TestCatalogLoad:
    """Loading the bundled catalog and resolving pollutants."""

    def test_bundled_catalog_has_six_criteria_pollutants(self):
        """The shipped catalog lists the six AirNow criteria pollutants."""
        cat = Catalog()
        assert sorted(cat.pollutants) == ["co", "no2", "o3", "pm10", "pm25", "so2"]

    def test_pollutants_alias_is_datasets(self):
        """`pollutants` aliases the base `datasets` field."""
        cat = Catalog()
        assert cat.pollutants is cat.datasets

    def test_get_pollutant_resolves_code(self):
        """`get_pollutant` returns the row with the AirNow code."""
        assert Catalog().get_pollutant("pm25").code == "PM25"

    def test_codes_for_maps_and_dedupes(self):
        """`codes_for` maps names to codes, order-stable, de-duplicated."""
        assert Catalog().codes_for(["pm25", "o3", "pm25"]) == ["PM25", "OZONE"]

    def test_unknown_pollutant_raises_with_hint(self):
        """An unknown-but-close name raises with a did-you-mean hint."""
        with pytest.raises(ValueError) as exc:
            Catalog().get_pollutant("pm2.5")
        assert "pm25" in str(exc.value)


@pytest.mark.airnow
class TestCatalogValidation:
    """Construction from in-memory rows and error paths."""

    def test_construct_from_pollutants_kwarg(self):
        """`Catalog(pollutants=...)` skips the disk read."""
        cat = Catalog(pollutants={"x": {"name": "x", "code": "X"}})
        assert cat.get_pollutant("x").code == "X"

    def test_missing_block_raises(self, tmp_path, monkeypatch):
        """An empty `pollutants:` block raises a clear error."""
        bad = tmp_path / "cat.yaml"
        bad.write_text("pollutants:\n", encoding="utf-8")
        monkeypatch.setattr("earthlens.airnow.catalog.CATALOG_PATH", bad)
        with pytest.raises(ValueError, match="pollutants"):
            Catalog.load(bad)

    def test_malformed_row_raises(self, tmp_path):
        """A row missing the required `code` fails validation."""
        bad = tmp_path / "cat.yaml"
        bad.write_text("pollutants:\n  x: { name: x }\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)

    def test_cache_returns_equal_catalog(self):
        """A repeated load hits the parse cache and returns equal data."""
        first = Catalog.load(CATALOG_PATH)
        second = Catalog.load(CATALOG_PATH)
        assert list(first.pollutants) == list(second.pollutants)


@pytest.mark.airnow
def test_pollutant_defaults():
    """A `Pollutant` row defaults units/display/group sensibly."""
    row = Pollutant(name="pm25", code="PM25")
    assert row.group == "other" and row.units == ""
