"""Unit tests for the Sentinel Hub Batch Statistical plane (C9).

These verify the **orchestration shape** against the faked SDK — the
create → analyse → start → monitor → retrieve sequence, the S3 input/output
wiring, and the per-feature flatten. They are **not** end-to-end behavioural
proof: Batch-Statistical runs over a user S3 GeoPackage + IAM role and is not run
against the live service.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub

_BATCH = {
    "input_features": "s3://bkt/farms.gpkg",
    "bucket": "s3://bkt/out",
    "iam_role_arn": "arn:aws:iam::1:role/r",
    "feature_ids": ["farm-1", "farm-2", "farm-3"],
}


def _bstat_backend(output_dir, batch_output=_BATCH, **kwargs) -> SentinelHub:
    """A Batch Statistical backend."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables={"sentinel-2-l2a-ndvi-stats": []},
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        api="batch-statistical",
        batch_output=batch_output,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestBatchStatistical:
    """Batch Statistical runs the async lifecycle and concatenates per-feature JSON."""

    def test_requires_batch_output(self, fake_sh, output_dir: Path):
        """Without batch_output the plane raises a clear error."""
        with pytest.raises(ValueError, match="FeatureCollection on S3"):
            _bstat_backend(output_dir, batch_output=None).download()

    def test_requires_input_features(self, fake_sh, output_dir: Path):
        """Without input_features the plane raises a clear error."""
        with pytest.raises(ValueError, match="input_features"):
            _bstat_backend(output_dir, batch_output={"bucket": "s3://b/o"}).download()

    def test_lifecycle_and_table(self, fake_sh, output_dir: Path):
        """The plane runs create/analyse/start and writes one row per feature."""
        paths = _bstat_backend(output_dir).download()
        assert len(paths) == 1 and paths[0].suffix == ".csv"
        client = fake_sh.SentinelHubBatchStatistical.instances[-1]
        assert client.calls == ["create", "start_analysis", "start_job"]
        df = pd.read_csv(paths[0])
        assert set(df["feature_id"]) == {"farm-1", "farm-2", "farm-3"}
        assert df.iloc[0]["mean"] == 0.5

    def test_s3_specs_wired(self, fake_sh, output_dir: Path):
        """The input-features + output S3 specs are wired into create()."""
        _bstat_backend(output_dir).download()
        client = fake_sh.SentinelHubBatchStatistical.instances[-1]
        assert client.created["input_features"]["url"] == "s3://bkt/farms.gpkg"
        assert client.created["output"]["url"] == "s3://bkt/out"
