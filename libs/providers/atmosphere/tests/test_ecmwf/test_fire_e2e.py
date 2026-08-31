"""Live end-to-end test for CEMS fire danger (gated).

Runs only under `-m e2e`; needs a Copernicus token and the fire licence accepted.
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestFireE2E:
    """Live fire-historical retrieve on the EWDS endpoint."""

    def test_live_fire_historical_returns_fwi(self, tmp_path, download_within_budget):
        """A tiny cems-fire-historical-v1 retrieve returns a non-empty file."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={"cems-fire-historical-v1": ["fire-weather-index"]},
            start="2024-01-01",
            end="2024-01-01",
            temporal_resolution="daily",
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0
