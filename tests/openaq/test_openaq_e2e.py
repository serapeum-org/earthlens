"""Live end-to-end tests for the OpenAQ v3 air-quality backend.

Hits the real OpenAQ v3 web service, which requires a free API key, so
these tests are gated behind both the `e2e` pytest marker and a skip on
a missing `OPENAQ_API_KEY` (mirrors the CMEMS e2e gate). A default
`pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m e2e tests/openaq
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

_HAVE_KEY = bool(os.environ.get("OPENAQ_API_KEY"))

# Los Angeles basin — a dense, always-reporting PM2.5 network.
_LA_LAT = [34.0, 34.3]
_LA_LON = [-118.5, -118.1]

_SCHEMA_COLUMNS = {
    "station_id",
    "parameter",
    "datetime_utc",
    "value",
    "units",
    "lat",
    "lon",
}


def _recent_window() -> tuple[str, str]:
    """A 7-day window ending ~3 days back to avoid publication latency."""
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=7)
    return start.isoformat(), end.isoformat()


@pytest.mark.e2e
@pytest.mark.openaq
@pytest.mark.skipif(not _HAVE_KEY, reason="set OPENAQ_API_KEY to run live OpenAQ e2e")
class TestOpenaqLiveQuery:
    """Live OpenAQ v3 queries (require a free OPENAQ_API_KEY)."""

    def test_pm25_city_window(self, tmp_path: Path):
        """A small LA bbox + recent week of PM2.5 returns plausible rows."""
        start, end = _recent_window()
        df = EarthLens(
            data_source="openaq",
            start=start,
            end=end,
            variables=["pm25"],
            lat_lim=_LA_LAT,
            lon_lim=_LA_LON,
            path=str(tmp_path),
            max_locations=5,
        ).download(progress_bar=False)

        assert not df.empty, "expected at least one PM2.5 measurement in LA"
        assert _SCHEMA_COLUMNS <= set(df.columns), f"missing columns: {df.columns}"
        assert (df["value"] >= 0).all(), "negative pollutant concentration"
        assert df["lat"].between(_LA_LAT[0], _LA_LAT[1]).all()
        assert df["lon"].between(_LA_LON[0], _LA_LON[1]).all()
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert len(list(tmp_path.glob("*.csv"))) == 1, "a CSV should be written"
