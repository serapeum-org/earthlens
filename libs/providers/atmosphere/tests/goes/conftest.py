"""Fixtures for the GOES backend tests: a fake unsigned-S3 client + factory."""

from __future__ import annotations

import io
from typing import Any

import pytest

from earthlens.goes import GOES, Catalog
from earthlens.testing import seed_s3

#: boto3 ships with the backend's optional extra; absent, skip the module
#: instead of interrupting collection for the directory.
ClientError = pytest.importorskip("botocore.exceptions").ClientError


class _FakeBody:
    """Minimal file-like stand-in for a boto3 streaming body."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        """Return up to `size` bytes (whole payload when `size < 0`)."""
        return self._buffer.read(size)


class FakeS3:
    """In-memory unsigned-S3 double with paged `list_objects_v2` + `get_object`.

    `pages` maps a key prefix to either a flat list of keys (one page) or a
    list of pages (each a list of keys), so pagination is exercised. Keys in
    `missing` raise `NoSuchKey` on `get_object`.
    """

    def __init__(
        self,
        pages: dict[str, Any] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self._pages = pages or {}
        self.missing = missing or set()
        self.listed: list[tuple[str, str, str | None]] = []
        self.requested: list[tuple[str, str]] = []

    def list_objects_v2(
        self, Bucket: str, Prefix: str, ContinuationToken: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        """Return the requested page of keys under `Prefix`."""
        self.listed.append((Bucket, Prefix, ContinuationToken))
        entry = self._pages.get(Prefix, [])
        pages = [entry] if entry and isinstance(entry[0], str) else entry
        index = int(ContinuationToken) if ContinuationToken else 0
        if index >= len(pages):
            return {"Contents": []}
        response: dict[str, Any] = {"Contents": [{"Key": k} for k in pages[index]]}
        if index + 1 < len(pages):
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(index + 1)
        return response

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        """Return fake bytes for a known key, or raise NoSuchKey for a miss."""
        self.requested.append((Bucket, Key))
        if Key in self.missing:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _FakeBody(b"netcdf:" + Key.encode())}


@pytest.fixture
def catalog() -> Catalog:
    """The bundled GOES catalog."""
    return Catalog()


@pytest.fixture
def make_goes(tmp_path):
    """Factory building a GOES instance writing under a temp dir."""

    def _make(**overrides: Any) -> GOES:
        kwargs: dict[str, Any] = dict(
            start="2026-07-03 12:00",
            end="2026-07-03 12:30",
            variables=None,
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            dataset="abi-l2-mcmip",
            domain="C",
            satellite="east",
            path=str(tmp_path),
            fmt="%Y-%m-%d %H:%M",
        )
        kwargs.update(overrides)
        return GOES(**kwargs)

    return _make


@pytest.fixture
def patch_client(monkeypatch):
    """Factory wiring a `FakeS3` onto a GOES instance's `_client`."""

    def _patch(goes: GOES, fake: FakeS3) -> FakeS3:
        monkeypatch.setattr(goes, "_client", lambda: fake)
        return fake

    return _patch


@pytest.fixture
def seed_goes(s3_emulator):
    """Seed the backend's own bucket with the given keys, on an in-process S3.

    Replaces `patch_client(goes, FakeS3(pages={prefix: keys}))`. Nothing is
    injected: `mock_aws()` intercepts inside botocore, so the unsigned client
    the backend builds resolves to the seeded bucket and the prefix walk runs
    against S3's real `list_objects_v2` rather than a re-implementation of it.

    `FakeS3` stays for `test_helpers.py`, which unit-tests the pagination
    helper itself and needs to drive continuation tokens directly — something
    the emulator only does past a thousand keys.
    """

    def _seed(goes, keys, *, missing=()):
        present = {
            key: b"netcdf:" + key.encode() for key in keys if key not in set(missing)
        }
        seed_s3(s3_emulator, goes._bucket, present)
        return s3_emulator

    return _seed
