"""Live end-to-end test for the Sensor.Community air-quality backend.

Hits the real Sensor.Community live API + archive, both public (no
credentials, no extra SDK), so this test is gated only behind the `e2e`
pytest marker. A default `pytest` invocation skips it.

Run with:

    pixi run -e dev pytest -m e2e tests/sensor_community
"""

from __future__ import annotations

import datetime as dt
import warnings
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.sensor_community import LicenseWarning

# Central Stuttgart — the densest Sensor.Community region.
_STUTTGART_LAT = [48.74, 48.82]
_STUTTGART_LON = [9.13, 9.23]

_SCHEMA_COLUMNS = {
    "station_id",
    "sensor_type",
    "parameter",
    "datetime_utc",
    "value",
    "units",
    "lat",
    "lon",
    "provider",
}


def _recent_day() -> str:
    """A single archive day ~2 days back (the archive lags real time)."""
    return (dt.date.today() - dt.timedelta(days=2)).isoformat()


@pytest.mark.e2e
@pytest.mark.sensor_community
class TestSensorCommunityLiveQuery:
    """Live Sensor.Community queries (public, no credentials)."""

    def test_pm_over_city_day(self, tmp_path: Path):
        """A tight Stuttgart bbox + recent day of PM returns plausible rows."""
        day = _recent_day()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            df = EarthLens(
                data_source="sensor-community",
                start=day,
                end=day,
                variables=["pm25", "pm10"],
                lat_lim=_STUTTGART_LAT,
                lon_lim=_STUTTGART_LON,
                path=str(tmp_path),
            ).download(progress_bar=False)

        assert not df.empty, "expected at least one Stuttgart reading"
        assert _SCHEMA_COLUMNS <= set(df.columns), f"missing columns: {df.columns}"
        assert set(df["parameter"].unique()) <= {"pm25", "pm10"}
        assert (df["value"].dropna() >= 0).all(), "negative PM concentration"
        assert df["lat"].between(_STUTTGART_LAT[0], _STUTTGART_LAT[1]).all()
        assert df["lon"].between(_STUTTGART_LON[0], _STUTTGART_LON[1]).all()
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert len(list(tmp_path.glob("sensor_community_*.csv"))) == 1
