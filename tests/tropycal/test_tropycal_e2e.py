"""Live end-to-end test for the Tropycal backend (gated on `e2e`).

Performs one real best-track query against tropycal's HURDAT2 source — no
credentials, but it downloads + parses the North Atlantic best-track
file, so it is marked `slow` and deselected from the default
`-m "not e2e"` run. Requires the `[tropycal]` extra (tropycal + cartopy).
"""

from __future__ import annotations

import pytest

from earthlens.earthlens import EarthLens
from earthlens.tropycal.events import POINT_COLUMNS

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
