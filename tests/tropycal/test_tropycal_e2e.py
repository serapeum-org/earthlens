"""Live end-to-end test for the Tropycal backend (gated on `e2e`).

Performs one real best-track query against tropycal's HURDAT2 source — no
credentials, but it downloads + parses the North Atlantic best-track
file, so it is marked `slow` and deselected from the default
`-m "not e2e"` run. Requires the `[tropycal]` extra (tropycal + cartopy).
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.earthlens import EarthLens
from earthlens.tropycal.events import POINT_COLUMNS, RECON_COLUMNS

pytestmark = [pytest.mark.tropycal, pytest.mark.e2e, pytest.mark.slow]


def test_hurdat_north_atlantic_2005_gulf(tmp_path):
    """A live HURDAT 2005 Gulf-of-Mexico query returns Katrina's track fixes."""
    facade = EarthLens(
        data_source="tropycal",
        variables=["north_atlantic"],
        start="2005-08-01",
        end="2005-09-15",
        lat_lim=[18.0, 31.0],
        lon_lim=[-98.0, -80.0],
        source="hurdat",
        path=str(tmp_path),
    )
    fc = facade.download(progress_bar=False)

    assert len(fc) > 0, "expected at least one in-window/in-box fix"
    assert set(POINT_COLUMNS).issubset(fc.columns), "point schema columns missing"
    assert fc.crs.to_epsg() == 4326, f"expected EPSG:4326, got {fc.crs}"
    assert (fc["vmax_kt"].dropna() >= 0).all(), "wind speeds must be non-negative"
    names = {str(n).upper() for n in fc["name"].dropna()}
    assert "KATRINA" in names, f"expected KATRINA in the 2005 Gulf tracks, got {names}"


def test_recon_katrina_2005(tmp_path):
    """A live recon query returns Katrina's flight-level observation points."""
    fc = EarthLens(
        data_source="tropycal",
        product="recon",
        recon_product="hdobs",
        variables=["AL122005"],
        basin="north_atlantic",
        source="hurdat",
        start="2005-08-23",
        end="2005-08-31",
        lat_lim=[18.0, 31.0],
        lon_lim=[-98.0, -80.0],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert len(fc) > 0, "expected recon observations for Katrina"
    assert set(RECON_COLUMNS).issubset(fc.columns), "recon schema columns missing"
    assert fc.crs.to_epsg() == 4326, f"expected EPSG:4326, got {fc.crs}"
    assert set(fc["storm_id"]) == {"AL122005"}, "all rows should belong to Katrina"


def test_ships_ian_2022(tmp_path):
    """A live SHIPS query returns Ian's forecast-guidance table (tabular)."""
    df = EarthLens(
        data_source="tropycal",
        product="ships",
        variables=["AL092022"],
        basin="north_atlantic",
        source="hurdat",
        ships_time="2022-09-27 00:00",
        start="2022-09-20",
        end="2022-10-01",
        lat_lim=[-90, 90],
        lon_lim=[-180, 180],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert isinstance(df, pd.DataFrame), "ships output must be a DataFrame"
    assert len(df) > 0, "expected SHIPS forecast hours for Ian"
    assert {"storm_id", "forecast_init", "fhr", "vmax_noland_kt"}.issubset(df.columns)
    assert (df["fhr"] >= 0).all(), "forecast hours must be non-negative"


def test_realtime_active_storms(tmp_path):
    """A live realtime query returns a valid FC (possibly empty off-season).

    Cannot assert non-empty: there may be no active storms when the suite runs.
    This verifies the live wiring and that an off-season result is a clean,
    schema-correct empty collection rather than an error.
    """
    fc = EarthLens(
        data_source="tropycal",
        product="realtime",
        variables=[],
        start="2026-01-01",
        end="2026-12-31",
        lat_lim=[-90, 90],
        lon_lim=[-180, 180],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert set(POINT_COLUMNS).issubset(
        fc.columns
    ), "realtime should use the point schema"
    assert fc.crs.to_epsg() == 4326, f"expected EPSG:4326, got {fc.crs}"
