"""Unit tests for the HDX resource-filter matcher."""

from __future__ import annotations

import pytest

from earthlens.hdx._helpers import match_resource

pytestmark = pytest.mark.hdx


class TestMatchResource:
    """Tests for match_resource (name glob OR format label)."""

    @pytest.mark.parametrize(
        "name, fmt, rfilter, expected",
        [
            ("kontur_pop.gpkg.gz", "Geopackage", "*.gpkg.gz", True),
            ("kontur_pop.gpkg.gz", "Geopackage", "*.csv", False),
            ("export.zip", "Geopackage", "geopackage", True),
            ("export.zip", "Geopackage", "Geopackage", True),
            ("data.csv", "CSV", "*.csv", True),
            ("data.csv", "CSV", "csv", True),
            ("anything.bin", "CSV", "", True),
            ("anything.bin", "CSV", "   ", True),
            ("roads.shp.zip", "SHP", "shp", True),
            ("roads.shp.zip", "SHP", "geotiff", False),
        ],
    )
    def test_match_matrix(self, name, fmt, rfilter, expected):
        """Name globs and bare format labels match as documented."""
        assert match_resource(name, fmt, rfilter) is expected

    def test_case_insensitive_name(self):
        """Name matching is case-insensitive."""
        assert match_resource("DATA.CSV", "CSV", "*.csv") is True

    def test_case_insensitive_format(self):
        """Format-label matching is case-insensitive."""
        assert match_resource("x.bin", "GeoTIFF", "geotiff") is True
