"""Live end-to-end test for the NWM backend.

Hits the real, public, unsigned `noaa-nwm-pds` bucket — no credentials
needed — so it is gated only on the `e2e` marker and network
reachability. A default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m "e2e and nwm" tests/nwm
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.nwm import NWM

pytestmark = [pytest.mark.e2e, pytest.mark.nwm]


def _network_available() -> bool:
    """Return whether the public NWM bucket is reachable (unsigned)."""
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config

        client = boto3.client(
            "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
        )
        client.list_objects_v2(Bucket="noaa-nwm-pds", MaxKeys=1, Delimiter="/")
        return True
    except Exception:
        return False


# A recently completed cycle date: a few days back is safely published and
# well inside the rolling operational retention window.
_PROBE_DATE = (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d")


@pytest.mark.skipif(not _network_available(), reason="noaa-nwm-pds unreachable")
def test_download_one_short_range_channel_rt(tmp_path):
    """Fetch one recent short_range channel_rt file and confirm it lands."""
    nwm = NWM(
        start=_PROBE_DATE,
        end=_PROBE_DATE,
        variables={"chrtout": ["streamflow"]},
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        configuration="short_range",
        cycles=[0],
        steps=[1],
        path=str(tmp_path),
    )
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 1
    written = Path(paths[0])
    assert written.exists()
    # channel_rt is ~14 MB (all ~2.7M reaches at one timestep); assert it is
    # a real file, not a truncated error body.
    assert written.stat().st_size > 1_000_000
    assert written.name.endswith(".conus.nc")


@pytest.mark.skipif(not _network_available(), reason="noaa-nwm-pds unreachable")
def test_retrospective_streamflow_subset(tmp_path):
    """Subset the retrospective chrtout Zarr to a few reaches and a short window."""
    import pandas as pd

    nwm = NWM(
        start="2010-06-01",
        end="2010-06-02",
        variables={"chrtout": ["streamflow"]},
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        configuration="analysis_assim",
        mode="retrospective",
        sites=[101, 179, 181],
        path=str(tmp_path),
    )
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 1
    table = Path(paths[0])
    assert table.exists() and table.suffix == ".parquet"
    frame = pd.read_parquet(table)
    # the tidy feature_id x time table carries the requested reaches + variable
    assert "streamflow" in frame.columns
    assert set(frame["feature_id"].unique()) == {101, 179, 181}
