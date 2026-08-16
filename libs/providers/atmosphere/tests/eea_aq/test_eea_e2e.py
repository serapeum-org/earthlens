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

    def test_malta_pm25_historical_era(self, tmp_path: Path):
        """Malta PM2.5 over the frozen pre-2013 Historical era returns plausible rows."""
        # Query the legacy Historical dataset (frozen, always served) rather than the
        # live Verified/Unverified eras, which periodically return zero files
        # service-wide on EEA's side (see #1046). This keeps the download/parse/schema
        # path deterministic; a still-empty result means EEA is fully unreachable, so skip.
        df = EarthLens(
            data_source="eea-aq",
            start="2010-01-01",
            end="2012-12-31",
            variables=["pm25"],
            country="MT",
            lat_lim=_MT_LAT,
            lon_lim=_MT_LON,
            path=str(tmp_path),
        ).download(progress_bar=False)

        if df.empty:
            pytest.skip("EEA download service returned no rows (upstream unavailable)")
        assert _SCHEMA_COLUMNS <= set(df.columns), f"missing columns: {df.columns}"
        assert set(df["parameter"].unique()) == {"pm25"}
        assert (df["country"] == "MT").all(), "all rows should be Maltese stations"
        # Readings EEA flags invalid carry a no-data sentinel (-999 in this era) and
        # are masked to NaN by the backend, so no sentinel may survive into `value`.
        # What remains is real, and raw hourly data carries occasional small
        # instrument-noise negatives near zero — assert a plausible band with a
        # positive centre rather than strictly non-negative.
        assert not (df["value"] == -999.0).any(), "no-data sentinel left in value"
        assert (df.loc[df["value"].isna(), "validity"] < 0).all()
        real = df["value"].dropna()
        assert not real.empty, "expected some real observations"
        assert real.between(-50, 10000).all(), "implausible PM2.5 concentration"
        assert real.median() > 0, "expected mostly-positive concentrations"
        assert df["datetime_utc"].dt.year.between(2010, 2012).all()
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert len(list(tmp_path.glob("eea_aq_*.csv"))) == 1, "a CSV should be written"
