"""Unit tests for `earthlens.ecmwf._categories`."""

from __future__ import annotations

import pytest

from earthlens.ecmwf._categories import _RULES, CATEGORIES, categorise_dataset

pytestmark = pytest.mark.cli


class TestCategoriseDataset:
    """Tests for categorise_dataset (the per-family routing rules)."""

    @pytest.mark.parametrize(
        "dataset_id, expected",
        [
            ("reanalysis-era5-single-levels", "era5"),
            ("reanalysis-era5-land", "era5"),
            ("reanalysis-carra-pressure-levels", "carra"),
            ("reanalysis-pan-carra", "carra"),
            ("reanalysis-pan-carra-means", "carra"),
            ("reanalysis-cerra-land", "cerra"),
            ("projections-cmip5-monthly-single-levels", "cmip5"),
            ("projections-cordex-domains-single-levels", "cordex"),
            ("seasonal-original-single-levels", "seasonal"),
            ("satellite-soil-moisture", "satellite"),
            ("cams-global-reanalysis-eac4", "ads"),
            ("cems-glofas-forecast", "ewds"),
            ("cems-flood-something", "ewds"),
            ("efas-forecast", "efas"),
            ("cems-fire-historical-v1", "fire"),
            ("cems-fire-seasonal", "fire"),
            ("tigge-forecasts", "ecds"),
            ("s2s-forecasts", "ecds"),
            ("s2s-reforecasts", "ecds"),
            ("derived-fire-fuel-biomass", "xds"),
            ("projections-fire-fuel-burned-area", "xds"),
        ],
    )
    def test_id_prefix_rules(self, dataset_id, expected):
        """A leading-prefix match routes the dataset to its family shard.

        Args:
            dataset_id: The Copernicus dataset id.
            expected: The category shard stem.
        """
        assert categorise_dataset(dataset_id) == expected, dataset_id

    @pytest.mark.parametrize(
        "dataset_id",
        [
            "reanalysis-oras5",
            "reanalysis-uerra-europe-single-levels",
            "derived-era5-single-levels-daily-statistics",
            "derived-utci-historical",
            "sis-agrometeorological-indicators",
            "insitu-observations-surface-land",
            "projections-cmip6",
            "ecv-for-climate-change",
            "not-a-real-dataset",
        ],
    )
    def test_residual_ids_fall_through_to_other(self, dataset_id):
        """A dataset matching no prefix rule lands in `other`.

        Args:
            dataset_id: A dataset id no prefix rule claims.
        """
        assert categorise_dataset(dataset_id) == "other", dataset_id


class TestRulesTable:
    """Sanity checks on the rule table itself."""

    def test_every_rule_targets_a_known_category(self):
        """Each rule's target category is one of the declared CATEGORIES."""
        unknown = {category for _, category in _RULES if category not in CATEGORIES}
        assert not unknown, f"rules target undeclared categories: {unknown}"

    def test_every_endpoint_has_a_category(self):
        """Each store slug is a declared category, so seeded rows get a shard."""
        from earthlens.ecmwf.endpoints import ENDPOINTS

        missing = {
            slug for slug in ENDPOINTS if slug not in CATEGORIES and slug != "cds"
        }
        assert not missing, f"stores without a shard category: {missing}"
