"""Live end-to-end test for the ISIMIP backend.

Hits the real, public ISIMIP repository REST API and the files-API cutout job —
no credentials needed — so it is gated only on the `e2e` marker and network
reachability. A default `pytest` run skips it. Requires the `isimip` extra
(`isimip-client`).

Run with:

    uv run --active pytest -m "e2e and isimip" libs/providers/atmosphere/tests/isimip
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens
from earthlens.isimip import Catalog

pytestmark = [pytest.mark.e2e, pytest.mark.isimip]

#: A small, widely-published dataset: GFDL-ESM4 SSP5-8.5 daily precipitation.
_FACETS = dict(
    dataset="ISIMIP3b",
    gcm="gfdl-esm4",
    scenario="ssp585",
    variables=["pr"],
)


def _require_api() -> None:
    """Skip the calling test when the ISIMIP API is unreachable.

    The reachability probe runs inside the test (not in a `skipif` condition), so
    collecting this module under `-m "not e2e"` never touches the network.
    """
    try:
        import requests

        resp = requests.get(
            Catalog().data_url + "/datasets/", params={"page_size": 1}, timeout=20
        )
        if resp.status_code != 200:
            pytest.skip("ISIMIP API unreachable")
    except Exception:
        pytest.skip("ISIMIP API unreachable")


def test_search_live():
    """Live facet query resolves the pr / ssp585 / gfdl-esm4 dataset to granules."""
    _require_api()
    backend = EarthLens(
        "isimip",
        start="2030-01-01",
        end="2030-12-31",
        lat_lim=[51.0, 53.0],
        lon_lim=[6.0, 8.0],
        **_FACETS,
    ).datasource
    products = backend._search()
    assert products, "expected at least one resolved dataset"
    assert all(p.metadata["paths"] for p in products)
    assert any(".nc" in path for p in products for path in p.metadata["paths"])


def test_download_small_cutout(tmp_path):
    """Live cutout of a tiny bbox returns a cut NetCDF granule, reopened by pyramids."""
    _require_api()
    paths = EarthLens(
        "isimip",
        start="2030-01-01",
        end="2030-12-31",
        lat_lim=[51.0, 53.0],
        lon_lim=[6.0, 8.0],
        path=str(tmp_path),
        **_FACETS,
    ).download(progress_bar=False)

    assert len(paths) == 1
    written = Path(paths[0])
    assert written.exists()
    assert written.suffix == ".nc"
    assert written.stat().st_size > 0
    # the cut granule is a small fraction of the ~1.2 GB global source
    assert written.stat().st_size < 100 * 1024 * 1024

    from pyramids.netcdf import NetCDF

    reopened = NetCDF.read_file(str(written))
    assert "pr" in reopened.variable_names
