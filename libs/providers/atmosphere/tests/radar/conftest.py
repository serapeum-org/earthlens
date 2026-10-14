"""Shared fakes for the NEXRAD radar backend tests (no network).

Injects a fake `boto3` whose unsigned S3 client serves a small in-memory
NEXRAD chunk tree, so the whole suite runs without `boto3` installed or
any network.
"""

from __future__ import annotations

import pytest

import earthlens.radar.backend as backend_module
from earthlens.radar.backend import BUCKET
from earthlens.testing import seed_s3


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
def fake_s3(s3_emulator):
    """An in-process S3 serving the KTLX tree from the real chunk bucket.

    Replaces a hand-written client that re-implemented `list_objects_v2` —
    including its `Delimiter` / `CommonPrefixes` branch and a `IsTruncated`
    field it always set to `False`. Those are exactly the semantics the
    backend's prefix walk depends on, so they are better borrowed from an S3
    implementation than restated here.

    Nothing is injected: `mock_aws()` intercepts inside botocore, so the
    unsigned client `Radar._s3_client` builds for itself resolves here too.
    """
    seed_s3(s3_emulator, BUCKET, _ktlx_objects())
    return s3_emulator


@pytest.fixture
def pin_client(monkeypatch):
    """Make the backend reuse one specific client instead of building its own.

    The emulator returns a fresh client per `boto3.client()` call — faithful to
    boto3 — so a test that injects a failure by replacing a method on *its*
    client needs the backend to use that same object.
    """

    def _pin(client):
        monkeypatch.setattr(backend_module, "_s3_client", lambda region: client)
        return client

    return _pin
