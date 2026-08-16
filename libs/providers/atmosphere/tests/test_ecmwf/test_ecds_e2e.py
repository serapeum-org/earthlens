"""Live end-to-end tests for the ECDS and XDS endpoints (gated).

Runs only under `-m e2e`; needs a Copernicus token. The same Personal Access
Token authenticates both stores, but each additionally requires its dataset
licence accepted, and ECDS requires the portal-scope `terms-of-use-ecds`
policy — without it every retrieve returns 403.

Both retrieves are wrapped in `download_within_budget` so a queued job cannot
consume the whole e2e lane.
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestEcdsE2E:
    """Live TIGGE retrieve on the ECDS endpoint."""

    def test_live_tigge_returns_2m_temperature(self, tmp_path, download_within_budget):
        """A one-day TIGGE control forecast returns a non-empty file."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={"tigge-forecasts": ["2m-temperature"]},
            start="2024-01-01",
            end="2024-01-01",
            temporal_resolution="daily",
            lat_lim=[50.0, 51.0],
            lon_lim=[9.0, 10.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0


class TestXdsE2E:
    """Live fire-fuel retrieve on the XDS endpoint."""

    def test_live_fuel_moisture_returns_file(self, tmp_path, download_within_budget):
        """A one-month live-fuel-moisture retrieve returns a non-empty file."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={
                "derived-fire-fuel-biomass": ["live-fuel-moisture-content-group"]
            },
            start="2000-01-01",
            end="2000-01-31",
            temporal_resolution="monthly",
            lat_lim=[50.0, 51.0],
            lon_lim=[9.0, 10.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out
        assert out[0].exists()
        assert out[0].stat().st_size > 0
