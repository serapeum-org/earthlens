"""Unit tests for the Sentinel Hub Batch Processing plane → S3 (C8).

These verify the **orchestration shape** against the faked SDK — the
create → analyse → (cost guard) → start → monitor sequence and the tiling/S3
output wiring. They are **not** end-to-end behavioural proof: Batch delivers to a
user S3 bucket + IAM role and is not run against the live service.
"""

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

    def test_max_cost_pu_guard(self, fake_sh, output_dir: Path):
        """An analysed cost_PU above batch_output['max_cost_pu'] aborts before start_job."""
        # the fake reports cost_PU=5.0; cap below that to trip the guard
        backend = _batch_backend(output_dir, batch_output={**_S3, "max_cost_pu": 1.0})
        with pytest.raises(ValueError, match="exceeds batch_output"):
            backend.download()
        client = fake_sh.BatchProcessClient.instances[-1]
        assert "start_job" not in client.calls  # aborted after analysis

    def test_under_cost_cap_runs(self, fake_sh, output_dir: Path):
        """A cost cap above the estimate lets the job proceed."""
        backend = _batch_backend(output_dir, batch_output={**_S3, "max_cost_pu": 100.0})
        assert backend.download() == ["s3://bkt/out"]

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
