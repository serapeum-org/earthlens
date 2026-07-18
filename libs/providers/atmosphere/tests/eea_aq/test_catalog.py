"""Tests for the EEA (`eea_aq`) pollutant catalog loader."""

from __future__ import annotations

import pytest

from earthlens.eea_aq import CATALOG_PATH, Catalog, Pollutant
from earthlens.eea_aq.catalog import clear_catalog_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the parse cache around each test."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.eea
class TestCatalog:
    """Loading and resolving EEA pollutants."""

    def test_six_criteria_pollutants(self):
        """The shipped catalog lists the six criteria pollutants."""
        assert sorted(Catalog().pollutants) == ["co", "no2", "o3", "pm10", "pm25", "so2"]

    def test_polls_for_maps_and_dedupes(self):
        """`polls_for` maps names to airbase notations, de-duplicated."""
        assert Catalog().polls_for(["pm25", "o3", "pm25"]) == ["PM2.5", "O3"]

    def test_code_to_name_reverse_map(self):
        """`code_to_name` maps the numeric EEA code back to the name."""
        assert Catalog().code_to_name()[6001] == "pm25"

    def test_unknown_raises(self):
        """An unknown pollutant name raises."""
        with pytest.raises(ValueError):
            Catalog().get_pollutant("pm2.5")

    def test_pollutants_alias(self):
        """`pollutants` aliases the base `datasets` field."""
        cat = Catalog()
        assert cat.pollutants is cat.datasets

    def test_construct_from_kwarg(self):
        """`Catalog(pollutants=...)` skips the disk read."""
        cat = Catalog(pollutants={"x": {"name": "x", "poll": "X", "code": 1}})
        assert cat.polls_for(["x"]) == ["X"]

    def test_missing_block_raises(self, tmp_path):
        """An empty `pollutants:` block raises."""
        bad = tmp_path / "cat.yaml"
        bad.write_text("pollutants:\n", encoding="utf-8")
        with pytest.raises(ValueError, match="pollutants"):
            Catalog.load(bad)

    def test_cache_returns_equal(self):
        """A repeated load returns equal data via the cache."""
        assert list(Catalog.load(CATALOG_PATH).pollutants) == list(
            Catalog.load(CATALOG_PATH).pollutants
        )


@pytest.mark.eea
def test_pollutant_requires_code():
    """A `Pollutant` requires `poll` and `code`."""
    row = Pollutant(name="pm25", poll="PM2.5", code=6001)
    assert (row.poll, row.code) == ("PM2.5", 6001)
