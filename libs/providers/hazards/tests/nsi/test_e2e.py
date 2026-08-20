"""Live end-to-end tests for the NSI backend (marker-gated, real network).

Selected with `-m "e2e and nsi"`. `structures` and `nfip` hit real US-federal
endpoints; the `nfhl` case is `xfail` because `hazards.fema.gov` is blocked from
the build environment (see the A1 gate captures)
and only runs green from a reachable network.
"""

from __future__ import annotations

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.nsi]


def test_structures_fips_live(tmp_path) -> None:
    """A live NSI structures pull for one census tract returns features."""
    fc = EarthLens(
        "nsi", source="structures", fips="22071012700", path=tmp_path
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 0


def test_structures_box_post_live(tmp_path) -> None:
    """A live NSI structures pull by bounding box (the POST-polygon path) returns features."""
    fc = EarthLens(
        "nsi",
        source="structures",
        lat_lim=[29.95, 29.96],
        lon_lim=[-90.07, -90.06],
        path=tmp_path,
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 0


def test_nfip_county_live(tmp_path) -> None:
    """A live NFIP claims pull for one county/year returns a claims table."""
    df = EarthLens(
        "nfip", filters={"county": "22071", "year": 2005}, max_records=25, path=tmp_path
    ).download()
    assert isinstance(df, pd.DataFrame)
    assert 0 < len(df) <= 25
    assert "building_paid" in df.columns


@pytest.mark.xfail(
    reason="hazards.fema.gov is network-blocked from this build env (HTTP 000); "
    "runs green from a reachable network.",
    raises=Exception,
    strict=False,
)
def test_nfhl_bbox_live(tmp_path) -> None:
    """A live FEMA NFHL flood-zone query for a small bbox returns zones."""
    fc = EarthLens(
        "nfhl", lat_lim=[29.95, 29.96], lon_lim=[-90.07, -90.06], path=tmp_path
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 0
