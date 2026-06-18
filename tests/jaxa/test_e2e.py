"""Live end-to-end tests for the JAXA backend.

Two tracks:

* **`jaxa-earth` (authless)** — pulls a tiny AW3D30 tile around Mt. Fuji
  through the official `jaxa.earth` API and writes a real COG via
  pyramids. Skipped when the SDK is not installed.
* **`gportal` (credentialed)** — issues a 1-product search against a
  GCOM-C/SGLI dataset and, when credentials are available, SFTP-downloads
  it. Skipped without `$GPORTAL_USERNAME` + `$GPORTAL_PASSWORD`.

Both tests are marked `e2e` and skipped by default (the suite runs with
`-m "not e2e"`); they only run under `pytest -m "jaxa and e2e"`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from earthlens.jaxa import JAXA

pytestmark = [pytest.mark.jaxa, pytest.mark.e2e]


@pytest.mark.slow
def test_jaxa_earth_authless_fetch_lives(tmp_path: Path) -> None:
    """Authless AW3D30 tile pull writes a non-empty COG over EPSG:4326."""
    pytest.importorskip("jaxa.earth")
    from pyramids.dataset import Dataset

    lens = JAXA(
        variables=["aw3d30"],
        start="2000-01-01",
        end="2030-12-31",
        lat_lim=[35.35, 35.40],
        lon_lim=[138.70, 138.78],
        resolution=1000.0,
        path=tmp_path,
    )
    written = lens.download()
    assert len(written) == 1
    assert written[0].exists()
    assert written[0].stat().st_size > 0
    cog = Dataset.read_file(str(written[0]))
    assert cog.epsg == 4326
    geo = cog.geotransform
    assert geo[0] == pytest.approx(138.70, abs=0.005)
    assert geo[3] == pytest.approx(35.40, abs=0.005)


@pytest.mark.slow
@pytest.mark.skipif(
    not (os.environ.get("GPORTAL_USERNAME") and os.environ.get("GPORTAL_PASSWORD")),
    reason="needs $GPORTAL_USERNAME + $GPORTAL_PASSWORD",
)
def test_gportal_search_lives(tmp_path: Path) -> None:
    """A live G-Portal search over a 1-day window matches a small product set.

    Search is **anonymous** even with credentials available (the SDK only
    needs creds for `download`). The test verifies search reaches the
    live catalog without going through the SFTP download stage.
    """
    pytest.importorskip("gportal")
    import gportal

    # Auth side effect — the JAXA backend would normally do this, but the
    # test calls gportal directly so we set the module attrs ourselves.
    gportal.username = os.environ["GPORTAL_USERNAME"]
    gportal.password = os.environ["GPORTAL_PASSWORD"]
    try:
        search = gportal.search(
            dataset_ids=["10003001"],
            start_time="2024-01-01",
            end_time="2024-01-02",
            count=3,
        )
        assert (search.matched() or 0) >= 1
    finally:
        gportal.username = None
        gportal.password = None
