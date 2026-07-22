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


@pytest.mark.stac
@pytest.mark.e2e
class TestDeafricaE2E:
    """Digital Earth Africa — anonymous, af-south-1."""

    def test_wofs_writes_cog(self, tmp_path: Path):
        """A one-item WOfS pull over Johannesburg writes a readable COG."""
        stac = STAC(
            start="2024-01-01",
            end="2024-12-31",
            variables={"deafrica/wofs_ls": ["water"]},
            lat_lim=[-26.5, -26.0],
            lon_lim=[28.0, 28.5],
            path=str(tmp_path),
            endpoint="deafrica",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
class TestDeaE2E:
    """Digital Earth Australia — anonymous, ap-southeast-2."""

    def test_wofs_writes_cog(self, tmp_path: Path):
        """A one-item WOfS pull over Canberra writes a readable COG."""
        stac = STAC(
            start="2024-01-01",
            end="2024-12-31",
            variables={"dea/ga_ls_wo_3": ["water"]},
            lat_lim=[-35.5, -35.0],
            lon_lim=[149.0, 149.5],
            path=str(tmp_path),
            endpoint="dea",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
class TestVedaE2E:
    """NASA VEDA — anonymous, us-west-2."""

    def test_nldas3_writes_cog(self, tmp_path: Path):
        """A one-item NLDAS-3 pull writes a readable COG."""
        stac = STAC(
            start="2020-01-01",
            end="2024-12-31",
            variables={"veda/nldas3": ["cog_default"]},
            lat_lim=[35.0, 45.0],
            lon_lim=[-100.0, -90.0],
            path=str(tmp_path),
            endpoint="veda",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
class TestBdcE2E:
    """Brazil Data Cube — anonymous (open collections; token-gated ones gated on BDC_ACCESS_TOKEN)."""

    def test_cbers4_wfi_writes_cog(self, tmp_path: Path):
        """A one-item CBERS-4 WFI 16-day composite over São Paulo writes a readable COG."""
        stac = STAC(
            start="2024-01-01",
            end="2024-12-31",
            variables={"bdc/CBERS4-WFI-16D-2": ["NDVI"]},
            lat_lim=[-23.7, -23.2],
            lon_lim=[-46.8, -46.3],
            path=str(tmp_path),
            endpoint="bdc",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
@pytest.mark.skipif(
    not (
        os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
    ),
    reason="usgs-landsat e2e is requester-pays — needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY",
)
class TestUsgsLandsatE2E:
    """USGS LandsatLook — requester-pays on s3://usgs-landsat (gated on AWS creds)."""

    def test_c2l2_sr_writes_cog(self, tmp_path: Path):
        """A one-item Landsat C2 L2 SR pull over SF Bay writes a readable COG."""
        stac = STAC(
            start="2024-06-01",
            end="2024-08-31",
            variables={"usgs-landsat/landsat-c2l2-sr": ["red"]},
            lat_lim=[37.5, 38.0],
            lon_lim=[-122.5, -122.0],
            path=str(tmp_path),
            endpoint="usgs-landsat",
            max_items=1,
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"
