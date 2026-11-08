"""Live end-to-end test for the NEXRAD Level-II radar backend.

Hits the real, public, unsigned `unidata-nexrad-level2-chunks` bucket — no
credentials — so it is gated on the `e2e` marker and bucket reachability.
A default `pytest` run skips it.

radar was the one unsigned-bucket backend with no live test, while its
siblings `goes`, `nwm`, `dem` and `s3` all have one against the same
transport; the offline suite therefore pinned the chunk-assembly logic
against a fake client that could not notice a key-layout change upstream.

Run with:

    pytest -m "e2e and radar" libs/providers/atmosphere/tests/radar
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from geopandas import GeoDataFrame

from earthlens.earthlens import EarthLens
from earthlens.testing import skip_live_unavailable

pytestmark = [pytest.mark.e2e, pytest.mark.radar]

#: Oklahoma City — a high-traffic WSR-88D that is almost always scanning, and
#: whose volumes land in the chunk feed within a minute or two.
_SITE = "KTLX"
_LAT_LIM = [34.0, 36.5]
_LON_LIM = [-98.5, -96.5]


def _recent_window(minutes: int = 20) -> tuple[str, str]:
    """Return a `(start, end)` window far enough back to be fully published."""
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, second=0, microsecond=0)
    end = now - dt.timedelta(minutes=30)
    start = end - dt.timedelta(minutes=minutes)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


@pytest.mark.requires_host("unidata-nexrad-level2-chunks.s3.amazonaws.com")
def test_recent_volume_downloads_and_inventories(tmp_path: Path) -> None:
    """A recent KTLX window assembles at least one readable Level-II volume."""
    start, end = _recent_window()
    inventory = EarthLens(
        data_source="radar",
        start=start,
        end=end,
        variables={_SITE: ["reflectivity"]},
        lat_lim=_LAT_LIM,
        lon_lim=_LON_LIM,
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert isinstance(inventory, GeoDataFrame), (
        f"radar is OUTPUT_KIND='vector', so it returns an inventory frame, "
        f"got {type(inventory)}"
    )
    if inventory.empty:
        # The chunk feed is real-time and a site does go down for maintenance;
        # an empty window is an availability condition, not a regression, and
        # must be diagnosed as one so the lane is not reported as a config gap.
        skip_live_unavailable(f"{_SITE} published no volume in the 20-minute window")

    assert (inventory["station_id"] == _SITE).all(), inventory["station_id"].unique()
    written = [Path(p) for p in inventory["path"] if p]
    assert written, "an assembled volume should be written to disk"
    first = written[0]
    assert first.exists(), first
    # A Level-II volume opens with the 24-byte "AR2V000x.NNN" version header.
    assert first.read_bytes()[:4] == b"AR2V", "not a Level-II volume header"
    assert first.stat().st_size > 100_000, "a real volume, not a truncated body"
