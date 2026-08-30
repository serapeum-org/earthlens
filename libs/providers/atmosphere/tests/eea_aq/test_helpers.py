"""Tests for the EEA (`eea_aq`) stateless helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import pytest

from earthlens.eea_aq._helpers import (
    adjacent_eras,
    countries_in_bbox,
    datasets_for_years,
    download_request,
    empty_frame,
    shape_frame,
)


class _LoopRequest:
    """Stand-in airbase request that writes a marker file on download."""

    def download(
        self, dir: str, skip_existing: bool = True, raise_for_status: bool = True
    ) -> None:
        Path(dir, "ok.txt").write_text("x", encoding="utf-8")


async def _adownload(request: _LoopRequest, directory: str) -> None:
    """Call `download_request` from inside a running event loop."""
    download_request(request, directory)


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
            # 2023+ resolves to BOTH eras: recently-promoted years may sit in
            # Verified while the UTD stream still carries them in Unverified.
            (2024, 2025, ["Verified", "Unverified"]),
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
class TestAdjacentEras:
    """The empty-primary-era fallback target (Verified <-> Unverified), year-gated."""

    def test_boundary_year_falls_back_to_unverified(self):
        """A Verified-only request at the promotion boundary falls back to Unverified."""
        assert adjacent_eras(["Verified"], 2022, 2022) == ["Unverified"]

    def test_out_of_range_year_does_not_fall_back(self):
        """A Verified-only request years before the boundary has no useful neighbour."""
        assert adjacent_eras(["Verified"], 2015, 2015) == []

    def test_range_touching_boundary_falls_back(self):
        """A range whose upper end reaches the boundary year falls back."""
        assert adjacent_eras(["Verified"], 2020, 2022) == ["Unverified"]

    def test_both_live_eras_have_no_untried_neighbour(self):
        """A recent-year sweep already spans both live eras: nothing to try."""
        assert adjacent_eras(["Verified", "Unverified"], 2024, 2024) == []

    def test_historical_only_has_no_live_neighbour(self):
        """Historical is a frozen archive with no adjacent live era."""
        assert adjacent_eras(["Historical"], 2010, 2010) == []

    def test_historical_plus_verified_out_of_range_does_not_fall_back(self):
        """A 2012-2013 straddling request cannot be served by Unverified 2023+."""
        assert adjacent_eras(["Historical", "Verified"], 2012, 2013) == []

    def test_reversed_years_normalised(self):
        """Reversed year arguments are normalised before the overlap test."""
        assert adjacent_eras(["Verified"], 2022, 2020) == ["Unverified"]


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

    def test_no_matching_code_logs_drift_diagnostic(self):
        """A non-empty Parquet matching no code logs a distinct drift warning."""
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(
            lambda m: messages.append(m.record["message"]), level="WARNING"
        )
        shape_frame(self._raw(), "Verified", {123: "xx"})  # no code matches
        logger.remove(sink)
        assert any("schema drift" in message.lower() for message in messages)

    def _flagged(self, value, validity):
        """One MT pm25 row carrying value/validity for the no-data masking tests."""
        return pd.DataFrame(
            {
                "Samplingpoint": ["MT/SPO-1"],
                "Pollutant": [6001],
                "Start": pd.to_datetime(["2011-01-01T00:00"]),
                "Value": [value],
                "Unit": ["ug.m-3"],
                "AggType": ["hour"],
                "Validity": [validity],
                "Verification": [3],
            }
        )

    def test_negative_flag_masks_sentinel_to_nan(self):
        """A -999 reading flagged invalid comes back as NaN with the flag preserved."""
        out = shape_frame(self._flagged("-999", -1), "Historical", {6001: "pm25"})
        assert pd.isna(out.loc[0, "value"])
        assert out.loc[0, "validity"] == -1

    def test_negative_flag_masks_zero_value(self):
        """An invalid row published as 0.0 is masked too, gated on the flag not the value."""
        out = shape_frame(self._flagged("0.0", -1), "Historical", {6001: "pm25"})
        assert pd.isna(out.loc[0, "value"])

    def test_maintenance_flag_masks_value(self):
        """A -99 maintenance flag also masks the reading."""
        out = shape_frame(self._flagged("12.0", -99), "Historical", {6001: "pm25"})
        assert pd.isna(out.loc[0, "value"])

    def test_valid_row_keeps_value_including_small_negative(self):
        """A valid row keeps its value, small near-zero instrument noise included."""
        out = shape_frame(self._flagged("-5.3", 1), "Verified", {6001: "pm25"})
        assert out.loc[0, "value"] == -5.3
        assert out.loc[0, "validity"] == 1

    def test_null_flag_sentinel_is_masked(self):
        """A -999 sentinel carried under a null flag is masked, not passed through."""
        out = shape_frame(self._flagged("-999", None), "Historical", {6001: "pm25"})
        assert pd.isna(out.loc[0, "value"])
        assert pd.isna(out.loc[0, "validity"])

    def test_null_flag_real_value_is_kept(self):
        """A genuine reading with a null flag is not over-masked."""
        out = shape_frame(self._flagged("7.2", None), "Unverified", {6001: "pm25"})
        assert out.loc[0, "value"] == 7.2

    def test_masked_sentinels_are_removed_by_dropna(self):
        """Masked no-data rows are real float NaNs, so value.dropna() now drops them."""
        raw = pd.DataFrame(
            {
                "Samplingpoint": ["MT/SPO-1", "MT/SPO-1"],
                "Pollutant": [6001, 6001],
                "Start": pd.to_datetime(["2011-01-01T00:00", "2011-01-01T01:00"]),
                "Value": ["14.6", "-999"],
                "Unit": ["ug.m-3", "ug.m-3"],
                "AggType": ["hour", "hour"],
                "Validity": [1, -1],
                "Verification": [3, 3],
            }
        )
        out = shape_frame(raw, "Historical", {6001: "pm25"})
        assert out["value"].dropna().tolist() == [14.6]

    def test_zero_flag_is_not_masked(self):
        """A zero Validity is not negative, so the reading is kept (strict < 0 boundary)."""
        out = shape_frame(self._flagged("3.0", 0), "Verified", {6001: "pm25"})
        assert out.loc[0, "value"] == 3.0
        assert out.loc[0, "validity"] == 0

    def test_masks_only_the_flagged_rows_in_a_mixed_frame(self):
        """Masking is row-selective: only the no-data rows in a mixed frame become NaN."""
        raw = pd.DataFrame(
            {
                "Samplingpoint": ["MT/SPO-1"] * 4,
                "Pollutant": [6001] * 4,
                "Start": pd.to_datetime(["2011-01-01T00:00"] * 4),
                "Value": ["14.6", "-999", "-999", "-5.3"],
                "Unit": ["ug.m-3"] * 4,
                "AggType": ["hour"] * 4,
                "Validity": [
                    1,
                    -1,
                    None,
                    1,
                ],  # valid / invalid / null-flag / valid-negative
                "Verification": [3] * 4,
            }
        )
        out = shape_frame(raw, "Historical", {6001: "pm25"})
        assert list(out["value"].isna()) == [False, True, True, False]
        assert out.loc[0, "value"] == 14.6
        assert out.loc[3, "value"] == -5.3


@pytest.mark.eea
def test_empty_frame_schema():
    """`empty_frame` has the full long schema and zero rows."""
    frame = empty_frame()
    assert frame.empty and "station_id" in frame.columns


@pytest.mark.eea
def test_download_request_direct(tmp_path):
    """`download_request` runs the request outside any event loop."""
    download_request(_LoopRequest(), str(tmp_path))
    assert (tmp_path / "ok.txt").exists()


@pytest.mark.eea
def test_download_request_nests_under_running_loop(tmp_path):
    """`download_request` applies nest_asyncio under a running loop (Jupyter)."""
    asyncio.run(_adownload(_LoopRequest(), str(tmp_path)))
    assert (tmp_path / "ok.txt").exists()
