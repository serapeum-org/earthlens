"""Live end-to-end tests for the CMEMS backend.

Hits the real Copernicus Marine portal. Gated behind both the
`e2e` pytest marker and the standard toolbox env vars
(`COPERNICUSMARINE_SERVICE_USERNAME` /
`COPERNICUSMARINE_SERVICE_PASSWORD`), so a default `pytest`
invocation skips them.

Run with:

    COPERNICUSMARINE_SERVICE_USERNAME=... \\
    COPERNICUSMARINE_SERVICE_PASSWORD=... \\
    pixi run -e dev pytest -m e2e tests/cmems
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

_HAVE_CREDS = bool(
    os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
    and os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")
)

# OSTIA NRT is a rolling near-real-time product: its coverage window
# slides forward and old days fall off the back, so a hardcoded date
# eventually leaves the dataset bounds (`CoordinatesOutOfDatasetBounds`).
# Probe ~30 days back from today, comfortably inside the NRT window and
# after the ~1-day publication latency.
_NRT_PROBE_DATE = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime(
    "%Y-%m-%d"
)


@pytest.mark.e2e
@pytest.mark.cmems
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason=(
        "set COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD to run live CMEMS e2e tests"
    ),
)
class TestCmemsLiveSubset:
    """Single tiny subset against a public CMEMS dataset."""

    def test_ostia_one_day_one_degree_box(self, tmp_path: Path):
        """OSTIA L4 SST — 1° box × 1 day → one NetCDF written."""
        el = EarthLens(
            data_source="cmems",
            start=_NRT_PROBE_DATE,
            end=_NRT_PROBE_DATE,
            variables={"METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2": ["analysed_sst"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[-10.0, -9.0],
            temporal_resolution="daily",
            path=str(tmp_path),
        )
        paths = el.download(progress_bar=False)
        assert paths, (
            f"no files written into {tmp_path!r}: {list(tmp_path.iterdir())!r}"
        )
        assert all(p.exists() for p in paths), (
            f"download() returned non-existent paths: {paths!r}"
        )
        assert all(p.stat().st_size > 0 for p in paths), (
            f"download() returned empty files: {[(p, p.stat().st_size) for p in paths]!r}"
        )

    def test_glorys_thetao_subset(self, tmp_path: Path):
        """GLORYS12 daily thetao subset — single point × 1 day."""
        el = EarthLens(
            data_source="cmems",
            start="2020-06-01",
            end="2020-06-01",
            variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
            lat_lim=[40.0, 40.5],
            lon_lim=[-10.0, -9.5],
            temporal_resolution="daily",
            path=str(tmp_path),
            minimum_depth=0.0,
            maximum_depth=5.0,
        )
        paths = el.download(progress_bar=False)
        assert paths, "GLORYS12 subset should write at least one file"
        assert paths[0].suffix == ".nc"

    def test_glorys_monthly_aggregate_via_real_pyramids(self, tmp_path: Path):
        """Full `aggregate=` path on a real GLORYS subset: real CF decode + reduce -> GeoTIFF.

        Exercises the real pyramids contract `_aggregate_one` relies on (no stubs):
        `get_time_variable` decodes the CF `time` axis, `_window_labels` buckets it,
        and `reduce("time", groupby=labels)` collapses to one slice per window written
        through `Dataset.from_array`. Three June 2020 days -> one monthly window.
        """
        el = EarthLens(
            data_source="cmems",
            start="2020-06-01",
            end="2020-06-03",
            variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
            lat_lim=[40.0, 40.5],
            lon_lim=[-10.0, -9.5],
            temporal_resolution="daily",
            path=str(tmp_path),
            minimum_depth=0.0,
            maximum_depth=5.0,
        )
        paths = el.download(
            progress_bar=False,
            aggregate=AggregationConfig(freq="1MS", op="mean", out_dir=str(tmp_path)),
        )
        assert paths, "monthly aggregate should write at least one GeoTIFF"
        assert all(p.suffix == ".tif" for p in paths), (
            f"aggregate output should be GeoTIFFs, got {[p.name for p in paths]!r}"
        )
        names = [p.name for p in paths]
        assert any(n.endswith("_thetao_1MS_20200601.tif") for n in names), (
            f"expected a June-2020 monthly window GeoTIFF, got {names!r}"
        )
        assert all(p.exists() and p.stat().st_size > 0 for p in paths), (
            f"aggregate GeoTIFFs should be non-empty: "
            f"{[(p.name, p.stat().st_size) for p in paths]!r}"
        )
