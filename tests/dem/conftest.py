"""Shared fixtures for the DEM backend tests: a fake unsigned S3 client."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError


class FakeS3Client:
    """Fake unsigned `boto3` client with `head_object` + `download_file`.

    Backed by a `keys` set of "present" bucket-relative keys. Any key not
    in the set raises `ClientError` with `Error.Code == "404"`, mirroring
    the real anonymous 404 the Copernicus DEM buckets return over ocean
    tiles. `download_file` writes a small placeholder payload so the
    backend's atomic-rename path is exercised.
    """

    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.head_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str, str]] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return a minimal head response, or raise 404 when absent."""
        self.head_calls.append((Bucket, Key))
        if Key not in self.keys:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
            )
        return {"ContentLength": 128}

    def download_file(
        self, Bucket: str, Key: str, Filename: str, ExtraArgs: dict | None = None
    ) -> None:
        """Write a small payload so the backend's `.part` rename runs."""
        self.download_calls.append((Bucket, Key, Filename))
        with open(Filename, "wb") as handle:
            handle.write(b"cog-placeholder")


@pytest.fixture
def fake_client_all_present():
    """Fake client that says every key requested is present."""
    return FakeS3Client(keys=set())  # populated by tests via .keys.add(...)


@pytest.fixture
def make_fake_client():
    """Factory: build a `FakeS3Client` from an iterable of present keys."""

    def _factory(present: list[str] | None = None) -> FakeS3Client:
        return FakeS3Client(keys=set(present or []))

    return _factory
