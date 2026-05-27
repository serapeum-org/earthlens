"""Fixtures for the NWM backend tests: a sample catalog and a fake S3 client."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from earthlens.nwm import NWM, Catalog


class _FakeBody:
    """Minimal stand-in for a boto3 streaming body."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        """Return the whole payload."""
        return self._data


class FakeS3:
    """In-memory unsigned-S3 client double recording every `get_object`."""

    def __init__(self, available: set[str] | None = None) -> None:
        self.available = available
        self.requested: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        """Return fake bytes for a known key, or raise NoSuchKey for a miss."""
        self.requested.append((Bucket, Key))
        if self.available is not None and Key not in self.available:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _FakeBody(b"netcdf:" + Key.encode())}


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
