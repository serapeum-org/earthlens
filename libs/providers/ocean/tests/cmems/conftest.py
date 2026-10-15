"""Fixtures for the CMEMS suite.

Holds the seam the wire test needs: everything the toolbox reads from
CloudFerro, served by the in-process S3 rather than replayed from a cassette.
`test_wire.py` explains why one exchange is replayed by two mechanisms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.testing import seed_s3

#: The CloudFerro endpoint the toolbox reads from. Not an AWS host, which is
#: the whole difficulty — see :func:`seed_cmems_s3`.
CMEMS_ENDPOINT = "https://s3.waw3-1.cloudferro.com"

#: Recorded objects, one directory per bucket, kept as files rather than
#: inside the cassette: they are binary, and base64 in YAML costs a third
#: again in size. `mdl-metadata` holds the catalog JSON, `mdl-arco-time-*`
#: the zarr store.
CMEMS_STORE = Path(__file__).parent / "s3_store"


@pytest.fixture
def seed_cmems_s3(monkeypatch: pytest.MonkeyPatch, s3_emulator):
    """Serve every recorded CloudFerro object from the in-process S3.

    `moto` matches by host and only knows AWS's own S3 endpoints by default,
    so a client pointed at CloudFerro sails straight past it to the real
    service. `MOTO_S3_CUSTOM_ENDPOINTS` is moto's own hook for that, and it
    is read per request, so setting it here works even though moto has
    already been imported by the time the fixture runs.

    Registering the host captures *all* traffic to it, plain `requests`
    included — which is why the catalog JSON is seeded here too rather than
    left in the cassette: it lives on the same host as the data.

    Returns:
        The seeding client, so a test can add to or inspect the store.
    """
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", CMEMS_ENDPOINT)
    seeded = 0
    for bucket_dir in sorted(p for p in CMEMS_STORE.iterdir() if p.is_dir()):
        objects = {
            path.relative_to(bucket_dir).as_posix(): path.read_bytes()
            for path in sorted(bucket_dir.rglob("*"))
            if path.is_file()
        }
        seed_s3(s3_emulator, bucket_dir.name, objects)
        seeded += len(objects)
    assert seeded, f"no recorded objects under {CMEMS_STORE}"
    return s3_emulator
