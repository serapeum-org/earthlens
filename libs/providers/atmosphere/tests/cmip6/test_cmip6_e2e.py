"""Live end-to-end test for the CMIP6 backend.

Hits the real, open Pangeo `gs://cmip6` bucket (and its consolidated-stores CSV)
anonymously — no credentials needed — so it is gated only on the `e2e` marker
and network reachability. A default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m "e2e and cmip6" tests/cmip6
"""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.core import EarthLens

from earthlens.cmip6 import Catalog, StoreResolver

pytestmark = [pytest.mark.e2e, pytest.mark.cmip6]

#: A small, widely-published store: CanESM5 SSP5-8.5 monthly near-surface air
#: temperature, member r1i1p1f1.
_FACETS = dict(
    source_id="CanESM5",
    experiment_id="ssp585",
    variable_id="tas",
    table_id="Amon",
    member_id="r1i1p1f1",
)


def _require_csv() -> None:
    """Skip the calling test when the consolidated-stores CSV is unreachable.

    The reachability probe runs **inside** the test (not in a `skipif`
    condition), so collecting this module under `-m "not e2e"` never touches the
    network.
    """
    try:
        import requests

        resp = requests.head(Catalog().csv_url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            pytest.skip("gs://cmip6 CSV unreachable")
    except Exception:
        pytest.skip("gs://cmip6 CSV unreachable")


def test_resolve_live_store(tmp_path):
    """Anonymously resolve the CanESM5 ssp585 tas facet tuple to a gs:// store."""
    _require_csv()
    catalog = Catalog()
    resolver = StoreResolver(
        catalog.csv_url, catalog.facet_columns, cache_path=tmp_path / "stores.csv"
    )
    stores = resolver.resolve(grid_label="gn", **_FACETS)
    assert len(stores) == 1
    assert stores[0].zstore.startswith("gs://cmip6/")
    assert stores[0].variable_id == "tas"


def test_download_small_subset(tmp_path):
    """Live-download a small Europe / one-quarter tas subset and reopen it."""
    _require_csv()
    paths = EarthLens(
        "cmip6",
        start="2050-01-01",
        end="2050-03-31",
        lat_lim=[40.0, 55.0],
        lon_lim=[0.0, 20.0],
        path=str(tmp_path),
        **_FACETS,
    ).download(progress_bar=False)

    assert len(paths) == 1
    written = Path(paths[0])
    assert written.exists()
    assert written.suffix == ".nc"
    assert written.stat().st_size > 0

    from pyramids.netcdf import NetCDF

    reopened = NetCDF.read_file(str(written))
    # one band per monthly step in the window (Jan-Mar 2050 = 3 steps)
    assert len(reopened.variable_names) == 3
