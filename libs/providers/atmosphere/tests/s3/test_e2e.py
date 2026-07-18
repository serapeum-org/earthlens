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
    import boto3
    import botocore
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
        client.list_objects_v2(
            Bucket="esa-worldcover", Prefix="v200/2021/map/", MaxKeys=1
        )
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
        start="2021-01-01",
        end="2021-01-01",
        lat_lim=[0.40, 0.45],
        lon_lim=[6.40, 6.45],
        dataset="copernicus-dem",
        path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1
    _assert_cropped(paths[0])


def test_esa_worldcover(tmp_path):
    """A small ESA WorldCover AOI downloads and crops to WGS84."""
    from earthlens.s3 import S3

    source = S3(
        start="2021-01-01",
        end="2021-01-01",
        lat_lim=[0.40, 0.45],
        lon_lim=[6.40, 6.45],
        dataset="esa-worldcover",
        path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1
    _assert_cropped(paths[0])


def test_sentinel2_reprojects_from_utm(tmp_path):
    """One Sentinel-2 band downloads, reprojects UTM->WGS84, and crops."""
    from earthlens.s3 import S3

    source = S3(
        start="2024-06-01",
        end="2024-06-06",
        lat_lim=[30.00, 30.06],
        lon_lim=[31.20, 31.26],
        dataset="sentinel-2-l2a",
        variables=["red"],
        path=str(tmp_path),
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
        start="2023-12-01",
        end="2023-12-01",
        lat_lim=[40.0, 42.0],
        lon_lim=[12.0, 14.0],
        dataset="era5",
        variables=["t2m"],
        path=str(tmp_path),
    )
    paths = source.download(progress_bar=False)
    assert len(paths) == 1 and Path(paths[0]).exists()


def _have_aws_credentials() -> bool:
    """True when a usable AWS credential chain is configured."""
    import boto3

    return boto3.Session().get_credentials() is not None


requester_pays = pytest.mark.skipif(
    not _have_aws_credentials(),
    reason="requester-pays datasets need valid AWS credentials (caller is billed)",
)


@requester_pays
def test_usgs_landsat_requester_pays(tmp_path):
    """One Landsat Collection-2 band downloads (requester-pays) and crops to WGS84.

    Requires valid AWS credentials; skipped otherwise. The caller's account is
    billed for the request/egress.
    """
    from earthlens.s3 import S3

    source = S3(
        start="2021-09-01",
        end="2021-09-01",
        lat_lim=[36.5, 37.0],
        lon_lim=[-120.5, -120.0],
        dataset="usgs-landsat",
        variables=["red"],
        scene="LC08_L2SP_039037_20210901_20210910_02_T1",
        path=str(tmp_path),
    )
    written = source._fetch(source._search()[:1])
    _assert_cropped(written[0])


@requester_pays
def test_naip_requester_pays(tmp_path):
    """One NAIP quad downloads (requester-pays) and crops to WGS84.

    Requires valid AWS credentials; skipped otherwise.
    """
    from earthlens.s3 import S3

    tile = "al/2021/100cm/rgbir_cog/30086/m_3008601_ne_16_060_20211004"
    source = S3(
        start="2021-10-04",
        end="2021-10-04",
        lat_lim=[30.0, 30.1],
        lon_lim=[-86.0, -85.9],
        dataset="naip-source",
        tile=tile,
        path=str(tmp_path),
    )
    written = source._fetch(source._search()[:1])
    _assert_cropped(written[0])


def _goes_source(tmp_path):
    """Fetch one GOES ABI C13 frame over a Texas AOI; return (output, raw)."""
    from earthlens.s3 import S3

    source = S3(
        start="2024-06-28",
        end="2024-06-28",
        lat_lim=[30.0, 32.0],
        lon_lim=[-100.0, -98.0],
        dataset="goes",
        variables=["C13"],
        path=str(tmp_path),
    )
    products = source._search()
    assert products, "no GOES frames found"
    written = source._fetch(products[:1])
    raw = next(Path(tmp_path).rglob("*.nc"))
    return written[0], raw


def test_goes_reprojects_from_geostationary(tmp_path):
    """One GOES ABI frame downloads, warps geostationary->WGS84, and crops."""
    written, _ = _goes_source(tmp_path)
    _assert_cropped(written)


def test_goes_matches_source_radiance(tmp_path):
    """The warped GOES crop carries the radiances its lon/lat actually name.

    A wrong scan-angle georeference still yields an EPSG:4326 raster with the
    requested bounds, so `_assert_cropped` cannot see it. This samples the
    geostationary source grid at the AOI centre and requires the output's
    value there to come from the same neighbourhood.
    """
    import numpy as np
    from pyramids.base.crs import reproject_coordinates
    from pyramids.dataset import Dataset

    written, raw = _goes_source(tmp_path)
    lon, lat = -99.0, 31.0

    src = Dataset.read_file(f'NETCDF:"{raw}":CMI')
    gt = src.geotransform
    xs, ys = reproject_coordinates([lon], [lat], from_crs=4326, to_crs=src.crs)
    col = int((xs[0] - gt[0]) / gt[1])
    row = int((ys[0] - gt[3]) / gt[5])
    source_arr = np.asarray(src.read_array())
    if source_arr.ndim == 3:
        source_arr = source_arr[0]
    # A 5x5 window absorbs the nearest-neighbour choice the warp makes.
    window = source_arr[row - 2 : row + 3, col - 2 : col + 3].astype("float64")

    out = Dataset.read_file(str(written))
    ogt = out.geotransform
    out_arr = np.asarray(out.read_array())
    if out_arr.ndim == 3:
        out_arr = out_arr[0]
    ocol = int((lon - ogt[0]) / ogt[1])
    orow = int((lat - ogt[3]) / ogt[5])
    value = float(out_arr[orow, ocol])

    assert window.min() <= value <= window.max(), (
        f"GOES crop at ({lon}, {lat}) reads {value}, outside the source "
        f"neighbourhood [{window.min()}, {window.max()}] — the geostationary "
        "grid is misregistered (the reprojected pixels do not match the header)."
    )
