"""Tests for the Sensor.Community pollutant catalog loader."""

from __future__ import annotations

import pytest

from earthlens.sensor_community import CATALOG_PATH, Catalog, Pollutant
from earthlens.sensor_community.catalog import clear_catalog_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the parse cache around each test."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.sensor_community
class TestCatalog:
    """Loading and resolving Sensor.Community pollutants."""

    def test_registered_pollutants(self):
        """The shipped catalog lists the expected pollutants."""
        assert sorted(Catalog().pollutants) == [
            "humidity", "pm1", "pm10", "pm25", "pressure", "temperature",
        ]

    def test_columns_for(self):
        """`columns_for` maps CSV columns to pollutant names."""
        assert Catalog().columns_for(["pm25", "pm10"]) == {"P2": "pm25", "P1": "pm10"}

    def test_sensor_types_for_union(self):
        """`sensor_types_for` unions the serving sensor types."""
        types = Catalog().sensor_types_for(["pm25", "temperature"])
        assert "sds011" in types and "dht22" in types

    def test_unknown_raises(self):
        """An unknown pollutant name raises."""
        with pytest.raises(ValueError):
            Catalog().get_pollutant("ozone")

    def test_construct_from_kwarg(self):
        """`Catalog(pollutants=...)` skips the disk read."""
        cat = Catalog(
            pollutants={"x": {"name": "x", "column": "P2", "sensor_types": ["sds011"]}}
        )
        assert cat.get_pollutant("x").column == "P2"

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


@pytest.mark.sensor_community
def test_pollutant_requires_sensor_types():
    """A `Pollutant` requires at least one sensor type."""
    with pytest.raises(ValueError):
        Pollutant(name="x", column="P2", sensor_types=[])
