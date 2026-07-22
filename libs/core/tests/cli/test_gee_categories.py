"""Unit tests for `earthlens.cli._gee_categories`."""

from __future__ import annotations

import pytest
from earthlens.cli._gee_categories import _RULES, CATEGORIES, categorise_asset

pytestmark = pytest.mark.cli


class TestCategoriseAsset:
    """Tests for categorise_asset (the per-family routing rules)."""

    @pytest.mark.parametrize(
        "asset_id, title, expected",
        [
            ("COPERNICUS/S1_GRD", "Sentinel-1 SAR GRD", "sar-radar"),
            ("UCSB-CHG/CHIRPS/DAILY", "CHIRPS Daily", "precipitation"),
            ("NASA/GDDP-CMIP6", "NEX GDDP CMIP6", "climate-reanalysis"),
            ("projects/foo/bar", "Community asset", "community"),
            ("LANDSAT/LC09/C02/T1_L2", "Landsat 9", "optical-multispectral"),
        ],
    )
    def test_id_prefix_rules(self, asset_id, title, expected):
        """A leading-path match routes the asset to its family file.

        Args:
            asset_id: The Earth Engine asset id.
            title: The asset title (unused by an id_prefix rule).
            expected: The category file stem.
        """
        assert categorise_asset(asset_id, title) == expected, asset_id

    def test_id_contains_rule(self):
        """An `id_contains` rule matches a needle anywhere in the id."""
        assert categorise_asset("FOO/3B-DAY/x", "") == "precipitation"

    def test_title_keyword_rule(self):
        """A `title_kw` rule matches when the id misses but the title hits."""
        assert categorise_asset("ZZZ/unknown", "Aerosol optical depth") == (
            "atmosphere-chemistry"
        )

    def test_unmatched_falls_through_to_other(self):
        """An asset matching no rule lands in `other`."""
        assert categorise_asset("ZZZ/nothing", "mystery dataset") == "other"


class TestRulesTable:
    """Sanity checks on the rule table itself."""

    def test_every_rule_targets_a_known_category(self):
        """Each rule's target category is one of the declared CATEGORIES."""
        unknown = {category for _, _, category in _RULES if category not in CATEGORIES}
        assert not unknown, f"rules target undeclared categories: {unknown}"

    def test_rule_kinds_are_recognised(self):
        """Every rule uses one of the three supported match kinds."""
        kinds = {kind for kind, _, _ in _RULES}
        assert kinds <= {"id_prefix", "id_contains", "title_kw"}, kinds
