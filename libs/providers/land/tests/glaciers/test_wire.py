"""The real RGI exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what the RGI mirror actually sends back, so
a change in that shape fails here rather than in the weekly live lane.

Novaya Zemlya, not the Alps extent the e2e test uses. RGI's unit of download
is a whole GTN-G region ZIP and the backend fetches *every* region whose box
the AOI touches, so an AOI's cost is set by how many overlapping boxes it
lands in, not by its own size: the Alps sit in region 11, and an Alps AOI
pulls 58 MB. Region 09 is the cheapest region that an AOI can select on its
own (3.6 MB) — region 06, smaller at 2.2 MB, is unreachable alone because its
box lies entirely inside Greenland Periphery's.
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

#: A point box inside GTN-G region 09 only, verified against the backend's own
#: `regions_for_bbox`.
_LAT_LIM = [75.95, 76.05]
_LON_LIM = [45.95, 46.05]


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path):
    """A recorded single-region request replays into glacier outlines."""
    fc = EarthLens(
        data_source="glaciers",
        variables=["rgi:outlines"],
        lat_lim=_LAT_LIM,
        lon_lim=_LON_LIM,
        path=str(tmp_path),
    ).download()

    assert str(fc.crs).upper() == "EPSG:4326"
    assert "rgi_id" in fc.columns
