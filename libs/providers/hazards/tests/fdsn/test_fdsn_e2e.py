"""Live end-to-end tests for the FDSN seismic-event backend.

Hits the real FDSN event web services. All bundled networks expose
public event services, so these tests are gated only behind the `e2e`
pytest marker plus network availability; a default `pytest` invocation
skips them.

Run with:

    uv run pytest -m e2e libs/providers/hazards/tests/fdsn
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.fdsn import FDSN
from earthlens.fdsn.events import ATTRIBUTE_COLUMNS

# A historically very active window + box: the 2011 Tohoku sequence off
# the Pacific coast of Japan, which has many M5+ events on record.
_ACTIVE_START = "2011-03-11"
_ACTIVE_END = "2011-03-18"
_JAPAN_LAT = [30.0, 45.0]
_JAPAN_LON = [135.0, 150.0]


@pytest.mark.e2e
@pytest.mark.fdsn
class TestUsgsLiveQuery:
    """Live USGS ComCat queries (public — no credentials needed)."""

    def test_active_window_returns_events(self, tmp_path: Path):
        """A known active window/box returns plausible M5+ events."""
        fc = EarthLens(
            variables=["USGS"],
            data_source="fdsn",
            start=_ACTIVE_START,
            end=_ACTIVE_END,
            lat_lim=_JAPAN_LAT,
            lon_lim=_JAPAN_LON,
            path=str(tmp_path),
            min_magnitude=5.0,
        ).download()

        assert len(fc) > 0, "expected at least one M5+ event in the Tohoku window"
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert fc.crs.to_epsg() == 4326
        assert (fc["magnitude"].dropna() >= 5.0).all(), "min_magnitude not honoured"
        assert (fc["depth_km"].dropna() >= 0).all(), "negative depth_km"
        assert fc["latitude"].between(_JAPAN_LAT[0], _JAPAN_LAT[1]).all()
        assert fc["longitude"].between(_JAPAN_LON[0], _JAPAN_LON[1]).all()
        assert (tmp_path / "usgs.gpkg").is_file(), "GeoPackage should be written"

    def test_quiet_query_returns_empty_but_valid(self, tmp_path: Path):
        """A deliberately quiet box/time returns an empty, schema-correct FC."""
        fc = FDSN(
            start="2020-01-01",
            end="2020-01-02",
            variables=["USGS"],
            lat_lim=[0.0, 0.5],
            lon_lim=[0.0, 0.5],
            path=str(tmp_path),
            min_magnitude=8.0,
        ).download()

        assert len(fc) == 0, "no M8+ events expected in a half-degree box over 1 day"
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"


@pytest.mark.e2e
@pytest.mark.fdsn
class TestEarthscopeLiveQuery:
    """Live EarthScope query (public event service — no token needed)."""

    def test_active_window_returns_events(self, tmp_path: Path):
        """EarthScope returns plausible events for the active window."""
        try:
            fc = FDSN(
                start=_ACTIVE_START,
                end=_ACTIVE_END,
                variables=["EARTHSCOPE"],
                lat_lim=_JAPAN_LAT,
                lon_lim=_JAPAN_LON,
                path=str(tmp_path),
                min_magnitude=5.0,
            ).download()
        except RuntimeError as exc:
            pytest.skip(f"EarthScope service unavailable: {exc}")

        assert len(fc) > 0, "expected at least one M5+ event from EarthScope"
        assert fc.crs.to_epsg() == 4326


@pytest.mark.e2e
@pytest.mark.fdsn
class TestShakemapLiveSideOutput:
    """Live ShakeMap side-output against USGS ComCat."""

    def test_writes_georeferenced_shakemap(self, tmp_path: Path):
        """A large USGS event yields a georeferenced ShakeMap GeoTIFF."""
        from pyramids.dataset import Dataset

        fc = FDSN(
            start="2023-02-06",
            end="2023-02-07",
            variables=["USGS"],
            lat_lim=[35.0, 39.0],
            lon_lim=[35.0, 39.0],
            path=str(tmp_path),
            min_magnitude=7.0,
            with_shakemap=True,
        ).download()

        assert len(fc) > 0, "expected at least one M7+ event in the window"
        rasters = sorted((tmp_path / "shakemap").rglob("mmi_mean.tif"))
        assert rasters, "expected a ShakeMap GeoTIFF per event"

        dataset = Dataset.read_file(rasters[0])
        assert dataset.driver_type == "geotiff"
        # pyramids resolves this through AutoIdentifyEPSG rather than
        # matching digits in the WKT, so a projection that merely mentions
        # 4326 without carrying the authority does not satisfy it.
        assert dataset.epsg == 4326, (
            f"the CRS should resolve to EPSG:4326, got {dataset.epsg}"
        )
        assert dataset.columns > 1, "the grid should have real width"
        assert dataset.rows > 1, "the grid should have real height"

        # Exact contents, not an allowlist of forbidden suffixes: GDAL drops a
        # `.prj` beside the grid when its CRS is assigned, which a suffix filter
        # would not notice.
        for event_dir in (tmp_path / "shakemap").iterdir():
            assert sorted(p.name for p in event_dir.iterdir()) == [
                ".shakemap.json",
                "mmi_mean.tif",
            ], f"{event_dir.name} should hold only the raster and its manifest"
