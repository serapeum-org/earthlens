"""Live end-to-end test for the EMODnet Bathymetry WCS row.

Hits the real, public EMODnet Bathymetry OGC WCS (no credentials), so it is
gated behind the `e2e` + `bathymetry` pytest markers plus network availability.
A default `pytest` invocation skips it.

Run with:

    uv run --active pytest -m "e2e and bathymetry" libs/providers/land/tests/bathymetry
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.bathymetry]

#: The EMODnet Bathymetry OGC WCS endpoint.
_WCS_URL = "https://ows.emodnet-bathymetry.eu/wcs"

#: A tiny North Sea AOI well inside the EMODnet coverage — small enough to fetch
#: in seconds and reliably below sea level (so depths come back negative).
_LAT_LIM = [53.0, 54.0]
_LON_LIM = [2.0, 3.0]


def _wcs_reachable() -> bool:
    """Return whether the EMODnet WCS answers a quick GetCapabilities GET."""
    url = f"{_WCS_URL}?service=WCS&request=GetCapabilities&version=1.0.0"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(autouse=True)
def _skip_when_offline() -> None:
    """Skip the live test cleanly when the EMODnet WCS host is unreachable."""
    if not _wcs_reachable():
        pytest.skip("EMODnet Bathymetry WCS unreachable (offline)")


def _elevation_stats(path: Path) -> tuple[float, float]:
    """Return the `(min, max)` finite elevation of a written GeoTIFF."""
    from pyramids.dataset import Dataset

    dataset = Dataset.read_file(str(path))
    array = dataset.read_array()
    finite = array[np.isfinite(array)]
    return float(finite.min()), float(finite.max())


class TestEmodnetLiveFetch:
    """Live EMODnet DTM subset over OGC WCS (no credentials)."""

    def test_emodnet_subset_writes_ocean_geotiff(self, tmp_path: Path):
        """A small EMODnet pull lands one GeoTIFF of plausible North Sea depths."""
        paths = EarthLens(
            data_source="bathymetry",
            dataset="emodnet",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert paths[0].suffix == ".tif", f"expected a .tif, got {paths[0]}"
        assert paths[0].exists(), f"the GeoTIFF was not written: {paths[0]}"
        low, high = _elevation_stats(paths[0])
        assert -11000 < low <= high < 100, f"implausible depths: {low}..{high}"
        assert low < 0, "a North Sea AOI must carry sub-sea-level depths"
