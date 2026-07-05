"""Live end-to-end tests for the GOES ABI backend.

Hits the real, public, unsigned `noaa-goes19` bucket — no credentials
needed — so it is gated only on the `e2e` marker and network
reachability. A default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m "e2e and goes" tests/goes
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.goes import GOES

pytestmark = [pytest.mark.e2e, pytest.mark.goes]


def _network_available() -> bool:
    """Return whether the public GOES bucket is reachable (unsigned)."""
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config

        client = boto3.client(
            "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
        )
        client.list_objects_v2(Bucket="noaa-goes19", MaxKeys=1, Delimiter="/")
        return True
    except Exception:
        return False


def _recent_window(minutes: int = 6) -> tuple[str, str]:
    """Return a `(start, end)` window a few hours back (safely published)."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    base = (now - dt.timedelta(hours=3)).replace(second=0, microsecond=0)
    end = base + dt.timedelta(minutes=minutes)
    return base.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")


@pytest.mark.skipif(not _network_available(), reason="noaa-goes19 unreachable")
def test_download_one_recent_mcmipc_granule(tmp_path):
    """Fetch a recent CONUS MCMIP granule from noaa-goes19 and confirm it lands."""
    start, end = _recent_window()
    goes = GOES(
        start=start,
        end=end,
        lat_lim=[20.0, 50.0],
        lon_lim=[-130.0, -60.0],
        dataset="abi-l2-mcmip",
        satellite="east",
        domain="C",
        fmt="%Y-%m-%d %H:%M",
        path=str(tmp_path),
    )
    paths = goes.download(progress_bar=False)
    assert len(paths) >= 1, "at least one CONUS granule in a 6-min window"
    written = Path(paths[0])
    assert written.exists(), "the granule was written to disk"
    assert written.name.startswith("OR_ABI-L2-MCMIPC-M6_G19_s"), "ABI CONUS G19 name"
    # A CONUS MCMIP granule is ~58 MB; assert it is a real file, not an error body.
    assert written.stat().st_size > 1_000_000, "a real granule, not a truncated body"


@pytest.mark.skipif(not _network_available(), reason="noaa-goes19 unreachable")
def test_channel_filter_fetches_only_requested_band(tmp_path):
    """A band-split radiance request fetches only the requested ABI channel."""
    start, end = _recent_window(minutes=6)
    goes = GOES(
        start=start,
        end=end,
        lat_lim=[20.0, 50.0],
        lon_lim=[-130.0, -60.0],
        dataset="abi-l1b-rad",
        variables=["C13"],
        satellite="east",
        domain="C",
        fmt="%Y-%m-%d %H:%M",
        path=str(tmp_path),
    )
    paths = goes.download(progress_bar=False)
    assert len(paths) >= 1, "at least one C13 radiance granule in the window"
    assert all("C13_G19" in Path(p).name for p in paths), "only the C13 channel fetched"
