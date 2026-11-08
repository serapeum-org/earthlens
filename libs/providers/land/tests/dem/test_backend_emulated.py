"""DEM against an in-process S3, rather than against a hand-written client.

The worked example for the emulator tier. `conftest.FakeS3Client` approximates
`head_object` / `download_file`; these drive the backend through botocore to a
real S3 implementation, so the things an approximation gets to decide for
itself — the `ClientError` shape for a missing key, what `download_file`
actually writes, how a prefix listing paginates — come from S3's own semantics.

Nothing is injected: `moto.mock_aws()` intercepts inside botocore, so the
unsigned client the backend builds for itself resolves to the seeded bucket.

The older fake-backed tests in `test_backend.py` still stand; this is the
pattern they migrate onto, one backend at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.testing import seed_s3

pytest.importorskip("boto3")
pytest.importorskip("moto", reason="the moto dev dependency is required")

from earthlens.dem import DEM  # noqa: E402 - after the SDK guards above

pytestmark = [pytest.mark.dem, pytest.mark.integration]

#: A one-degree tile over land, in the GLO-30 naming the backend builds.
_LAT_LIM = [45.0, 45.4]
_LON_LIM = [7.0, 7.4]

#: The Copernicus DEM buckets live in eu-central-1, and S3 is region-aware —
#: a bucket created elsewhere answers this client with a redirect, not the
#: object. Seeding in the dataset's own region is part of what makes the
#: emulator a faithful stand-in; the hand-written fake has no regions at all.
_REGION = "eu-central-1"


def _backend(tmp_path: Path) -> DEM:
    """Build a GLO-30 DEM backend writing under `tmp_path`."""
    return DEM(
        dataset="cop-dem-glo-30",
        lat_lim=list(_LAT_LIM),
        lon_lim=list(_LON_LIM),
        path=str(tmp_path),
    )


def test_present_tile_downloads_from_the_emulated_bucket(
    tmp_path: Path, s3_emulator
) -> None:
    """A seeded key is found and written, through the real botocore path."""
    source = _backend(tmp_path)
    products = source._search()
    assert products, "the backend should derive at least one tile for this bbox"

    bucket = products[0].metadata["bucket"]
    payload = b"\x49\x49\x2a\x00fake-cog-body"
    seed_s3(
        s3_emulator,
        bucket,
        {product.href: payload for product in products},
        region=_REGION,
    )

    written = source.download(progress_bar=False)
    assert written, "a seeded tile should be downloaded"
    first = Path(written[0])
    assert first.exists(), first
    # The emulator serves exactly what was put, so the bytes round-trip —
    # a hand-written `download_file` chooses its own placeholder instead.
    assert first.read_bytes() == payload


def test_absent_tile_is_skipped_on_a_real_404(tmp_path: Path, s3_emulator) -> None:
    """An empty bucket yields S3's own 404, and the backend skips the tile.

    The hand-written fake raises `ClientError` with `Error.Code == "404"`; the
    real service answers `HeadObject` with `404` and botocore surfaces it as
    `Error.Code == "404"` only for HEAD. Letting S3 decide is the point.
    """
    source = _backend(tmp_path)
    products = source._search()
    bucket = products[0].metadata["bucket"]
    seed_s3(s3_emulator, bucket, {}, region=_REGION)  # bucket exists, tile does not

    written = source.download(progress_bar=False)
    assert written == [], "an absent tile should be skipped, not raised"


def test_a_missing_bucket_is_not_mistaken_for_a_missing_tile(
    tmp_path: Path, s3_emulator
) -> None:
    """A bucket that does not exist raises, and raises `403` rather than `404`.

    The backend treats `404` as "this tile is not in the dataset" and skips
        it, so a mistyped or retired bucket must *not* arrive as `404` — otherwise
        every tile is silently skipped and the download reports success having
        downloaded nothing. S3 answers `NoSuchBucket` to a caller it recognises
        (and `403` to an anonymous one, since it hides existence from a caller who
        may not read it); neither is `404`, which is the property that matters.

        The hand-written fake cannot express any of this: it has no concept of a
        bucket, so every error it can produce is the `404` the backend swallows.
    """
    from botocore.exceptions import ClientError

    source = _backend(tmp_path)
    # Deliberately seed nothing, so the bucket itself is absent.
    with pytest.raises(ClientError) as excinfo:
        source.download(progress_bar=False)
    code = excinfo.value.response["Error"]["Code"]
    assert code in {"NoSuchBucket", "403"}, excinfo.value.response
    assert code != "404", "a missing bucket must not look like a missing tile"
