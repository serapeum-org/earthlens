"""Live end-to-end tests for the STAC backend (network; gated behind -m e2e).

These hit real STAC APIs and write real COGs, so they are marked `e2e` and
deselected by the default `-m "not e2e"` run. Earth Search is anonymous and
always runs (given the `[stac]` extra); MPC needs `planetary-computer` installed
(public, no account); CDSE and usgs-landsat read with credentials taken from the
environment. Gating is by marker only — `-m e2e` decides whether a test runs — so
a missing credential or a known defect surfaces as an `xfail` with a stated
reason, never as a silent env-var skip.

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
class TestCdseE2E:
    """Copernicus Data Space — S3-credentialled asset reads.

    Credentials come from `CDSE_S3_ACCESS_KEY` / `CDSE_S3_SECRET_KEY` in the
    environment, but the `e2e` marker — not an env-var check — decides whether
    this runs, so it executes under `-m e2e` and fails loudly (never skips
    silently) if the live read breaks. The non-AWS `eodata` endpoint reads with
    the default multi-threaded workers: pyramids-core >=0.71.0 (the pinned floor,
    serapeum-org/pyramids#1348) routes the custom endpoint to them.
    """

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
        """A one-item WOfS pull over Lake Tana writes a readable COG.

        Lake Tana, not a dry inland city: WOfS is Water Observations from
        Space, and `max_items=1` keeps a single Landsat scene, so the AOI has
        to be one the first returned scene covers with real observations. The
        earlier Johannesburg box returned a scene that clipped to all-nodata
        over the small window (crop produced no valid pixels); a large
        permanent lake yields a scene full of valid water/dry classifications.
        """
        stac = STAC(
            start="2024-01-01",
            end="2024-12-31",
            variables={"deafrica/wofs_ls": ["water"]},
            lat_lim=[11.9, 12.1],
            lon_lim=[37.2, 37.4],
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
@pytest.mark.xfail(
    reason=(
        "earthlens#938: s3://veda-data-store denies anonymous reads, so the asset "
        "open fails with AccessDenied. VEDA publishes through its raster API rather "
        "than as directly-readable public COGs, so this endpoint cannot be served by "
        "the direct-COG backend as catalogued; #938 carries the disposition."
    ),
    strict=False,
)
class TestVedaE2E:
    """NASA VEDA — anonymous, us-west-2.

    Currently `xfail`: the bucket is not anonymously readable (see the marker).
    `strict=False`, so it flips to `xpass` if VEDA ever opens direct S3 access.
    """

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
        "requester-pays on s3://usgs-landsat: needs valid AWS_ACCESS_KEY_ID / "
        "AWS_SECRET_ACCESS_KEY, which are not provisioned as repo secrets (the "
        "credentialled account is billed for the transfer). A provisioning task, "
        "not a code defect."
    ),
    strict=False,
)
class TestUsgsLandsatE2E:
    """USGS LandsatLook — requester-pays on `s3://usgs-landsat`.

    Credentials come from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the
    environment; the `e2e` marker decides whether this runs, so it executes under
    `-m e2e` and reports `xfail` rather than skipping on an env-var check.
    `strict=False`, so it flips to `xpass` once the secrets are provisioned.
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
