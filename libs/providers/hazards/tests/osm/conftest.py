"""Shared offline fixtures for the OpenStreetMap backend tests.

Drives the backend end-to-end without network by faking both query SDKs and
the Overpass HTTP layer:

* `fake_overpy` replaces the `overpy` module so `overpy.Overpass().parse_json`
  returns a recorded `Result` of nodes/ways/relations.
* `fake_overpass_post` replaces `requests.post` on the backend module with a
  recorder returning a canned response (the backend POSTs the QL itself).
* `fake_ohsome` replaces the `ohsome` module so `OhsomeClient().elements
  .geometry.post(...).as_dataframe()` returns a fixture `GeoDataFrame` with the
  `(@osmId, @snapshotTimestamp)` MultiIndex the real SDK produces.

None of the fakes expose `xarray`, so any accidental use surfaces loudly.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
import pytest
import requests
from shapely.geometry import Point


class FakeNode:
    """Stand-in for an overpy `Node` (Decimal lat/lon + tags dict)."""

    def __init__(self, nid: int, lat: float, lon: float, tags: dict[str, str]) -> None:
        self.id = nid
        self.lat = Decimal(str(lat))
        self.lon = Decimal(str(lon))
        self.tags = tags


class FakeWay:
    """Stand-in for an overpy `Way` with inline `out geom;` coordinates."""

    def __init__(
        self, wid: int, tags: dict[str, str], coords: list[tuple[float, float]]
    ) -> None:
        self.id = wid
        self.tags = tags
        self.attributes = {
            "geometry": [
                {"lat": Decimal(str(lat)), "lon": Decimal(str(lon))}
                for lon, lat in coords
            ]
        }


class FakeRelation:
    """Stand-in for an overpy `Relation` (skipped by overpy_to_gdf)."""

    def __init__(self, rid: int, tags: dict[str, str]) -> None:
        self.id = rid
        self.tags = tags


class FakeResult:
    """Stand-in for an overpy `Result` exposing nodes/ways/relations."""

    def __init__(self, nodes: list, ways: list, relations: list) -> None:
        self.nodes = nodes
        self.ways = ways
        self.relations = relations


def make_result() -> FakeResult:
    """Build a result with one node (Point), a closed + an open way, a relation."""
    node = FakeNode(1, 49.41, 8.69, {"amenity": "hospital", "name": "Clinic"})
    closed = FakeWay(
        2,
        {"amenity": "hospital", "building": "yes"},
        [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.0, 0.0)],
    )
    open_way = FakeWay(3, {"highway": "residential"}, [(0.0, 0.0), (1.0, 1.0)])
    relation = FakeRelation(4, {"type": "multipolygon"})
    return FakeResult([node], [closed, open_way], [relation])


def make_ohsome_frame() -> gpd.GeoDataFrame:
    """Build a GeoDataFrame with the (@osmId, @snapshotTimestamp) MultiIndex."""
    frame = pd.DataFrame({"@other_tags": ["{}", "{}"]})
    gdf = gpd.GeoDataFrame(
        frame,
        geometry=[Point(8.69, 49.41), Point(8.70, 49.42)],
        crs="EPSG:4326",
    )
    gdf.index = pd.MultiIndex.from_tuples(
        [("way/2", "2020-01-01T00:00:00Z"), ("node/1", "2020-01-01T00:00:00Z")],
        names=["@osmId", "@snapshotTimestamp"],
    )
    return gdf


@dataclass
class FakeOverpyState:
    """Records the texts handed to `parse_json` and the result it returns."""

    result: FakeResult = field(default_factory=make_result)
    parsed_texts: list[str] = field(default_factory=list)


@pytest.fixture
def fake_overpy(monkeypatch: pytest.MonkeyPatch) -> FakeOverpyState:
    """Replace the `overpy` module with a recording fake."""
    state = FakeOverpyState()

    class _Overpass:
        def parse_json(self, text: str, encoding: str = "utf-8") -> FakeResult:
            state.parsed_texts.append(text)
            return state.result

    module = types.ModuleType("overpy")
    module.Overpass = _Overpass
    monkeypatch.setitem(sys.modules, "overpy", module)
    return state


@dataclass
class FakePostState:
    """Records every Overpass POST and serves a canned response body."""

    calls: list[dict] = field(default_factory=list)
    text: str = '{"version": 0.6, "elements": []}'
    ok: bool = True


@pytest.fixture
def fake_overpass_post(monkeypatch: pytest.MonkeyPatch) -> FakePostState:
    """Replace `requests.post` on the backend with a recording fake."""
    from earthlens.osm import backend

    state = FakePostState()

    class _Response:
        def __init__(self, text: str) -> None:
            self.text = text
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            if not state.ok:
                raise requests.HTTPError("Overpass returned an error status")

        def close(self) -> None:
            return None

    def _post(url: str, **kwargs: Any) -> _Response:
        state.calls.append(
            {
                "url": url,
                "data": kwargs.get("data"),
                "headers": kwargs.get("headers"),
                "timeout": kwargs.get("timeout"),
            }
        )
        return _Response(state.text)

    monkeypatch.setattr(backend.requests, "post", _post)
    return state


@dataclass
class FakeOhsomeState:
    """Records the client-construction + post kwargs and serves a fixture frame.

    The backend issues the request as `OhsomeClient(...).post(endpoint=
    "elements/geometry", ...)`, so both the constructor kwargs (`user_agent` /
    `retry` / `log`) and the post kwargs are captured; `error` lets a test make
    the post raise instead of returning a frame.
    """

    frame: gpd.GeoDataFrame = field(default_factory=make_ohsome_frame)
    post_kwargs: dict[str, Any] = field(default_factory=dict)
    client_kwargs: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None


@pytest.fixture
def fake_ohsome(monkeypatch: pytest.MonkeyPatch) -> FakeOhsomeState:
    """Replace the `ohsome` module with a recording fake.

    Mirrors both SDK call forms: the root `OhsomeClient(...).post(endpoint=
    "elements/geometry", ...)` the backend uses (recording the construction
    kwargs so a test can assert the retry / user-agent policy) and the chained
    `.elements.geometry.post(...)` form.
    """
    state = FakeOhsomeState()

    def _record_post(**kwargs: Any):
        state.post_kwargs = kwargs
        if state.error is not None:
            raise state.error
        return types.SimpleNamespace(as_dataframe=lambda: state.frame)

    class _Geometry:
        def post(self, **kwargs: Any):
            return _record_post(**kwargs)

    class _Elements:
        geometry = _Geometry()

    class _OhsomeClient:
        elements = _Elements()

        def __init__(self, **kwargs: Any) -> None:
            state.client_kwargs = kwargs

        def post(self, **kwargs: Any):
            return _record_post(**kwargs)

    module = types.ModuleType("ohsome")
    module.OhsomeClient = _OhsomeClient
    monkeypatch.setitem(sys.modules, "ohsome", module)
    return state


@pytest.fixture
def osm_kwargs(tmp_path) -> Callable[..., dict[str, Any]]:
    """Factory for the standard `OSM(...)` constructor kwargs."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "variables": ["overpass:hospitals"],
            "lat_lim": [49.40, 49.42],
            "lon_lim": [8.67, 8.71],
            "path": str(tmp_path),
        }
        base.update(overrides)
        return base

    return _make
