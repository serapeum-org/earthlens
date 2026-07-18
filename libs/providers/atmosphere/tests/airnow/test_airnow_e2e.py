"""Live end-to-end test for the AirNow air-quality backend.

Hits the real AirNow `/aq/data/` service, which requires a free API key,
so this test is gated behind both the `e2e` pytest marker and a skip on a
missing `AIRNOW_API_KEY` (mirrors the OpenAQ e2e gate). A default
`pytest` invocation skips it.

Run with:

    pixi run -e dev pytest -m e2e tests/airnow
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

_HAVE_KEY = bool(os.environ.get("AIRNOW_API_KEY"))

# Los Angeles basin — a dense, always-reporting monitor network.
_LA_LAT = [34.0, 34.3]
_LA_LON = [-118.5, -118.1]

_SCHEMA_COLUMNS = {
    "station_id",
    "parameter",
    "datetime_utc",
    "value",
    "units",
    "aqi",
    "lat",
    "lon",
    "provider",
}


def _recent_day() -> tuple[str, str]:
    """A single day ~3 days back to avoid publication latency."""
    day = dt.date.today() - dt.timedelta(days=3)
    return day.isoformat(), day.isoformat()


@pytest.mark.e2e
@pytest.mark.airnow
@pytest.mark.skipif(not _HAVE_KEY, reason="set AIRNOW_API_KEY to run live AirNow e2e")
class TestAirnowLiveQuery:
    """Live AirNow queries (require a free AIRNOW_API_KEY)."""

    def test_pm25_city_day(self, tmp_path: Path):
        """A small LA bbox + recent day of PM2.5 returns plausible rows."""
        start, end = _recent_day()
        df = EarthLens(
            data_source="airnow",
            start=start,
            end=end,
            variables=["pm25"],
            lat_lim=_LA_LAT,
            lon_lim=_LA_LON,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert not df.empty, "expected at least one PM2.5 observation in LA"
        assert _SCHEMA_COLUMNS <= set(df.columns), f"missing columns: {df.columns}"
        assert df["value"].notna().any(), "the value column is entirely NaN (wrong field?)"
        assert (df["value"].dropna() >= 0).all(), "negative pollutant concentration"
        assert df["lat"].between(_LA_LAT[0], _LA_LAT[1]).all()
        assert df["lon"].between(_LA_LON[0], _LA_LON[1]).all()
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert len(list(tmp_path.glob("airnow_*.csv"))) == 1, "a CSV should be written"
