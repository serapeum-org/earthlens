"""Unit tests for the Sentinel Hub Batch Processing plane → S3 (C8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub

_S3 = {"bucket": "s3://bkt/out", "iam_role_arn": "arn:aws:iam::1:role/r", "grid_id": 2}


def _batch_backend(output_dir, **kwargs) -> SentinelHub:
    """A backend forced onto the Batch plane."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables={"sentinel-2-l2a-ndvi": []},
        lat_lim=[10.0, 40.0],
        lon_lim=[0.0, 30.0],
        path=output_dir,
        resolution=10,
        api="batch",
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestBatchFetch:
    """The Batch plane runs create → analyse → start → monitor and returns URIs."""

    def test_requires_batch_output(self, fake_sh, output_dir: Path):
        """Forcing api='batch' without batch_output raises a clear error."""
        with pytest.raises(ValueError, match="tiles server-side to S3"):
            _batch_backend(output_dir).download()

    def test_job_lifecycle(self, fake_sh, output_dir: Path):
        """A batch download drives the full create/analyse/start/monitor sequence."""
        uris = _batch_backend(output_dir, batch_output=_S3).download()
        assert uris == ["s3://bkt/out"]
        client = fake_sh.BatchProcessClient.instances[-1]
        assert client.calls == ["create", "start_analysis", "start_job", "monitor"]

    def test_tiling_grid_forwarded(self, fake_sh, output_dir: Path):
        """The configured grid_id + resolution reach the tiling-grid input."""
        _batch_backend(output_dir, batch_output=_S3).download()
        client = fake_sh.BatchProcessClient.instances[-1]
        tiling = client.created["input"]
        assert tiling["grid_id"] == 2
        assert tiling["resolution"] == 10
        delivery = client.created["output"]["delivery"]
        assert delivery["url"] == "s3://bkt/out"
        assert delivery["iam_role_arn"] == "arn:aws:iam::1:role/r"

    def test_auto_routes_to_batch_with_s3(self, fake_sh, output_dir: Path):
        """A huge AOI with an S3 bucket auto-routes to Batch."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[10.0, 40.0],
            lon_lim=[0.0, 30.0],
            path=output_dir,
            resolution=2,
            batch_output=_S3,
            client_id="a",
            client_secret="b",
        )
        assert backend._resolve_plane() == "batch"
