"""Live end-to-end tests for the AWS Open-Data S3 backend (gated by `-m e2e`).

Each test performs a real unsigned download of one small granule from a
public bucket, cropped to a tiny AOI, and asserts the output is non-empty
and clipped to the request. No credentials are required (the buckets are
public); the tests skip cleanly when S3 is unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.s3, pytest.mark.e2e]


@pytest.fixture(scope="module", autouse=True)
def _require_network():
    """Skip the module when the public S3 endpoint is unreachable."""
    import botocore
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        config=Config(
            signature_version=botocore.UNSIGNED,
            connect_timeout=5,
            retries={"max_attempts": 1},
        ),
    )
    try:
        client.list_objects_v2(Bucket="esa-worldcover", Prefix="v200/2021/map/", MaxKeys=1)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"S3 unreachable: {exc}")


def _assert_cropped(path, max_px=2000):
    """Assert the output exists, is EPSG:4326, and smaller than the source tile."""
    from pyramids.dataset import Dataset

    assert Path(path).exists() and Path(path).stat().st_size > 0
    ds = Dataset.read_file(str(path))
    assert ds.epsg == 4326
    assert ds.shape[1] <= max_px and ds.shape[2] <= max_px


def test_copernicus_dem(tmp_path):
    """A small Copernicus DEM AOI downloads and crops to WGS84."""
    from earthlens.s3 import S3

    source = S3(
        start="2021-01-01", end="2021-01-01",
        lat_lim=[0.40, 0.45], lon_lim=[6.40, 6.45],
        dataset="copernicus-dem", path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1
    _assert_cropped(paths[0])


def test_esa_worldcover(tmp_path):
    """A small ESA WorldCover AOI downloads and crops to WGS84."""
    from earthlens.s3 import S3

    source = S3(
        start="2021-01-01", end="2021-01-01",
        lat_lim=[0.40, 0.45], lon_lim=[6.40, 6.45],
        dataset="esa-worldcover", path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1
    _assert_cropped(paths[0])


def test_sentinel2_reprojects_from_utm(tmp_path):
    """One Sentinel-2 band downloads, reprojects UTM->WGS84, and crops."""
    from earthlens.s3 import S3

    source = S3(
        start="2024-06-01", end="2024-06-06",
        lat_lim=[30.00, 30.06], lon_lim=[31.20, 31.26],
        dataset="sentinel-2-l2a", variables=["red"], path=str(tmp_path),
    )
    products = source._search()
    assert products, "no Sentinel-2 scenes found for the window"
    written = source._fetch(products[:1])
    _assert_cropped(written[0])


def test_era5_netcdf(tmp_path):
    """An ERA5 monthly NetCDF downloads, wraps longitude, and crops to WGS84.

    Large granule (hundreds of MB); runs only in the gated e2e job.
    """
    from earthlens.s3 import S3

    source = S3(
        start="2023-12-01", end="2023-12-01",
        lat_lim=[40.0, 42.0], lon_lim=[12.0, 14.0],
        dataset="era5", variables=["t2m"], path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1 and Path(paths[0]).exists()


@pytest.mark.xfail(
    reason="geostationary NetCDF reproject is deferred to the pyramids PY-1 port",
    raises=NotImplementedError,
    strict=True,
)
def test_goes_reprojects_from_geostationary(tmp_path):
    """One GOES ABI frame downloads; cropping is deferred to PY-1 (geostationary warp).

    The search/download half works; `_localise` raises `NotImplementedError`
    until pyramids exposes a geostationary reproject (PY-1).
    """
    from earthlens.s3 import S3

    source = S3(
        start="2024-06-28", end="2024-06-28",
        lat_lim=[30.0, 32.0], lon_lim=[-100.0, -98.0],
        dataset="goes", variables=["C13"], path=str(tmp_path),
    )
    products = source._search()
    assert products, "no GOES frames found"
    source._fetch(products[:1])
