"""Live end-to-end tests for the admin administrative-boundaries backend.

Hits the real, public sources — geoBoundaries (API + GitHub release GeoJSON),
CGAZ (GitHub release GeoPackage), Natural Earth (NACIS CDN ZIP), and US Census
TIGER/Line (census.gov ZIP). All four are open, so these tests need no
credentials; they are gated behind the `e2e` pytest marker plus network
availability, and a default `pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m "e2e and admin" tests/admin
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.admin]


def _reachable(url: str) -> bool:
    """Return whether `url` answers a quick HEAD/GET within the timeout."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "earthlens-e2e"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(autouse=True)
def _skip_when_offline() -> None:
    """Skip the live tests cleanly when the source hosts are unreachable."""
    if not _reachable("https://www.geoboundaries.org/api/current/gbOpen/KEN/ADM1/"):
        pytest.skip("geoBoundaries / public sources unreachable (offline)")


def _assert_polygons_4326(fc) -> None:
    """Assert a non-empty polygon FeatureCollection in EPSG:4326."""
    assert len(fc) > 0, "expected at least one boundary polygon"
    assert fc.crs.to_epsg() == 4326, f"expected EPSG:4326, got {fc.crs}"
    geom_types = set(fc.geom_type)
    assert geom_types <= {"Polygon", "MultiPolygon"}, f"unexpected geom: {geom_types}"


class TestAdminLiveFetch:
    """Live boundary pulls from the four public sources (no credentials)."""

    def test_geoboundaries_country_adm1(self):
        """A geoBoundaries ADM1 pull for one country returns polygons in 4326."""
        fc = EarthLens(
            data_source="admin",
            variables=["geoboundaries:adm1"],
            country="KEN",
        ).download(progress_bar=False)
        _assert_polygons_4326(fc)

    def test_natural_earth_countries(self):
        """A Natural Earth 110m countries pull returns polygons in 4326."""
        fc = EarthLens(
            data_source="natural-earth",
            variables=["natural_earth:countries"],
            scale="110m",
        ).download(progress_bar=False)
        _assert_polygons_4326(fc)

    def test_tiger_states(self):
        """A TIGER states pull reprojects NAD83 to EPSG:4326 polygons."""
        fc = EarthLens(
            data_source="tiger",
            variables=["tiger:state"],
        ).download(progress_bar=False)
        _assert_polygons_4326(fc)

    @pytest.mark.slow
    def test_cgaz_adm0(self):
        """A CGAZ seamless ADM0 pull returns polygons declared EPSG:4326.

        CGAZ ships one large global GeoPackage (hundreds of MB), so this is
        marked slow as well as e2e.
        """
        fc = EarthLens(
            data_source="admin",
            variables=["cgaz:adm0"],
        ).download(progress_bar=False)
        _assert_polygons_4326(fc)
