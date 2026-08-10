"""Live end-to-end test for the FABDEM backend.

Hits the real, public University of Bristol FABDEM V1-2 file tree (no
credentials — FABDEM is CC-BY-NC-SA 4.0). A 10-degree bundle is ~0.8 GB, so
this pull is genuinely large; it is gated behind the `e2e` **and** `slow`
markers plus network availability, and a default `pytest` run skips it.

Run with:

    pytest -m "e2e and fabdem" libs/providers/land/tests/fabdem
"""

from __future__ import annotations

import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.fabdem]

#: A tiny land AOI over the English Channel coast (SE England) — inside the
#: N50E000 bundle, reliably above sea level.
_LAT_LIM = [50.85, 50.95]
_LON_LIM = [0.05, 0.15]


def _bristol_reachable() -> bool:
    """Return whether the Bristol data host answers a quick request."""
    try:
        request = urllib.request.Request(
            "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/readme.txt",
            method="HEAD",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(autouse=True)
def _skip_when_offline() -> None:
    """Skip cleanly when the Bristol host is unreachable."""
    if not _bristol_reachable():
        pytest.skip("Bristol data.bris.ac.uk unreachable (offline)")


class TestFabdemLiveFetch:
    """Live FABDEM subset (open HTTPS — no credentials, one ~0.8 GB bundle)."""

    def test_subset_writes_bare_earth_geotiff(self, tmp_path: Path):
        """A small land pull lands one GeoTIFF of plausible bare-earth heights."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            paths = EarthLens(
                data_source="fabdem",
                lat_lim=_LAT_LIM,
                lon_lim=_LON_LIM,
                path=str(tmp_path),
            ).download(progress_bar=False)

        assert paths == [tmp_path / "fabdem_V1-2.tif"], f"unexpected outputs: {paths}"
        assert paths[0].exists()
        assert any(isinstance(w.message, LicenseWarning) for w in caught), (
            "the non-commercial FABDEM licence must emit a LicenseWarning"
        )
        from pyramids.dataset import Dataset

        from earthlens.base import close_quietly

        dataset = Dataset.read_file(str(paths[0]))
        array = dataset.read_array()
        close_quietly(dataset)
        # FABDEM no-data is -9999.0 (finite), so np.isfinite would not drop it;
        # filter on the actual no-data sentinel like the JRC e2e does.
        valid = array[array > -9999]
        assert valid.size > 0, "the AOI carried no elevation cells"
        low, high = float(valid.min()), float(valid.max())
        assert -20 < low <= high < 400, f"implausible bare-earth heights: {low}..{high}"
