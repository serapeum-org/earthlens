"""Live end-to-end test for the EEA (`eea_aq`) air-quality backend.

Hits the real EEA download service via `airbase`, which is public (no
credentials), so this test is gated behind the `e2e` pytest marker and
skips when the `[eea_aq]` extra (`airbase`) is not installed. A default
`pytest` invocation skips it.

Run with:

    pixi run -e dev pytest -m e2e tests/eea_aq
"""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.earthlens import EarthLens

pytest.importorskip("airbase", reason="install earthlens[eea_aq] to run live EEA e2e")

# Malta — the smallest EEA reporting country, quickest to download.
_MT_LAT = [35.7, 36.1]
_MT_LON = [14.1, 14.6]

_SCHEMA_COLUMNS = {
    "station_id",
    "country",
    "parameter",
    "datetime_utc",
    "value",
    "units",
    "dataset",
    "provider",
}


@pytest.mark.e2e
@pytest.mark.eea
class TestEeaLiveQuery:
    """Live EEA queries via airbase (public, no credentials)."""

    def test_malta_pm25_verified_year(self, tmp_path: Path):
        """Malta PM2.5 over a Verified-era month returns plausible rows."""
        df = EarthLens(
            data_source="eea-aq",
            start="2022-06-01",
            end="2022-06-30",
            variables=["pm25"],
            country="MT",
            lat_lim=_MT_LAT,
            lon_lim=_MT_LON,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert not df.empty, "expected at least one Malta PM2.5 observation"
        assert _SCHEMA_COLUMNS <= set(df.columns), f"missing columns: {df.columns}"
        assert set(df["parameter"].unique()) == {"pm25"}
        assert (df["country"] == "MT").all(), "all rows should be Maltese stations"
        assert (df["value"].dropna() >= 0).all(), "negative pollutant concentration"
        assert (df["datetime_utc"].dt.year == 2022).all()
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert len(list(tmp_path.glob("eea_aq_*.csv"))) == 1, "a CSV should be written"
