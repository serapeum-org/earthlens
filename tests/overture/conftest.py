"""Shared fixtures for the Overture backend tests.

Builds synthetic Overture `GeoDataFrame`s (no network) and a recording
fake for `overturemaps.core.geodataframe`, so the backend can be
exercised end-to-end offline. The `fake_overture` fixture patches the
SDK entry point the backend imports inside `_fetch` and records every
`(overture_type, bbox, release)` call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import Point

#: A few representative `sources` cells covering the license-derivation paths.
PERMISSIVE_SOURCES = [
    {"dataset": "Foursquare", "license": "Apache-2.0"},
    {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
]
OSM_SOURCES = [{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}]
OSM_SECOND_SOURCES = [
    {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
    {"dataset": "OpenStreetMap", "license": "ODbL-1.0"},
]
NO_LICENSE_SOURCES = [{"dataset": "Overture"}]


def _make_gdf(
    sources_per_row: list[Any] | None = None,
    *,
    set_crs: bool = False,
) -> gpd.GeoDataFrame:
    """Build a synthetic Overture `GeoDataFrame` with a `sources` column.

    Args:
        sources_per_row: One `sources` cell per row. Defaults to a single
            permissive row.
        set_crs: When `True`, tag the frame `EPSG:4326` (the real SDK
            omits the CRS — the default mirrors that).

    Returns:
        geopandas.GeoDataFrame: id / geometry / sources, one row per cell.
    """
    if sources_per_row is None:
        sources_per_row = [PERMISSIVE_SOURCES]
    rows = len(sources_per_row)
    gdf = gpd.GeoDataFrame(
        {
            "id": [f"feat-{i}" for i in range(rows)],
            "sources": sources_per_row,
        },
        geometry=[Point(float(i), float(i)) for i in range(rows)],
    )
    if set_crs:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


class _FakeOverture:
    """Callable `overturemaps.core.geodataframe` stand-in that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gdf_for_type: dict[str, gpd.GeoDataFrame] = {}
        self.default_gdf: gpd.GeoDataFrame = _make_gdf()

    def __call__(
        self,
        overture_type: str,
        bbox: tuple[float, float, float, float] | None = None,
        release: str | None = None,
        **kwargs: Any,
    ) -> gpd.GeoDataFrame:
        self.calls.append(
            {"type": overture_type, "bbox": bbox, "release": release, "kwargs": kwargs}
        )
        gdf = self._gdf_for_type.get(overture_type, self.default_gdf)
        return gdf.copy()

    def set_gdf(self, overture_type: str, gdf: gpd.GeoDataFrame) -> None:
        """Pin the frame returned for one Overture type."""
        self._gdf_for_type[overture_type] = gdf

    def set_default_gdf(self, gdf: gpd.GeoDataFrame) -> None:
        """Pin the frame returned for any unmapped type."""
        self.default_gdf = gdf


@pytest.fixture
def make_gdf() -> Callable[..., gpd.GeoDataFrame]:
    """Factory for a synthetic Overture `GeoDataFrame` (see `_make_gdf`)."""
    return _make_gdf


@pytest.fixture
def fake_overture(monkeypatch: pytest.MonkeyPatch) -> _FakeOverture:
    """Patch `overturemaps.core.geodataframe` with the recording fake."""
    state = _FakeOverture()
    monkeypatch.setattr("overturemaps.core.geodataframe", state)
    return state


@pytest.fixture
def license_warnings() -> Iterator[list[Any]]:
    """Capture emitted warnings into a list for the test's duration."""
    import warnings as _warnings

    with _warnings.catch_warnings(record=True) as record:
        _warnings.simplefilter("always")
        yield record
