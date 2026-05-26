"""Facade-integration tests for the NWM backend (no network)."""

from __future__ import annotations

import pytest

import earthlens.nwm
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


class TestFacadeRegistration:
    """Tests that the facade exposes the NWM backend under both keys."""

    def test_keys_present(self):
        """Both `nwm` and `national-water-model` keys are registered."""
        assert "nwm" in EarthLens.DataSources
        assert "national-water-model" in EarthLens.DataSources

    def test_keys_resolve_to_nwm_class(self):
        """Both keys resolve to `earthlens.nwm.NWM`."""
        assert EarthLens.DataSources["nwm"] is earthlens.nwm.NWM
        assert EarthLens.DataSources["national-water-model"] is earthlens.nwm.NWM

    def test_facade_builds_backend(self, tmp_path):
        """EarthLens(data_source='nwm', ...) constructs the NWM backend."""
        lens = EarthLens(
            data_source="nwm",
            start="2026-05-25",
            end="2026-05-25",
            variables={"short_range": ["channel_rt"]},
            lat_lim=[25, 50],
            lon_lim=[-125, -66],
            path=str(tmp_path),
            cycles=[0],
            steps=[1],
        )
        assert isinstance(lens.datasource, earthlens.nwm.NWM)
