"""Shared fakes for the NOAA National Water Model backend tests (no network).

Injects a fake `boto3` whose unsigned S3 client serves a small in-memory
NWM object tree, so the whole suite runs without `boto3` installed or any
network.
"""

from __future__ import annotations

import sys
import types

import pytest


class _Body:
    """Stand-in for an S3 object body."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    """Minimal anonymous S3 client over an in-memory `{key: bytes}` map.

    A missing key raises `KeyError` on `get_object`, mimicking the
    "(cycle, step) not published yet" miss the backend skips over.
    """

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.get_calls: list[str] = []

    def get_object(self, Bucket, Key):
        """Record the get and return the object's bytes (KeyError if absent)."""
        self.get_calls.append(Key)
        return {"Body": _Body(self.objects[Key])}


def _nwm_objects() -> dict[str, bytes]:
    """short_range cycle 00 with f001/f002 channel_rt + a land f001 file."""
    base = "nwm.20260525/short_range/nwm.t00z.short_range"
    return {
        f"{base}.channel_rt.f001.conus.nc": b"\x89HDF-c1",
        f"{base}.channel_rt.f002.conus.nc": b"\x89HDF-c2",
        f"{base}.land.f001.conus.nc": b"\x89HDF-l1",
    }


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> _FakeS3:
    """Inject a fake `boto3`/`botocore` whose client serves the NWM tree."""
    client = _FakeS3(_nwm_objects())
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = lambda *a, **k: client
    botocore = types.ModuleType("botocore")
    botocore.UNSIGNED = object()
    client_mod = types.ModuleType("botocore.client")
    client_mod.Config = lambda **k: None
    botocore.client = client_mod
    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.client", client_mod)
    return client
