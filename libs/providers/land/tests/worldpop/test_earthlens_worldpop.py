"""Tests for the WorldPop backend wiring into the EarthLens facade."""

from __future__ import annotations

import pytest
from earthlens.earthlens import EarthLens

from earthlens.worldpop import WorldPop

pytestmark = pytest.mark.worldpop


@pytest.mark.parametrize("key", ["worldpop", "world-pop"])
def test_keys_registered(key):
    """Both facade keys are registered."""
    assert key in EarthLens.DataSources


@pytest.mark.parametrize("key", ["worldpop", "world-pop"])
def test_keys_resolve_to_worldpop(key):
    """Both facade keys resolve to the WorldPop class."""
    assert EarthLens.DataSources[key] is WorldPop


def test_facade_builds_and_routes(tmp_path):
    """EarthLens(data_source='worldpop') constructs the backend."""
    facade = EarthLens(
        data_source="worldpop",
        variables=["pop"],
        start="2020",
        end="2020",
        lat_lim=[-4.0, 4.0],
        lon_lim=[34.0, 41.0],
        fmt="%Y",
        path=str(tmp_path),
        aoi="KEN",
    )
    assert isinstance(facade.datasource, WorldPop)
    assert facade.datasource.OUTPUT_KIND == "mixed"


def test_facade_forwards_backend_kwargs(tmp_path):
    """Backend-specific kwargs (constrained/resolution) are forwarded."""
    facade = EarthLens(
        data_source="worldpop",
        variables=["pop"],
        start="2020",
        end="2020",
        lat_lim=[-4.0, 4.0],
        lon_lim=[34.0, 41.0],
        fmt="%Y",
        path=str(tmp_path),
        aoi="KEN",
        constrained=True,
        generation="R2025A",
    )
    assert facade.datasource._subalias_ids["pop"] == "G2_CN_POP_R25A_100m"
