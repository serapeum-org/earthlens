"""Lock-in for #1394: dekad/pentad datasets walk the month-reset calendar grid.

A fixed `10D` / `5D` `pandas_freq` stride drifts off the month-reset
`{dekad}` / `{pentad}` index, silently skipping one period a year (e.g.
February's short third dekad) and re-fetching another. `_overlapping_dates`
now steps on the calendar grid for the `dekadal` / `pentadal` cadences while
leaving the GEFS `5-day` / `10-day` forecast-horizon rows on the fixed stride.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import date_windows
from earthlens.chc import Catalog
from earthlens.chc.backend import CHIRPS

pytestmark = [pytest.mark.chc]

_DEKAD_DAYS = {1, 11, 21}
_PENTAD_DAYS = {1, 6, 11, 16, 21, 26}


def _chirps_over_year(tmp_path, year: int = 2001) -> CHIRPS:
    """A CHIRPS whose request window spans one whole calendar year."""
    return CHIRPS(
        start=f"{year}-01-01",
        end=f"{year}-12-31",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )


def _dates_for(chirps: CHIRPS, ds_key: str) -> pd.DatetimeIndex:
    """`_overlapping_dates` for one bundled dataset over the request window."""
    dataset = chirps._catalog.datasets[ds_key]
    return chirps._overlapping_dates(ds_key, dataset)


class TestDekadalCalendarWalk:
    """Dekadal datasets yield exactly the three calendar dekads of every month."""

    @pytest.mark.parametrize(
        "ds_key", ["global-dekad", "wbgt-dekad", "chirps-gefs-v3-dekad-lead0"]
    )
    def test_full_year_has_36_dekads(self, tmp_path, ds_key):
        """A full year is 36 dekads -- three per month, none missing."""
        dates = _dates_for(_chirps_over_year(tmp_path), ds_key)
        assert len(dates) == 36

    def test_every_start_day_is_a_dekad_boundary(self, tmp_path):
        """Each start lands on day 1, 11 or 21 -- never a drifted stride day."""
        dates = _dates_for(_chirps_over_year(tmp_path), "global-dekad")
        assert {d.day for d in dates} == _DEKAD_DAYS

    def test_february_third_dekad_is_present(self, tmp_path):
        """The period the old 10D stride dropped (Feb dekad 3) is fetched."""
        dates = _dates_for(_chirps_over_year(tmp_path), "global-dekad")
        assert pd.Timestamp("2001-02-21") in dates

    def test_three_dekads_per_month_no_duplicates(self, tmp_path):
        """Every month contributes exactly three distinct dekad starts."""
        dates = _dates_for(_chirps_over_year(tmp_path), "global-dekad")
        assert len(set(dates)) == len(dates)
        per_month = pd.Series(1, index=dates).groupby([dates.year, dates.month]).sum()
        assert (per_month == 3).all()


class TestPentadalCalendarWalk:
    """Pentadal datasets yield exactly the six calendar pentads of every month."""

    # Note: the SPI pentad rows are deliberately NOT exercised here. Their
    # cadence is pentadal (so the date walk is correct), but their filename
    # template `SPI_..PentAccum_{year}_p{pentad}.tif` indexes pentad-OF-YEAR
    # (`p01`..`p72`, verified live on the FTP) while the `{pentad}` placeholder
    # renders pentad-of-MONTH (`1`..`6`). That mismatch is a pre-existing SPI
    # defect independent of this change, so asserting a 72-date walk for SPI
    # here would wrongly read as validating SPI downloads.
    @pytest.mark.parametrize("ds_key", ["global-pentad", "chirps-gefs-v3-pentad-lead0"])
    def test_full_year_has_72_pentads(self, tmp_path, ds_key):
        """A full year is 72 pentads -- six per month, none missing."""
        dates = _dates_for(_chirps_over_year(tmp_path), ds_key)
        assert len(dates) == 72

    def test_every_start_day_is_a_pentad_boundary(self, tmp_path):
        """Each start lands on a pentad boundary day."""
        dates = _dates_for(_chirps_over_year(tmp_path), "global-pentad")
        assert {d.day for d in dates} == _PENTAD_DAYS

    def test_february_sixth_pentad_is_present(self, tmp_path):
        """The period the old 5D stride dropped (Feb pentad 6) is fetched."""
        dates = _dates_for(_chirps_over_year(tmp_path), "global-pentad")
        assert pd.Timestamp("2001-02-26") in dates


class TestLead1PentadRespectsDatasetStart:
    """A lead whose archive starts mid-month begins on its own boundary."""

    def test_lead1_starts_on_jan_6(self, tmp_path):
        """`pentad-lead1` publishes from 2001-01-06, so the walk starts there."""
        dates = _dates_for(_chirps_over_year(tmp_path), "chirps-gefs-v3-pentad-lead1")
        assert dates[0] == pd.Timestamp("2001-01-06")
        assert {d.day for d in dates} <= _PENTAD_DAYS


class TestForecastHorizonsKeepFixedStride:
    """GEFS 5-day/10-day forecast rows share the stride but are NOT calendar-walked."""

    @pytest.mark.parametrize(
        ("ds_key", "freq"),
        [("chirps-gefs-v3-05day", "5D"), ("chirps-gefs-v3-10day", "10D")],
    )
    def test_forecast_rows_match_fixed_date_windows(self, tmp_path, ds_key, freq):
        """Daily-issued forecasts keep the fixed `date_windows` stride untouched."""
        chirps = _chirps_over_year(tmp_path)
        dataset = chirps._catalog.datasets[ds_key]
        window_start = max(chirps.time.start_date, pd.Timestamp(dataset.start_date))
        expected = date_windows(window_start, chirps.time.end_date, freq)
        assert list(_dates_for(chirps, ds_key)) == list(expected)

    def test_05day_is_not_the_calendar_pentad_set(self, tmp_path):
        """The 5-day forecast stride visits non-boundary days a pentad walk never would."""
        dates = _dates_for(_chirps_over_year(tmp_path), "chirps-gefs-v3-05day")
        assert {d.day for d in dates} - _PENTAD_DAYS
