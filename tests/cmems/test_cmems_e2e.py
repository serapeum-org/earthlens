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

import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens


_HAVE_CREDS = bool(
    os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
    and os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")
)


@pytest.mark.e2e
@pytest.mark.cmems
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason=(
        "set COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD to run "
        "live CMEMS e2e tests"
    ),
)
class TestCmemsLiveSubset:
    """Single tiny subset against a public CMEMS dataset."""

    def test_ostia_one_day_one_degree_box(self, tmp_path: Path):
        """OSTIA L4 SST — 1° box × 1 day → one NetCDF written."""
        el = EarthLens(
            data_source="cmems",
            start="2024-01-15",
            end="2024-01-15",
            variables={"METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2": ["analysed_sst"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[-10.0, -9.0],
            temporal_resolution="daily",
            path=str(tmp_path),
        )
        paths = el.download(progress_bar=False)
        assert paths, f"no files written into {tmp_path!r}: {list(tmp_path.iterdir())!r}"
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


