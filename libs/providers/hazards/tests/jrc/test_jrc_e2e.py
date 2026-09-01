"""Live end-to-end test for the JRC hazard backend (EFHM + sea-level).

Hits the real, public JRC CEMS-EFAS flood-hazard HTTPS directory (no
credentials — the EFHM is CC-BY-4.0), reading only the AOI's pixel window over
GDAL's `/vsicurl`, so it is fast and small. Gated behind the `e2e` marker plus
network availability; a default `pytest` run skips it.

Run with:

    pytest -m "e2e and jrc" libs/providers/hazards/tests/jrc
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.jrc]

#: A tiny Rhine-delta AOI (Netherlands) — inside the EFHM coverage, small
#: enough to window-read in seconds.
_LAT_LIM = [51.8, 52.0]
_LON_LIM = [4.8, 5.0]


#: The two product trees the e2e tests read from; both must answer.
_PROBE_URLS = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard/",
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/FLOODS/sea_level_forecasts/"
    "probabilistic_data_driven/",
)


def _jrc_reachable() -> bool:
    """Return whether both JRC product trees answer a quick request."""
    for url in _PROBE_URLS:
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    return False
        except (urllib.error.URLError, OSError):
            return False
    return True


@pytest.fixture(autouse=True)
def _skip_when_offline() -> None:
    """Skip cleanly when the JRC host is unreachable."""
    if not _jrc_reachable():
        pytest.skip("JRC jeodpp unreachable (offline)")


class TestEfhmLiveFetch:
    """Live EFHM windowed reads (open HTTPS — no credentials)."""

    def test_rp100_writes_depth_geotiff(self, tmp_path: Path):
        """A small RP100 pull lands one GeoTIFF of plausible water depths."""
        paths = EarthLens(
            data_source="jrc-flood",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            return_periods=[100],
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert paths == [tmp_path / "efhm_RP100.tif"], f"unexpected outputs: {paths}"
        assert paths[0].exists()
        from pyramids.dataset import Dataset

        from earthlens.base import close_quietly

        dataset = Dataset.read_file(str(paths[0]))
        array = dataset.read_array()
        close_quietly(dataset)
        valid = array[array > -9999]
        assert valid.size > 0, "the AOI window carried no valid depth cells"
        assert 0 <= float(np.nanmax(valid)) < 100, "implausible flood depth"

    def test_multiple_return_periods(self, tmp_path: Path):
        """Two return periods each land their own GeoTIFF."""
        paths = EarthLens(
            data_source="efhm",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            return_periods=[10, 500],
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert [p.name for p in paths] == ["efhm_RP10.tif", "efhm_RP500.tif"]


class TestSeaLevelLiveFetch:
    """Live sea-level TWL forecast reads (open HTTPS — no credentials)."""

    def test_medium_term_gridded_writes_geotiff(self, tmp_path: Path):
        """A latest-cycle North Sea pull lands one georeferenced multi-band GeoTIFF."""
        paths = EarthLens(
            data_source="jrc:sea-level-forecast",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one output, got {paths}"
        assert paths[0].exists(), f"{paths[0]} was not written"
        from pyramids.dataset import Dataset

        from earthlens.base import close_quietly

        dataset = Dataset.read_file(str(paths[0]))
        band_count = dataset.band_count
        epsg = dataset.epsg
        origin_x, cell, _, origin_y, _, _ = dataset.geotransform
        array = np.asarray(dataset.read_array())
        close_quietly(dataset)

        assert band_count >= 1, "expected one band per forecast step"
        assert epsg == 4326
        assert cell == pytest.approx(0.25), (
            "geotransform must be degrees, not index space"
        )
        assert np.isfinite(array).sum() > 0, "the AOI window carried no valid TWL cells"
        # Ground truth, not the code's own constant: the origin must be the NW
        # corner actually requested, and the field must be oriented north-up. A
        # N/S flip or a 180-degree longitude shift would still satisfy the
        # cell-size and CRS assertions above, so pin the origin explicitly.
        assert origin_x == pytest.approx(3.0, abs=0.25), (
            f"west edge should be ~3E, got {origin_x}"
        )
        assert origin_y == pytest.approx(53.0, abs=0.25), (
            f"north edge should be ~53N, got {origin_y}"
        )
        # This AOI straddles the Dutch coast: a north-up read has both land
        # (NaN) and sea (finite) cells. A vertical flip lands in open ocean and
        # loses the NaNs entirely.
        band = array[0] if array.ndim == 3 else array
        assert np.isnan(band).any(), "expected land cells (NaN) in a coastal AOI"
        assert np.isfinite(band).any(), "expected sea cells (finite) in a coastal AOI"

    def test_subseasonal_coastal_returns_dataframe(self):
        """The coastal key returns the global per-country summary as a DataFrame."""
        import pandas as pd

        result = EarthLens(data_source="jrc:coastal-forecast").download(
            progress_bar=False
        )
        assert isinstance(result, pd.DataFrame)
        assert "GID_0" in result.columns, f"missing GID_0: {list(result.columns)[:5]}"
        assert len(result) > 0, "the coastal summary came back empty"
