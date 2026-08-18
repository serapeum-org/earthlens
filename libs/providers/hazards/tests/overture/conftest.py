"""Shared fixtures for the Overture backend tests.

Builds synthetic Overture `GeoDataFrame`s (no network) and a recording
fake for `overturemaps.core.geodataframe`, so the backend can be
exercised end-to-end offline. The `fake_overture` fixture patches the
SDK entry points the backend imports inside `_fetch` — the readers and
`get_latest_release` — and records every `(overture_type, bbox, release)`
call.

An autouse guard keeps that promise honest: any non-`e2e` test that
reaches the live STAC catalog fails instead of quietly succeeding
through the backend's offline fallback.
"""

from __future__ import annotations

from collections.abc import Callable
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

#: The release the offline fake reports as the one Overture publishes. Distinct
#: from every id in the bundled index, so a test can tell the two apart.
FAKE_RELEASE = "2099-12-31.0"


def _make_gdf(
    sources_per_row: list[Any] | None = None,
    *,
    set_crs: bool = False,
    nested: bool = False,
) -> gpd.GeoDataFrame:
    """Build a synthetic Overture `GeoDataFrame` with a `sources` column.

    Args:
        sources_per_row: One `sources` cell per row. Defaults to a single
            permissive row.
        set_crs: When `True`, tag the frame `EPSG:4326` (the real SDK
            omits the CRS — the default mirrors that).
        nested: When `True`, add `names` / `categories` struct columns that
            mirror Overture's deeply-nested schema, to exercise the
            GeoJSON / GPKG serialisation path.

    Returns:
        geopandas.GeoDataFrame: id / geometry / sources (+ optional nested
            struct columns), one row per cell.
    """
    if sources_per_row is None:
        sources_per_row = [PERMISSIVE_SOURCES]
    rows = len(sources_per_row)
    data: dict[str, Any] = {
        "id": [f"feat-{i}" for i in range(rows)],
        "sources": sources_per_row,
    }
    if nested:
        data["names"] = [{"primary": f"Place {i}", "common": None} for i in range(rows)]
        data["categories"] = [
            {"primary": "restaurant", "alternate": []} for _ in range(rows)
        ]
    gdf = gpd.GeoDataFrame(
        data,
        geometry=[Point(float(i), float(i)) for i in range(rows)],
    )
    if set_crs:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


class _FakeOverture:
    """Callable `overturemaps.core.geodataframe` stand-in that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reader_calls: list[dict[str, Any]] = []
        self.latest_calls: int = 0
        self.latest: str = FAKE_RELEASE
        self._gdf_for_type: dict[str, gpd.GeoDataFrame] = {}
        self.default_gdf: gpd.GeoDataFrame = _make_gdf()

    def latest_release(self) -> str:
        """Stand in for the SDK's live release lookup, counting the calls."""
        self.latest_calls += 1
        return self.latest

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

    def record_batch_reader(
        self,
        overture_type: str,
        bbox: tuple[float, float, float, float] | None = None,
        release: str | None = None,
        **kwargs: Any,
    ):
        """Stand-in for `overturemaps.core.record_batch_reader`.

        Encodes the canned `GeoDataFrame` to a geoarrow-WKB table and hands
        back a `pyarrow.RecordBatchReader` (small chunks) so the streaming
        path can be exercised offline.
        """
        import pyarrow as pa

        self.reader_calls.append(
            {"type": overture_type, "bbox": bbox, "release": release, "kwargs": kwargs}
        )
        gdf = self._gdf_for_type.get(overture_type, self.default_gdf).copy()
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        table = pa.table(gdf.to_arrow(geometry_encoding="WKB"))
        return pa.RecordBatchReader.from_batches(
            table.schema, table.to_batches(max_chunksize=1)
        )

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
    """Patch the SDK readers and the release lookup with the recording fake."""
    state = _FakeOverture()
    monkeypatch.setattr("overturemaps.core.geodataframe", state)
    monkeypatch.setattr(
        "overturemaps.core.record_batch_reader", state.record_batch_reader
    )
    monkeypatch.setattr("overturemaps.core.get_latest_release", state.latest_release)
    return state


def _refuse_urlopen(*_args: Any, **_kwargs: Any) -> None:
    """Fail the test rather than let it reach the live STAC catalog."""
    pytest.fail(
        "an offline Overture test reached the live STAC catalog; patch "
        "overturemaps.core.get_latest_release (the fake_overture fixture "
        "does) instead of relying on the backend's offline fallback",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def no_live_stac(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block the live release lookup for every non-e2e Overture test.

    Both live doors are covered: the SDK's `urlopen` (the release lookup)
    and the refresher's `requests.get` (the STAC child-link recovery).
    `_resolve_release` falls back to the bundled index on any failure, so
    an accidental live call passes the suite and only shows up as a slow,
    network-dependent run. `pytest.fail` raises a `BaseException`, which
    that fallback does not catch, so the leak surfaces as a failure.
    """
    if request.node.get_closest_marker("e2e"):
        return
    monkeypatch.setattr("overturemaps.core.urlopen", _refuse_urlopen)
    monkeypatch.setattr("earthlens.overture.cli.requests.get", _refuse_urlopen)
