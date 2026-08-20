"""Live end-to-end test for the EFAS suite (gated).

Runs only under `-m e2e`; needs a Copernicus token and the EFAS licence
accepted in the portal (the EFAS datasets 403 until then).
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestEfasE2E:
    """Live EFAS forecast retrieve on the EWDS endpoint."""

    def test_live_efas_forecast_returns_discharge(
        self, tmp_path, download_within_budget
    ):
        """A tiny efas-forecast retrieve returns a non-empty river-discharge file."""
        # The 24-hour discharge product is a frozen 2018-2020 archive; the live
        # forecast publishes discharge as the 6-hour product, which is served
        # through the present. Exercise the current path with a recent date.
        lens = EarthLens(
            data_source="ecmwf",
            variables={"efas-forecast": ["river-discharge-in-the-last-6-hours"]},
            start="2026-07-01",
            end="2026-07-01",
            temporal_resolution="daily",
            lat_lim=[45.0, 50.0],
            lon_lim=[5.0, 10.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0
