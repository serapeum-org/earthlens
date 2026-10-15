"""The real GHSL exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what JRC actually sends back, so a change
in that shape fails here rather than in the weekly live lane.

The AOI is chosen for payload size, not for interest. GHSL's unit of download
is a fixed Mollweide tile, so the AOI cannot make the response smaller — only
pick a cheaper tile. `R2_C24` (Arctic Siberia) is the smallest of the 375
published `GHS_POP` 100 m tiles at 1.66 MB, against ~8 MB for a populated one;
the tabular products bottom out at 1.5 MB, so there is no cheaper path through
this backend. A tile that is mostly nodata exercises the same URL template,
zip streaming, member resolution and pyramids read as a populated one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens

#: A small box inside Mollweide tile `R2_C24`, verified against the backend's
#: own `tiles_for_bbox` to select that tile and no other.
_LAT_LIM = [62.4, 62.6]
_LON_LIM = [82.4, 82.6]


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path):
    """A recorded single-tile GHS_POP request replays into a GeoTIFF."""
    result = EarthLens(
        data_source="ghsl",
        variables=["GHS_POP"],
        start="2020-01-01",
        end="2020-12-31",
        lat_lim=_LAT_LIM,
        lon_lim=_LON_LIM,
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result, "a file-writing backend returns the paths it wrote"
    assert len(result) == 1, f"the AOI spans one tile, got {result}"
    assert Path(result[0]).exists(), "the written path exists"
