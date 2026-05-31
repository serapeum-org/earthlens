"""Shared fixtures for the AWS Open-Data S3 backend tests.

Provides an offline fake `boto3` S3 client (records `download_file`
calls, serves a tiny synthetic COG, can simulate missing objects and
listing pages) and a synthetic Copernicus-DEM-shaped GeoTIFF so the full
search/fetch/crop path runs with no network.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from earthlens.s3.auth import S3Auth


class FakeS3Client:
    """Minimal stand-in for a `boto3` S3 client used in the backend tests.

    Args:
        listing: Map of bucket to the object keys that bucket "contains",
            used by the paginator for `prefix_listing` datasets.
        fixture: Path to a local file copied to the destination on every
            `download_file` call (the tiny synthetic COG).
        missing: Keys for which `download_file` raises a 404-style
            `ClientError` (to exercise the skip-missing path).
    """

    def __init__(self, listing=None, fixture=None, missing=None, broken=None):
        self.listing = listing or {}
        self.fixture = fixture
        self.missing = set(missing or [])
        self.broken = set(broken or [])
        self.downloaded: list[tuple[str, str]] = []
        self.extra_args: list[dict | None] = []

    def download_file(self, bucket: str, key: str, dst: str, ExtraArgs=None) -> None:
        self.downloaded.append((bucket, key))
        self.extra_args.append(ExtraArgs)
        if key in self.broken:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "500", "Message": "Internal Error"}}, "GetObject"
            )
        if key in self.missing:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
            )
        import shutil

        shutil.copyfile(self.fixture, dst)

    def get_paginator(self, _name: str) -> "FakePaginator":
        return FakePaginator(self.listing)


class FakePaginator:
    """Paginator over an in-memory `{bucket: [keys]}` table."""

    def __init__(self, listing: dict[str, list[str]]):
        self.listing = listing

    def paginate(self, Bucket: str, Prefix: str = "", Delimiter: str | None = None):
        keys = [k for k in self.listing.get(Bucket, []) if k.startswith(Prefix)]
        if Delimiter:
            prefixes = sorted(
                {
                    Prefix + rest.split("/")[0] + "/"
                    for k in keys
                    if "/" in (rest := k[len(Prefix):])
                }
            )
            yield {"CommonPrefixes": [{"Prefix": p} for p in prefixes]}
        else:
            yield {"Contents": [{"Key": k, "Size": 10} for k in keys]}


@pytest.fixture
def tiny_cog(tmp_path_factory) -> Path:
    """A 1-degree synthetic GeoTIFF covering [6, 0, 7, 1] in EPSG:4326."""
    from pyramids.dataset import Dataset

    path = tmp_path_factory.mktemp("fixtures") / "tile.tif"
    arr = np.arange(100, dtype="float32").reshape(1, 10, 10)
    ds = Dataset.create_from_array(
        arr=arr, top_left_corner=(6.0, 1.0), cell_size=0.1, epsg=4326
    )
    ds.to_file(str(path))
    return path


@pytest.fixture
def tiny_era5_nc(tmp_path_factory) -> Path:
    """A synthetic ERA5-shaped NetCDF: global 0-360 lon, an Italy lat band, 2 times."""
    import xarray as xr

    path = tmp_path_factory.mktemp("ncfix") / "era5.nc"
    lon = np.arange(0, 360, 0.25)
    lat = np.arange(42, 39.75, -0.25)
    arr = np.random.RandomState(0).rand(2, lat.size, lon.size).astype("float32")
    xr.Dataset(
        {"VAR_2T": (("time", "latitude", "longitude"), arr)},
        coords={"time": np.array([0.0, 1.0]), "latitude": lat, "longitude": lon},
    ).to_netcdf(path)
    return path


@pytest.fixture
def fake_client_factory(tiny_cog):
    """Return a builder for a `FakeS3Client` backed by the synthetic COG."""

    def _build(listing=None, missing=None, broken=None) -> FakeS3Client:
        return FakeS3Client(
            listing=listing, fixture=tiny_cog, missing=missing, broken=broken
        )

    return _build


@pytest.fixture
def patch_auth(monkeypatch):
    """Patch `S3Auth.client` to return a supplied fake client."""

    def _apply(client) -> None:
        monkeypatch.setattr(S3Auth, "client", lambda self: client)

    return _apply
