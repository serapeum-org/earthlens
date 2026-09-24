"""Live end-to-end tests for the STAC backend (network; gated behind -m e2e).

These hit real STAC APIs and write real COGs, so they are marked `e2e` and
deselected by the default `-m "not e2e"` run. Earth Search is anonymous and
always runs (given the `[stac]` extra); MPC needs `planetary-computer` installed
(public, no account); CDSE and usgs-landsat read with S3 credentials taken from
the environment (CI secrets) — the `e2e` marker decides whether they run, so
under `-m e2e` they execute and fail loudly if their keys are absent rather than
skipping on an env-var check.

Note on asset keys: the catalog aliases the *collection id* per endpoint but not
the *asset keys*, which differ across providers (Earth Search exposes
`red`/`green`/`nir`; MPC exposes `B04`/`B08`). Each test therefore passes the
asset names that endpoint actually serves.
"""

from __future__ import annotations

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
@pytest.mark.xfail(
    reason=(
        "blocked on serapeum-org/pyramids#983: CloudConfig applies the custom "
        "AWS_S3_ENDPOINT thread-locally, which GDAL's VSICurl worker threads do "
        "not see, so a large CDSE (non-AWS S3) read is sent to real AWS and 403s. "
        "The credentials are valid (a single small read succeeds); remove this "
        "xfail once pyramids#983 lands."
    ),
    strict=False,
)
class TestCdseE2E:
    """Copernicus Data Space — S3-credentialled asset reads.

    Reads its assets with `CDSE_S3_ACCESS_KEY` / `CDSE_S3_SECRET_KEY` from the
    environment (CI provides them as secrets). The `e2e` marker — not an
    env-var check — decides whether this runs, so under `-m e2e` it executes
    and fails loudly when the keys are missing rather than skipping silently.

    Currently `xfail` — the CDSE keys authenticate and read a single asset, but
    the mosaic/merge read is blocked upstream by pyramids#983 (see the marker).
    """

    def test_sentinel2_writes_cog(self, tmp_path: Path):
        """A one-item CDSE Sentinel-2 pull writes a readable COG.

        CDSE serves resolution-suffixed asset keys (`B04_10m`), unlike MPC /
        Earth Search which expose a bare `B04`, so the request names `B04_10m`.
        """
        stac = STAC(
            start=_START,
            end=_END,
            variables={"sentinel-2-l2a": ["B04_10m"]},
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
class TestEodcGfmE2E:
    """EODC Copernicus GFM — anonymous public STAC, no credentials."""

    def test_flood_extent_writes_cog(self, tmp_path: Path):
        """A GFM ensemble flood-extent pull over the 2022 Pakistan (Sindh) flood.

        The AOI + date are chosen so the SAR swath actually carries flood-mapped
        pixels — GFM flood extent is sparse, and a box over an unobserved swath
        is legitimately all-nodata (the crop would then find no valid pixels).
        """
        stac = STAC(
            start="2022-09-11",
            end="2022-09-11",
            variables={"eodc/gfm": ["ensemble_flood_extent"]},
            lat_lim=[27.0, 28.0],
            lon_lim=[67.0, 68.0],
            path=str(tmp_path),
            endpoint="eodc",
        )
        paths = stac.download()
        assert paths, "expected at least one COG written"
        assert all(Path(p).is_file() for p in paths), f"missing COG file in {paths}"


@pytest.mark.stac
@pytest.mark.e2e
@pytest.mark.xfail(
    reason=(
        "requester-pays: needs valid AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
        "repo secrets (not yet provisioned), which bill the account for the "
        "transfer. Remove this xfail once the AWS secrets are set."
    ),
    strict=False,
)
class TestUsgsLandsatE2E:
    """USGS LandsatLook — requester-pays on `s3://usgs-landsat`.

    Reads its assets with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the
    environment (CI provides them as secrets); the bucket is requester-pays, so
    the credentialled account is billed for the transfer. The `e2e` marker — not
    an env-var check — decides whether this runs, so under `-m e2e` it executes
    and fails loudly when the keys are missing rather than skipping silently.

    Currently `xfail` — no AWS requester-pays credentials are provisioned as repo
    secrets yet; `strict=False` so it flips to `xpass` once they are added.
    """

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
