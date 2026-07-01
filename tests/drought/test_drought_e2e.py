"""Live end-to-end tests for the drought backend (USDM / EDO+GDO / SPEIbase).

All three sources are public — these tests are gated only behind the `e2e`
pytest marker plus network availability, no credentials. A default `pytest`
invocation skips them.

Run with:

    pixi run -e dev pytest -m "drought and e2e" tests/drought

Each test first checks its source host is reachable and serving real data and
skips cleanly otherwise (the USDM / Copernicus endpoints are normally up).
`digital.csic.es` fronts SPEIbase with an Anubis proof-of-work wall that only
challenges browser-like (`Mozilla…`) User-Agents; the backend and the
reachability probe both send a plain `earthlens-…` UA, so the wall passes the
raw NetCDF through and the SPEIbase test runs — the skip is a network-outage
guard, not an expected bot-wall.
"""

from __future__ import annotations

import datetime as dt
import urllib.request
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

# A recent USDM week: snap to a Tuesday whose Thursday release has rolled out.
# Two weeks back is comfortably released regardless of the weekday today.
_TODAY = dt.date.today()
_USDM_WINDOW_END = (_TODAY - dt.timedelta(days=14)).strftime("%Y-%m-%d")


def _reachable(url: str, *, want_binary: bool = False) -> bool:
    """Return True when `url` answers 2xx (and, optionally, with non-HTML bytes)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "earthlens-e2e"})
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status >= 400:
                return False
            if want_binary:
                head = response.read(8)
                ctype = response.headers.get("content-type", "")
                # A bot-check / error page is HTML; real data is not.
                return b"<" not in head[:1] and "html" not in ctype.lower()
            return True
    except Exception:
        return False


@pytest.mark.e2e
class TestUsdmLive:
    """Live USDM weekly polygon fetch (public, no credentials)."""

    def test_recent_week_returns_polygons(self, tmp_path: Path):
        """A recent USDM week returns a drought-class FeatureCollection in 4326."""
        if not _reachable(
            "https://droughtmonitor.unl.edu/data/json/usdm_20250617.json"
        ):
            pytest.skip("USDM host unreachable")
        fc = EarthLens(
            data_source="usdm",
            dataset="usdm",
            variables=[],
            start=_USDM_WINDOW_END,
            end=_USDM_WINDOW_END,
            lat_lim=[24.0, 50.0],
            lon_lim=[-125.0, -66.0],
        ).download(progress_bar=False)
        assert fc.crs.to_epsg() == 4326
        assert "DM" in fc.columns
        if len(fc):
            assert set(fc["DM"]).issubset({0, 1, 2, 3, 4})


@pytest.mark.e2e
class TestEdoLive:
    """Live Copernicus EDO/GDO WCS GetCoverage fetch (public, no credentials)."""

    _EDO_PROBE = (
        "https://drought.emergency.copernicus.eu/api/wcs?map=DO_WCS"
        "&SERVICE=WCS&VERSION=2.0.0&REQUEST=GetCoverage&coverageID=spaST"
        "&CRS=EPSG:4326&format=GEOTIFF&TIME=2025-12-21&SELECTED_TIMESCALE=01"
        "&SUBSET=Long(5,15)&SUBSET=Lat(40,50)"
    )

    def test_edo_spaST_returns_geotiff(self, tmp_path: Path):
        """An EDO SPI ERA5 short-term coverage downloads as a pyramids-openable TIFF."""
        if not _reachable(self._EDO_PROBE, want_binary=True):
            pytest.skip("Copernicus EDO WCS endpoint unreachable / not serving data")
        paths = EarthLens(
            data_source="drought",
            dataset="edo-spaST",
            variables=[],
            start="2025-12-21",
            end="2025-12-21",
            lat_lim=[40.0, 50.0],
            lon_lim=[5.0, 15.0],
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert len(paths) == 1
        assert paths[0].suffix == ".tif" and paths[0].stat().st_size > 0
        from pyramids.dataset import Dataset

        ds = Dataset.read_file(str(paths[0]))
        try:
            assert ds.epsg in (4326, 0) or ds.epsg is not None
        finally:
            ds.close()

    def test_gdo_smand_returns_geotiff(self, tmp_path: Path):
        """A GDO ensemble soil-moisture anomaly coverage downloads as a TIFF.

        GDO rides the same `map=DO_WCS` map as EDO (there is no GDO_WCS map);
        `smand` has data in 2024, so a mid-2024 date returns a real raster.
        """
        probe = self._EDO_PROBE.replace(
            "coverageID=spaST&CRS", "coverageID=smand&CRS"
        ).replace("TIME=2025-12-21", "TIME=2024-06-21")
        if not _reachable(probe, want_binary=True):
            pytest.skip("Copernicus EDO/GDO WCS endpoint unreachable / not serving data")
        paths = EarthLens(
            data_source="drought",
            dataset="gdo-smand",
            variables=[],
            start="2024-06-21",
            end="2024-06-21",
            lat_lim=[40.0, 50.0],
            lon_lim=[5.0, 15.0],
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert len(paths) == 1 and paths[0].stat().st_size > 0


@pytest.mark.e2e
class TestSpeibaseLive:
    """Live CSIC SPEIbase v2.11 NetCDF fetch (public CC-BY, no credentials)."""

    def test_speibase_month_returns_geotiff(self, tmp_path: Path):
        """A SPEIbase month slices into a per-month GeoTIFF.

        `digital.csic.es` fronts the bitstream with an Anubis wall that only
        challenges `Mozilla…` User-Agents; the plain `earthlens-…` UA passes
        through, so this normally runs. The reachability guard only skips on a
        genuine network outage (or a non-NetCDF body).
        """
        from earthlens.drought import Catalog

        endpoint = Catalog().get("speibase-12").endpoint
        if not _reachable(endpoint, want_binary=True):
            pytest.skip(
                "SPEIbase host unreachable (digital.csic.es did not serve NetCDF "
                "from this network)"
            )
        paths = EarthLens(
            data_source="drought",
            dataset="speibase-12",
            variables=[],
            start="2023-06-01",
            end="2023-06-30",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert len(paths) == 1
        assert paths[0].suffix == ".tif" and paths[0].stat().st_size > 0
