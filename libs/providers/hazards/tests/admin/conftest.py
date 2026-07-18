"""Shared fixtures and fakes for the admin backend tests (no network)."""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pytest
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import Polygon


def _square(x: float, y: float, side: float = 1.0) -> Polygon:
    """Return a unit-ish square polygon anchored at `(x, y)`."""
    return Polygon([(x, y), (x + side, y), (x + side, y + side), (x, y + side)])


def make_fc(
    n: int = 2,
    crs: Any = "EPSG:4326",
    columns: dict[str, list[Any]] | None = None,
) -> FeatureCollection:
    """Build a real FeatureCollection of `n` square polygons in `crs`."""
    cols = columns or {"name": [f"poly{i}" for i in range(n)]}
    geoms = [_square(float(i), 0.0) for i in range(n)]
    gdf = gpd.GeoDataFrame(cols, geometry=geoms, crs=crs)
    return FeatureCollection(gdf)


@pytest.fixture
def fc_factory():
    """Expose the `make_fc` builder to tests."""
    return make_fc
