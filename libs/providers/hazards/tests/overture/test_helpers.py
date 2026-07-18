"""Unit tests for `earthlens.overture._helpers` (per-row licensing)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from earthlens.overture._helpers import (
    CDLA_PERMISSIVE,
    ODBL,
    LicenseWarning,
    derive_license_ids,
    row_license,
    warn_if_odbl,
)


@pytest.mark.overture
class TestRowLicense:
    """`row_license` derivation across the three rule branches."""

    def test_odbl_explicit(self):
        """An explicit ODbL source yields ODbL."""
        assert (
            row_license([{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}]) == ODBL
        )

    def test_odbl_wins_when_not_first(self):
        """ODbL wins even when a permissive source is listed first."""
        sources = [
            {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
            {"dataset": "OpenStreetMap", "license": "ODbL-1.0"},
        ]
        assert row_license(sources) == ODBL

    def test_osm_dataset_without_license_falls_to_odbl(self):
        """An OSM dataset with no explicit license still derives ODbL."""
        assert row_license([{"dataset": "OpenStreetMap"}]) == ODBL

    def test_multiple_permissive_joined_sorted(self):
        """Several permissive licenses are sorted and joined."""
        sources = [
            {"dataset": "Foursquare", "license": "Apache-2.0"},
            {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
        ]
        assert row_license(sources) == "Apache-2.0; CDLA-Permissive-2.0"

    def test_duplicate_licenses_collapsed(self):
        """Repeated identical licenses collapse to one token."""
        sources = [
            {"dataset": "A", "license": "CDLA-Permissive-2.0"},
            {"dataset": "B", "license": "CDLA-Permissive-2.0"},
        ]
        assert row_license(sources) == "CDLA-Permissive-2.0"

    def test_no_license_not_osm_falls_back_to_cdla(self):
        """No explicit license and not OSM falls back to CDLA-Permissive."""
        assert row_license([{"dataset": "Overture"}]) == CDLA_PERMISSIVE

    def test_empty_string_license_is_ignored(self):
        """An empty-string `license` is treated as absent (falls back to CDLA)."""
        assert row_license([{"dataset": "Overture", "license": ""}]) == CDLA_PERMISSIVE

    @pytest.mark.parametrize("empty", [None, [], np.nan, 123, "no-structs"])
    def test_empty_or_missing_sources_default_cdla(self, empty):
        """None / empty / NaN / non-iterable / struct-less sources default to CDLA."""
        assert row_license(empty) == CDLA_PERMISSIVE

    def test_numpy_array_of_structs(self):
        """A numpy object array of struct dicts (the SDK shape) is handled."""
        arr = np.array(
            [{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}], dtype=object
        )
        assert row_license(arr) == ODBL


@pytest.mark.overture
class TestDeriveLicenseIds:
    """`derive_license_ids` over a frame's `sources` column."""

    def test_per_row_mapping(self):
        """Each row's license is derived independently and index-aligned."""
        frame = pd.DataFrame(
            {
                "sources": [
                    [{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}],
                    [{"dataset": "Overture", "license": "CDLA-Permissive-2.0"}],
                ]
            }
        )
        result = derive_license_ids(frame)
        assert list(result) == [ODBL, CDLA_PERMISSIVE]

    def test_missing_sources_column_all_cdla(self):
        """A frame without a `sources` column defaults every row to CDLA."""
        frame = pd.DataFrame({"id": ["a", "b", "c"]})
        result = derive_license_ids(frame)
        assert list(result) == [CDLA_PERMISSIVE] * 3
        assert list(result.index) == list(frame.index)


@pytest.mark.overture
class TestWarnIfOdbl:
    """`warn_if_odbl` emission and count."""

    def test_warns_and_counts_when_odbl_present(self):
        """A `LicenseWarning` is emitted and the ODbL count returned."""
        series = pd.Series([ODBL, CDLA_PERMISSIVE, ODBL])
        with pytest.warns(LicenseWarning, match=r"2 of 3 feature"):
            count = warn_if_odbl(series, "buildings/building")
        assert count == 2

    def test_silent_when_no_odbl(self, recwarn):
        """No warning is emitted when no row is ODbL."""
        series = pd.Series([CDLA_PERMISSIVE, "Apache-2.0"])
        count = warn_if_odbl(series, "places/place")
        assert count == 0
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]
