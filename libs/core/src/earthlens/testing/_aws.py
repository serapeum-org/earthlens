"""An in-process S3, so a bucket backend is tested against S3's own semantics.

Six backends read unsigned public buckets (goes, nwm, dem, radar, s3, caravan),
and each grew its own `FakeS3Client` — a hand-written `head_object` /
`list_objects_v2` / `get_object` / `download_file` that approximates what
botocore does. The approximations drift from the real thing in the places that
matter most: the exact `ClientError` shape for a missing key, the pagination
contract, `Delimiter` / `CommonPrefixes` behaviour, and the streaming body.

`moto` runs a real S3 implementation in-process instead, so the backend code
under test takes its normal path through botocore and the test asserts against
S3's semantics rather than against a fake's. The sibling `unicloud` repo
already tests its S3 layer this way.

Nothing here patches an earthlens seam: `mock_aws()` intercepts inside
botocore, so every client a backend builds — unsigned ones included — resolves
to the in-process bucket.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

#: Credentials that stop botocore's resolution chain at the first step. Without
#: them a *signed* client walks on to the EC2 instance metadata service at
#: 169.254.169.254 — which is a real outbound connection from a unit lane, and
#: makes the test's behaviour depend on whether the machine running it happens
#: to have an AWS profile.
_DUMMY_CREDENTIALS: dict[str, str] = {
    # The `nosec B105` markers below are deliberate: these placeholders are
    # inert by design, and being inert is the entire point. They stop
    # botocore's credential chain at its first step so it never reaches the
    # metadata service. No account stands behind them, and the in-process
    # emulator never checks a signature.
    "AWS_ACCESS_KEY_ID": "testing",  # nosec B105
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105
    "AWS_SECURITY_TOKEN": "testing",  # nosec B105
    "AWS_SESSION_TOKEN": "testing",  # nosec B105
    "AWS_DEFAULT_REGION": "us-east-1",
    # Belt and braces: some botocore paths probe IMDS for the region or role
    # even when credentials are present.
    "AWS_EC2_METADATA_DISABLED": "true",
}


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install dummy AWS credentials for the duration of a test.

    Configures the SDK under test; it gates nothing (`-m` still decides what
    runs).

    Args:
        monkeypatch: pytest's patcher, so the environment is restored.
    """
    for name, value in _DUMMY_CREDENTIALS.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def s3_emulator(aws_credentials: None) -> Iterator[Any]:
    """Yield a `boto3` S3 client backed by an in-process S3.

    Every client built while this fixture is active — including the unsigned
    ones the public-bucket backends construct for themselves — resolves to the
    same in-process store, so a backend needs no injection seam.

    Yields:
        A `boto3` S3 client for seeding and asserting.

    Examples:
        ```python
        def test_backend_finds_the_granule(s3_emulator):
            seed_s3(s3_emulator, "noaa-goes19", {"ABI/OR_x.nc": b"payload"})
            ...
        ```
    """
    moto = pytest.importorskip("moto", reason="the moto dev dependency is required")
    import boto3

    with moto.mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


def seed_s3(
    client: Any,
    bucket: str,
    objects: Mapping[str, bytes],
    *,
    region: str = "us-east-1",
    public: bool = True,
) -> None:
    """Create `bucket` if needed and put every `key -> body` in `objects`.

    Args:
        client: The S3 client from :func:`s3_emulator`.
        bucket: Bucket name, created if it does not exist.
        objects: Mapping of key to body.
        region: Region for the bucket. `us-east-1` is special-cased by S3 —
            it rejects an explicit `LocationConstraint` — which is exactly the
            sort of detail a hand-written fake does not reproduce. S3 is
            region-aware, so a bucket seeded in the wrong region answers the
            backend's client with a redirect rather than the object.
        public: Attach an anonymous read policy, mirroring the public buckets
            these backends actually read. Default `True` because every
            earthlens bucket backend builds an *unsigned* client, and an
            unsigned request to a private bucket is a `403` — correct of S3,
            and nothing to do with the behaviour under test.

    Examples:
        ```python
        >>> from earthlens.testing import seed_s3
        >>> seed_s3.__name__
        'seed_s3'

        ```
    """
    from botocore.exceptions import ClientError

    try:
        if region == "us-east-1":
            client.create_bucket(Bucket=bucket)
        else:
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
    except ClientError as exc:  # pragma: no cover - only on a repeat seed
        if exc.response["Error"]["Code"] not in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            raise
    # `public-read` per object rather than a bucket policy: the backends
    # build *unsigned* clients, and an anonymous `HeadObject` is only
    # allowed by the object's own ACL. A bucket policy granting
    # `s3:GetObject` lets anonymous GET through but still refuses HEAD,
    # which would fail the search step before any download.
    extra = {"ACL": "public-read"} if public else {}
    for key, body in objects.items():
        client.put_object(Bucket=bucket, Key=key, Body=body, **extra)
