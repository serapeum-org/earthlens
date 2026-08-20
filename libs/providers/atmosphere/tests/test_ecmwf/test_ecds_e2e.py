"""Live end-to-end tests for the ECDS and XDS endpoints (gated).

Runs only under `-m e2e`; needs a Copernicus token. The same Personal Access
Token authenticates both stores, but each additionally requires its dataset
licence accepted at the **current revision**, and ECDS requires the
portal-scope `terms-of-use-ecds` policy — without it every retrieve 403s.

Each case asserts the file actually contains the variable the catalog row
promises, not merely that bytes arrived: a wrong `nc_variable` or a silently
empty result would pass a size check. Retrieves are wrapped in
`download_within_budget` so a queued job cannot consume the whole e2e lane.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


def _netcdf_members(path: Path) -> list[Path]:
    """Return the NetCDF files in a retrieved result, unwrapping a ZIP once.

    Both readers below go through this, so a zipped result is extracted once
    into one place, and an archive carrying no NetCDF says so instead of
    surfacing as an empty variable set or a bare `IndexError`.
    """
    if not zipfile.is_zipfile(path):
        return [path]
    target = path.parent / f"{path.stem}_unzipped"
    if not target.exists():
        with zipfile.ZipFile(path) as archive:
            archive.extractall(target)
    members = sorted(target.rglob("*.nc"))
    assert members, f"{path.name} is a zip with no .nc member"
    return members


def _variables_in(path: Path) -> set[str]:
    """Return the NetCDF variable names in a retrieved file (zip or flat)."""
    import xarray as xr

    names: set[str] = set()
    for member in _netcdf_members(path):
        with xr.open_dataset(member) as dataset:
            names.update(dataset.data_vars)
    return names


def _time_stamps(path: Path) -> list[pd.Timestamp]:
    """Return the datetime values on a retrieved file's time coordinate."""
    import xarray as xr

    member = _netcdf_members(path)[0]
    with xr.open_dataset(member) as dataset:
        name = next(
            (
                coord
                for coord in dataset.coords
                if "datetime" in str(dataset[coord].dtype)
            ),
            None,
        )
        assert name is not None, f"no datetime coordinate in {member.name}"
        return [pd.Timestamp(value) for value in dataset[name].values.ravel()]


#: How far back to reach for an S2S real-time cycle. The real-time stream is a
#: rolling archive, so a pinned recent date silently ages out and the case then
#: fails on the calendar rather than on the code; this is far enough inside the
#: retention window to be safe and recent enough to still be served.
_S2S_LAG_DAYS = 14


def _recent_s2s_cycle() -> pd.Timestamp:
    """Return an S2S real-time cycle date that is inside the retention window."""
    return (pd.Timestamp.utcnow() - pd.Timedelta(days=_S2S_LAG_DAYS)).normalize()


def _fetch(dataset, variable, start, end, resolution, tmp_path, runner, budget_s=None):
    """Download one curated variable and return its written path."""
    lens = EarthLens(
        data_source="ecmwf",
        variables={dataset: [variable]},
        start=start,
        end=end,
        temporal_resolution=resolution,
        lat_lim=[50.0, 51.0],
        lon_lim=[9.0, 10.0],
        path=str(tmp_path),
    )
    # None defers to the fixture's own default rather than restating it here.
    out = runner(lens) if budget_s is None else runner(lens, budget_s)
    assert out, f"{dataset}/{variable} returned no paths"
    assert out[0].exists()
    assert out[0].stat().st_size > 0
    return out[0]


class TestEcdsE2E:
    """Live retrieves on the ECDS endpoint."""

    def test_live_tigge_returns_2m_temperature(self, tmp_path, download_within_budget):
        """A one-day TIGGE control forecast returns the `t2m` field.

        TIGGE gets double the default budget: it is a 13-centre archive and has
        the least predictable queue of the rows here, so the shared 900s cap
        fails it on a slow day while the request is still queued and valid.
        """
        path = _fetch(
            "tigge-forecasts",
            "2m-temperature",
            "2024-01-01",
            "2024-01-01",
            "daily",
            tmp_path,
            download_within_budget,
            budget_s=1800.0,
        )
        assert "t2m" in _variables_in(path)

    def test_live_s2s_forecast_returns_2m_temperature(
        self, tmp_path, download_within_budget
    ):
        """An S2S real-time forecast returns the `t2m` field."""
        cycle = f"{_recent_s2s_cycle():%Y-%m-%d}"
        path = _fetch(
            "s2s-forecasts",
            "2m-temperature",
            cycle,
            cycle,
            "daily",
            tmp_path,
            download_within_budget,
        )
        assert "t2m" in _variables_in(path)

    @pytest.mark.parametrize("start", ["2015-01-01", "2015-06-01"])
    def test_live_s2s_reforecast_tracks_the_requested_month(
        self, tmp_path, download_within_budget, start
    ):
        """The reforecast date follows the model cycle, so any month resolves.

        A June window is included deliberately: while `hmonth`/`hday` were
        pinned to `01`/`01` only a January request could succeed, and the
        failure surfaced as an empty result rather than an error.

        The dates inside the file are what prove the copy worked: a store that
        ignored `hmonth`/`hday` would still return a well-formed `mx2t6`.
        """
        path = _fetch(
            "s2s-reforecasts",
            "maximum-2m-temperature-in-the-last-6-hours",
            start,
            start,
            "daily",
            tmp_path,
            download_within_budget,
        )
        assert "mx2t6" in _variables_in(path)

        cycle = pd.Timestamp(start)
        stamps = _time_stamps(path)
        assert {stamp.year for stamp in stamps} == {1995}, (
            f"reforecast should sit in the pinned hyear 1995, got {stamps[:3]}"
        )
        assert {stamp.month for stamp in stamps} == {cycle.month}, (
            f"reforecast month should track the {cycle:%B} cycle, got {stamps[:3]}"
        )


class TestXdsE2E:
    """Live retrieves on the XDS endpoint."""

    def test_live_fuel_moisture_returns_lfmc(self, tmp_path, download_within_budget):
        """A one-month live-fuel-moisture retrieve returns the `LFMC` field."""
        path = _fetch(
            "derived-fire-fuel-biomass",
            "live-fuel-moisture-content-group",
            "2000-01-01",
            "2000-01-31",
            "monthly",
            tmp_path,
            download_within_budget,
        )
        assert "LFMC" in _variables_in(path)

    def test_live_burned_area_returns_baf_pred(self, tmp_path, download_within_budget):
        """The annual burned-area retrieve returns the `BAF_pred` field."""
        path = _fetch(
            "projections-fire-fuel-burned-area",
            "burned-area",
            "1950-01-01",
            "1950-12-31",
            "monthly",
            tmp_path,
            download_within_budget,
        )
        assert "BAF_pred" in _variables_in(path)
