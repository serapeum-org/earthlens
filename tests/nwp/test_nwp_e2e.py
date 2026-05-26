"""Live end-to-end tests for the NWP backend.

Hits the real open buckets (no credentials — NOAA NODD via Herbie, DWD
Open Data over HTTPS). Gated behind the `e2e` marker, so a default
`pytest` run skips them; they also skip cleanly when the per-centre SDK
is unavailable or the network/data is unreachable.

Run with:

    pixi run -e dev pytest -m "e2e and nwp" tests/nwp
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io

import pytest

from earthlens.earthlens import EarthLens
from earthlens.nwp import Catalog
from earthlens.nwp.centres.dwd import DWDCentre

pytestmark = [pytest.mark.nwp, pytest.mark.e2e]


def _herbie_available() -> bool:
    """Return whether `herbie` (and its eccodes stack) imports cleanly."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import herbie  # noqa: F401
    except Exception:
        return False
    return True


def _recent_cycle(hours_ago: int = 36) -> dt.datetime:
    """A 00/06/12/18 cycle roughly `hours_ago` in the past (data still online)."""
    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=hours_ago)
    return moment.replace(hour=(moment.hour // 6) * 6, minute=0, second=0, microsecond=0)


class TestGFSLive:
    """Live GFS analysis-step fetch through the facade."""

    @pytest.mark.skipif(not _herbie_available(), reason="herbie/eccodes not installed")
    def test_gfs_subset_to_cog(self, tmp_path):
        """A one-variable GFS f000 request writes a bbox-cropped COG."""
        cycle = _recent_cycle()
        try:
            lens = EarthLens(
                data_source="nwp",
                variables={"gfs": ["temperature_2m"]},
                start=cycle.strftime("%Y-%m-%d"),
                end=cycle.strftime("%Y-%m-%d"),
                lat_lim=[40, 45],
                lon_lim=[-80, -75],
                path=str(tmp_path),
                steps=[0],
            )
            paths = lens.download(progress_bar=False)
        except Exception as exc:  # network / data-availability flake
            pytest.skip(f"GFS live fetch unavailable: {exc}")
        assert paths, "expected at least one COG"
        assert all(p.exists() and p.stat().st_size > 0 for p in paths)


class TestICONLive:
    """Live DWD ICON raw-GRIB fetch (no crop — native icosahedral grid)."""

    def test_icon_file_downloads(self, tmp_path):
        """A single ICON variable downloads + decompresses to a non-empty GRIB2."""
        model = Catalog().get_model("icon-global")
        # DWD keeps only ~the last day online; use yesterday's 00Z run.
        cycle = (_recent_cycle(hours_ago=24)).replace(hour=0)
        try:
            out = DWDCentre(tmp_path).fetch_one(
                model, cycle, 0, ["temperature_2m"], "auto"
            )
        except Exception as exc:
            pytest.skip(f"ICON live fetch unavailable: {exc}")
        assert out.exists() and out.stat().st_size > 0
