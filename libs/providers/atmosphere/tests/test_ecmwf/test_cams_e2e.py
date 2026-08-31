"""Live end-to-end test for the ADS / CAMS store (gated).

Runs only under `-m e2e`; needs a Copernicus token, the ADS site policies
accepted, and the EAC4 licence accepted (ADS retrieves 403 until then).
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestCamsE2E:
    """Live EAC4 retrieve on the ADS endpoint."""

    def test_live_eac4_returns_file(self, tmp_path, download_within_budget):
        """A tiny EAC4 retrieve returns a non-empty (zipped NetCDF) file."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={"cams-global-reanalysis-eac4": ["2m-temperature"]},
            start="2023-01-01",
            end="2023-01-01",
            temporal_resolution="daily",
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0
