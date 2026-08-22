"""Live end-to-end test for GloFAS on the CEMS Early Warning Data Store (EWDS).

Submits one tiny `cems-glofas-forecast` retrieve through the EWDS endpoint (the
shared CDS Personal Access Token authenticates against EWDS) and checks a
NetCDF with river-discharge values on the native 0.05° grid comes back. Runs
only under `-m e2e` (gated by the marker, like the rest of the e2e suite);
needs a configured Copernicus token and the GloFAS licence accepted.
"""

from __future__ import annotations

from functools import partial

import pytest

from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.e2e]

_GLOFAS = "cems-glofas-forecast"
_GLOFAS_CODE = "river-discharge-in-the-last-24-hours"


class TestGlofasE2E:
    """End-to-end GloFAS retrieve against the live EWDS endpoint."""

    def test_live_glofas_forecast_retrieve(self, tmp_path, download_within_budget):
        """A tiny GloFAS forecast retrieves a NetCDF on the native 0.05° grid."""
        ecmwf = ECMWF(
            start="2024-01-01",
            end="2024-01-01",
            variables={_GLOFAS: [_GLOFAS_CODE]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            temporal_resolution="daily",
            path=tmp_path,
        )
        assert ecmwf.space.resolution == 0.05, "GloFAS bbox must snap to 0.05°"

        variable = Catalog().get_variable(_GLOFAS, _GLOFAS_CODE)
        target = download_within_budget(partial(ecmwf._api, variable))

        assert target.exists(), f"GloFAS NetCDF not created at {target}"
        assert target.stat().st_size > 0, f"GloFAS NetCDF is empty: {target}"
