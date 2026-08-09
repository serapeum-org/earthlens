"""Gated end-to-end tests for the Aqueduct backend (live files.wri.org fetch).

Selected with `-m "e2e and aqueduct"`; the default suite deselects `e2e`. These
hit the real WRI public file host, so they need network but no credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.aqueduct import Aqueduct
from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.aqueduct]


def test_live_country_download_returns_real_collection(tmp_path: Path) -> None:
    """A live country download returns a populated FeatureCollection."""
    fc = Aqueduct(
        admin_level="country",
        metric="population_affected",
        year=2010,
        scenario="baseline",
        return_period=100,
        path=tmp_path,
        cache_dir=tmp_path,
    ).download()

    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 200  # ~253 countries in the shipped shapefile
    assert list(fc.columns) == ["unit_id", "unit_name", "rp_100", "geometry"]
    assert fc.crs.to_epsg() == 4326
    assert (fc["rp_100"] >= 0).all()
    assert list(tmp_path.glob("aqueduct_country_*.gpkg"))


def test_live_facade_country_filter(tmp_path: Path) -> None:
    """The facade routes a country-filtered live download to one unit."""
    fc = EarthLens(
        "aqueduct",
        admin_level="country",
        country="Kenya",
        metric="gdp_affected",
        year=2030,
        scenario="ssp2-rcp8p5",
        return_period=[100, 1000],
        path=tmp_path,
        cache_dir=tmp_path,
    ).download()

    assert list(fc["unit_name"]) == ["KENYA"]
    assert {"rp_100", "rp_1000"}.issubset(fc.columns)
