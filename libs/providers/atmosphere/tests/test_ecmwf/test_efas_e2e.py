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

    def test_live_efas_forecast_returns_discharge(self, tmp_path):
        """A tiny efas-forecast retrieve returns a non-empty river-discharge file."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={"efas-forecast": ["river-discharge-in-the-last-24-hours"]},
            start="2024-01-01",
            end="2024-01-01",
            temporal_resolution="daily",
            lat_lim=[45.0, 50.0],
            lon_lim=[5.0, 10.0],
            path=str(tmp_path),
        )
        out = lens.download()
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0
