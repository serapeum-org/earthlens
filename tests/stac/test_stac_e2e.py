"""Live end-to-end tests for the STAC backend (network; gated behind -m e2e).

These hit real STAC APIs and write real COGs, so they are marked `e2e` and
deselected by the default `-m "not e2e"` run. Earth Search is anonymous and
always runs (given the `[stac]` extra); MPC needs `planetary-computer` installed
(public, no account); CDSE needs S3 dashboard keys in the environment and skips
cleanly without them.

Note on asset keys: the catalog aliases the *collection id* per endpoint but not
the *asset keys*, which differ across providers (Earth Search exposes
`red`/`green`/`nir`; MPC exposes `B04`/`B08`). Each test therefore passes the
asset names that endpoint actually serves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("pystac_client", reason="STAC e2e needs the [stac] extra")

from earthlens.stac import STAC

_LAT = [40.40, 40.45]
_LON = [-3.72, -3.67]
_START = "2024-06-01"
_END = "2024-06-20"


@pytest.mark.stac
@pytest.mark.e2e
class TestEarthSearchE2E:
    """Earth Search (Element 84 / AWS) — anonymous, no credentials."""

    def test_sentinel2_writes_cog(self, tmp_path: Path):
        """A one-item Sentinel-2 pull over Madrid writes a readable COG."""
        stac = STAC(
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a": ["red"]},
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
            endpoint="earth-search",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
class TestPlanetaryComputerE2E:
    """Microsoft Planetary Computer — SAS URL signing (no account needed)."""

    def test_sentinel2_writes_cog(self, tmp_path: Path):
        """A one-item Sentinel-2 pull from MPC writes a readable COG."""
        pytest.importorskip("pystac_client", reason="MPC e2e needs pystac-client")
        stac = STAC(
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
            endpoint="planetary-computer",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
@pytest.mark.skipif(
    not (os.environ.get("CDSE_S3_ACCESS_KEY") and os.environ.get("CDSE_S3_SECRET_KEY")),
    reason="CDSE e2e needs CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY",
)
class TestCdseE2E:
    """Copernicus Data Space — S3-credentialled asset reads (gated on keys)."""

    def test_sentinel2_writes_cog(self, tmp_path: Path):
        """A one-item CDSE Sentinel-2 pull writes a readable COG."""
        stac = STAC(
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
            endpoint="cdse",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"
