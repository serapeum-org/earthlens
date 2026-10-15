"""The real WorldPop exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what WorldPop actually sends back, so a
change in that shape fails here rather than in the weekly live lane.

Monaco, not the Comoros extent the e2e test uses: WorldPop's unit of download
is one country GeoTIFF, so the AOI picks the payload size directly, and the
cassette has to live in the repository for ever. `com_ppp_2020.tif` is 2.3 MB;
`mco_ppp_2020.tif` is 2,803 bytes, and both exercise the identical path —
REST lookup, href resolution, GeoTIFF fetch, crop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens

#: Monaco, from the backend's own `iso3_bbox.yaml`.
_ISO3 = "MCO"
_LON_LIM = [7.378, 7.439]
_LAT_LIM = [43.732, 43.771]


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path):
    """A recorded country pop request replays into a cropped GeoTIFF."""
    result = EarthLens(
        data_source="worldpop",
        variables=["pop"],
        start="2020",
        end="2020",
        fmt="%Y",
        lat_lim=_LAT_LIM,
        lon_lim=_LON_LIM,
        aoi=_ISO3,
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result, "a file-writing backend returns the paths it wrote"
    tifs = [Path(p) for p in result if str(p).endswith(".tif")]
    assert len(tifs) == 1, f"expected one GeoTIFF, got {result}"
    assert tifs[0].exists(), "the written path exists"
