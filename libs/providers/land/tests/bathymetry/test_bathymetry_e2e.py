"""Live end-to-end tests for the bathymetry backend.

Hits the real, public NOAA ERDDAP `griddap` services (NOAA CoastWatch for
GEBCO 2020, NOAA upwell for ETOPO1), so these tests are gated behind the
`e2e` pytest marker plus network availability — no credentials are needed
(both DEMs are open). A default `pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m "e2e and bathymetry" tests/bathymetry
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.bathymetry]

#: A tiny deep-ocean AOI off the NW African coast — small enough to fetch in
#: seconds and reliably below sea level (so elevations come back negative).
_LAT_LIM = [25.0, 26.0]
_LON_LIM = [-18.0, -17.0]


def _erddap_reachable() -> bool:
    """Return whether the NOAA CoastWatch ERDDAP answers a quick GET."""
    try:
        with urllib.request.urlopen(
            "https://coastwatch.pfeg.noaa.gov/erddap/", timeout=10
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(autouse=True)
def _skip_when_offline() -> None:
    """Skip the live tests cleanly when the ERDDAP host is unreachable."""
    if not _erddap_reachable():
        pytest.skip("NOAA ERDDAP unreachable (offline)")


def _elevation_stats(path: Path) -> tuple[float, float]:
    """Return the `(min, max)` finite elevation of a written GeoTIFF."""
    from pyramids.dataset import Dataset

    dataset = Dataset.read_file(str(path))
    array = dataset.read_array()
    finite = array[np.isfinite(array)]
    return float(finite.min()), float(finite.max())


class TestBathymetryLiveFetch:
    """Live DEM subsets (open ERDDAP griddap — no credentials)."""

    def test_gebco_subset_writes_ocean_geotiff(self, tmp_path: Path):
        """A small GEBCO 2020 pull lands one GeoTIFF of plausible ocean depths."""
        paths = EarthLens(
            data_source="bathymetry",
            dataset="gebco_2020",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert paths[0].suffix == ".tif" and paths[0].exists()
        low, high = _elevation_stats(paths[0])
        assert -11000 < low < high < 9000, f"implausible elevations: {low}..{high}"
        assert low < 0, "a deep-ocean AOI must carry sub-sea-level depths"

    @pytest.mark.parametrize("dataset", ["etopo1_ice", "etopo1_bedrock"])
    def test_etopo_variants_write_geotiff(self, tmp_path: Path, dataset: str):
        """ETOPO ice and bedrock pulls each land a plausible-elevation GeoTIFF."""
        paths = EarthLens(
            data_source="etopo",
            dataset=dataset,
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        low, high = _elevation_stats(paths[0])
        assert -11000 < low < high < 9000, f"implausible elevations: {low}..{high}"
