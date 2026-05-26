"""Live end-to-end test for the openEO backend (network; gated behind -m e2e).

Hits the real CDSE openEO backend, which needs an authenticated OIDC session:
either a refresh token cached on disk (`~/.openeo/`) from a prior interactive
login, or `OPENEO_CLIENT_ID` / `OPENEO_CLIENT_SECRET` for the headless
client-credentials flow. The test skips cleanly when neither is present, so the
default `-m "not e2e"` run (and CI without secrets) never fails on it.

CI / headless setup: register an OIDC service account on CDSE and set
`OPENEO_CLIENT_ID` + `OPENEO_CLIENT_SECRET` (see
docs/reference/openeo/authentication.md).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("openeo", reason="openEO e2e needs the [openeo] extra")

from earthlens.earthlens import EarthLens

_OPENEO_HOME = Path.home() / ".openeo"


def _has_credentials() -> bool:
    """Return whether some openEO OIDC credential source is available."""
    if os.environ.get("OPENEO_CLIENT_ID") and os.environ.get("OPENEO_CLIENT_SECRET"):
        return True
    if os.environ.get("OPENEO_REFRESH_TOKEN"):
        return True
    # A cached refresh token from a prior interactive `authenticate_oidc()`.
    return _OPENEO_HOME.exists() and any(_OPENEO_HOME.rglob("refresh-tokens.json"))


_SKIP_REASON = (
    "no openEO OIDC credentials (set OPENEO_CLIENT_ID/SECRET, OPENEO_REFRESH_TOKEN, "
    "or sign in once interactively to cache a refresh token in ~/.openeo)"
)


@pytest.mark.openeo
@pytest.mark.e2e
@pytest.mark.skipif(not _has_credentials(), reason=_SKIP_REASON)
class TestOpeneoE2E:
    """A tiny live NDVI pull against CDSE openEO."""

    def test_sentinel2_ndvi_monthly_writes_file(self, tmp_path: Path):
        """A one-month NDVI recipe over a tiny bbox writes a NetCDF file."""
        facade = EarthLens(
            data_source="openeo",
            start="2023-06-01",
            end="2023-06-30",
            variables={"sentinel-2-l2a-ndvi-monthly": []},
            lat_lim=[40.40, 40.45],
            lon_lim=[3.67, 3.72],
            path=str(tmp_path),
            max_cloud_cover=40,
        )
        paths = facade.download()
        assert paths, "expected at least one file written"
        assert all(Path(p).is_file() for p in paths), f"missing output in {paths}"
