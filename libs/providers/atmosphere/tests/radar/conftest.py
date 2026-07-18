"""Shared fakes for the NEXRAD radar backend tests (no network).

Injects a fake `boto3` whose unsigned S3 client serves a small in-memory
NEXRAD chunk tree, so the whole suite runs without `boto3` installed or
any network.
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
    """Minimal anonymous S3 client over an in-memory `{key: bytes}` map."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.get_calls: list[str] = []

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, **kwargs):
        """Mimic list_objects_v2 (CommonPrefixes with Delimiter, else Contents)."""
        keys = [k for k in self.objects if k.startswith(Prefix)]
        if Delimiter:
            prefixes: set[str] = set()
            contents: list[str] = []
            for k in keys:
                rest = k[len(Prefix) :]
                if Delimiter in rest:
                    prefixes.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
                else:
                    contents.append(k)
            return {
                "CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)],
                "Contents": [{"Key": k} for k in sorted(contents)],
                "IsTruncated": False,
            }
        return {"Contents": [{"Key": k} for k in sorted(keys)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        """Record the get and return the object's bytes."""
        self.get_calls.append(Key)
        return {"Body": _Body(self.objects[Key])}


def _ktlx_objects() -> dict[str, bytes]:
    """A KTLX tree with two volumes — 12:00:00 (3 chunks) and 13:05:00 (2)."""
    return {
        "KTLX/100/20240601-120000-001-S": b"AR2V0006.100<S>",
        "KTLX/100/20240601-120000-002-I": b"<I2>",
        "KTLX/100/20240601-120000-003-E": b"<E>",
        "KTLX/101/20240601-130500-001-S": b"AR2V0006.101<S>",
        "KTLX/101/20240601-130500-002-E": b"<E>",
    }


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> _FakeS3:
    """Inject a fake `boto3`/`botocore` whose client serves the KTLX tree."""
    client = _FakeS3(_ktlx_objects())
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
