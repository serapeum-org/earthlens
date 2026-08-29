"""Live end-to-end test for the JRC European flood-hazard backend.

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


def _jrc_reachable() -> bool:
    """Return whether the JRC jeodpp host answers a quick request."""
    try:
        request = urllib.request.Request(
            "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard/",
            method="HEAD",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


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
