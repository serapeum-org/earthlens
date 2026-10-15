"""Fixtures for the NWM backend tests: a sample catalog and fake S3 + reader."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from earthlens.nwm import NWM, Catalog
from earthlens.nwm.backend import BUCKET
from earthlens.testing import seed_s3

#: boto3 ships with the backend's optional extra; absent, skip the module
#: instead of interrupting collection for the directory.
ClientError = pytest.importorskip("botocore.exceptions").ClientError


class FakeLabeled:
    """In-memory `LabeledDataset` double recording the selection chain.

    Each `select*` returns `self` (records the call); `to_parquet` writes a
    sentinel file and returns its path so the backend's wiring is exercised
    without any real NetCDF/Zarr or network.
    """

    #: Class-level log of `(method, args)` across every instance, so a test
    #: can assert what the backend asked the reader to do.
    calls: list[tuple[str, Any]] = []

    def __init__(self, href: str, variables: Any) -> None:
        self.href = href
        self.variables = variables

    @classmethod
    def read_file(cls, path, *, anon: bool = False, variables=None, **kw):
        """Record the open and return a fresh recording instance."""
        cls.calls.append(("read_file", {"path": str(path), "anon": anon}))
        return cls(str(path), variables)

    def select(self, **labels: Any) -> FakeLabeled:
        """Record a label selection."""
        FakeLabeled.calls.append(("select", labels))
        return self

    def select_by_coord(self, coord: str, values: Any) -> FakeLabeled:
        """Record a secondary-coordinate (gage_id) selection."""
        FakeLabeled.calls.append(("select_by_coord", (coord, list(values))))
        return self

    def select_bbox(self, bbox: Any, **kw: Any) -> FakeLabeled:
        """Record a bbox selection."""
        FakeLabeled.calls.append(("select_bbox", tuple(bbox)))
        return self

    def select_time(self, start: Any, end: Any, **kw: Any) -> FakeLabeled:
        """Record a time-window selection."""
        FakeLabeled.calls.append(("select_time", (start, end)))
        return self

    def to_parquet(self, path, **kw: Any) -> Path:
        """Write a sentinel Parquet file and return its path."""
        path = Path(path)
        path.write_bytes(b"PAR1-fake")
        FakeLabeled.calls.append(("to_parquet", str(path)))
        return path

    def close(self) -> None:
        """No-op handle release."""


@pytest.fixture
def fake_reader(monkeypatch):
    """Factory wiring `FakeLabeled` onto an NWM instance's `_reader`."""

    def _patch(nwm: NWM) -> type[FakeLabeled]:
        FakeLabeled.calls = []
        monkeypatch.setattr(nwm, "_reader", lambda: FakeLabeled)
        return FakeLabeled

    return _patch


class _FakeGridDataset:
    """Stand-in for a pyramids `Dataset` returned by `NetCDF.subset`."""

    def to_cog(self, path: str, **kw) -> None:
        """Write a sentinel COG GeoTIFF."""
        Path(path).write_bytes(b"II*\x00-fake-cog")


class FakeNetCDF:
    """In-memory `pyramids.netcdf.NetCDF` double recording `subset` calls."""

    calls: list[tuple[str, Any]] = []

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def read_file(cls, path, **kw) -> FakeNetCDF:
        """Record the open and return a recording instance."""
        cls.calls.append(("read_file", str(path)))
        return cls(str(path))

    def subset(self, variable, *, time=None, bbox=None, crs=4326, **dims):
        """Record a windowed subset and return a fake Dataset."""
        FakeNetCDF.calls.append(
            ("subset", {"variable": variable, "time": time, "bbox": bbox})
        )
        return _FakeGridDataset()

    def close(self) -> None:
        """No-op handle release."""


@pytest.fixture
def fake_netcdf(monkeypatch):
    """Factory wiring `FakeNetCDF` onto an NWM instance's `_netcdf_reader`."""

    def _patch(nwm: NWM) -> type[FakeNetCDF]:
        FakeNetCDF.calls = []
        monkeypatch.setattr(nwm, "_netcdf_reader", lambda: FakeNetCDF)
        return FakeNetCDF

    return _patch


@pytest.fixture
def catalog() -> Catalog:
    """The bundled NWM catalog."""
    return Catalog()


@pytest.fixture
def make_nwm(tmp_path):
    """Factory building an NWM instance writing under a temp dir."""

    def _make(**overrides: Any) -> NWM:
        kwargs: dict[str, Any] = dict(
            start="2026-05-26",
            end="2026-05-26",
            variables={"chrtout": ["streamflow"]},
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            configuration="short_range",
            path=str(tmp_path),
        )
        kwargs.update(overrides)
        return NWM(**kwargs)

    return _make


@pytest.fixture
def patch_client(monkeypatch):
    """Factory wiring a `FakeS3` onto an NWM instance's `_client`."""

    def _patch(nwm: NWM, fake: FakeS3) -> FakeS3:
        monkeypatch.setattr(nwm, "_client", lambda: fake)
        return fake

    return _patch


@pytest.fixture
def seed_nwm(s3_emulator):
    """Publish the keys an NWM run plans for, on an in-process S3.

    Replaces `patch_client(nwm, FakeS3(available=...))`. The old double's
    `available=None` meant "every key exists", which a real bucket cannot
    express — so the plan itself (`NWM._search`) says which keys to publish,
    and `available=` narrows that to the subset a test wants present. A key
    left out is then genuinely absent, and the `NoSuchKey` the backend sees is
    S3's own, not a hand-built `ClientError`.

    Nothing is injected: `mock_aws()` intercepts inside botocore, so the
    unsigned client the backend builds resolves to the seeded bucket.
    """

    def _seed(nwm, available=None):
        planned = [product.href for product in nwm._search()]
        keys = planned if available is None else [k for k in planned if k in available]
        seed_s3(
            s3_emulator,
            BUCKET,
            {key: b"netcdf:" + key.encode() for key in keys},
        )
        return s3_emulator

    return _seed
