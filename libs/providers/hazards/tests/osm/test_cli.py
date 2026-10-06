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

    def test_flags_live_row_missing_query_template(self):
        """A live row without a query_template is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "live:x": SimpleNamespace(
                    protocol="live", query_template="", geometry_types=["Point"]
                )
            }
        )
        checked, issues = validator(catalog)
        assert checked == 1
        assert any("missing query_template" in i for i in issues)

    def test_flags_history_row_missing_filter(self):
        """A history row without an ohsome_filter is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "history:x": SimpleNamespace(
                    protocol="history", ohsome_filter="", geometry_types=["Polygon"]
                )
            }
        )
        _checked, issues = validator(catalog)
        assert any("missing ohsome_filter" in i for i in issues)

    def test_flags_bulk_row_missing_method(self):
        """A bulk row without a pyrosm_method is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bulk:x": SimpleNamespace(
                    protocol="bulk", pyrosm_method="", geometry_types=["Polygon"]
                )
            }
        )
        _checked, issues = validator(catalog)
        assert any("missing pyrosm_method" in i for i in issues)
