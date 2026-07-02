"""Tests for the EEA (`eea_aq`) stateless helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.eea_aq._helpers import (
    countries_in_bbox,
    datasets_for_years,
    empty_frame,
    shape_frame,
)


@pytest.mark.eea
class TestCountriesInBbox:
    """Resolving a bbox to reporting countries."""

    def test_low_countries(self):
        """A box over the Low Countries picks its neighbours."""
        assert countries_in_bbox((50.8, 52.0), (4.0, 6.0)) == ["BE", "DE", "FR", "NL"]

    def test_malta_only(self):
        """A tight box over Malta picks only MT."""
        assert countries_in_bbox((35.8, 36.0), (14.2, 14.5)) == ["MT"]

    def test_no_country_ocean(self):
        """A box in the mid-Atlantic picks nothing."""
        assert countries_in_bbox((10.0, 12.0), (-40.0, -38.0)) == []

    def test_custom_table(self):
        """A custom table is honoured."""
        table = {"ZZ": (0.0, 0.0, 1.0, 1.0)}
        assert countries_in_bbox((0.5, 0.6), (0.5, 0.6), table) == ["ZZ"]

    def test_reversed_limits_normalised(self):
        """Reversed lat/lon limits still resolve correctly."""
        assert countries_in_bbox((36.0, 35.8), (14.5, 14.2)) == ["MT"]


@pytest.mark.eea
class TestDatasetsForYears:
    """Mapping a year range to reporting-era datasets."""

    @pytest.mark.parametrize(
        "start, end, expected",
        [
            (2010, 2011, ["Historical"]),
            (2015, 2016, ["Verified"]),
            (2024, 2025, ["Unverified"]),
            (2021, 2024, ["Verified", "Unverified"]),
            (2010, 2025, ["Historical", "Verified", "Unverified"]),
        ],
    )
    def test_ranges(self, start, end, expected):
        """Each range maps to the datasets that span it, chronologically."""
        assert datasets_for_years(start, end) == expected

    def test_reversed_years_normalised(self):
        """A reversed year pair is normalised."""
        assert datasets_for_years(2024, 2021) == ["Verified", "Unverified"]


@pytest.mark.eea
class TestShapeFrame:
    """Reshaping a raw EEA Parquet frame."""

    def _raw(self):
        return pd.DataFrame(
            {
                "Samplingpoint": ["MT/SPO-1", "DE/SPO-2"],
                "Pollutant": [6001, 999],
                "Start": pd.to_datetime(["2023-01-01T00:00", "2023-01-01T00:00"]),
                "Value": ["5.6", "7.0"],
                "Unit": ["ug.m-3", "ug.m-3"],
                "AggType": ["hour", "hour"],
                "Validity": [1, 1],
                "Verification": [3, 2],
            }
        )

    def test_maps_code_and_parses_country(self):
        """Codes map to names and the country prefix is parsed."""
        out = shape_frame(self._raw(), "Verified", {6001: "pm25"})
        assert list(out["parameter"]) == ["pm25"]
        assert out.loc[0, "country"] == "MT"
        assert out.loc[0, "value"] == 5.6

    def test_drops_unrequested_codes(self):
        """A code absent from the map is dropped (row 2 here)."""
        out = shape_frame(self._raw(), "Verified", {6001: "pm25"})
        assert len(out) == 1

    def test_empty_raw_returns_empty(self):
        """An empty raw frame yields the schema-only frame."""
        assert shape_frame(pd.DataFrame(), "Verified", {6001: "pm25"}).empty

    def test_no_matching_code_returns_empty(self):
        """No matching code yields the schema-only frame."""
        out = shape_frame(self._raw(), "Verified", {123: "xx"})
        assert out.empty and "country" in out.columns


@pytest.mark.eea
def test_empty_frame_schema():
    """`empty_frame` has the full long schema and zero rows."""
    frame = empty_frame()
    assert frame.empty and "station_id" in frame.columns
