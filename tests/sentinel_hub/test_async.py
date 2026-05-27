"""Unit tests for the Sentinel Hub Async Processing plane (C5, S3-delivered)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub

_S3 = {"bucket": "s3://my-bucket/out", "iam_role_arn": "arn:aws:iam::1:role/r"}


def _medium_backend(output_dir, **kwargs) -> SentinelHub:
    """A backend whose bbox renders to ~5000 px/side (medium AOI)."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables={"sentinel-2-l2a-ndvi": []},
        lat_lim=[40.0, 40.5],
        lon_lim=[14.0, 14.5],
        path=output_dir,
        resolution=10,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestAsyncRouting:
    """Medium AOIs route to async (with S3) or tiling (without)."""

    def test_medium_without_s3_routes_to_tiling(self, fake_sh, output_dir: Path):
        """A medium AOI with no S3 bucket falls back to local tiling."""
        assert _medium_backend(output_dir)._resolve_plane() == "tiling"

    def test_medium_with_s3_routes_to_async(self, fake_sh, output_dir: Path):
        """A medium AOI with an S3 bucket routes to async."""
        backend = _medium_backend(output_dir, batch_output=_S3)
        assert backend._resolve_plane() == "async"


class TestAsyncFetch:
    """The async plane submits S3-delivered jobs and returns the URIs."""

    def test_requires_s3(self, fake_sh, output_dir: Path):
        """Forcing api='async' without batch_output raises a clear error."""
        backend = _medium_backend(output_dir, api="async")
        with pytest.raises(ValueError, match="deliver server-side to S3"):
            backend.download()

    def test_submits_and_returns_uris(self, fake_sh, output_dir: Path):
        """An async download submits the job and returns the S3 URI."""
        backend = _medium_backend(output_dir, api="async", batch_output=_S3)
        uris = backend.download()
        assert uris == ["s3://my-bucket/out/async-result.tiff"]
        req = fake_sh.AsyncProcessRequest.instances[-1]
        assert req.submitted is True
        assert req.delivery["url"] == "s3://my-bucket/out"

    def test_size_guard(self, fake_sh, output_dir: Path):
        """An async render above 10000 px/side is rejected."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[10.0, 40.0],
            lon_lim=[0.0, 30.0],
            path=output_dir,
            resolution=10,
            api="async",
            batch_output=_S3,
            client_id="a",
            client_secret="b",
        )
        with pytest.raises(ValueError, match="Async Processing API"):
            backend.download()
