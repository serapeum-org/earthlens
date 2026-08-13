"""Tests for the OSM catalog-tooling validator (`earthlens.osm.cli`).

Moved out of core's CLI test suite when the OSM validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.osm.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the OSM structural lint."""

    def test_flags_overpass_row_missing_query_template(self):
        """An overpass row without a query_template is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "overpass:x": SimpleNamespace(
                    protocol="overpass", query_template="", geometry_types=["Point"]
                )
            }
        )
        checked, issues = validator(catalog)
        assert checked == 1
        assert any("missing query_template" in i for i in issues)

    def test_flags_ohsome_row_missing_filter(self):
        """An ohsome row without an ohsome_filter is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "ohsome:x": SimpleNamespace(
                    protocol="ohsome", ohsome_filter="", geometry_types=["Polygon"]
                )
            }
        )
        _checked, issues = validator(catalog)
        assert any("missing ohsome_filter" in i for i in issues)

    def test_flags_pbf_row_missing_method(self):
        """A pbf row without a pyrosm_method is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "pbf:x": SimpleNamespace(
                    protocol="pbf", pyrosm_method="", geometry_types=["Polygon"]
                )
            }
        )
        _checked, issues = validator(catalog)
        assert any("missing pyrosm_method" in i for i in issues)
