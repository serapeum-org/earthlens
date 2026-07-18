"""Live end-to-end tests for the Solar & Wind Atlas backend.

Hits the real, public atlases — the Global Wind Atlas COGs on figshare (read
windowed over `/vsicurl`) and the Global Solar Atlas ZIP archives on
`api.globalsolaratlas.info` (downloaded once, then cropped locally). No
credentials are needed (both are CC-BY-4.0). Gated behind the `e2e` marker plus
network availability; a default `pytest` run skips them.

The Global Wind Atlas case is fast (only the bbox window transfers). The Global
Solar Atlas case is marked `slow` because it downloads the full ~2.7 GB global
archive once.

Run with:

    pixi run -e dev pytest -m "e2e and solar_wind_atlas" tests/solar_wind_atlas
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.solar_wind_atlas]

#: A tiny onshore AOI near DTU (Denmark) — small enough to fetch in seconds and
#: reliably windy / sunlit so the values come back in a plausible range.
_LAT_LIM = [55.0, 55.5]
_LON_LIM = [12.0, 12.5]


def _reachable(url: str) -> bool:
    """Return whether `url` answers a quick range GET (handles 2xx/3xx)."""
    try:
        request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status in (200, 206)
    except (urllib.error.URLError, OSError):
        return False


def _band_stats(path: Path) -> tuple[float, float]:
    """Return the `(min, max)` finite value of a written GeoTIFF."""
    from pyramids.dataset import Dataset

    dataset = Dataset.read_file(str(path))
    array = np.asarray(dataset.read_array(), dtype="float64")
    finite = array[np.isfinite(array)]
    return float(finite.min()), float(finite.max())


class TestWindAtlasLiveFetch:
    """Live Global Wind Atlas windowed `/vsicurl` read (open figshare COG)."""

    @pytest.fixture(autouse=True)
    def _skip_when_offline(self) -> None:
        """Skip cleanly when the figshare download host is unreachable."""
        if not _reachable("https://ndownloader.figshare.com/files/17247017"):
            pytest.skip("figshare unreachable (offline)")

    def test_wind_100m_windowed_subset(self, tmp_path: Path) -> None:
        """A small wind_100m window lands one GeoTIFF of plausible wind speeds."""
        paths = EarthLens(
            data_source="solar-wind-atlas",
            variables=["wind_100m"],
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert paths[0].suffix == ".tif" and paths[0].exists()
        low, high = _band_stats(paths[0])
        assert 0.0 <= low <= high < 50.0, f"implausible wind speed: {low}..{high}"


@pytest.mark.slow
class TestSolarAtlasLiveFetch:
    """Live Global Solar Atlas download-then-crop (one-time ~2.7 GB fetch)."""

    @pytest.fixture(autouse=True)
    def _skip_when_offline(self) -> None:
        """Skip cleanly when the Global Solar Atlas host is unreachable."""
        url = (
            "https://api.globalsolaratlas.info/download/World/"
            "World_GHI_GISdata_LTAy_AvgDailyTotals_GlobalSolarAtlas-v2_GEOTIFF.zip"
        )
        if not _reachable(url):
            pytest.skip("Global Solar Atlas host unreachable (offline)")

    def test_ghi_subset_after_full_download(self, tmp_path: Path) -> None:
        """A small GHI window lands one GeoTIFF of plausible irradiation."""
        paths = EarthLens(
            data_source="solar-wind-atlas",
            variables=["ghi"],
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert paths[0].suffix == ".tif" and paths[0].exists()
        low, high = _band_stats(paths[0])
        assert 0.0 <= low <= high < 12.0, f"implausible GHI: {low}..{high}"
