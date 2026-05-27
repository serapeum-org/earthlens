"""Live end-to-end tests for the Sentinel Hub backend (network; gated by -m e2e).

Hits the real Sentinel Hub deployment on CDSE, which needs an OAuth2
client-credentials pair minted in the CDSE Dashboard. The tests skip cleanly when
`SH_CLIENT_ID` / `SH_CLIENT_SECRET` are not both set, so the default
`-m "not e2e"` run (and CI without secrets) never fails on them.

CI / headless setup: create an OAuth client (Grant Type `Client Credentials`) at
https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings and set
`SH_CLIENT_ID` + `SH_CLIENT_SECRET` (see docs/reference/sentinel-hub/auth.md).

Batch and Batch-Statistical are intentionally not exercised live: they deliver to
a user S3 bucket needing an IAM role, so they are covered by faked-SDK tests only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip(
    "sentinelhub", reason="Sentinel Hub e2e needs the [sentinel-hub] extra"
)

from earthlens.earthlens import EarthLens  # noqa: E402

_SKIP_REASON = (
    "no Sentinel Hub credentials (set SH_CLIENT_ID / SH_CLIENT_SECRET; mint an "
    "OAuth client_credentials pair in the CDSE Dashboard)"
)


def _has_credentials() -> bool:
    """Return whether both SH client-credentials env vars are set."""
    return bool(os.environ.get("SH_CLIENT_ID") and os.environ.get("SH_CLIENT_SECRET"))


# A tiny AOI near Naples + a short window keeps every live render cheap.
_LAT = [40.80, 40.83]
_LON = [14.24, 14.27]
_START = "2020-06-10"
_END = "2020-06-20"

_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [14.24, 40.80],
            [14.27, 40.80],
            [14.27, 40.83],
            [14.24, 40.83],
            [14.24, 40.80],
        ]
    ],
}


@pytest.mark.sentinel_hub
@pytest.mark.e2e
@pytest.mark.skipif(not _has_credentials(), reason=_SKIP_REASON)
class TestSentinelHubLive:
    """Live Process / Statistical / local-tiling renders against CDSE."""

    def test_process_ndvi_render(self, tmp_path: Path):
        """A Process NDVI render over a tiny bbox writes a GeoTIFF."""
        facade = EarthLens(
            data_source="sentinel-hub",
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=_LAT,
            lon_lim=_LON,
            path=tmp_path,
            resolution=20,
        )
        paths = facade.download()
        assert len(paths) == 1 and Path(paths[0]).exists()
        assert Path(paths[0]).stat().st_size > 0

    def test_statistical_mean_ndvi(self, tmp_path: Path):
        """A Statistical mean-NDVI over a small polygon writes a non-empty table."""
        import pandas as pd

        facade = EarthLens(
            data_source="sentinel-hub",
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a-ndvi-stats": []},
            lat_lim=_LAT,
            lon_lim=_LON,
            path=tmp_path,
            resolution=20,
            api="statistical",
            geometry=_POLYGON,
        )
        paths = facade.download()
        assert len(paths) == 1
        frame = pd.read_csv(paths[0])
        assert not frame.empty
        assert "mean" in frame.columns

    def test_local_tiling_render(self, tmp_path: Path):
        """A bbox forced to a 2×2 tile grid merges into one GeoTIFF."""
        facade = EarthLens(
            data_source="sentinel-hub",
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.6, 41.1],
            lon_lim=[14.0, 14.5],
            path=tmp_path,
            resolution=20,
            api="tiling",
        )
        paths = facade.download()
        assert len(paths) == 1 and Path(paths[0]).exists()
