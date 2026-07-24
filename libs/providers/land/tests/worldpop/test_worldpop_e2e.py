"""Live end-to-end tests for the WorldPop backend.

Hits the real, public, anonymous WorldPop hub (`hub.worldpop.org` /
`data.worldpop.org`), so these tests are gated behind the `e2e` pytest
marker plus network availability — no credentials are needed (CC-BY-4.0). A
default `pytest` invocation skips them; they also skip cleanly offline.

Run with:
    pixi run -e dev pytest -m "e2e and worldpop" tests/worldpop
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens
from earthlens.worldpop.rest import BASE_URL

pytestmark = [pytest.mark.e2e, pytest.mark.worldpop]

#: Comoros — a tiny ISO3 whose 100 m national rasters download in seconds.
_ISO3 = "COM"
_LAT_LIM = [-12.5, -11.3]
_LON_LIM = [43.2, 44.6]


def _online() -> bool:
    """Return whether the WorldPop REST API is reachable (skip guard)."""
    import requests

    try:
        requests.get(f"{BASE_URL}/pop", timeout=15).raise_for_status()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_network():
    """Skip the whole module when the WorldPop hub is unreachable."""
    if not _online():
        pytest.skip("WorldPop hub unreachable; skipping live e2e.")


def _worldpop(tmp_path: Path, **kw):
    """Build an EarthLens WorldPop facade over the Comoros test extent."""
    base = dict(
        data_source="worldpop",
        start="2020",
        end="2020",
        lat_lim=_LAT_LIM,
        lon_lim=_LON_LIM,
        fmt="%Y",
        path=str(tmp_path),
        aoi=_ISO3,
    )
    base.update(kw)
    return EarthLens(**base)


def test_live_pop_pull_writes_geotiff(tmp_path: Path):
    """A small-country pop 2020 100 m pull lands a non-empty cropped GeoTIFF."""
    from pyramids.dataset import Dataset

    out = _worldpop(tmp_path, variables=["pop"], year=2020).download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1, f"expected one GeoTIFF, got {out}"
    ds = Dataset.read_file(str(tifs[0]))
    assert ds.epsg == 4326, f"expected WGS84, got {ds.epsg}"


def test_live_age_structures_table(tmp_path: Path):
    """An age_structures pull yields per-cohort rasters and a non-empty table."""
    out = _worldpop(tmp_path, variables=["age_structures"], year=2020).download(
        progress_bar=False
    )
    tifs = [p for p in out if str(p).endswith(".tif")]
    csvs = [p for p in out if str(p).endswith(".csv")]
    assert len(tifs) == 36, f"expected 36 cohort rasters, got {len(tifs)}"
    frame = pd.read_csv(csvs[0])
    assert len(frame) == 36 and frame["population"].sum() > 0


def test_live_multiyear_aggregate(tmp_path: Path):
    """A two-year pop request reduces to one window raster via aggregate=."""
    out = _worldpop(tmp_path, variables=["pop"], years=[2010, 2020]).download(
        progress_bar=False, aggregate=AggregationConfig(freq="100YS", op="mean")
    )
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1 and "_mean" in tifs[0].name


def test_live_dependency_ratios_archive(tmp_path: Path):
    """The live dependency_ratios .7z (Africa) extracts + crops to the AOI."""
    pytest.importorskip("py7zr")
    from earthlens.earthlens import EarthLens

    # a mainland-Kenya bbox (the continental Africa raster covers it).
    out = EarthLens(
        data_source="worldpop",
        variables=["dependency_ratios"],
        start="2010",
        end="2010",
        fmt="%Y",
        lat_lim=[-4.0, 4.0],
        lon_lim=[34.0, 41.0],
        path=str(tmp_path),
        aoi="KEN",
        resolution="1km",
    ).download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 3  # total / old-age / young-age ratios
    assert all("DepRatio" in p.name for p in tifs)


def test_live_global_mosaic_url_resolves():
    """The live global-mosaic path resolves a per-year whole-world GeoTIFF URL.

    Only the URL is resolved (via the `?id=` detail endpoint) — the global
    1 km mosaic itself is ~1.1 GB, too large to download in a test.
    """
    from earthlens.worldpop.rest import global_files_for_year

    files = global_files_for_year("pop", "wpgp1km", 2000)
    assert files and files[0].endswith(".tif")
    assert "1km" in files[0]
