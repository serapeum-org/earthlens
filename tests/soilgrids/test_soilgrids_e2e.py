"""Live end-to-end test for the SoilGrids backend.

Hits the real, public ISRIC SoilGrids WCS at `maps.isric.org` (no credentials —
CC-BY 4.0), fetching a tiny bbox subset of one coverage and confirming pyramids
opens the written GeoTIFF with plausible scaled-integer values. Gated behind the
`e2e` marker plus network availability; a default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m "e2e and soilgrids" tests/soilgrids
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.soilgrids]

#: A tiny onshore AOI over the Netherlands — small enough to fetch in seconds and
#: entirely land, so the pH window comes back fully valued.
_LAT_LIM = [51.0, 51.5]
_LON_LIM = [5.0, 5.5]

#: The SoilGrids no-data sentinel for the int16 property grids.
_NO_DATA = -32768


def _reachable(url: str) -> bool:
    """Return whether `url` answers a quick GET (handles 2xx/3xx)."""
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status in (200, 206)
    except (urllib.error.URLError, OSError):
        return False


def _valid_stats(path: Path) -> tuple[float, float]:
    """Return the `(min, max)` of a GeoTIFF, excluding the no-data sentinel."""
    from pyramids.dataset import Dataset

    dataset = Dataset.read_file(str(path))
    array = np.asarray(dataset.read_array(), dtype="float64")
    valid = array[np.isfinite(array) & (array != _NO_DATA)]
    return float(valid.min()), float(valid.max())


class TestSoilGridsLiveFetch:
    """Live SoilGrids WCS subset (open MapServer coverage, no auth)."""

    @pytest.fixture(autouse=True)
    def _skip_when_offline(self) -> None:
        """Skip cleanly when the SoilGrids WCS host is unreachable."""
        url = (
            "https://maps.isric.org/mapserv?map=/map/phh2o.map"
            "&SERVICE=WCS&VERSION=2.0.0&REQUEST=GetCapabilities"
        )
        if not _reachable(url):
            pytest.skip("maps.isric.org unreachable (offline)")

    def test_phh2o_topsoil_subset(self, tmp_path: Path) -> None:
        """A small phh2o 0-5cm mean window lands one GeoTIFF of plausible pH."""
        paths = EarthLens(
            data_source="soilgrids",
            variables=["phh2o"],
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            depths=["0-5cm"],
            quantiles=["mean"],
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert paths[0].name == "phh2o_0-5cm_mean.tif"
        assert paths[0].suffix == ".tif" and paths[0].exists()
        low, high = _valid_stats(paths[0])
        # Values are pH x10 scaled integers, so a physical pH 2..11 -> 20..110.
        assert 20.0 <= low <= high <= 120.0, f"implausible scaled pH: {low}..{high}"
